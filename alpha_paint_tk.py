#!/usr/bin/env python3
"""
读取数据集目录中的图片，在白底 RGB 上重新绘制 alpha（原图若有 alpha 先合成到白底后丢弃）。
支持画笔与矩形框选填充；保存为带 alpha 的 PNG 到输出目录。

依赖: Pillow, numpy（与 guo582_label_tk 一致）
"""

from __future__ import annotations

import argparse
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

import numpy as np
from PIL import Image, ImageTk

SCRIPT_DIR = Path(__file__).resolve().parent
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


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


def load_saved_alpha(path: Path, size: tuple[int, int]) -> np.ndarray | None:
    if not path.is_file():
        return None
    try:
        saved = Image.open(path).convert("RGBA")
        if saved.size != size:
            return None
        return np.array(saved.getchannel("A"), dtype=np.uint8, copy=True)
    except Exception as e:
        print(f"读取已保存 alpha 失败 {path}: {e}")
        return None


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
    def __init__(self, src_dir: Path, out_dir: Path, recursive: bool) -> None:
        self.src_dir = src_dir.resolve()
        self.out_dir = out_dir.resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.files = list_images(self.src_dir, recursive)
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
        ttk.Button(top, text="保存 (S)", command=self.save_current).pack(side=tk.LEFT, padx=8)
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

        self.canvas = tk.Canvas(self.root, bg="#333333", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.canvas.bind("<Configure>", lambda _e: self.request_redraw())
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<Left>", lambda _e: self.prev_image())
        self.root.bind("<Right>", lambda _e: self.next_image())
        self.root.bind("<x>", lambda _e: self.skip_image())
        self.root.bind("<X>", lambda _e: self.skip_image())
        self.root.bind("<s>", lambda _e: self.save_current())

        if not self.files:
            messagebox.showerror("错误", f"目录中未找到图片: {self.src_dir}")
            self.root.after(100, self.root.destroy)
            return

        self.load_index(0)

    def paint_value(self) -> int:
        return 255 if self.paint_var.get() == "opaque" else 0

    def rel_out_path(self, path: Path) -> Path:
        if path.resolve().is_relative_to(self.src_dir):
            rel = path.resolve().relative_to(self.src_dir)
        else:
            rel = Path(path.name)
        return rel.with_suffix(".png")

    def load_index(self, idx: int) -> None:
        if not self.files:
            return
        idx = max(0, min(len(self.files) - 1, idx))
        self.index = idx
        path = self.files[idx]
        rel = self.rel_out_path(path)
        saved_path = self.out_dir / rel
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
        self.status_var.set(
            f"[{idx + 1}/{len(self.files)}] {path.name}  |  {iw}x{ih}  |  {load_hint}  |  输出: {saved_path}"
        )
        self._is_modified = False
        self.request_redraw()

    def prev_image(self) -> None:
        if self.index > 0:
            self.save_current(silent=True)
            self.load_index(self.index - 1)

    def next_image(self) -> None:
        if self.index + 1 < len(self.files):
            self.save_current(silent=True)
            self.load_index(self.index + 1)

    def skip_image(self) -> None:
        """跳过当前图片：不保存，直接进入下一张，并丢弃未保存修改。"""
        if self.index + 1 < len(self.files):
            self._is_modified = False
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

    def save_current(self, silent: bool = False) -> None:
        if self.rgb is None or self.alpha is None or self.current_path is None:
            return
        rel = self.rel_out_path(self.current_path)
        dest = self.out_dir / rel
        
        # 拷贝当前状态，防止翻页后被主线程修改
        alpha_copy = self.alpha.copy()
        rgb_ref = self.rgb
        
        self._is_modified = False
        if not silent:
            self.status_var.set(f"正在后台保存: {dest}...")
            
        threading.Thread(
            target=self._bg_save,
            args=(dest, rgb_ref, alpha_copy, silent),
            daemon=True
        ).start()

    def _bg_save(self, dest: Path, rgb_img: Image.Image, alpha_array: np.ndarray, silent: bool) -> None:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # 强制深拷贝 RGB，防止主线程销毁或复用导致 size mismatch
            rgb_copy = rgb_img.copy()
            r, g, b = rgb_copy.split()
            a_img = Image.fromarray(alpha_array)
            out = Image.merge("RGBA", (r, g, b, a_img))
            
            # 先保存到临时文件，再原子替换，防止读取到写了一半的残缺图片
            tmp_dest = dest.with_suffix('.png.tmp')
            out.save(tmp_dest, format="PNG", compress_level=9)
            tmp_dest.replace(dest)
            
            if not silent:
                self.root.after(0, lambda: self.status_var.set(f"已保存: {dest}"))
        except Exception as e:
            print(f"后台保存失败 {dest}: {e}")
            if not silent:
                self.root.after(0, lambda: self.status_var.set(f"保存失败: {dest}"))


def main() -> None:
    ap = argparse.ArgumentParser(description="对白底 RGB 重新绘制 alpha，输出 PNG")
    ap.add_argument("input_dir", type=Path, help="输入数据集目录")
    ap.add_argument("output_dir", type=Path, help="输出目录（保留相对路径，扩展名 .png）")
    ap.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="递归扫描子目录中的图片",
    )
    args = ap.parse_args()
    src = args.input_dir.expanduser().resolve()
    if not src.is_dir():
        raise SystemExit(f"输入目录不存在: {src}")
    app = AlphaPaintApp(src, args.output_dir.expanduser().resolve(), args.recursive)
    app.root.mainloop()


if __name__ == "__main__":
    main()
