#!/usr/bin/env python3
"""
Refine Tags GUI - 存量数据集标签二次精修工具 (UI与翻译增强版)
"""

import argparse
import asyncio
import json
import logging
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from typing import Any, Iterable, List, Tuple

from PIL import Image, ImageTk

try:
    import googletrans  # type: ignore
except Exception:
    googletrans = None

try:
    from deep_translator import GoogleTranslator as _DeepGoogleTranslator
except Exception:
    _DeepGoogleTranslator = None  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

# --- 常规配置 ---
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
# httpx 客户端超时；单条 translate 另有 wait_for
_TRANSLATE_TIMEOUT_SEC = 25.0
_TRANSLATE_PER_TAG_SEC = 32.0
_TRANSLATE_CONCURRENCY = 5
# 单 tag 连续尝试次数（瞬时失败/空返回时自动隔秒重试）
_TRANSLATE_PER_TAG_ATTEMPTS = 3
_TRANSLATE_INNER_RETRY_DELAY_SEC = 1.25
# 整批仍失败时，主线程调度额外轮次（退避），避免只打一次就进「跳过」
_BG_TRANSLATE_MAX_ROUNDS = 3
_BG_TRANSLATE_ROUND_DELAY_MS = 2200
_TRANSLATION_LOCK = threading.Lock()
# 与 guo582_label_tk 共用的磁盘缓存：只存「有用译文」str->str
_TRANSLATION_CACHE: dict[str, str] = {}


def _checkerboard_rgb(w: int, h: int, cell: int = 10) -> Image.Image:
    """用于预览透明像素（与常见图像编辑器一致）。"""
    c1, c2 = (198, 198, 198), (140, 140, 140)
    tile = Image.new("RGB", (cell * 2, cell * 2))
    t1 = Image.new("RGB", (cell, cell), c1)
    t2 = Image.new("RGB", (cell, cell), c2)
    tile.paste(t1, (0, 0))
    tile.paste(t2, (cell, 0))
    tile.paste(t2, (0, cell))
    tile.paste(t1, (cell, cell))
    out = Image.new("RGB", (w, h))
    ty = 0
    while ty < h:
        tx = 0
        while tx < w:
            out.paste(tile, (tx, ty))
            tx += cell * 2
        ty += cell * 2
    return out


def _load_image_for_preview(path: Path) -> Image.Image:
    """保留 alpha；无透明则 RGB，便于预览与后续缩放。"""
    im = Image.open(path)
    if im.mode == "RGBA":
        return im
    if im.mode == "LA":
        return im.convert("RGBA")
    if im.mode == "P" and "transparency" in im.info:
        return im.convert("RGBA")
    return im.convert("RGB")


def _norm_tag_text(s: str) -> str:
    """下划线/空白归一，便于比较 danbooru tag 与 API 回包。"""
    return " ".join(s.replace("_", " ").split()).casefold()


def _has_non_latin_script(s: str) -> bool:
    """含中日韩假名、谚文、CJK 标点等即视为有「译文形态」（旧逻辑只认 \u4e00-\u9fff 会漏假名等）。"""
    for c in s:
        o = ord(c)
        if 0x3040 <= o <= 0x30FF:  # 平假名、片假名
            return True
        if 0x31F0 <= o <= 0x31FF:
            return True
        if 0x2E80 <= o <= 0x9FFF:  # CJK 部首与统一汉字等
            return True
        if 0xAC00 <= o <= 0xD7AF:  # 谚文音节
            return True
        if 0x3000 <= o <= 0x303F:  # CJK 标点（如 、）
            return True
        if 0xFF00 <= o <= 0xFFEF:  # 全角拉丁数字等
            return True
    return False


def _is_usable_translation(en: str, zh: str) -> bool:
    """可展示且可写入共享 json：非空，且与原文在归一化后不同，或含 CJK/假名/韩文等。"""
    if not zh or not str(zh).strip():
        return False
    z = str(zh).strip()
    if _has_non_latin_script(z):
        return True
    return _norm_tag_text(z) != _norm_tag_text(en)


def _sanitize_disk_cache(raw: dict[str, object]) -> dict[str, str]:
    """去掉 en→en 等无效项（旧版把失败也写进 json 导致满屏「未译」）。"""
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if _is_usable_translation(k, v):
            out[k] = v
    return out


def _format_tag_row(
    en: str,
    zh_cached: str | None,
    *,
    inflight: bool,
    skip_session: bool,
) -> str:
    """
    - 有可用译文：en · 中文
    - 正在请求：en · …
    - 本轮不再请求（失败等）：仅 en，不套贬义文案
    """
    if googletrans is None and _DeepGoogleTranslator is None:
        return f"{en}  ·  无翻译库（pip install deep-translator）"
    if zh_cached is not None and _is_usable_translation(en, zh_cached):
        return f"{en}  ·  {zh_cached}"
    if skip_session:
        return en
    if inflight:
        return f"{en}  ·  …"
    return en


# --- 核心翻译逻辑 ---
def _translate_missing_tags(missing: List[str]) -> dict[str, str]:
    """成功项写入共享缓存；失败不写缓存（由 GUI 会话集合标记）。"""
    if not missing:
        return {}
    if googletrans is None and _DeepGoogleTranslator is None:
        return {}

    result: dict[str, str] = {}

    if _DeepGoogleTranslator is not None:
        import time
        try:
            tr = _DeepGoogleTranslator(source="en", target="zh-CN")
            for i, t in enumerate(missing):
                for attempt in range(_TRANSLATE_PER_TAG_ATTEMPTS):
                    try:
                        zh = tr.translate(t)
                        if zh:
                            z = zh.strip()
                            if _is_usable_translation(t, z):
                                result[t] = z
                                break
                    except Exception as e:
                        logger.debug("deep_translator attempt %s/%s failed: %s", attempt + 1, _TRANSLATE_PER_TAG_ATTEMPTS, e)
                    if attempt + 1 < _TRANSLATE_PER_TAG_ATTEMPTS:
                        time.sleep(_TRANSLATE_INNER_RETRY_DELAY_SEC * (attempt + 1))
                if i + 1 < len(missing):
                    time.sleep(0.06)
            if result:
                return result
        except Exception as e:
            logger.exception("deep_translator aborted: %s", e)
            # fallthrough to googletrans if available

    if googletrans is not None:
        try:
            import httpx
        except ImportError:
            httpx = None
        try:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                async def _do() -> dict[str, str]:
                    timeout = httpx.Timeout(_TRANSLATE_TIMEOUT_SEC) if httpx is not None else None
                    res: dict[str, str] = {}
                    sem = asyncio.Semaphore(max(1, _TRANSLATE_CONCURRENCY))
                    async def one_tag(translator: Any, t: str) -> tuple[str, str | None]:
                        async with sem:
                            for attempt in range(_TRANSLATE_PER_TAG_ATTEMPTS):
                                try:
                                    tr = await asyncio.wait_for(
                                        translator.translate(t, src="auto", dest="zh-cn"),
                                        timeout=_TRANSLATE_PER_TAG_SEC,
                                    )
                                    val = getattr(tr, "text", None)
                                    if val:
                                        z = val.strip()
                                        if _is_usable_translation(t, z):
                                            return t, z
                                except (TimeoutError, Exception) as e:
                                    logger.debug("googletrans attempt %s/%s: %s", attempt + 1, _TRANSLATE_PER_TAG_ATTEMPTS, e)
                                if attempt + 1 < _TRANSLATE_PER_TAG_ATTEMPTS:
                                    await asyncio.sleep(_TRANSLATE_INNER_RETRY_DELAY_SEC * (attempt + 1))
                            return t, None
                    async with googletrans.Translator(timeout=timeout) as translator:
                        pairs = await asyncio.gather(*[one_tag(translator, t) for t in missing])
                        for t, z in pairs:
                            if z is not None:
                                res[t] = z
                    return res
                return loop.run_until_complete(_do())
            finally:
                try:
                    asyncio.set_event_loop(None)
                except Exception:
                    pass
                loop.close()
        except Exception as e:
            logger.exception("googletrans aborted: %s", e)

    return result

def split_tags(caption: str) -> List[str]:
    return [x.strip() for x in caption.replace("\n", ",").split(",") if x.strip()]

def join_tags(tags: Iterable[str]) -> str:
    return ", ".join([t.strip() for t in tags if t.strip()])

def load_text(path: Path) -> str:
    if not path.exists(): return ""
    try: return path.read_text(encoding="utf-8").strip()
    except Exception: return ""

@dataclass
class AppConfig:
    data_dir: Path
    hot_tags: List[str]

class RefineTool:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.files = sorted([p for p in cfg.data_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS])
        self.index = 0
        
        # 共享主脚本的缓存文件名
        self.session_path = cfg.data_dir / ".refine_session.json"
        self.cache_path = cfg.data_dir / ".guo582_translation_cache.json"
        
        self._load_persistent_data()
        
        self.root = tk.Tk()
        self.root.title(f"Refine Tags - {cfg.data_dir.name}")
        self.root.geometry("1600x950")
        
        # 基础布局
        self.left = ttk.Frame(self.root)
        self.left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.right = ttk.Frame(self.root, width=450)
        self.right.pack(side=tk.RIGHT, fill=tk.Y)
        self.right.pack_propagate(False)
        
        # 左侧: 图片展示
        self.canvas = tk.Canvas(self.left, bg="#1e1e1e", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.status_var = tk.StringVar()
        ttk.Label(self.left, textvariable=self.status_var).pack(anchor="w", padx=10, pady=5)
        
        # 右侧: 分为 标签区(Top) 和 操作区(Bottom)
        self.right_top = ttk.Frame(self.right)
        self.right_top.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.right_bottom = ttk.Frame(self.right)
        self.right_bottom.pack(side=tk.BOTTOM, fill=tk.X)

        # 1. 当前标签区
        ttk.Label(self.right_top, text="当前标签 (勾选保留):", font=("", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        tag_container = ttk.Frame(self.right_top)
        tag_container.pack(fill=tk.BOTH, expand=True, padx=10)
        
        self.tag_canvas = tk.Canvas(tag_container, highlightthickness=0)
        self.tag_scroll = ttk.Scrollbar(tag_container, orient="vertical", command=self.tag_canvas.yview)
        self.tag_frame = ttk.Frame(self.tag_canvas)
        self.tag_canvas.configure(yscrollcommand=self.tag_scroll.set)
        self.tag_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tag_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tag_canvas.create_window((0, 0), window=self.tag_frame, anchor="nw")
        self.tag_frame.bind("<Configure>", lambda e: self.tag_canvas.configure(scrollregion=self.tag_canvas.bbox("all")))
        
        # 滚轮支持（当前标签区）
        self.tag_hover = False
        tag_container.bind("<Enter>", lambda _e: self._set_tag_hover(True))
        tag_container.bind("<Leave>", lambda _e: self._set_tag_hover(False))
        self.root.bind_all("<MouseWheel>", self._on_global_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_global_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_global_mousewheel, add="+")

        # 2. 快捷标签区（复选框，不用按钮）
        ttk.Label(self.right_bottom, text="快捷标签 (勾选添加):", font=("", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        self.hot_frame = ttk.Frame(self.right_bottom)
        self.hot_frame.pack(fill=tk.X, padx=10, pady=(0, 6))
        self.hot_vars: dict[str, tk.BooleanVar] = {}
        self._build_hot_checkboxes()
        
        # 3. 额外标签输入
        ttk.Label(self.right_bottom, text="额外标签 (自动换行，实时同步预览):").pack(anchor="w", padx=10)
        self.extra_text = tk.Text(self.right_bottom, height=5, wrap="word", font=("", 10))
        self.extra_text.pack(fill=tk.X, padx=10, pady=5)
        self.extra_text.bind("<KeyRelease>", lambda e: self.update_preview())

        # 4. 预览与导航
        ttk.Label(self.right_bottom, text="最终结果预览:").pack(anchor="w", padx=10)
        self.preview_var = tk.StringVar()
        self.preview_entry = ttk.Entry(self.right_bottom, textvariable=self.preview_var, state="readonly")
        self.preview_entry.pack(fill=tk.X, padx=10, pady=5)
        
        btn_row = ttk.Frame(self.right_bottom)
        btn_row.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_row, text="⬅️ 上一张", command=self.prev).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(btn_row, text="💾 保存 (Ctrl+S)", command=self.save).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(btn_row, text="下一张 ➡️", command=self.next).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        btn_row2 = ttk.Frame(self.right_bottom)
        btn_row2.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Button(
            btn_row2,
            text="🔄 强制重试本图（清跳过；平时已自动多轮重试）",
            command=self.retry_translate_current,
        ).pack(fill=tk.X)

        # 运行时状态
        self.current_tags: List[str] = []
        self.tag_vars: dict[str, tk.BooleanVar] = {}
        self.tk_img = None
        # 翻译：进行中 / 本会话已结束（失败或未拿到可用译文），与磁盘缓存正交
        self._tx_inflight: set[str] = set()
        self._tx_skip_session: set[str] = set()
        self._tx_lock = threading.Lock()
        
        # 绑定事件
        self.root.bind("<Control-s>", lambda e: self.save())
        self.root.bind("<Left>", lambda e: self.prev())
        self.root.bind("<Right>", lambda e: self.next())
        self.canvas.bind("<Configure>", lambda e: self.draw_image())
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self.load_image()

    def retry_translate_current(self) -> None:
        """去掉当前 caption 里各 tag 的「本会话跳过」，重新走接口（接口修好或网络恢复后用）。"""
        if not self.files or googletrans is None:
            return
        with self._tx_lock:
            for t in self.current_tags:
                self._tx_skip_session.discard(t)
            self._tx_inflight.clear()
        self.refresh_tags_ui(keep_selection=True)

    def _set_tag_hover(self, v): self.tag_hover = v
    def _on_global_mousewheel(self, e):
        if self.tag_hover:
            delta = -1 if (getattr(e, 'delta', 0) > 0 or getattr(e, 'num', 0) == 4) else 1
            self.tag_canvas.yview_scroll(delta, "units")

    def _load_persistent_data(self):
        global _TRANSLATION_CACHE
        if self.cache_path.exists():
            try:
                raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    return
                clean = _sanitize_disk_cache(raw)
                with _TRANSLATION_LOCK:
                    # 丢弃无效 en→en，避免与 guo582 共用文件时脏数据
                    for k in list(_TRANSLATION_CACHE.keys()):
                        if k in raw and k not in clean:
                            _TRANSLATION_CACHE.pop(k, None)
                    _TRANSLATION_CACHE.update(clean)
            except Exception:
                pass
        if self.session_path.exists():
            try: self.index = json.loads(self.session_path.read_text(encoding="utf-8")).get("index", 0)
            except: pass

    def on_close(self):
        self.save()
        try:
            self.session_path.write_text(json.dumps({"index": self.index}))
            with _TRANSLATION_LOCK:
                to_save = {
                    k: v
                    for k, v in _TRANSLATION_CACHE.items()
                    if _is_usable_translation(k, v)
                }
                self.cache_path.write_text(
                    json.dumps(to_save, ensure_ascii=False, indent=2)
                )
        except Exception:
            pass
        self.root.destroy()

    def _build_hot_checkboxes(self):
        for w in self.hot_frame.winfo_children():
            w.destroy()
        self.hot_vars = {}
        wrap_len = 380
        for tag in self.cfg.hot_tags:
            v = tk.BooleanVar(value=False)
            self.hot_vars[tag] = v
            cb = tk.Checkbutton(
                self.hot_frame,
                text=tag,
                variable=v,
                command=self.update_preview,
                anchor="w",
                justify="left",
                wraplength=wrap_len,
            )
            cb.pack(fill=tk.X, pady=1)

    def load_image(self):
        if not self.files: return
        self.index = max(0, min(self.index, len(self.files)-1))
        p = self.files[self.index]
        self.orig_img = _load_image_for_preview(p)
        self.draw_image()
        self.current_tags = split_tags(load_text(p.with_suffix(".txt")))
        self.extra_text.delete("1.0", tk.END)
        # 换图：取消「进行中」标记（后台线程仍会更新共享缓存）；失败跳过集保留，避免同会话刷屏重试
        with self._tx_lock:
            self._tx_inflight.clear()
        self.refresh_tags_ui(keep_selection=False)
        self._sync_hot_checkbox_from_current()
        self.status_var.set(f"[{self.index+1}/{len(self.files)}] {p.name} ({self.orig_img.width}x{self.orig_img.height})")

    def _sync_hot_checkbox_from_current(self):
        cur = set(self.current_tags)
        for tag, v in self.hot_vars.items():
            v.set(tag in cur)

    def draw_image(self):
        if not hasattr(self, 'orig_img'): return
        cw, ch = max(10, self.canvas.winfo_width()), max(10, self.canvas.winfo_height())
        iw, ih = self.orig_img.size
        s = min(cw/iw, ch/ih)
        nw, nh = max(1, int(iw*s)), max(1, int(ih*s))
        res = self.orig_img.resize((nw, nh), Image.Resampling.BILINEAR)
        if res.mode == "RGBA":
            board = _checkerboard_rgb(nw, nh)
            board.paste(res, (0, 0), mask=res.split()[3])
            res = board
        self.tk_img = ImageTk.PhotoImage(res)
        self.canvas.delete("all")
        self.canvas.create_image(cw//2, ch//2, image=self.tk_img)

    def refresh_tags_ui(self, keep_selection=True):
        # 记录当前已勾选状态
        selected_set = set(t for t, v in self.tag_vars.items() if v.get()) if keep_selection else set(self.current_tags)
        
        for w in self.tag_frame.winfo_children(): w.destroy()
        self.tag_vars = {}
        
        with _TRANSLATION_LOCK:
            snap = {t: _TRANSLATION_CACHE.get(t) for t in self.current_tags}

        need_fetch: list[str] = []
        with self._tx_lock:
            for en in self.current_tags:
                z = snap.get(en)
                if z is not None and _is_usable_translation(en, z):
                    continue
                if en in self._tx_skip_session:
                    continue
                if en in self._tx_inflight:
                    continue
                need_fetch.append(en)
            for en in need_fetch:
                self._tx_inflight.add(en)

        wrap_len = 380
        for en in self.current_tags:
            if en in self.hot_vars:
                v = self.hot_vars[en]
                v.set(en in selected_set)
            else:
                v = tk.BooleanVar(value=(en in selected_set))
            self.tag_vars[en] = v
            z = snap.get(en)
            usable = z is not None and _is_usable_translation(en, z)
            with self._tx_lock:
                skip = en in self._tx_skip_session
                inf = en in self._tx_inflight
            inflight_ui = inf and not usable and not skip
            display_text = _format_tag_row(
                en, z, inflight=inflight_ui, skip_session=skip and not usable
            )
            cb = tk.Checkbutton(
                self.tag_frame,
                text=display_text,
                variable=v,
                command=self.update_preview,
                anchor="w",
                justify="left",
                wraplength=wrap_len,
                font=("", 9),
            )
            cb.pack(fill=tk.X, pady=1)

        if need_fetch and (googletrans is not None or _DeepGoogleTranslator is not None):
            threading.Thread(
                target=self._bg_translate,
                args=(tuple(need_fetch),),
                kwargs={"round_idx": 0},
                daemon=True,
            ).start()
        self.update_preview()

    def _bg_translate(self, missing: Tuple[str, ...], *, round_idx: int = 0) -> None:
        new_map: dict[str, str] = {}
        try:
            new_map = _translate_missing_tags(list(missing))
            with _TRANSLATION_LOCK:
                _TRANSLATION_CACHE.update(new_map)
        finally:
            still_fail = [m for m in missing if m not in new_map]
            schedule_next = False
            wait_ms = 0

            with self._tx_lock:
                for m in missing:
                    self._tx_inflight.discard(m)
                if still_fail and round_idx + 1 < _BG_TRANSLATE_MAX_ROUNDS:
                    wait_ms = _BG_TRANSLATE_ROUND_DELAY_MS * (round_idx + 1)
                    for m in still_fail:
                        self._tx_inflight.add(m)
                    schedule_next = True
                else:
                    for m in still_fail:
                        self._tx_skip_session.add(m)

            if schedule_next:
                def _next_round(
                    sf: Tuple[str, ...] = tuple(still_fail),
                    ri: int = round_idx + 1,
                ) -> None:
                    self._bg_translate(sf, round_idx=ri)

                try:
                    self.root.after(wait_ms, lambda: threading.Thread(target=_next_round, daemon=True).start())
                except tk.TclError:
                    with self._tx_lock:
                        for m in still_fail:
                            self._tx_inflight.discard(m)
                            self._tx_skip_session.add(m)

            try:
                self.root.after(0, lambda: self.refresh_tags_ui(keep_selection=True))
            except tk.TclError:
                pass

    def _collect_final_tags(self) -> str:
        """
        主列表「勾选保留」为准；快捷栏只追加「不在原文里」的标签，或原文里有且主列表仍勾选的
        （避免同一词既在主列表取消、又因快捷栏仍勾选而被写回）。
        """
        selected = [en for en in self.current_tags if en in self.tag_vars and self.tag_vars[en].get()]
        hot_checked: List[str] = []
        for tag, v in self.hot_vars.items():
            if not v.get():
                continue
            if tag in self.current_tags:
                if tag not in self.tag_vars or not self.tag_vars[tag].get():
                    continue
            hot_checked.append(tag)
        extra = split_tags(self.extra_text.get("1.0", tk.END))
        merged = list(dict.fromkeys(selected + hot_checked + extra))
        return join_tags(merged)

    def update_preview(self):
        # 主列表取消后，同步关掉同名快捷勾，避免误以为仍选中
        for en in self.current_tags:
            if en in self.hot_vars and en in self.tag_vars and not self.tag_vars[en].get():
                self.hot_vars[en].set(False)
        self.preview_var.set(self._collect_final_tags())

    def save(self):
        if not self.files: return
        p = self.files[self.index]
        tags_str = self._collect_final_tags()
        p.with_suffix(".txt").write_text(tags_str, encoding="utf-8")
        self.root.title(f"✅ 已保存: {p.name}")

    def next(self):
        self.save()
        if self.index < len(self.files) - 1:
            self.index += 1
            self.load_image()

    def prev(self):
        self.save()
        if self.index > 0:
            self.index -= 1
            self.load_image()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="datasets/guo582_gui_out", help="数据集目录")
    p.add_argument("--hot", default="1girl,solo,long hair,looking at viewer,smile,blush", help="高频标签")
    args = p.parse_args()
    
    cfg = AppConfig(data_dir=Path(args.dir), hot_tags=split_tags(args.hot))
    if not cfg.data_dir.exists(): return print(f"Error: 找不到目录 {args.dir}")
    
    app = RefineTool(cfg)
    app.root.mainloop()

if __name__ == "__main__":
    main()
