#!/usr/bin/env python3
"""
读取数据集目录或单个图片文件，在白底 RGB 上重新绘制 alpha（原图若有 alpha 先合成到白底后丢弃）。
支持画笔与矩形框选填充；保存 PNG 到输出目录（半透明 RGBA 或黑白 alpha 灰度图）。

依赖: Pillow, numpy（与 guo582_label_tk 一致）
"""

from __future__ import annotations

import argparse
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import numpy as np
from PIL import Image, ImageTk

SCRIPT_DIR = Path(__file__).resolve().parent
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
SAVE_FORMAT_RGBA = "rgba"
SAVE_FORMAT_ALPHA_BW = "alpha_bw"
SAVE_FORMAT_CHOICES = (SAVE_FORMAT_RGBA, SAVE_FORMAT_ALPHA_BW)


def list_images(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        out: list[Path] = []
        for ext in IMAGE_EXTS:
            out.extend(root.rglob(f"*{ext}"))
            out.extend(root.rglob(f"*{ext.upper()}"))
        return sorted({p.resolve() for p in out if p.is_file()})
    return sorted(
        p
        for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def load_rgb_white_flatten(path: Path) -> Image.Image:
    """RGBA/LA/P 等带透明通道的图：合成到白底，返回 RGB。"""
    try:
        im = Image.open(path)
    except Image.DecompressionBombError:
        # 该图片像素过大触发 Pillow 安全阈值；按“不要跳过”要求，放开限制并重试加载
        Image.MAX_IMAGE_PIXELS = None
        im = Image.open(path)
    im = im.convert("RGBA")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    bg.paste(im, mask=im.split()[3])
    return bg


def alpha_array_from_image(im: Image.Image) -> np.ndarray:
    """从已保存 PNG 提取 alpha：RGBA 取 A 通道，灰度图整图即 alpha。"""
    if im.mode in ("L", "LA"):
        return np.array(im.getchannel("L"), dtype=np.uint8, copy=True)
    rgba = im.convert("RGBA")
    return np.array(rgba.getchannel("A"), dtype=np.uint8, copy=True)


def load_saved_alpha(path: Path, size: tuple[int, int]) -> np.ndarray | None:
    if not path.is_file():
        return None
    try:
        saved = Image.open(path)
        if saved.size != size:
            return None
        return alpha_array_from_image(saved)
    except Exception as e:
        print(f"读取已保存 alpha 失败 {path}: {e}")
        return None


def build_save_image(
    rgb_img: Image.Image, alpha_array: np.ndarray, save_format: str
) -> Image.Image:
    if save_format == SAVE_FORMAT_ALPHA_BW:
        return Image.fromarray(alpha_array, mode="L")
    rgb_copy = rgb_img.copy()
    r, g, b = rgb_copy.split()
    a_img = Image.fromarray(alpha_array)
    return Image.merge("RGBA", (r, g, b, a_img))


def build_checkerboard(width: int, height: int, checker: int = 12) -> Image.Image:
    yy, xx = np.mgrid[0:height, 0:width]
    c = ((xx // checker) + (yy // checker)) % 2
    out = np.where(c == 0, 204, 153).astype(np.uint8)
    out = np.repeat(out[..., None], 3, axis=2)
    return Image.fromarray(out)


def compose_preview(rgb_view: Image.Image, alpha_view: Image.Image, checkerboard: Image.Image) -> Image.Image:
    """在显示分辨率下预览当前 RGBA，避免对原图反复整图计算。"""
    rgba_view = rgb_view.convert("RGBA")
    rgba_view.putalpha(alpha_view)
    base = checkerboard.convert("RGBA")
    return Image.alpha_composite(base, rgba_view).convert("RGB")


def stamp_disc(alpha: np.ndarray, cx: int, cy: int, radius: int, value: int) -> None:
    h, w = alpha.shape
    if radius <= 0:
        return
    y0, y1 = max(0, cy - radius), min(h, cy + radius + 1)
    x0, x1 = max(0, cx - radius), min(w, cx + radius + 1)
    if y0 >= y1 or x0 >= x1:
        return
    yy, xx = np.ogrid[y0:y1, x0:x1]
    mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius * radius
    sl = alpha[y0:y1, x0:x1]
    sl[mask] = np.uint8(value)


def stroke_line(alpha: np.ndarray, x0: int, y0: int, x1: int, y1: int, radius: int, value: int) -> None:
    dist = max(abs(x1 - x0), abs(y1 - y0), 1)
    for i in range(dist + 1):
        t = i / dist
        cx = int(round(x0 + (x1 - x0) * t))
        cy = int(round(y0 + (y1 - y0) * t))
        stamp_disc(alpha, cx, cy, radius, value)


def fill_rect(alpha: np.ndarray, x0: int, y0: int, x1: int, y1: int, value: int) -> None:
    h, w = alpha.shape
    xa, xb = sorted((x0, x1))
    ya, yb = sorted((y0, y1))
    xa = max(0, min(w - 1, xa))
    xb = max(0, min(w - 1, xb))
    ya = max(0, min(h - 1, ya))
    yb = max(0, min(h - 1, yb))
    if xa <= xb and ya <= yb:
        alpha[ya : yb + 1, xa : xb + 1] = np.uint8(value)


class AlphaPaintApp:
    def __init__(
        self,
        src_dir: Path,
        out_dir: Path,
        recursive: bool,
        save_format: str = SAVE_FORMAT_RGBA,
        files_override: list[Path] | None = None,
    ) -> None:
        if save_format not in SAVE_FORMAT_CHOICES:
            raise ValueError(f"save_format 须为 {SAVE_FORMAT_CHOICES}，收到: {save_format}")
        self.save_format = save_format
        self.src_dir = src_dir.resolve()
        self.out_dir = out_dir.resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if files_override is not None:
            raw_files = [p.expanduser().resolve() for p in files_override]
        else:
            raw_files = list_images(self.src_dir, recursive)
        self.files = self._prioritize_unmasked(raw_files)
        self.index = 0

        self.rgb: Image.Image | None = None
        self.alpha: np.ndarray | None = None
        self.current_path: Path | None = None

        self.scale = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.tk_img: ImageTk.PhotoImage | None = None
        self.image_id: int | None = None
        self._redraw_after_id: str | None = None
        self._cached_view_size: tuple[int, int] | None = None
        self._cached_rgb_view: Image.Image | None = None
        self._cached_checker: Image.Image | None = None

        self._dragging = False
        self._last_raw: tuple[int, int] | None = None
        self._rect_start: tuple[int, int] | None = None
        self._rect_preview_id: int | None = None
        self._is_modified = False

        # Python 3 Tk：必须先有根窗口，再创建 StringVar / IntVar
        self.root = tk.Tk()
        self.root.title("Alpha 绘制 (Tk)")
        self.root.geometry("1400x900")

        # 默认框选模式
        self.tool_var = tk.StringVar(master=self.root, value="rect")  # brush | rect
        self.paint_var = tk.StringVar(master=self.root, value="transparent")  # opaque | transparent
        self.brush_r_var = tk.IntVar(master=self.root, value=24)

        top = ttk.Frame(self.root)
        top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        ttk.Button(top, text="上一张 (←)", command=self.prev_image).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="下一张 (→)", command=self.next_image).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="跳过 (X)", command=self.skip_image).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="保存并下一张 (Ctrl+S)", command=self.save_and_next).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="清空 alpha", command=self.reset_alpha_transparent).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="填满 alpha", command=self.reset_alpha_opaque).pack(side=tk.LEFT, padx=2)

        ttk.Label(top, text="工具:").pack(side=tk.LEFT, padx=(16, 2))
        ttk.Radiobutton(top, text="画笔", variable=self.tool_var, value="brush").pack(side=tk.LEFT)
        ttk.Radiobutton(top, text="框选填充", variable=self.tool_var, value="rect").pack(side=tk.LEFT)

        ttk.Label(top, text="绘制:").pack(side=tk.LEFT, padx=(12, 2))
        ttk.Radiobutton(top, text="不透明(255)", variable=self.paint_var, value="opaque").pack(side=tk.LEFT)
        ttk.Radiobutton(top, text="透明(0)", variable=self.paint_var, value="transparent").pack(side=tk.LEFT)

        ttk.Label(top, text="笔刷半径:").pack(side=tk.LEFT, padx=(12, 2))
        ttk.Scale(top, from_=2, to=120, variable=self.brush_r_var, orient=tk.HORIZONTAL, length=140).pack(
            side=tk.LEFT
        )

        self.status_var = tk.StringVar(master=self.root, value="")
        ttk.Label(self.root, textvariable=self.status_var).pack(side=tk.TOP, anchor=tk.W, padx=8)
        ttk.Label(
            self.root,
            text="快捷键: Ctrl+S 保存并下一张  |  S 仅保存  |  Ctrl+N / X 跳过  |  ← → 翻页  |  画布滚轮调节笔刷半径",
            foreground="#888888",
        ).pack(side=tk.TOP, anchor=tk.W, padx=8, pady=(0, 4))

        self.canvas = tk.Canvas(self.root, bg="#333333", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.canvas.bind("<Configure>", lambda _e: self.request_redraw())
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<MouseWheel>", self.on_brush_wheel)
        self.canvas.bind("<Button-4>", self.on_brush_wheel)
        self.canvas.bind("<Button-5>", self.on_brush_wheel)
        self._setup_keyboard_shortcuts()

        if not self.files:
            print(f"错误: 未找到图片: {self.src_dir}")
            self.root.after(100, self.root.destroy)
            return

        self.load_index(0)

    def _setup_keyboard_shortcuts(self) -> None:
        self.root.bind_all("<Control-s>", self._on_hotkey_save_and_next, add=True)
        self.root.bind_all("<Control-S>", self._on_hotkey_save_and_next, add=True)
        self.root.bind_all("<Control-n>", self._on_hotkey_skip, add=True)
        self.root.bind_all("<Control-N>", self._on_hotkey_skip, add=True)
        self.root.bind_all("<Left>", self._on_hotkey_prev, add=True)
        self.root.bind_all("<Right>", self._on_hotkey_next, add=True)
        self.root.bind_all("<x>", self._on_hotkey_skip, add=True)
        self.root.bind_all("<X>", self._on_hotkey_skip, add=True)
        self.root.bind_all("<s>", self._on_hotkey_save_only, add=True)
        self.root.bind_all("<S>", self._on_hotkey_save_only, add=True)

    def _on_hotkey_save_and_next(self, _event=None) -> str:
        self.save_and_next()
        return "break"

    def _on_hotkey_save_only(self, _event=None) -> str:
        self.save_current()
        return "break"

    def _on_hotkey_skip(self, _event=None) -> str:
        self.skip_image()
        return "break"

    def _on_hotkey_prev(self, _event=None) -> str:
        self.prev_image()
        return "break"

    def _on_hotkey_next(self, _event=None) -> str:
        self.next_image()
        return "break"

    def paint_value(self) -> int:
        return 255 if self.paint_var.get() == "opaque" else 0

    def on_brush_wheel(self, event: tk.Event) -> str:
        """画布滚轮：上滚增大笔刷半径，下滚减小（与滑块范围 2–120 一致）。"""
        if event.num == 4:
            delta = 1
        elif event.num == 5:
            delta = -1
        elif event.delta > 0:
            delta = 1
        else:
            delta = -1
        cur = int(self.brush_r_var.get())
        step = max(2, cur // 12)
        new_r = max(2, min(120, cur + delta * step))
        self.brush_r_var.set(new_r)
        return "break"

    def rel_out_path(self, path: Path) -> Path:
        if path.resolve().is_relative_to(self.src_dir):
            rel = path.resolve().relative_to(self.src_dir)
        else:
            rel = Path(path.name)
        return rel.with_suffix(".png")

    def mask_out_path(self, path: Path) -> Path:
        return self.out_dir / self.rel_out_path(path)

    def has_mask(self, path: Path) -> bool:
        return self.mask_out_path(path).is_file()

    def count_pending_masks(self) -> int:
        return sum(1 for p in self.files if not self.has_mask(p))

    def _prioritize_unmasked(self, files: list[Path]) -> list[Path]:
        """输出目录尚无对应 mask PNG 的排在前面，同组内保持原扫描顺序。"""
        indexed = list(enumerate(files))

        def sort_key(item: tuple[int, Path]) -> tuple[int, int]:
            i, p = item
            return (1 if self.has_mask(p) else 0, i)

        indexed.sort(key=sort_key)
        return [p for _, p in indexed]

    def load_index(self, idx: int) -> None:
        if not self.files:
            return
        idx = max(0, min(len(self.files) - 1, idx))
        self.index = idx
        path = self.files[idx]
        rel = self.rel_out_path(path)
        saved_path = self.mask_out_path(path)
        pending = self.count_pending_masks()
        self.current_path = path
        self.rgb = load_rgb_white_flatten(path)
        iw, ih = self.rgb.size
        saved_alpha = load_saved_alpha(saved_path, self.rgb.size)
        if saved_alpha is not None:
            self.alpha = saved_alpha
            load_hint = "已加载已保存 alpha"
        else:
            self.alpha = np.full((self.rgb.height, self.rgb.width), 255, dtype=np.uint8)
            load_hint = "默认满 alpha，当前绘制=透明(0)"
        self._cached_view_size = None
        self._cached_rgb_view = None
        self._cached_checker = None
        fmt_hint = "RGBA半透明" if self.save_format == SAVE_FORMAT_RGBA else "灰度alpha(白=不透明)"
        mask_tag = "已有mask" if self.has_mask(path) else "待画mask"
        self.status_var.set(
            f"[{idx + 1}/{len(self.files)}] 待处理 {pending}  |  {mask_tag}  |  {path.name}  |  {iw}x{ih}  |  {load_hint}  |  保存:{fmt_hint}  |  {saved_path}"
        )
        self._is_modified = False
        self.request_redraw()

    def prev_image(self) -> None:
        if self.index > 0:
            self.save_current()
            self.load_index(self.index - 1)

    def next_image(self) -> None:
        if self.index + 1 < len(self.files):
            self.save_current()
            self.load_index(self.index + 1)

    def skip_image(self) -> None:
        """跳过当前图片：不保存，直接进入下一张，并丢弃未保存修改。"""
        if self.index + 1 < len(self.files):
            self._is_modified = False
            self.load_index(self.index + 1)

    def save_and_next(self) -> None:
        if self.rgb is None or self.alpha is None or self.current_path is None:
            return
        self.save_current()
        if self.index + 1 < len(self.files):
            self.load_index(self.index + 1)

    def reset_alpha_opaque(self) -> None:
        if self.rgb is None:
            return
        self.alpha = np.full((self.rgb.height, self.rgb.width), 255, dtype=np.uint8)
        self._is_modified = True
        self.request_redraw()

    def reset_alpha_transparent(self) -> None:
        if self.rgb is None:
            return
        self.alpha = np.zeros((self.rgb.height, self.rgb.width), dtype=np.uint8)
        self._is_modified = True
        self.request_redraw()

    def raw_to_screen(self, x: int, y: int) -> tuple[int, int]:
        return int(self.offset_x + x * self.scale), int(self.offset_y + y * self.scale)

    def screen_to_raw(self, x: int, y: int) -> tuple[int, int]:
        if self.rgb is None:
            return 0, 0
        rx = int((x - self.offset_x) / self.scale)
        ry = int((y - self.offset_y) / self.scale)
        rx = max(0, min(self.rgb.width - 1, rx))
        ry = max(0, min(self.rgb.height - 1, ry))
        return rx, ry

    def request_redraw(self) -> None:
        if self._redraw_after_id is not None:
            return
        self._redraw_after_id = self.root.after_idle(self.redraw)

    def redraw(self) -> None:
        self._redraw_after_id = None
        if self.rgb is None or self.alpha is None:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        iw, ih = self.rgb.size
        self.scale = min(cw / iw, ch / ih)
        vw, vh = max(1, int(iw * self.scale)), max(1, int(ih * self.scale))

        if self._cached_view_size != (vw, vh):
            self._cached_view_size = (vw, vh)
            self._cached_rgb_view = self.rgb.resize((vw, vh), Image.Resampling.BILINEAR)
            self._cached_checker = build_checkerboard(vw, vh)

        alpha_view = Image.fromarray(self.alpha).resize((vw, vh), Image.Resampling.NEAREST)
        preview = compose_preview(self._cached_rgb_view, alpha_view, self._cached_checker)
        self.tk_img = ImageTk.PhotoImage(preview)
        self.offset_x = (cw - vw) // 2
        self.offset_y = (ch - vh) // 2
        if self.image_id is None:
            self.image_id = self.canvas.create_image(self.offset_x, self.offset_y, anchor=tk.NW, image=self.tk_img)
        else:
            self.canvas.coords(self.image_id, self.offset_x, self.offset_y)
            self.canvas.itemconfig(self.image_id, image=self.tk_img)

    def on_press(self, e: tk.Event) -> None:
        if self.rgb is None or self.alpha is None:
            return
        rx, ry = self.screen_to_raw(e.x, e.y)
        tool = self.tool_var.get()
        if tool == "brush":
            self._dragging = True
            self._last_raw = (rx, ry)
            stamp_disc(self.alpha, rx, ry, int(self.brush_r_var.get()), self.paint_value())
            self.request_redraw()
        else:
            self._rect_start = (rx, ry)
            self._dragging = True

    def on_motion(self, e: tk.Event) -> None:
        if self.rgb is None or self.alpha is None or not self._dragging:
            return
        rx, ry = self.screen_to_raw(e.x, e.y)
        tool = self.tool_var.get()
        if tool == "brush":
            if self._last_raw is not None:
                lx, ly = self._last_raw
                stroke_line(self.alpha, lx, ly, rx, ry, int(self.brush_r_var.get()), self.paint_value())
            self._last_raw = (rx, ry)
            self.request_redraw()
        else:
            if self._rect_start is None:
                return
            x0, y0 = self._rect_start
            sx0, sy0 = self.raw_to_screen(x0, y0)
            sx1, sy1 = self.raw_to_screen(rx, ry)
            if self._rect_preview_id is not None:
                self.canvas.delete(self._rect_preview_id)
            self._rect_preview_id = self.canvas.create_rectangle(
                sx0, sy0, sx1, sy1, outline="#00ff88", width=2
            )

    def on_release(self, e: tk.Event) -> None:
        if self.rgb is None or self.alpha is None:
            return
        tool = self.tool_var.get()
        if tool == "rect" and self._dragging and self._rect_start is not None:
            rx, ry = self.screen_to_raw(e.x, e.y)
            x0, y0 = self._rect_start
            fill_rect(self.alpha, x0, y0, rx, ry, self.paint_value())
            if self._rect_preview_id is not None:
                self.canvas.delete(self._rect_preview_id)
                self._rect_preview_id = None
            self._is_modified = True
        elif tool == "brush" and self._dragging:
            self._is_modified = True
        self._dragging = False
        self._last_raw = None
        self._rect_start = None
        self.request_redraw()

    def save_current(self) -> None:
        if self.rgb is None or self.alpha is None or self.current_path is None:
            return
        dest = self.mask_out_path(self.current_path)
        alpha_copy = self.alpha.copy()
        rgb_ref = self.rgb
        self._is_modified = False
        threading.Thread(
            target=self._bg_save,
            args=(dest, rgb_ref, alpha_copy, self.save_format),
            daemon=True,
        ).start()

    def _bg_save(
        self, dest: Path, rgb_img: Image.Image, alpha_array: np.ndarray, save_format: str
    ) -> None:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            out = build_save_image(rgb_img, alpha_array, save_format)
            tmp_dest = dest.with_suffix(".png.tmp")
            out.save(tmp_dest, format="PNG", compress_level=9)
            tmp_dest.replace(dest)
        except Exception as e:
            print(f"保存失败 {dest}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="对白底 RGB 重新绘制 alpha，输出 PNG")
    ap.add_argument("input_path", type=Path, help="输入数据集目录，或单个图片文件")
    ap.add_argument("output_dir", type=Path, help="输出目录（保留相对路径，扩展名 .png）")
    ap.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="递归扫描子目录中的图片（仅当输入为目录时有效）",
    )
    ap.add_argument(
        "--save-format",
        choices=SAVE_FORMAT_CHOICES,
        default=SAVE_FORMAT_RGBA,
        help="rgba: 半透明 RGBA PNG；alpha_bw: 黑白灰度 alpha（白=255 不透明，黑=0 透明）",
    )
    args = ap.parse_args()
    src = args.input_path.expanduser().resolve()
    out = args.output_dir.expanduser().resolve()
    if src.is_file():
        if src.suffix.lower() not in IMAGE_EXTS:
            raise SystemExit(f"不支持的图片扩展名（支持 {', '.join(sorted(IMAGE_EXTS))}）: {src}")
        app = AlphaPaintApp(
            src.parent, out, recursive=False, save_format=args.save_format, files_override=[src]
        )
    elif src.is_dir():
        app = AlphaPaintApp(src, out, args.recursive, save_format=args.save_format)
    else:
        raise SystemExit(f"输入路径不存在: {src}")
    app.root.mainloop()


if __name__ == "__main__":
    main()
