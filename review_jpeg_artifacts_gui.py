#!/usr/bin/env python3
"""全屏浏览图片，左右键判断 tag 是否适用。

用法:
  python review_jpeg_artifacts_gui.py [目录] [--tag TAG] [--auto-ext .jpg,.jpeg] [--tag-position head|random]

左键 = 有该特征（加 tag 并跳到下一张）
右键 = 无该特征（删 tag 并跳到下一张）
Q/Esc 退出（自动保存进度）   Space 跳过   Backspace 回退   F 切换填充/适应   M 开关放大镜
右下角显示标记状态；指定后缀的文件自动标；递归处理子目录；进度保存到 <目录>/.review_<tag>.json
鼠标移动时显示跟随光标的圆形放大镜（3x），方便检查 JPEG 伪影等细节
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from tkinter import BOTH, Canvas, Event, Tk

from PIL import Image, ImageDraw, ImageTk

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


# ---------- tag 操作 ----------

def has_tag(tags_str: str, tag: str) -> bool:
    for p in tags_str.replace("|||", ",").split(","):
        if p.strip().lower() == tag.lower():
            return True
    return False


def add_tag(tags_str: str, tag: str, position: str = "head") -> str:
    """添加 tag。若有 ||| 分隔符则追加到 ||| 前（promoted 区段末尾）；否则按 position 放置。"""
    sections = tags_str.split("|||", 1)
    promoted_str = sections[0].strip()
    has_sep = len(sections) > 1
    remaining_str = sections[1].strip() if has_sep else ""

    promoted_parts = [p.strip() for p in promoted_str.split(",") if p.strip()]
    remaining_parts = [p.strip() for p in remaining_str.split(",") if p.strip()]

    # 去重：先移除已存在的同名 tag
    promoted_parts = [p for p in promoted_parts if p.lower() != tag.lower()]
    remaining_parts = [p for p in remaining_parts if p.lower() != tag.lower()]

    if has_sep:
        # 有 ||| 时：追加到 promoted 区段末尾（||| 前的最后一个 tag）
        promoted_parts.append(tag)
    elif position == "head":
        promoted_parts.insert(0, tag)
    elif position == "random":
        insert_index = random.randint(0, len(promoted_parts))
        promoted_parts.insert(insert_index, tag)
    else:
        promoted_parts.append(tag)

    if remaining_parts:
        return ", ".join(promoted_parts) + " ||| " + ", ".join(remaining_parts)
    return ", ".join(promoted_parts)


def remove_tag(tags_str: str, tag: str) -> str:
    sections = tags_str.split("|||", 1)
    promoted_str = sections[0].strip()
    has_sep = len(sections) > 1
    remaining_str = sections[1].strip() if has_sep else ""

    promoted_parts = [p.strip() for p in promoted_str.split(",") if p.strip() and p.strip().lower() != tag.lower()]
    remaining_parts = [p.strip() for p in remaining_str.split(",") if p.strip() and p.strip().lower() != tag.lower()]

    if remaining_parts:
        return ", ".join(promoted_parts) + " ||| " + ", ".join(remaining_parts)
    return ", ".join(promoted_parts)


# ---------- txt 读写 ----------

def normalize_tags(tags_str: str) -> str:
    sections = tags_str.replace("\n", ",").split("|||", 1)
    promoted_parts = [p.strip() for p in sections[0].split(",") if p.strip()]
    if len(sections) > 1:
        remaining_parts = [p.strip() for p in sections[1].split(",") if p.strip()]
        if remaining_parts:
            return ", ".join(promoted_parts) + " ||| " + ", ".join(remaining_parts)
    return ", ".join(promoted_parts)


def read_tags(txt_path: Path) -> str:
    if not txt_path.is_file():
        return ""
    return normalize_tags(txt_path.read_text(encoding="utf-8"))


def write_tags(txt_path: Path, new_tags: str) -> None:
    txt_path.write_text(normalize_tags(new_tags), encoding="utf-8", newline="\n")


# ---------- 主逻辑 ----------

class App:
    def __init__(
        self,
        root: Tk,
        input_dir: Path,
        tag: str,
        auto_ext: set[str],
        tag_position: str,
        fullscreen: bool = True,
        fit_mode: bool = False,
    ) -> None:
        self.root = root
        self.tag = tag
        self.auto_ext = auto_ext
        self.tag_position = tag_position
        self.fullscreen = fullscreen
        self.fit_mode = fit_mode  # False=填满屏幕, True=适应屏幕
        self.root.title(f"Review: {tag}")
        self.root.configure(bg="black")

        if fullscreen:
            self.root.attributes("-fullscreen", True)
            self.w = root.winfo_screenwidth()
            self.h = root.winfo_screenheight()
        else:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            self.w = int(sw * 0.9)
            self.h = int(sh * 0.9)
            x = (sw - self.w) // 2
            y = (sh - self.h) // 2
            self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")
            self.root.update_idletasks()  # 确保 geometry 生效后再取实际尺寸
            self.w = root.winfo_width()
            self.h = root.winfo_height()
            # 窗口可调整大小
            self.root.bind("<Configure>", self._on_resize)

        self.input_dir = input_dir.resolve()
        safe_tag = tag.replace(" ", "_").replace("/", "_")
        self.progress_file = self.input_dir / f".review_{safe_tag}.json"

        # 递归收集 (图片, txt) 对
        self.pairs: list[tuple[Path, Path]] = []
        for p in sorted(self.input_dir.rglob("*"), key=lambda path: path.relative_to(self.input_dir).as_posix()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                self.pairs.append((p, p.with_suffix(".txt")))

        # 恢复进度
        saved = self._load_progress()
        self.idx = saved.get("index", 0)
        if self.idx >= len(self.pairs):
            self.idx = 0

        # 一个 Canvas 搞定一切
        self.canvas = Canvas(root, bg="black", highlightthickness=0)
        self.canvas.pack(fill=BOTH, expand=True)

        # ---------- 放大镜 ----------
        self.magnifier_on = True          # 是否启用放大镜
        self.mag_size = 180               # 放大镜显示尺寸 (px)
        self.mag_zoom = 3                 # 放大倍率
        self.mag_region = self.mag_size // self.mag_zoom  # 原始图中采样的区域大小
        self._mouse_x = -1
        self._mouse_y = -1
        self._img_offset_x = 0
        self._img_offset_y = 0
        self._img_scale = 1.0
        self._mag_photo = None

        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Leave>", self._on_mouse_leave)

        # ---------- 键盘 / 鼠标 ----------
        self.root.bind("<Button-1>", lambda e: self._act(True))
        self.root.bind("<Button-3>", lambda e: self._act(False))
        self.root.bind("<Key>", self._on_key)

        self._show()

    # ---------- 进度 ----------

    def _load_progress(self) -> dict:
        try:
            if self.progress_file.is_file():
                return json.loads(self.progress_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_progress(self) -> None:
        self.progress_file.write_text(
            json.dumps({"index": self.idx}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _on_resize(self, event: Event) -> None:
        if self.fullscreen:
            return
        self.w = event.width
        self.h = event.height
        if hasattr(self, "_current_pil"):
            self._redraw()

    # ---------- 显示 ----------

    def _show(self) -> None:
        if self.idx >= len(self.pairs):
            self.canvas.delete("all")
            self.canvas.create_text(
                self.w // 2, self.h // 2,
                text="Done!", fill="white", font=("", 48),
            )
            return

        img_path, txt_path = self.pairs[self.idx]

        # 读图
        try:
            im = Image.open(img_path)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
            elif im.mode != "RGB":
                im = im.convert("RGB")
        except Exception:
            self.idx += 1
            self._show()
            return

        self._current_pil = im
        self._current_img_path = img_path
        self._current_txt_path = txt_path

        # 自动标
        is_auto = img_path.suffix.lower() in self.auto_ext
        tags = read_tags(txt_path)
        tagged = has_tag(tags, self.tag)
        auto = False
        if is_auto and not tagged:
            write_tags(txt_path, add_tag(tags, self.tag, self.tag_position))
            tagged = True
            auto = True

        if auto:
            self._status_text = f"AUTO ({img_path.suffix})"
            self._status_color = "orange"
        elif tagged:
            self._status_text = "TAGGED"
            self._status_color = "#00ff00"
        else:
            self._status_text = "CLEAN"
            self._status_color = "#ff4444"

        self._render()

    def _render(self) -> None:
        """根据 self._current_pil 缩放并绘制到 Canvas（含叠加层）。"""
        im = self._current_pil
        if self.fit_mode:
            scale = min(self.w / im.width, self.h / im.height)
        else:
            scale = max(self.w / im.width, self.h / im.height)
        im_scaled = im.resize(
            (int(im.width * scale), int(im.height * scale)),
            Image.LANCZOS,
        )
        if im_scaled.mode == "RGBA":
            bg = Image.new("RGB", im_scaled.size, (0, 0, 0))
            bg.paste(im_scaled, mask=im_scaled.split()[3])
            im_scaled = bg

        # 记录缩放和偏移，供放大镜使用
        self._img_scale = scale
        self._img_offset_x = self.w // 2 - im_scaled.width // 2
        self._img_offset_y = self.h // 2 - im_scaled.height // 2

        self._photo = ImageTk.PhotoImage(im_scaled)
        self.canvas.delete("all")
        self.canvas.create_image(
            self.w // 2, self.h // 2, image=self._photo, anchor="center",
        )

        # 右下角状态
        self.canvas.create_text(
            self.w - 20, self.h - 20,
            anchor="se", text=self._status_text, fill=self._status_color, font=("", 20),
        )

        # 顶部中间进度 + 显示模式
        mode_label = "适应屏幕" if self.fit_mode else "填满屏幕"
        self.canvas.create_text(
            self.w // 2, 10,
            anchor="n", text=f"{self.idx + 1} / {len(self.pairs)}  [{mode_label}]",
            fill="#888888", font=("", 14),
        )

    def _redraw(self) -> None:
        """窗口大小变化时重绘（不重新读取图片和 tag）。"""
        self._render()
        # 重绘后鼠标可能还在原位置，尝试刷新放大镜
        if self._mouse_x >= 0:
            self._draw_magnifier()

    # ---------- 放大镜 ----------

    def _on_mouse_move(self, event: Event) -> None:
        self._mouse_x = event.x
        self._mouse_y = event.y
        if self.magnifier_on:
            self._draw_magnifier()

    def _on_mouse_leave(self, _event: Event) -> None:
        self._mouse_x = -1
        self._mouse_y = -1
        self.canvas.delete("magnifier")

    def _draw_magnifier(self) -> None:
        """在鼠标位置附近绘制圆形放大镜。"""
        self.canvas.delete("magnifier")
        if not hasattr(self, "_current_pil") or self._mouse_x < 0:
            return

        mx, my = self._mouse_x, self._mouse_y
        scale = self._img_scale
        ox, oy = self._img_offset_x, self._img_offset_y
        im = self._current_pil

        # 画布坐标 → 原始图坐标
        orig_x = (mx - ox) / scale
        orig_y = (my - oy) / scale

        if orig_x < 0 or orig_y < 0 or orig_x >= im.width or orig_y >= im.height:
            return

        # 截取原始图中以光标为中心的区域
        half_r = self.mag_region // 2
        x1 = max(0, int(orig_x - half_r))
        y1 = max(0, int(orig_y - half_r))
        x2 = min(im.width, x1 + self.mag_region)
        y2 = min(im.height, y1 + self.mag_region)
        x1 = max(0, x2 - self.mag_region)
        y1 = max(0, y2 - self.mag_region)

        if x2 <= x1 or y2 <= y1:
            return

        region = im.crop((x1, y1, x2, y2))
        # 边界补齐
        if region.size != (self.mag_region, self.mag_region):
            padded = Image.new("RGB", (self.mag_region, self.mag_region), (0, 0, 0))
            padded.paste(region, (0, 0))
            region = padded

        # 放大（NEAREST 保留像素颗粒感，方便检查压缩伪影）
        region = region.resize((self.mag_size, self.mag_size), Image.NEAREST)
        region = region.convert("RGBA")

        # 圆形遮罩
        mask = Image.new("L", (self.mag_size, self.mag_size), 0)
        mask_draw = ImageDraw.Draw(mask)
        m = 3
        mask_draw.ellipse((m, m, self.mag_size - m, self.mag_size - m), fill=255)
        region.putalpha(mask)

        # 圆形边框 + 十字准星
        draw = ImageDraw.Draw(region)
        draw.ellipse((m, m, self.mag_size - m, self.mag_size - m), outline="white", width=2)
        cx = self.mag_size // 2
        cy = self.mag_size // 2
        gap = 8
        L = 14
        draw.line((cx - L, cy, cx - gap, cy), fill="red", width=2)
        draw.line((cx + gap, cy, cx + L, cy), fill="red", width=2)
        draw.line((cx, cy - L, cx, cy - gap), fill="red", width=2)
        draw.line((cx, cy + gap, cx, cy + L), fill="red", width=2)

        self._mag_photo = ImageTk.PhotoImage(region)

        # 放大镜位置：默认在光标右下方，超出屏幕则翻转到另一侧
        mag_x = mx + 30
        mag_y = my + 30
        if mag_x + self.mag_size > self.w:
            mag_x = mx - self.mag_size - 30
        if mag_y + self.mag_size > self.h:
            mag_y = my - self.mag_size - 30
        mag_x = max(0, min(mag_x, self.w - self.mag_size))
        mag_y = max(0, min(mag_y, self.h - self.mag_size))

        self.canvas.create_image(
            mag_x, mag_y, image=self._mag_photo, anchor="nw", tags="magnifier",
        )

    # ---------- 操作 ----------

    def _act(self, is_artifact: bool) -> None:
        _, txt_path = self.pairs[self.idx]
        tags = read_tags(txt_path)
        new_tags = add_tag(tags, self.tag, self.tag_position) if is_artifact else remove_tag(tags, self.tag)
        write_tags(txt_path, new_tags)
        self.idx += 1
        self._save_progress()
        self._show()

    def _on_key(self, event: Event) -> None:
        k = event.keysym.lower()
        if k in ("q", "escape"):
            self._save_progress()
            self.root.destroy()
        elif k == "space":
            self.idx += 1
            self._save_progress()
            self._show()
        elif k == "backspace":
            self.idx = max(0, self.idx - 1)
            self._show()
        elif k == "f":
            self.fit_mode = not self.fit_mode
            if hasattr(self, "_current_pil"):
                self._redraw()
        elif k == "m":
            self.magnifier_on = not self.magnifier_on
            if not self.magnifier_on:
                self.canvas.delete("magnifier")
            elif self._mouse_x >= 0:
                self._draw_magnifier()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="全屏浏览图片，左右键判断 tag 是否适用。"
    )
    parser.add_argument(
        "dir", nargs="?", default=None,
        help="图片目录（默认: datasets/discord）",
    )
    parser.add_argument(
        "--tag", default="jpeg artifacts",
        help="要审核的 tag 名称（默认: 'jpeg artifacts'）",
    )
    parser.add_argument(
        "--auto-ext", default=".jpg,.jpeg",
        help="自动标记的文件后缀，逗号分隔（默认: .jpg,.jpeg）",
    )
    parser.add_argument(
        "--tag-position", choices=("head", "random"), default="head",
        help="添加 tag 的位置：head=放在头部，random=随机插入（默认: head）",
    )
    parser.add_argument(
        "--no-fullscreen", action="store_true",
        help="窗口模式（非全屏），可自由调整大小",
    )
    parser.add_argument(
        "--fit", action="store_true",
        help="初始使用适应屏幕模式（默认填满屏幕）；浏览中可按 F 键切换",
    )
    args = parser.parse_args()

    if args.dir:
        input_dir = Path(args.dir).resolve()
    else:
        input_dir = Path(__file__).resolve().parent / "datasets" / "discord"

    if not input_dir.is_dir():
        print(f"目录不存在: {input_dir}")
        sys.exit(1)

    auto_ext = {e.strip().lower() for e in args.auto_ext.split(",") if e.strip()}
    if not auto_ext:
        auto_ext = set()

    print(f"目录:   {input_dir}")
    print(f"Tag:    {args.tag}")
    print(f"位置:   {args.tag_position}")
    print(f"自动标: {auto_ext or '(无)'}")
    print(f"模式:   {'窗口' if args.no_fullscreen else '全屏'}")

    root = Tk()
    App(
        root,
        input_dir,
        args.tag,
        auto_ext,
        args.tag_position,
        fullscreen=not args.no_fullscreen,
        fit_mode=args.fit,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
