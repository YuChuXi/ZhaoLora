#!/usr/bin/env python3
"""全屏浏览图片，左右键判断 tag 是否适用。

用法:
  python review_jpeg_artifacts_gui.py [目录] [--tag TAG] [--auto-ext .jpg,.jpeg]

左键 = 有该特征（加 tag 并跳到下一张）
右键 = 无该特征（删 tag 并跳到下一张）
Q/Esc 退出（自动保存进度）   Space 跳过   Backspace 回退
右下角显示标记状态；指定后缀的文件自动标；进度保存到 <目录>/.review_<tag>.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tkinter import BOTH, Canvas, Event, Tk

from PIL import Image, ImageTk

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


# ---------- tag 操作 ----------

def has_tag(tags_str: str, tag: str) -> bool:
    for p in tags_str.split(","):
        if p.strip().lower() == tag.lower():
            return True
    return False


def add_tag(tags_str: str, tag: str) -> str:
    parts = [p.strip() for p in tags_str.split(",") if p.strip()]
    filtered = [p for p in parts if p.lower() != tag.lower()]
    if filtered:
        return tag + ", " + ", ".join(filtered)
    return tag


def remove_tag(tags_str: str, tag: str) -> str:
    parts = [p.strip() for p in tags_str.split(",") if p.strip()]
    filtered = [p for p in parts if p.lower() != tag.lower()]
    return ", ".join(filtered)


# ---------- txt 读写 ----------

def read_tags(txt_path: Path) -> str:
    if not txt_path.is_file():
        return ""
    lines = txt_path.read_text(encoding="utf-8").splitlines()
    return lines[0] if len(lines) >= 1 else ""


def write_tags(txt_path: Path, new_tags: str) -> None:
    lines = txt_path.read_text(encoding="utf-8").splitlines() if txt_path.is_file() else []
    while len(lines) < 1:
        lines.append("")
    lines[0] = new_tags
    txt_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


# ---------- 主逻辑 ----------

class App:
    def __init__(self, root: Tk, input_dir: Path, tag: str, auto_ext: set[str], fullscreen: bool = True, fit_mode: bool = False) -> None:
        self.root = root
        self.tag = tag
        self.auto_ext = auto_ext
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

        # 收集 (图片, txt) 对
        self.pairs: list[tuple[Path, Path]] = []
        for p in sorted(self.input_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                self.pairs.append((p, p.with_suffix(".txt")))
        self.pairs.sort(key=lambda x: x[0].name)

        # 恢复进度
        saved = self._load_progress()
        self.idx = saved.get("index", 0)
        if self.idx >= len(self.pairs):
            self.idx = 0

        # 一个 Canvas 搞定一切
        self.canvas = Canvas(root, bg="black", highlightthickness=0)
        self.canvas.pack(fill=BOTH, expand=True)

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
            write_tags(txt_path, add_tag(tags, self.tag))
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
        im = im.resize(
            (int(im.width * scale), int(im.height * scale)),
            Image.LANCZOS,
        )
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (0, 0, 0))
            bg.paste(im, mask=im.split()[3])
            im = bg

        self._photo = ImageTk.PhotoImage(im)
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

    # ---------- 操作 ----------

    def _act(self, is_artifact: bool) -> None:
        _, txt_path = self.pairs[self.idx]
        tags = read_tags(txt_path)
        write_tags(txt_path, add_tag(tags, self.tag) if is_artifact else remove_tag(tags, self.tag))
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
    print(f"自动标: {auto_ext or '(无)'}")
    print(f"模式:   {'窗口' if args.no_fullscreen else '全屏'}")

    root = Tk()
    App(root, input_dir, args.tag, auto_ext, fullscreen=not args.no_fullscreen, fit_mode=args.fit)
    root.mainloop()


if __name__ == "__main__":
    main()
