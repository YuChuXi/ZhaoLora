#!/usr/bin/env python3
"""
guo582 数据集人工清洗工具 (Tkinter 实时裁剪版)

特点:
- 非 Gradio, 原生窗口
- 拖拽裁剪框时实时显示匹配尺寸和倍率
- 禁止向上匹配 (不允许匹配到超过裁剪区的尺寸)
- 对裁剪结果跑本地 WD14
- 右侧复选框人工增删标签
- 保存图片和 txt, txt 末尾附加实际裁剪区分辨率

依赖: 标签中译需 `pip install deep-translator`（googletrans 非官方接口多数情况下已不再返回译文）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from typing import Iterable, List, Tuple, Optional

import numpy as np
import onnxruntime as ort
import pandas as pd
import torch
from PIL import Image, ImageTk

try:
    from deep_translator import GoogleTranslator as _DeepGoogleTranslator
except Exception:
    _DeepGoogleTranslator = None  # type: ignore[misc, assignment]

try:
    import googletrans  # type: ignore
except Exception:
    googletrans = None


SCRIPT_DIR = Path(__file__).resolve().parent
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SIDE_BUCKETS = [512, 768, 1000, 1024, 1152, 1280, 1440, 1536, 1792, 2048]
ZOOM_BUCKETS = [1.0, 1.5, 1024/768, 2.0, 2.5, 3.0, 4.0]
WD14_THRESHOLD = 0.1
MAX_OUTPUT_PIXELS = 1536 * 1536

WD14_MODEL_DIR = SCRIPT_DIR / "wd14_tagger_model" / "SmilingWolf_wd-vit-large-tagger-v3"
WD14_ONNX_PATH = WD14_MODEL_DIR / "model.onnx"
WD14_CSV_PATH = WD14_MODEL_DIR / "selected_tags.csv"

_WD14_SESSION = None
_WD14_LABELS: List[str] = []
_TRANSLATION_CACHE: dict[str, str] = {}
_TRANSLATION_LOCK = threading.Lock()


class Tags:
    """
    标签唯一入口：逗号 caption 解析、语义去重（下划线≡空格）、写出格式、勾选匹配。
    GUI 与 --batch_no_gui 只应通过本类处理 tag，避免逻辑散落。
    """

    @staticmethod
    def split_caption(caption: str) -> List[str]:
        return [x.strip() for x in caption.replace("\n", ",").split(",") if x.strip()]

    @staticmethod
    def join_caption(tags: Iterable[str]) -> str:
        return ", ".join([t.strip() for t in tags if t.strip()])

    @staticmethod
    def equivalence_key(tag: str) -> str:
        """语义键：下划线/空格/大小写/多空格等价。"""
        return " ".join(tag.strip().lower().replace("_", " ").split())

    @staticmethod
    def _canonical_spacing(tag: str) -> str:
        """写出用：下划线改空格并压平空白。"""
        return " ".join(tag.strip().replace("_", " ").split())

    @staticmethod
    def dedupe_ordered(tags: Iterable[str]) -> List[str]:
        """按语义去重，保序；每条输出均为 _canonical_spacing。"""
        by_key: dict[str, str] = {}
        order_keys: List[str] = []
        for raw in tags:
            t = raw.strip()
            if not t:
                continue
            k = Tags.equivalence_key(t)
            if k not in by_key:
                by_key[k] = t
                order_keys.append(k)
        return [Tags._canonical_spacing(by_key[k]) for k in order_keys]

    @staticmethod
    def merge_for_disk(base: List[str], wd14: List[str]) -> str:
        """原 txt ∪ WD14 → 去重、排序、拼接（批量保存与 GUI 保存一致）。"""
        merged = sorted(Tags.dedupe_ordered([*base, *wd14]))
        return Tags.join_caption(merged)

    @staticmethod
    def merge_ui_preview(selected: List[str], extra_caption: str) -> str:
        """勾选 tag + 额外文本框 → 去重、拼接（不排序，保勾选顺序）。"""
        extra = Tags.split_caption(extra_caption)
        return Tags.join_caption(Tags.dedupe_ordered([*selected, *extra]))

    @staticmethod
    def panel_sorted_list(base_tags: List[str], wd_tags: List[str]) -> List[str]:
        """右侧面板：原/wd 去重后字典序列表（用于复选框与翻译）。"""
        base_u = sorted(set(base_tags))
        wd_u = sorted(set(wd_tags))
        return sorted(Tags.dedupe_ordered(base_u + wd_u))

    @staticmethod
    def selection_key_set(selected: Optional[set[str]]) -> Optional[set[str]]:
        """把已选 tag（任意写法）转为语义键集合，供复选框恢复状态。"""
        if selected is None:
            return None
        return {Tags.equivalence_key(s) for s in selected}


def load_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _translate_missing_tags(missing: List[str]) -> dict[str, str]:
    """en→zh-CN；优先 deep-translator（googletrans 非官方接口多数环境已返回原文）。"""
    if not missing:
        return {}
    out: dict[str, str] = {x: x for x in missing}

    if _DeepGoogleTranslator is not None:
        try:
            tr = _DeepGoogleTranslator(source="en", target="zh-CN")
            for i, t in enumerate(missing):
                try:
                    zh = tr.translate(t)
                    out[t] = zh.strip() if zh else t
                except Exception:
                    pass
                if i + 1 < len(missing):
                    time.sleep(0.06)
            return out
        except Exception:
            pass

    if googletrans is None:
        return out

    try:
        translator = googletrans.Translator()

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)

            async def _do():
                texts: List[str] = []
                for t in missing:
                    try:
                        res = await translator.translate(t, src="en", dest="zh-cn")
                        text = getattr(res, "text", None)
                        texts.append(text if text else t)
                    except Exception:
                        texts.append(t)
                return texts

            texts = loop.run_until_complete(_do())
        finally:
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
            loop.close()

        for en, zh in zip(missing, texts):
            out[en] = zh if zh else en
        for en in missing:
            out.setdefault(en, en)
        return out
    except Exception:
        return out


def translate_tags_cached(tags: List[str]) -> List[str]:
    if not tags:
        return tags
    uniq = list(dict.fromkeys(tags))
    with _TRANSLATION_LOCK:
        missing = [t for t in uniq if t not in _TRANSLATION_CACHE]
    if missing:
        new_map = _translate_missing_tags(missing)
        with _TRANSLATION_LOCK:
            _TRANSLATION_CACHE.update(new_map)
    with _TRANSLATION_LOCK:
        return [_TRANSLATION_CACHE.get(t, t) for t in tags]


def _translation_disk_entry_keep(key: str, value: str) -> bool:
    """丢弃「译文与 tag 完全相同且无汉字」的旧缓存（googletrans 失效期写入的假条目）。"""
    if key != value:
        return True
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def get_cached_translations(tags: List[str]) -> tuple[list[str], list[str]]:
    """
    仅返回缓存命中的翻译，未命中直接回退英文，并返回 missing 列表。
    用于避免 UI 线程被翻译请求阻塞。
    """
    if not tags:
        return tags, []
    zh = []
    missing = []
    with _TRANSLATION_LOCK:
        for t in tags:
            v = _TRANSLATION_CACHE.get(t)
            if v is None:
                zh.append(t)
                missing.append(t)
            else:
                zh.append(v)
    return zh, missing


def _get_wd14():
    global _WD14_SESSION, _WD14_LABELS
    if _WD14_SESSION is not None:
        return _WD14_SESSION, _WD14_LABELS
    if not WD14_ONNX_PATH.exists() or not WD14_CSV_PATH.exists():
        raise RuntimeError(f"找不到本地 WD14 模型: {WD14_MODEL_DIR}")
    df = pd.read_csv(WD14_CSV_PATH)
    _WD14_LABELS = df["name"].tolist()
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if torch.cuda.is_available() else ["CPUExecutionProvider"]
    _WD14_SESSION = ort.InferenceSession(str(WD14_ONNX_PATH), providers=providers)
    return _WD14_SESSION, _WD14_LABELS


def run_wd14_on_image(img: Image.Image) -> List[str]:
    try:
        session, labels = _get_wd14()
    except Exception as e:
        print(f"WD14 加载失败: {e}")
        return []
    # WD14 只吃 RGB；RGBA 应先用 alpha 合成到白底，避免透明区变黑影响识别精度。
    if img.mode == "RGBA":
        rgb = Image.new("RGB", img.size, (255, 255, 255))
        rgb.paste(img, mask=img.split()[3])
        img = rgb
    else:
        img = img.convert("RGB")
    w, h = img.size
    max_side = max(w, h)
    canvas = Image.new("RGB", (max_side, max_side), (255, 255, 255))
    canvas.paste(img, ((max_side - w) // 2, (max_side - h) // 2))
    input_img = canvas.resize((448, 448), Image.Resampling.LANCZOS)
    # WD14 ONNX 常见推理链路基于 OpenCV(BGR)。
    # 当前图像来自 PIL(RGB)，这里显式转为 BGR 以避免红蓝误判。
    arr = np.array(input_img).astype(np.float32)
    arr = arr[:, :, ::-1]
    input_data = arr[np.newaxis, ...]
    input_name = session.get_inputs()[0].name
    probs = session.run(None, {input_name: input_data})[0][0]
    tags = []
    for i, p in enumerate(probs):
        if i < len(labels) and p >= WD14_THRESHOLD:
            name = labels[i]
            if name in ["general", "sensitive", "questionable", "explicit"]:
                continue
            tags.append(name.replace("_", " ").strip())
    return tags


def build_capture_candidates() -> List[dict]:
    """
    构建全部可吸附的(裁剪尺寸 -> 目标尺寸@倍率)映射。
    会去重同一裁剪尺寸，优先保留目标分辨率更大的组合。
    """
    dedup = {}
    for tw in SIDE_BUCKETS:
        for th in SIDE_BUCKETS:
            # 输出总像素上限约束
            if tw * th > MAX_OUTPUT_PIXELS:
                continue
            for z in ZOOM_BUCKETS:
                cw = int(round(tw * z))
                ch = int(round(th * z))
                key = (cw, ch)
                cur = dedup.get(key)
                cand = {"crop_w": cw, "crop_h": ch, "target_w": tw, "target_h": th, "zoom": z}
                if cur is None or (tw * th) > (cur["target_w"] * cur["target_h"]):
                    dedup[key] = cand
    return list(dedup.values())


def choose_best_candidate(
    candidates: List[dict],
    max_w: int,
    max_h: int,
    desired_w: int,
    desired_h: int,
    weight_w: float = 1.0,
    weight_h: float = 1.0,
) -> Optional[dict]:
    usable = [c for c in candidates if c["crop_w"] <= max_w and c["crop_h"] <= max_h]
    if not usable:
        return None
    usable.sort(
        key=lambda c: (
            weight_w * abs(c["crop_w"] - desired_w) + weight_h * abs(c["crop_h"] - desired_h),
            -(c["target_w"] * c["target_h"]),
            -c["zoom"],
        )
    )
    return usable[0]


def parse_default_crop_spec(spec: str) -> Tuple[int, int, float]:
    """
    解析默认裁切匹配，如 1152x1152@1.0 或 1152x1152@1.0x -> (target_w, target_h, zoom)。
    须与内部 SIDE_BUCKETS × ZOOM_BUCKETS 生成的候选一致，否则将回退自动匹配。
    """
    s = spec.strip().lower().rstrip("x")
    m = re.match(r"^(\d+)\s*x\s*(\d+)\s*@\s*([\d.]+)\s*x?$", s)
    if not m:
        raise argparse.ArgumentTypeError(
            f"无效格式 {spec!r}，示例: 1152x1152@1.0 或 1152x1152@1.0x"
        )
    tw, th, z = int(m.group(1)), int(m.group(2)), float(m.group(3))
    if tw < 1 or th < 1 or z <= 0:
        raise argparse.ArgumentTypeError("宽高须为正整数，倍率须为正数")
    return (tw, th, z)


def find_candidate_by_target_zoom(
    candidates: List[dict],
    max_w: int,
    max_h: int,
    target_w: int,
    target_h: int,
    zoom: float,
) -> Optional[dict]:
    """在候选中找指定 target_w×target_h@zoom，且裁切区域可放入 max_w×max_h。"""
    for c in candidates:
        if c["target_w"] != target_w or c["target_h"] != target_h:
            continue
        if not math.isclose(float(c["zoom"]), float(zoom), rel_tol=1e-5, abs_tol=1e-4):
            continue
        if c["crop_w"] <= max_w and c["crop_h"] <= max_h:
            return c
    return None


@dataclass
class AppConfig:
    src_dir: Path
    out_dir: Path
    disable_orig_tags: bool = False
    default_crop: Optional[Tuple[int, int, float]] = None  # (target_w, target_h, zoom)
    # 新图打开时默认勾选「此图不裁切缩放」；会话里已保存的状态仍会覆盖
    default_no_crop_resize: bool = False


class LabelTool:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self.files = sorted([p for p in cfg.src_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS])
        self.index = 0
        self.candidates = build_capture_candidates()
        self.session_path = self.cfg.out_dir / ".guo582_tk_session.json"
        self.translation_cache_path = self.cfg.out_dir / ".guo582_translation_cache.json"
        self.session_data = {"version": 1, "last_index": 0, "items": {}}
        self.done_count = 0
        self._save_after_id = None
        self._translate_job_token = 0
        self._load_persistent_data()

        self.root = tk.Tk()
        self.root.title("guo582 实时裁剪打标 (Tk)")
        self.root.geometry("1800x1000")

        self.left = ttk.Frame(self.root)
        self.left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.right = ttk.Frame(self.root, width=520)
        self.right.pack(side=tk.RIGHT, fill=tk.Y)
        self.right.pack_propagate(False)

        self.canvas = tk.Canvas(self.left, bg="#222222")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="等待加载")
        self.live_var = tk.StringVar(value="实时匹配: -")
        ttk.Label(self.left, textvariable=self.status_var).pack(anchor="w", padx=8, pady=2)
        ttk.Label(self.left, textvariable=self.live_var, foreground="#0077cc").pack(anchor="w", padx=8, pady=2)

        # 右侧布局: 顶部标签区 + 底部固定操作区，避免按钮被挤掉
        self.right_top = ttk.Frame(self.right)
        self.right_top.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.right_bottom = ttk.Frame(self.right)
        self.right_bottom.pack(side=tk.BOTTOM, fill=tk.X)

        ttk.Label(self.right_top, text="标签列表 (勾选保留)").pack(anchor="w", padx=8, pady=6)
        tag_container = ttk.Frame(self.right_top)
        tag_container.pack(fill=tk.BOTH, expand=True, padx=8)
        self.tag_canvas = tk.Canvas(tag_container, height=520, highlightthickness=0)
        self.tag_scroll = ttk.Scrollbar(tag_container, orient="vertical", command=self.tag_canvas.yview)
        self.tag_frame = ttk.Frame(self.tag_canvas)
        self.tag_canvas.configure(yscrollcommand=self.tag_scroll.set)
        self.tag_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tag_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tag_canvas.create_window((0, 0), window=self.tag_frame, anchor="nw")
        self.tag_frame.bind("<Configure>", lambda e: self.tag_canvas.configure(scrollregion=self.tag_canvas.bbox("all")))

        # 滚轮: 鼠标在标签区域时，全局滚轮也可滚动
        self.tag_hover = False
        self.tag_canvas.bind("<Enter>", lambda _e: self._set_tag_hover(True))
        self.tag_canvas.bind("<Leave>", lambda _e: self._set_tag_hover(False))
        self.tag_frame.bind("<Enter>", lambda _e: self._set_tag_hover(True))
        self.tag_frame.bind("<Leave>", lambda _e: self._set_tag_hover(False))
        self.root.bind_all("<MouseWheel>", self._on_global_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_global_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_global_mousewheel, add="+")

        self.caption_var = tk.StringVar()
        ttk.Label(self.right_bottom, text="最终预览").pack(anchor="w", padx=8, pady=(8, 2))
        self.caption_entry = ttk.Entry(self.right_bottom, textvariable=self.caption_var)
        self.caption_entry.pack(fill=tk.X, padx=8)

        btn_row = ttk.Frame(self.right_bottom)
        btn_row.pack(fill=tk.X, padx=8, pady=10)
        ttk.Button(btn_row, text="上一张", command=self.prev_image).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(btn_row, text="跳过", command=self.next_image).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(btn_row, text="应用裁剪+打标", command=self.apply_crop_and_tag).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        self.no_crop_resize_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.right_bottom,
            text="此图不裁切缩放（保存原图尺寸）",
            variable=self.no_crop_resize_var,
            command=self.on_toggle_no_crop_resize,
        ).pack(anchor="w", padx=8, pady=(2, 2))
        ttk.Button(self.right_bottom, text="保存并下一张", command=self.save_and_next).pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(
            self.right_bottom,
            text="快捷键: Ctrl+S 保存并下一张  |  Ctrl+N 跳过（不保存）",
            foreground="#666666",
        ).pack(anchor="w", padx=8, pady=(0, 2))
        ttk.Label(self.right_bottom, text="额外标签(逗号分隔)").pack(anchor="w", padx=8, pady=(10, 2))
        self.extra_text = tk.Text(self.right_bottom, height=5, wrap="word")
        self.extra_text.pack(fill=tk.X, padx=8, pady=(0, 6))
        self.extra_text.bind("<KeyRelease>", lambda _e: self.on_extra_text_change())
        self.extra_text.bind("<FocusOut>", lambda _e: self._persist_current_item_state())

        # image state
        self.orig_img: Image.Image | None = None
        self.view_img: Image.Image | None = None
        self.tk_img = None
        self.scale = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.rect_id = None
        self.crop_box_orig = None  # (x1,y1,x2,y2) in original coords
        self.matched = (512, 512, 1.0)
        self.actual_crop = (0, 0)
        self.display_to_raw = []
        self.tag_vars = []
        self.base_tags: List[str] = []
        self.wd14_tags: List[str] = []
        self.handle_tol_px = 8
        self.drag_mode = None  # new/move/resize_left/resize_right/resize_top/resize_bottom
        self.drag_start_raw = (0, 0)
        self.drag_base_box = None
        self.wd14_job_token = 0
        self.current_file_key = ""

        self.canvas.bind("<Configure>", lambda _e: self.redraw_image())
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Motion>", self.on_motion)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._setup_keyboard_shortcuts()

        self.load_current(auto_skip_unfinished=True, skip_step=1)

    def run(self):
        self.root.mainloop()

    def _setup_keyboard_shortcuts(self) -> None:
        self.root.bind_all("<Control-s>", self._on_hotkey_save, add=True)
        self.root.bind_all("<Control-S>", self._on_hotkey_save, add=True)
        self.root.bind_all("<Control-n>", self._on_hotkey_skip, add=True)
        self.root.bind_all("<Control-N>", self._on_hotkey_skip, add=True)

    def _on_hotkey_save(self, _event=None) -> str | None:
        self.save_and_next()
        return "break"

    def _on_hotkey_skip(self, _event=None) -> str | None:
        if self.root.focus_get() == self.extra_text:
            return None
        self.next_image()
        return "break"

    def _item_key(self, p: Path) -> str:
        return p.name

    def _load_persistent_data(self):
        global _TRANSLATION_CACHE
        if self.translation_cache_path.exists():
            try:
                data = json.loads(self.translation_cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    merged = {
                        str(k): str(v)
                        for k, v in data.items()
                        if _translation_disk_entry_keep(str(k), str(v))
                    }
                    with _TRANSLATION_LOCK:
                        _TRANSLATION_CACHE.update(merged)
            except Exception:
                pass

        if self.session_path.exists():
            try:
                data = json.loads(self.session_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.session_data = data
            except Exception:
                pass

        try:
            self.index = int(self.session_data.get("last_index", 0))
        except Exception:
            self.index = 0
        if self.files:
            self.index = max(0, min(self.index, len(self.files) - 1))
        self.done_count = self._count_done_filesystem()

    def _count_done_filesystem(self) -> int:
        done = 0
        for p in self.files:
            if (self.cfg.out_dir / p.name).exists() and (self.cfg.out_dir / p.with_suffix(".txt").name).exists():
                done += 1
        return done

    def _is_completed(self, p: Path) -> bool:
        return (self.cfg.out_dir / p.name).exists() and (self.cfg.out_dir / p.with_suffix(".txt").name).exists()

    def _find_next_unfinished(self, start_idx: int, step: int = 1, include_start: bool = True) -> Optional[int]:
        if not self.files:
            return None
        n = len(self.files)
        start_idx = max(0, min(start_idx, n - 1))
        offset_start = 0 if include_start else 1
        for k in range(offset_start, n):
            i = (start_idx + step * k) % n
            if not self._is_completed(self.files[i]):
                return i
        return None

    def _save_translation_cache(self):
        try:
            with _TRANSLATION_LOCK:
                payload = dict(_TRANSLATION_CACHE)
            self.translation_cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _save_session(self):
        if self.files:
            self.session_data["last_index"] = self.index
        try:
            payload = json.dumps(self.session_data, ensure_ascii=False, indent=2)
        except Exception:
            return

        def _worker():
            try:
                self.session_path.write_text(payload, encoding="utf-8")
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _schedule_session_save(self, delay_ms: int = 250):
        if self._save_after_id is not None:
            try:
                self.root.after_cancel(self._save_after_id)
            except Exception:
                pass
        self._save_after_id = self.root.after(delay_ms, self._flush_scheduled_save)

    def _flush_scheduled_save(self):
        self._save_after_id = None
        self._save_session()

    def _persist_current_item_state(self, flush: bool = False):
        if not self.files:
            return
        p = self.files[self.index]
        key = self._item_key(p)
        selected = self.selected_raw_tags()
        item = {
            "crop_box_orig": list(self.crop_box_orig) if self.crop_box_orig else None,
            "matched": [self.matched[0], self.matched[1], self.matched[2]],
            "actual_crop": [self.actual_crop[0], self.actual_crop[1]],
            "base_tags": list(self.base_tags),
            "wd14_tags": list(self.wd14_tags),
            "selected_tags": selected,
            "no_crop_resize": bool(self.no_crop_resize_var.get()),
            "extra_tags_text": self.get_extra_tags_text(),
            "caption": self.caption_var.get(),
        }
        self.session_data.setdefault("items", {})[key] = item
        if flush:
            self._save_session()
        else:
            self._schedule_session_save()

    def _apply_saved_item_state(self, p: Path):
        key = self._item_key(p)
        items = self.session_data.get("items", {})
        if not isinstance(items, dict):
            return
        item = items.get(key)
        if not isinstance(item, dict):
            return
        iw, ih = self.orig_img.size if self.orig_img is not None else (0, 0)

        saved_box = item.get("crop_box_orig")
        if isinstance(saved_box, list) and len(saved_box) == 4:
            x1, y1, x2, y2 = [int(v) for v in saved_box]
            x1 = max(0, min(iw - 1, x1))
            y1 = max(0, min(ih - 1, y1))
            x2 = max(x1 + 1, min(iw, x2))
            y2 = max(y1 + 1, min(ih, y2))
            self.crop_box_orig = (x1, y1, x2, y2)
            self.actual_crop = (x2 - x1, y2 - y1)

        saved_m = item.get("matched")
        if isinstance(saved_m, list) and len(saved_m) == 3:
            try:
                self.matched = (int(saved_m[0]), int(saved_m[1]), float(saved_m[2]))
            except Exception:
                pass

        saved_base = item.get("base_tags")
        if isinstance(saved_base, list) and not self.cfg.disable_orig_tags:
            self.base_tags = [str(x) for x in saved_base]
        saved_wd = item.get("wd14_tags")
        if isinstance(saved_wd, list):
            self.wd14_tags = [str(x) for x in saved_wd]
        self.no_crop_resize_var.set(bool(item.get("no_crop_resize", False)))
        self.on_toggle_no_crop_resize()

        self.set_extra_tags_text(str(item.get("extra_tags_text", "")))
        selected = item.get("selected_tags")
        if not isinstance(selected, list):
            selected = None
        else:
            selected = set([str(x) for x in selected])
        self.refresh_tag_panel(default_selected=selected)

    def on_close(self):
        self._persist_current_item_state(flush=True)
        self._flush_scheduled_save()
        self._save_translation_cache()
        self.root.destroy()

    def get_extra_tags_text(self) -> str:
        return self.extra_text.get("1.0", "end-1c").strip()

    def set_extra_tags_text(self, text: str):
        self.extra_text.delete("1.0", "end")
        if text:
            self.extra_text.insert("1.0", text)

    def on_extra_text_change(self):
        self.refresh_caption()

    def on_toggle_no_crop_resize(self):
        if self.orig_img is None:
            self._persist_current_item_state(flush=False)
            return
        if self.no_crop_resize_var.get():
            # 不裁切模式: 直接锁定为整图
            iw, ih = self.orig_img.size
            self.crop_box_orig = (0, 0, iw, ih)
            self.actual_crop = (iw, ih)
            self.live_var.set(f"实时匹配: 不裁切模式，整图 {iw}x{ih}")
            self.redraw_image()
        else:
            # 退出不裁切模式后恢复常规实时提示
            self.live_var.set(
                f"实时匹配: 裁剪区 {self.actual_crop[0]}x{self.actual_crop[1]} -> "
                f"{self.matched[0]}x{self.matched[1]} @ {self.matched[2]:.1f}x"
            )
        self._persist_current_item_state(flush=False)

    def load_current(self, auto_skip_unfinished: bool = True, skip_step: int = 1):
        if not self.files:
            self.status_var.set("src_dir 没有图片")
            return
        # 自动跳过已完成项（可选）
        if auto_skip_unfinished:
            next_unfinished = self._find_next_unfinished(self.index, step=skip_step, include_start=True)
            if next_unfinished is not None:
                self.index = next_unfinished
        self.index = max(0, min(self.index, len(self.files) - 1))
        p = self.files[self.index]
        self._translate_job_token += 1
        self.current_file_key = str(p)
        # 全程使用 RGBA，补齐透明通道
        self.orig_img = Image.open(p).convert("RGBA")
        iw, ih = self.orig_img.size
        init = None
        if self.cfg.default_crop is not None:
            tw, th, z = self.cfg.default_crop
            init = find_candidate_by_target_zoom(self.candidates, iw, ih, tw, th, z)
        if init is None:
            init = choose_best_candidate(self.candidates, iw, ih, iw, ih)
        if init is None:
            self.crop_box_orig = (0, 0, iw, ih)
            self.actual_crop = (iw, ih)
            self.matched = (min(iw, 1536), min(ih, 1536), 1.0)
        else:
            cw, ch = init["crop_w"], init["crop_h"]
            x1 = (iw - cw) // 2
            y1 = (ih - ch) // 2
            self.crop_box_orig = (x1, y1, x1 + cw, y1 + ch)
            self.actual_crop = (cw, ch)
            self.matched = (init["target_w"], init["target_h"], init["zoom"])
        self.base_tags = [] if self.cfg.disable_orig_tags else Tags.split_caption(load_text(p.with_suffix(".txt")))
        self.wd14_tags = []
        self.no_crop_resize_var.set(self.cfg.default_no_crop_resize)
        self.refresh_tag_panel()
        self._apply_saved_item_state(p)
        if self.no_crop_resize_var.get():
            self.on_toggle_no_crop_resize()
        self.redraw_image()
        mode_note = " | 原始tag已禁用" if self.cfg.disable_orig_tags else ""
        self.status_var.set(
            f"{p.name} ({self.index+1}/{len(self.files)}) 原图:{self.orig_img.width}x{self.orig_img.height} | 已完成:{self.done_count}/{len(self.files)}{mode_note}"
        )
        if self.no_crop_resize_var.get():
            self.live_var.set(
                f"实时匹配: 不裁切模式，整图 {self.orig_img.width}x{self.orig_img.height}"
            )
        else:
            self.live_var.set(
                f"实时匹配: 裁剪区 {self.actual_crop[0]}x{self.actual_crop[1]} -> "
                f"{self.matched[0]}x{self.matched[1]} @ {self.matched[2]:.1f}x"
            )
        self.refresh_caption()
        self.start_background_wd14()

    def start_background_wd14(self):
        if self.orig_img is None:
            return
        self.wd14_job_token += 1
        token = self.wd14_job_token
        file_key = self.current_file_key
        img = self.orig_img.copy()
        self.status_var.set(self.status_var.get() + " | WD14后台打标中...")

        def _worker():
            tags = run_wd14_on_image(img)
            self.root.after(0, lambda: self._on_background_wd14_done(token, file_key, tags))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_background_wd14_done(self, token: int, file_key: str, tags: List[str]):
        if token != self.wd14_job_token or file_key != self.current_file_key:
            return
        self.wd14_tags = sorted(set(tags))
        self.refresh_tag_panel(keep_existing_selection=True)
        cur = self.status_var.get().replace(" | WD14后台打标中...", "")
        self.status_var.set(cur + " | WD14已更新")
        self._persist_current_item_state(flush=False)

    def redraw_image(self):
        if self.orig_img is None:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        iw, ih = self.orig_img.size
        self.scale = min(cw / iw, ch / ih)
        vw, vh = max(1, int(iw * self.scale)), max(1, int(ih * self.scale))
        self.view_img = self.orig_img.resize((vw, vh), Image.Resampling.BILINEAR)
        self.tk_img = ImageTk.PhotoImage(self.view_img)
        self.offset_x = (cw - vw) // 2
        self.offset_y = (ch - vh) // 2
        self.canvas.delete("all")

        # 绘制棋盘格背景
        grid_size = 20
        for y in range(0, vh, grid_size):
            for x in range(0, vw, grid_size):
                color = "#cccccc" if (x // grid_size + y // grid_size) % 2 == 0 else "#eeeeee"
                x1 = self.offset_x + x
                y1 = self.offset_y + y
                x2 = min(x1 + grid_size, self.offset_x + vw)
                y2 = min(y1 + grid_size, self.offset_y + vh)
                self.canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="")

        self.canvas.create_image(self.offset_x, self.offset_y, anchor=tk.NW, image=self.tk_img)
        if self.crop_box_orig:
            x1, y1, x2, y2 = self.crop_box_orig
            sx1, sy1 = self.raw_to_screen(x1, y1)
            sx2, sy2 = self.raw_to_screen(x2, y2)
            self.rect_id = self.canvas.create_rectangle(sx1, sy1, sx2, sy2, outline="#00ffff", width=2)
            # 角点可视化，便于触发对角拖拽
            hs = 5
            for cx, cy in ((sx1, sy1), (sx2, sy1), (sx1, sy2), (sx2, sy2)):
                self.canvas.create_rectangle(cx - hs, cy - hs, cx + hs, cy + hs, fill="#00ffff", outline="#003333")

    def raw_to_screen(self, x: int, y: int):
        return int(self.offset_x + x * self.scale), int(self.offset_y + y * self.scale)

    def screen_to_raw(self, x: int, y: int):
        if self.orig_img is None:
            return 0, 0
        rx = int((x - self.offset_x) / self.scale)
        ry = int((y - self.offset_y) / self.scale)
        rx = max(0, min(self.orig_img.width - 1, rx))
        ry = max(0, min(self.orig_img.height - 1, ry))
        return rx, ry

    def on_press(self, e):
        if self.orig_img is None:
            return
        if self.no_crop_resize_var.get():
            return
        rx, ry = self.screen_to_raw(e.x, e.y)
        self.drag_start_raw = (rx, ry)
        self.drag_base_box = self.crop_box_orig
        self.drag_mode = self._hit_test_mode(e.x, e.y)
        self._set_canvas_cursor(self.drag_mode)

    def _hit_test_mode(self, sx: int, sy: int) -> str:
        if not self.crop_box_orig:
            return "new"
        x1, y1, x2, y2 = self.crop_box_orig
        sx1, sy1 = self.raw_to_screen(x1, y1)
        sx2, sy2 = self.raw_to_screen(x2, y2)
        left, right = min(sx1, sx2), max(sx1, sx2)
        top, bottom = min(sy1, sy2), max(sy1, sy2)
        tol_px = max(8, self.handle_tol_px + 2)

        near_tl = abs(sx - left) <= tol_px and abs(sy - top) <= tol_px
        near_tr = abs(sx - right) <= tol_px and abs(sy - top) <= tol_px
        near_bl = abs(sx - left) <= tol_px and abs(sy - bottom) <= tol_px
        near_br = abs(sx - right) <= tol_px and abs(sy - bottom) <= tol_px
        near_left = abs(sx - left) <= tol_px and top <= sy <= bottom
        near_right = abs(sx - right) <= tol_px and top <= sy <= bottom
        near_top = abs(sy - top) <= tol_px and left <= sx <= right
        near_bottom = abs(sy - bottom) <= tol_px and left <= sx <= right
        inside = (left <= sx <= right) and (top <= sy <= bottom)
        if near_tl:
            return "resize_tl"
        if near_tr:
            return "resize_tr"
        if near_bl:
            return "resize_bl"
        if near_br:
            return "resize_br"
        if near_left:
            return "resize_left"
        if near_right:
            return "resize_right"
        if near_top:
            return "resize_top"
        if near_bottom:
            return "resize_bottom"
        if inside:
            return "move"
        return "new"

    def on_drag(self, e):
        if self.orig_img is None or self.drag_mode is None:
            return
        if self.no_crop_resize_var.get():
            return
        rx, ry = self.screen_to_raw(e.x, e.y)
        iw, ih = self.orig_img.size
        if not self.drag_base_box:
            self.drag_base_box = self.crop_box_orig if self.crop_box_orig else (0, 0, 1, 1)
        bx1, by1, bx2, by2 = self.drag_base_box
        cur_w = max(1, bx2 - bx1)
        cur_h = max(1, by2 - by1)

        new_box = self.crop_box_orig
        cand = None

        if self.drag_mode == "move":
            sx, sy = self.drag_start_raw
            dx, dy = rx - sx, ry - sy
            nx1, ny1, nx2, ny2 = bx1 + dx, by1 + dy, bx2 + dx, by2 + dy
            if nx1 < 0:
                nx2 -= nx1
                nx1 = 0
            if ny1 < 0:
                ny2 -= ny1
                ny1 = 0
            if nx2 > iw:
                nx1 -= (nx2 - iw)
                nx2 = iw
            if ny2 > ih:
                ny1 -= (ny2 - ih)
                ny2 = ih
            new_box = (max(0, nx1), max(0, ny1), min(iw, nx2), min(ih, ny2))
            cand = choose_best_candidate(self.candidates, iw, ih, nx2 - nx1, ny2 - ny1)
        elif self.drag_mode == "new":
            sx, sy = self.drag_start_raw
            desired_w = abs(rx - sx)
            desired_h = abs(ry - sy)
            if desired_w < 2 or desired_h < 2:
                return
            sign_x = 1 if rx >= sx else -1
            sign_y = 1 if ry >= sy else -1
            max_w = (iw - sx) if sign_x > 0 else sx
            max_h = (ih - sy) if sign_y > 0 else sy
            cand = choose_best_candidate(self.candidates, max_w, max_h, desired_w, desired_h)
            if cand is None:
                return
            cw, ch = cand["crop_w"], cand["crop_h"]
            x1 = sx if sign_x > 0 else sx - cw
            y1 = sy if sign_y > 0 else sy - ch
            x2 = x1 + cw
            y2 = y1 + ch
            new_box = (max(0, x1), max(0, y1), min(iw, x2), min(ih, y2))
        elif self.drag_mode == "resize_left":
            fixed_x2 = bx2
            max_w = max(1, fixed_x2)
            desired_w = max(1, fixed_x2 - rx)
            cand = choose_best_candidate(self.candidates, max_w, ih, desired_w, cur_h, weight_w=1.0, weight_h=4.0)
            if cand is None:
                return
            cw = cand["crop_w"]
            nx1 = fixed_x2 - cw
            new_box = (nx1, by1, fixed_x2, by1 + cand["crop_h"])
        elif self.drag_mode == "resize_right":
            fixed_x1 = bx1
            max_w = max(1, iw - fixed_x1)
            desired_w = max(1, rx - fixed_x1)
            cand = choose_best_candidate(self.candidates, max_w, ih, desired_w, cur_h, weight_w=1.0, weight_h=4.0)
            if cand is None:
                return
            cw = cand["crop_w"]
            new_box = (fixed_x1, by1, fixed_x1 + cw, by1 + cand["crop_h"])
        elif self.drag_mode == "resize_top":
            fixed_y2 = by2
            max_h = max(1, fixed_y2)
            desired_h = max(1, fixed_y2 - ry)
            cand = choose_best_candidate(self.candidates, iw, max_h, cur_w, desired_h, weight_w=4.0, weight_h=1.0)
            if cand is None:
                return
            ch = cand["crop_h"]
            ny1 = fixed_y2 - ch
            new_box = (bx1, ny1, bx1 + cand["crop_w"], fixed_y2)
        elif self.drag_mode == "resize_bottom":
            fixed_y1 = by1
            max_h = max(1, ih - fixed_y1)
            desired_h = max(1, ry - fixed_y1)
            cand = choose_best_candidate(self.candidates, iw, max_h, cur_w, desired_h, weight_w=4.0, weight_h=1.0)
            if cand is None:
                return
            ch = cand["crop_h"]
            new_box = (bx1, fixed_y1, bx1 + cand["crop_w"], fixed_y1 + ch)
        elif self.drag_mode == "resize_tl":
            fixed_x2, fixed_y2 = bx2, by2
            max_w = max(1, fixed_x2)
            max_h = max(1, fixed_y2)
            desired_w = max(1, fixed_x2 - rx)
            desired_h = max(1, fixed_y2 - ry)
            cand = choose_best_candidate(self.candidates, max_w, max_h, desired_w, desired_h)
            if cand is None:
                return
            new_box = (fixed_x2 - cand["crop_w"], fixed_y2 - cand["crop_h"], fixed_x2, fixed_y2)
        elif self.drag_mode == "resize_tr":
            fixed_x1, fixed_y2 = bx1, by2
            max_w = max(1, iw - fixed_x1)
            max_h = max(1, fixed_y2)
            desired_w = max(1, rx - fixed_x1)
            desired_h = max(1, fixed_y2 - ry)
            cand = choose_best_candidate(self.candidates, max_w, max_h, desired_w, desired_h)
            if cand is None:
                return
            new_box = (fixed_x1, fixed_y2 - cand["crop_h"], fixed_x1 + cand["crop_w"], fixed_y2)
        elif self.drag_mode == "resize_bl":
            fixed_x2, fixed_y1 = bx2, by1
            max_w = max(1, fixed_x2)
            max_h = max(1, ih - fixed_y1)
            desired_w = max(1, fixed_x2 - rx)
            desired_h = max(1, ry - fixed_y1)
            cand = choose_best_candidate(self.candidates, max_w, max_h, desired_w, desired_h)
            if cand is None:
                return
            new_box = (fixed_x2 - cand["crop_w"], fixed_y1, fixed_x2, fixed_y1 + cand["crop_h"])
        elif self.drag_mode == "resize_br":
            fixed_x1, fixed_y1 = bx1, by1
            max_w = max(1, iw - fixed_x1)
            max_h = max(1, ih - fixed_y1)
            desired_w = max(1, rx - fixed_x1)
            desired_h = max(1, ry - fixed_y1)
            cand = choose_best_candidate(self.candidates, max_w, max_h, desired_w, desired_h)
            if cand is None:
                return
            new_box = (fixed_x1, fixed_y1, fixed_x1 + cand["crop_w"], fixed_y1 + cand["crop_h"])

        if not new_box:
            return
        x1, y1, x2, y2 = map(int, new_box)
        x1 = max(0, min(iw - 1, x1))
        y1 = max(0, min(ih - 1, y1))
        x2 = max(x1 + 1, min(iw, x2))
        y2 = max(y1 + 1, min(ih, y2))
        w = x2 - x1
        h = y2 - y1
        cand = choose_best_candidate(self.candidates, w, h, w, h) if cand is None else cand
        if cand is not None:
            # 吸附成候选定义尺寸，避免出现中间态尺寸
            sw, sh = cand["crop_w"], cand["crop_h"]
            if self.drag_mode in ("move", "resize_left", "resize_right", "resize_top", "resize_bottom", "resize_tl", "resize_tr", "resize_bl", "resize_br"):
                if self.drag_mode == "move":
                    x2 = min(iw, x1 + sw)
                    y2 = min(ih, y1 + sh)
                    x1 = x2 - sw
                    y1 = y2 - sh
                elif self.drag_mode == "resize_left":
                    x1 = x2 - sw
                    y2 = y1 + sh
                elif self.drag_mode == "resize_right":
                    x2 = x1 + sw
                    y2 = y1 + sh
                elif self.drag_mode == "resize_top":
                    y1 = y2 - sh
                    x2 = x1 + sw
                elif self.drag_mode == "resize_bottom":
                    y2 = y1 + sh
                    x2 = x1 + sw
                elif self.drag_mode == "resize_tl":
                    x1 = x2 - sw
                    y1 = y2 - sh
                elif self.drag_mode == "resize_tr":
                    x2 = x1 + sw
                    y1 = y2 - sh
                elif self.drag_mode == "resize_bl":
                    x1 = x2 - sw
                    y2 = y1 + sh
                elif self.drag_mode == "resize_br":
                    x2 = x1 + sw
                    y2 = y1 + sh
            else:
                # new 模式已按候选生成，此处仅兜底
                x2 = min(iw, x1 + sw)
                y2 = min(ih, y1 + sh)
            self.matched = (cand["target_w"], cand["target_h"], cand["zoom"])
            self.actual_crop = (sw, sh)
        else:
            self.actual_crop = (w, h)

        self.crop_box_orig = (x1, y1, x2, y2)
        self.redraw_image()
        self.live_var.set(
            f"实时匹配: 裁剪区 {self.actual_crop[0]}x{self.actual_crop[1]} -> "
            f"{self.matched[0]}x{self.matched[1]} @ {self.matched[2]:.1f}x | 模式:{self.drag_mode}"
        )
        self.refresh_caption()

    def on_release(self, e):
        self.drag_mode = None
        self.drag_base_box = None
        self.on_motion(e)
        self._persist_current_item_state(flush=False)

    def on_motion(self, e):
        if self.no_crop_resize_var.get():
            self._set_canvas_cursor("new")
            return
        mode = self._hit_test_mode(e.x, e.y)
        self._set_canvas_cursor(mode)

    def _set_canvas_cursor(self, mode: str):
        if mode == "move":
            cursor = "fleur"
        elif mode in ("resize_tl", "resize_br"):
            cursor = "top_left_corner"
        elif mode in ("resize_tr", "resize_bl"):
            cursor = "top_right_corner"
        elif mode in ("resize_left", "resize_right"):
            cursor = "sb_h_double_arrow"
        elif mode in ("resize_top", "resize_bottom"):
            cursor = "sb_v_double_arrow"
        else:
            cursor = "crosshair"
        try:
            self.canvas.configure(cursor=cursor)
        except tk.TclError:
            # 某些 Linux/Tk 环境不支持部分 cursor 名称，降级避免报错刷屏
            fallback = "crosshair" if mode != "move" else "fleur"
            try:
                self.canvas.configure(cursor=fallback)
            except tk.TclError:
                pass

    def _set_tag_hover(self, v: bool):
        self.tag_hover = v

    def _on_global_mousewheel(self, e):
        if not getattr(self, "tag_hover", False):
            return
        self._on_tag_mousewheel(e)

    def _on_tag_mousewheel(self, e):
        if getattr(e, "delta", 0):
            step = -1 * int(e.delta / 120)
        elif getattr(e, "num", None) == 4:
            step = -1
        else:
            step = 1
        self.tag_canvas.yview_scroll(step, "units")

    def refresh_tag_panel(self, keep_existing_selection: bool = False, default_selected: Optional[set[str]] = None):
        selected_before = set(self.selected_raw_tags()) if keep_existing_selection else set()
        base = sorted(set(self.base_tags))
        wd = sorted(set(self.wd14_tags))
        all_tags = Tags.panel_sorted_list(self.base_tags, self.wd14_tags)
        zh, missing = get_cached_translations(all_tags)
        b_keys = {Tags.equivalence_key(t) for t in base}
        w_keys = {Tags.equivalence_key(t) for t in wd}
        pairs = []
        for en, cn in zip(all_tags, zh):
            k = Tags.equivalence_key(en)
            src = []
            if k in b_keys:
                src.append("原")
            if k in w_keys:
                src.append("W")
            pairs.append((f"[{'/'.join(src)}] {en} ({cn})", en))

        selected_set = default_selected if default_selected is not None else set(all_tags)
        if keep_existing_selection and default_selected is None:
            selected_set = selected_before | (set(wd) - set(base))
        self.set_tags(pairs, default_selected=selected_set)
        if missing:
            self._start_background_translate_missing(missing, self._translate_job_token)

    def _start_background_translate_missing(self, missing: list[str], token: int):
        if not missing:
            return
        uniq_missing = list(dict.fromkeys(missing))

        def _worker():
            new_map = _translate_missing_tags(uniq_missing)
            with _TRANSLATION_LOCK:
                _TRANSLATION_CACHE.update(new_map)
            self.root.after(0, lambda: self._on_background_translate_done(token))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_background_translate_done(self, token: int):
        if token != self._translate_job_token:
            return
        selected = set(self.selected_raw_tags())
        self.refresh_tag_panel(default_selected=selected)

    def set_tags(self, display_raw_pairs: List[Tuple[str, str]], default_selected: Optional[set[str]] = None):
        for w in self.tag_frame.winfo_children():
            w.destroy()
        self.display_to_raw = display_raw_pairs
        self.tag_vars = []
        wrap_len = max(220, self.right.winfo_width() - 120)
        selected_keys = Tags.selection_key_set(default_selected)
        for disp, _raw in display_raw_pairs:
            checked = True if selected_keys is None else (Tags.equivalence_key(_raw) in selected_keys)
            v = tk.BooleanVar(value=checked)
            cb = tk.Checkbutton(
                self.tag_frame,
                text=disp,
                variable=v,
                command=self.on_tag_toggle,
                anchor="w",
                justify="left",
                wraplength=wrap_len,
            )
            cb.pack(anchor="w", fill=tk.X, pady=1)
            self.tag_vars.append(v)
        self.refresh_caption()

    def on_tag_toggle(self):
        self.refresh_caption()
        self._persist_current_item_state(flush=False)

    def selected_raw_tags(self):
        out = []
        for (disp, raw), v in zip(self.display_to_raw, self.tag_vars):
            if v.get():
                out.append(raw)
        return out

    def apply_crop_and_tag(self):
        if self.orig_img is None:
            return
        if self.no_crop_resize_var.get():
            crop = self.orig_img
            cw, ch = crop.size
        elif self.crop_box_orig is None:
            crop = self.orig_img
            cw, ch = crop.size
        else:
            l, t, r, b = self.crop_box_orig
            crop = self.orig_img.crop((l, t, r, b))
            cw, ch = crop.size
        cand = choose_best_candidate(self.candidates, cw, ch, cw, ch)
        if cand is not None:
            self.matched = (cand["target_w"], cand["target_h"], cand["zoom"])
        tw, th, z = self.matched
        self.actual_crop = (cw, ch)

        # 手动应用裁切后，按当前裁切区域重新计算 WD14，并保留用户现有勾选
        self.wd14_tags = sorted(set(run_wd14_on_image(crop)))
        self.refresh_tag_panel(keep_existing_selection=True)
        prefix = "整图打标" if self.no_crop_resize_var.get() else "已打标"
        self.status_var.set(f"{prefix}: 裁剪区 {cw}x{ch} -> {tw}x{th} @ {z:.1f}x")
        self.refresh_caption()
        self._persist_current_item_state(flush=False)

    def refresh_caption(self):
        self.caption_var.set(Tags.merge_ui_preview(self.selected_raw_tags(), self.get_extra_tags_text()))

    def save_and_next(self):
        if self.orig_img is None or not self.files:
            return
        p = self.files[self.index]
        # 保存阶段不会运行 WD14；只做裁剪缩放与写盘。
        if self.crop_box_orig is None:
            crop = self.orig_img
        else:
            l, t, r, b = self.crop_box_orig
            crop = self.orig_img.crop((l, t, r, b))
        img_out_exists = (self.cfg.out_dir / p.with_suffix(".png").name).exists()
        txt_out_exists = (self.cfg.out_dir / p.with_suffix(".txt").name).exists()
        if self.no_crop_resize_var.get():
            out_img = self.orig_img.copy()
            tw, th = out_img.size
        else:
            tw, th, _ = self.matched
            out_img = crop.resize((tw, th), Image.Resampling.LANCZOS)
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        # 统一保存为 .png，保留 alpha 通道
        out_path = self.cfg.out_dir / p.with_suffix(".png").name
        out_img.save(out_path, format="PNG")
        out_path.with_suffix(".txt").write_text(self.caption_var.get(), encoding="utf-8")
        if not (img_out_exists and txt_out_exists):
            self.done_count += 1
        self.status_var.set(
            f"已保存: {out_path.name} -> {tw}x{th} | 已完成:{self.done_count}/{len(self.files)}"
        )
        self.next_image()

    def next_image(self):
        if not self.files:
            return
        self._persist_current_item_state(flush=False)
        next_unfinished = self._find_next_unfinished(self.index, step=1, include_start=False)
        if next_unfinished is None:
            self.status_var.set(f"全部完成: {self.done_count}/{len(self.files)}")
            return
        self.index = next_unfinished
        self._schedule_session_save(delay_ms=10)
        self.load_current(auto_skip_unfinished=True, skip_step=1)

    def prev_image(self):
        if not self.files:
            return
        self._persist_current_item_state(flush=False)
        self.index = max(0, self.index - 1)
        self._schedule_session_save(delay_ms=10)
        # 上一张按真实顺序回退，避免被自动跳过覆盖
        self.load_current(auto_skip_unfinished=False)


def run_batch_no_gui(cfg: AppConfig, *, force: bool = False) -> None:
    """
    不启动 GUI：对 src_dir 内全部图像整图跑 WD14，与原 txt 标签合并后写入 out_dir；
    图像不裁切、不缩放，统一保存为 RGBA PNG。
    """
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted([p for p in cfg.src_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS])
    if not files:
        print("src_dir 没有图片")
        return
    n_skip = 0
    n_ok = 0
    for i, p in enumerate(files):
        out_img_path = cfg.out_dir / p.with_suffix(".png").name
        out_txt_path = out_img_path.with_suffix(".txt")
        if not force and out_img_path.exists() and out_txt_path.exists():
            n_skip += 1
            print(f"[{i + 1}/{len(files)}] 跳过(已存在): {out_img_path.name}")
            continue
        print(f"[{i + 1}/{len(files)}] 处理: {p.name} -> {out_img_path.name}")
        img = Image.open(p).convert("RGBA")
        base = [] if cfg.disable_orig_tags else Tags.split_caption(load_text(p.with_suffix(".txt")))
        wd = run_wd14_on_image(img)
        caption = Tags.merge_for_disk(base, wd)
        
        img.save(out_img_path, format="PNG")
        out_txt_path.write_text(caption, encoding="utf-8")
        n_ok += 1
    print(f"完成: 新处理 {n_ok} 张, 跳过 {n_skip} 张, 合计扫描 {len(files)} 张")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src_dir", default="datasets/zhao")
    parser.add_argument("--out_dir", default="datasets/zhao_washed")
    parser.add_argument("--disable_orig_tags", action="store_true", help="禁用读取原数据集txt标签，仅使用WD14与手动标签")
    parser.add_argument(
        "--batch_no_gui",
        action="store_true",
        help="不启动 GUI：批量整图 WD14 打标，与原 txt 标签合并后保存到 out_dir，不裁切不缩放",
    )
    parser.add_argument(
        "--batch_force",
        action="store_true",
        help="与 --batch_no_gui 联用：已存在同名图片+txt 时也重新处理覆盖",
    )
    parser.add_argument(
        "--default-crop",
        type=parse_default_crop_spec,
        default=None,
        dest="default_crop",
        metavar="WxH@Z",
        help=(
            "打开每张图时的默认裁切匹配，如 1152x1152@1.0 或 1152x1152@1.0x（目标分辨率×倍率，"
            "须为工具内置桶组合；原图放不下或无法匹配时仍回退自动吸附）"
        ),
    )
    parser.add_argument(
        "--default-no-crop-resize",
        action="store_true",
        help=(
            "打开每张图时默认勾选「此图不裁切缩放」；保存原分辨率、不做 LANCZOS 缩放。"
            "若该图在会话 JSON 里已有记录，仍以会话为准。"
        ),
    )
    args = parser.parse_args()
    cfg = AppConfig(
        Path(args.src_dir),
        Path(args.out_dir),
        disable_orig_tags=args.disable_orig_tags,
        default_crop=args.default_crop,
        default_no_crop_resize=args.default_no_crop_resize,
    )
    if args.batch_no_gui:
        run_batch_no_gui(cfg, force=args.batch_force)
        return
    app = LabelTool(cfg)
    app.run()


if __name__ == "__main__":
    main()

