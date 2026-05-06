#!/usr/bin/env python3
"""
单图多区域裁剪 GUI：打开一张图，拖拽添加多个矩形选区，支持拖动平移与边角缩放，一次性导出多张图。

依赖: Pillow
"""

from __future__ import annotations

import argparse
import math
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

# 画布上命中边角/边线缩放的容差（像素）
HANDLE_PX = 10
MIN_SIDE = 3


def clamp_box(x0: int, y0: int, x1: int, y1: int, w: int, h: int) -> tuple[int, int, int, int]:
    xa, xb = sorted((x0, x1))
    ya, yb = sorted((y0, y1))
    xa = max(0, min(w - 1, xa))
    xb = max(0, min(w - 1, xb))
    ya = max(0, min(h - 1, ya))
    yb = max(0, min(h - 1, yb))
    return xa, ya, xb, yb


def enforce_min_size(xa: int, ya: int, xb: int, yb: int, handle: str) -> tuple[int, int, int, int]:
    """保证宽高至少 MIN_SIDE（像素），必要时推开对边。"""
    if xb - xa + 1 < MIN_SIDE:
        if handle in ("e", "ne", "se"):
            xb = xa + MIN_SIDE - 1
        else:
            xa = xb - MIN_SIDE + 1
    if yb - ya + 1 < MIN_SIDE:
        if handle in ("s", "se", "sw"):
            yb = ya + MIN_SIDE - 1
        else:
            ya = yb - MIN_SIDE + 1
    return xa, ya, xb, yb


class MultiCropApp:
    def __init__(self, initial_path: Path | None) -> None:
        self.root = tk.Tk()
        self.root.title("多区域裁剪")
        self.root.geometry("1280x820")

        self.image_path: Path | None = None
        self.orig: Image.Image | None = None
        self.crops: list[tuple[int, int, int, int]] = []
        self.selected_idx: int | None = None

        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.view_w = 1
        self.view_h = 1
        self.tk_img: ImageTk.PhotoImage | None = None
        self._img_item: int | None = None
        self._scroll_mode = False

        # 手势：新建框 | 移动 | 缩放
        self._g_new_start: tuple[int, int] | None = None
        self._g_new_end: tuple[int, int] | None = None
        self._g_move: tuple[int, tuple[int, int], tuple[int, int, int, int]] | None = None
        self._g_resize: tuple[int, str, tuple[int, int], tuple[int, int, int, int]] | None = None

        self.out_dir_var = tk.StringVar(master=self.root, value="")
        self.status_var = tk.StringVar(master=self.root, value="请打开一张图片")

        top = ttk.Frame(self.root)
        top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        ttk.Button(top, text="打开图片", command=self.open_image).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="导出全部", command=self.export_all).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="删除选中", command=self.delete_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="清空选区", command=self.clear_crops).pack(side=tk.LEFT, padx=2)
        ttk.Label(top, text="输出目录:").pack(side=tk.LEFT, padx=(16, 2))
        ttk.Entry(top, textvariable=self.out_dir_var, width=42).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="浏览…", command=self.browse_out_dir).pack(side=tk.LEFT, padx=2)

        mid = ttk.Frame(self.root)
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        canvas_host = ttk.Frame(mid)
        canvas_host.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(canvas_host, bg="#2b2b2b", highlightthickness=0)
        self._vscroll = ttk.Scrollbar(canvas_host, orient=tk.VERTICAL, command=self.canvas.yview)
        self._hscroll = ttk.Scrollbar(canvas_host, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self._vscroll.set, xscrollcommand=self._hscroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self._vscroll.grid(row=0, column=1, sticky="ns")
        self._hscroll.grid(row=1, column=0, sticky="ew")
        canvas_host.rowconfigure(0, weight=1)
        canvas_host.columnconfigure(0, weight=1)

        right = ttk.Frame(mid, width=280)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)
        ttk.Label(
            right,
            text="选区：空白处拖拽新建；框内拖动平移；边角/边线拖动缩放",
            font=("", 9, "bold"),
            wraplength=260,
        ).pack(anchor="w", padx=8, pady=(8, 4))
        self.listbox = tk.Listbox(right, height=24, selectmode=tk.SINGLE)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)

        ttk.Label(self.root, textvariable=self.status_var).pack(side=tk.BOTTOM, anchor=tk.W, padx=8, pady=4)

        self.canvas.bind("<Configure>", lambda _e: self.full_redraw())
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_mousewheel_h)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)
        self.root.bind("<Delete>", lambda _e: self.delete_selected())
        self.root.bind("<BackSpace>", lambda _e: self.delete_selected())

        if initial_path is not None and initial_path.is_file():
            self._load_path(initial_path)

    def browse_out_dir(self) -> None:
        d = filedialog.askdirectory()
        if d:
            self.out_dir_var.set(d)

    def open_image(self) -> None:
        p = filedialog.askopenfilename(
            filetypes=[
                ("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                ("全部", "*.*"),
            ]
        )
        if p:
            self._load_path(Path(p))

    def _load_path(self, path: Path) -> None:
        self.image_path = path.resolve()
        self.orig = Image.open(self.image_path).convert("RGBA")
        self.crops.clear()
        self.selected_idx = None
        self._clear_gestures()
        self._refresh_listbox()
        self.status_var.set(f"已打开: {self.image_path.name}  {self.orig.width}x{self.orig.height}")
        if not self.out_dir_var.get().strip():
            self.out_dir_var.set(str(self.image_path.parent / f"{self.image_path.stem}_crops"))
        self.full_redraw()

    def _clear_gestures(self) -> None:
        self._g_new_start = None
        self._g_new_end = None
        self._g_move = None
        self._g_resize = None

    def _canvas_xy(self, e: tk.Event) -> tuple[float, float]:
        return self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)

    def raw_to_screen(self, x: int, y: int) -> tuple[int, int]:
        sx = self.offset_x + x * self.scale
        sy = self.offset_y + y * self.scale
        return int(sx), int(sy)

    def screen_to_raw(self, x: float, y: float) -> tuple[int, int]:
        if self.orig is None:
            return 0, 0
        w, h = self.orig.size
        rx = int(round((x - self.offset_x) / self.scale))
        ry = int(round((y - self.offset_y) / self.scale))
        rx = max(0, min(w - 1, rx))
        ry = max(0, min(h - 1, ry))
        return rx, ry

    def _hit_resize_or_move(self, cx: float, cy: float) -> tuple | None:
        """命中则返回 ('resize', idx, handle) 或 ('move', idx)；否则 None 表示新建框。"""
        t = HANDLE_PX

        def dist(ax: float, ay: float) -> float:
            return math.hypot(cx - ax, cy - ay)

        for i in range(len(self.crops) - 1, -1, -1):
            x0, y0, x1, y1 = self.crops[i]
            sx0, sy0 = self.raw_to_screen(x0, y0)
            sx1, sy1 = self.raw_to_screen(x1, y1)
            l, r = min(sx0, sx1), max(sx0, sx1)
            top, bot = min(sy0, sy1), max(sy0, sy1)

            if dist(l, top) <= t:
                return ("resize", i, "nw")
            if dist(r, top) <= t:
                return ("resize", i, "ne")
            if dist(l, bot) <= t:
                return ("resize", i, "sw")
            if dist(r, bot) <= t:
                return ("resize", i, "se")

            if abs(cy - top) <= t and l <= cx <= r:
                return ("resize", i, "n")
            if abs(cy - bot) <= t and l <= cx <= r:
                return ("resize", i, "s")
            if abs(cx - l) <= t and top <= cy <= bot:
                return ("resize", i, "w")
            if abs(cx - r) <= t and top <= cy <= bot:
                return ("resize", i, "e")

            if l <= cx <= r and top <= cy <= bot:
                return ("move", i)
        return None

    def _on_mousewheel(self, e: tk.Event) -> None:
        if self.orig is None or not self._scroll_mode:
            return
        if getattr(e, "delta", 0):
            delta = -1 if e.delta > 0 else 1
        elif getattr(e, "num", None) == 4:
            delta = -1
        else:
            delta = 1
        self.canvas.yview_scroll(delta, "units")

    def _on_mousewheel_h(self, e: tk.Event) -> None:
        if self.orig is None or not self._scroll_mode:
            return
        if getattr(e, "delta", 0):
            delta = -1 if e.delta > 0 else 1
        else:
            delta = 0
        if delta:
            self.canvas.xview_scroll(delta, "units")

    def full_redraw(self) -> None:
        if self.orig is None:
            self.canvas.delete("all")
            self._img_item = None
            self.canvas.configure(scrollregion=(0, 0, 0, 0))
            return

        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        iw, ih = self.orig.size
        ar_img = iw / ih
        ar_vp = cw / ch
        eps = 1e-9
        if ar_img > ar_vp + eps:
            self.scale = ch / ih
            hint = "极宽图：已按画布高度适配；横向拖滚动条，Shift+滚轮可横向滚动"
        elif ar_img < ar_vp - eps:
            self.scale = cw / iw
            hint = "极高图：已按画布宽度适配，可纵向滚动"
        else:
            self.scale = min(cw / iw, ch / ih)
            hint = ""

        self.view_w = max(1, int(iw * self.scale))
        self.view_h = max(1, int(ih * self.scale))
        view = self.orig.resize((self.view_w, self.view_h), Image.Resampling.BILINEAR)
        self.tk_img = ImageTk.PhotoImage(view.convert("RGB"))

        self._scroll_mode = self.view_w > cw or self.view_h > ch
        self.canvas.delete("all")
        if self._scroll_mode:
            self.offset_x = 0.0
            self.offset_y = 0.0
            self._img_item = self.canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_img)
            self.canvas.configure(scrollregion=(0, 0, self.view_w, self.view_h))
            self._vscroll.grid()
            self._hscroll.grid()
        else:
            self.offset_x = float((cw - self.view_w) // 2)
            self.offset_y = float((ch - self.view_h) // 2)
            self.canvas.configure(scrollregion=(0, 0, cw, ch))
            self._img_item = self.canvas.create_image(self.offset_x, self.offset_y, anchor=tk.NW, image=self.tk_img)
            self._vscroll.grid_remove()
            self._hscroll.grid_remove()

        self._draw_crop_layer()

        base = f"已打开: {self.image_path.name}  {iw}x{ih}  ·显示 {self.view_w}x{self.view_h}"
        self.status_var.set(f"{base}  ·  {hint}" if hint else base)

    def _draw_crop_layer(self) -> None:
        self.canvas.delete("croplayer")
        if self.orig is None:
            return

        for i, box in enumerate(self.crops):
            x0, y0, x1, y1 = box
            sx0, sy0 = self.raw_to_screen(x0, y0)
            sx1, sy1 = self.raw_to_screen(x1, y1)
            color = "#00d4ff" if self.selected_idx == i else "#00ff88"
            self.canvas.create_rectangle(sx0, sy0, sx1, sy1, outline=color, width=2, tags="croplayer")

        if self._g_new_start is not None and self._g_new_end is not None:
            x0, y0 = self._g_new_start
            x1, y1 = self._g_new_end
            sx0, sy0 = self.raw_to_screen(x0, y0)
            sx1, sy1 = self.raw_to_screen(x1, y1)
            self.canvas.create_rectangle(
                sx0, sy0, sx1, sy1, outline="#ffff00", width=2, dash=(4, 4), tags="croplayer"
            )

        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.crops):
            x0, y0, x1, y1 = self.crops[self.selected_idx]
            sx0, sy0 = self.raw_to_screen(x0, y0)
            sx1, sy1 = self.raw_to_screen(x1, y1)
            l, r = min(sx0, sx1), max(sx0, sx1)
            top, bot = min(sy0, sy1), max(sy0, sy1)
            hs = 4
            for cx, cy in ((l, top), (r, top), (l, bot), (r, bot)):
                self.canvas.create_rectangle(
                    cx - hs, cy - hs, cx + hs, cy + hs, outline="#ffffff", width=1, fill="#333333", tags="croplayer"
                )

    def on_press(self, e: tk.Event) -> None:
        if self.orig is None:
            return
        cx, cy = self._canvas_xy(e)
        pick = self._hit_resize_or_move(cx, cy)
        raw = self.screen_to_raw(cx, cy)

        if pick is None:
            self._clear_gestures()
            self._g_new_start = raw
            self._g_new_end = raw
            self._draw_crop_layer()
            return

        if pick[0] == "move":
            _, idx = pick
            self.selected_idx = idx
            self._refresh_listbox_select()
            self._g_move = (idx, raw, tuple(self.crops[idx]))
            self._g_resize = None
            self._g_new_start = None
            self._g_new_end = None
            self._draw_crop_layer()
            return

        if pick[0] == "resize":
            _, idx, handle = pick
            self.selected_idx = idx
            self._refresh_listbox_select()
            self._g_resize = (idx, handle, raw, tuple(self.crops[idx]))
            self._g_move = None
            self._g_new_start = None
            self._g_new_end = None
            self._draw_crop_layer()

    def _refresh_listbox_select(self) -> None:
        if self.selected_idx is None:
            return
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.selected_idx)
        self.listbox.see(self.selected_idx)

    def on_motion(self, e: tk.Event) -> None:
        if self.orig is None:
            return
        cx, cy = self._canvas_xy(e)
        raw = self.screen_to_raw(cx, cy)
        w, h = self.orig.size

        if self._g_new_start is not None:
            self._g_new_end = raw
            self._draw_crop_layer()
            return

        if self._g_move is not None:
            idx, (srx, sry), box0 = self._g_move
            xa, ya, xb, yb = box0
            dx = raw[0] - srx
            dy = raw[1] - sry
            na, nb, nc, nd = xa + dx, ya + dy, xb + dx, yb + dy
            if na < 0:
                sh = -na
                na += sh
                nc += sh
            if nc > w - 1:
                sh = nc - (w - 1)
                na -= sh
                nc -= sh
            if nb < 0:
                sh = -nb
                nb += sh
                nd += sh
            if nd > h - 1:
                sh = nd - (h - 1)
                nb -= sh
                nd -= sh
            self.crops[idx] = clamp_box(na, nb, nc, nd, w, h)
            self._draw_crop_layer()
            return

        if self._g_resize is not None:
            idx, handle, _start, box0 = self._g_resize
            xa, ya, xb, yb = box0
            rx, ry = raw[0], raw[1]
            if "n" in handle:
                ya = ry
            if "s" in handle:
                yb = ry
            if "w" in handle:
                xa = rx
            if "e" in handle:
                xb = rx
            xa, ya, xb, yb = clamp_box(xa, ya, xb, yb, w, h)
            xa, ya, xb, yb = enforce_min_size(xa, ya, xb, yb, handle)
            xa, ya, xb, yb = clamp_box(xa, ya, xb, yb, w, h)
            self.crops[idx] = (xa, ya, xb, yb)
            self._draw_crop_layer()

    def on_release(self, e: tk.Event) -> None:
        if self.orig is None:
            return
        w, h = self.orig.size

        if self._g_new_start is not None and self._g_new_end is not None:
            x0, y0 = self._g_new_start
            x1, y1 = self._g_new_end
            self._g_new_start = None
            self._g_new_end = None
            box = clamp_box(x0, y0, x1, y1, w, h)
            xa, ya, xb, yb = box
            if (xb - xa + 1) < MIN_SIDE or (yb - ya + 1) < MIN_SIDE:
                self.status_var.set("选区太小，已忽略")
                self._draw_crop_layer()
                return
            self.crops.append(box)
            self.selected_idx = len(self.crops) - 1
            self._refresh_listbox()
            self._refresh_listbox_select()
            self.status_var.set(f"已添加选区 #{len(self.crops)}  ({xb - xa + 1}x{yb - ya + 1})")
            self._draw_crop_layer()
            return

        if self._g_move is not None:
            self._g_move = None
            self._refresh_listbox()
            self.status_var.set("已移动选区")
            self._draw_crop_layer()
            return

        if self._g_resize is not None:
            self._g_resize = None
            self._refresh_listbox()
            self.status_var.set("已缩放选区")
            self._draw_crop_layer()

    def _refresh_listbox(self) -> None:
        self.listbox.delete(0, tk.END)
        for i, (xa, ya, xb, yb) in enumerate(self.crops):
            self.listbox.insert(tk.END, f"#{i + 1}  {xa},{ya} → {xb},{yb}  ({xb - xa + 1}x{yb - ya + 1})")

    def _on_list_select(self, _e: tk.Event | None = None) -> None:
        sel = self.listbox.curselection()
        if not sel:
            self.selected_idx = None
        else:
            self.selected_idx = int(sel[0])
        self._draw_crop_layer()

    def delete_selected(self) -> None:
        if self.selected_idx is None or not (0 <= self.selected_idx < len(self.crops)):
            return
        self._clear_gestures()
        del self.crops[self.selected_idx]
        self.selected_idx = None
        self._refresh_listbox()
        self.full_redraw()
        self.status_var.set("已删除选中选区")

    def clear_crops(self) -> None:
        self._clear_gestures()
        self.crops.clear()
        self.selected_idx = None
        self._refresh_listbox()
        self.full_redraw()
        self.status_var.set("已清空全部选区")

    def export_all(self) -> None:
        if self.orig is None:
            messagebox.showwarning("提示", "请先打开图片")
            return
        if not self.crops:
            messagebox.showwarning("提示", "请先框选至少一个区域")
            return
        out = self.out_dir_var.get().strip()
        if not out:
            messagebox.showwarning("提示", "请设置输出目录")
            return
        out_path = Path(out).expanduser().resolve()
        out_path.mkdir(parents=True, exist_ok=True)
        stem = self.image_path.stem if self.image_path else "crop"
        ext = ".png"
        for i, box in enumerate(self.crops):
            xa, ya, xb, yb = box
            patch = self.orig.crop((xa, ya, xb + 1, yb + 1))
            dest = out_path / f"{stem}_{i + 1:03d}{ext}"
            patch.save(dest)
        self.status_var.set(f"已导出 {len(self.crops)} 张 → {out_path}")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    ap = argparse.ArgumentParser(description="单图多区域裁剪 GUI")
    ap.add_argument("image", nargs="?", type=Path, default=None, help="可选：启动时打开的图片路径")
    args = ap.parse_args()
    app = MultiCropApp(args.image)
    app.run()


if __name__ == "__main__":
    main()
