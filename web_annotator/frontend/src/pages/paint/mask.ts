/**
 * 蒙版画布的像素级操作.
 *
 * 蒙版唯一权威为离屏画布的 A 通道（RGB 恒白），所有绘制、填充、模板合并
 * 与持久化均以该画布为数据源；显示层只读取它做合成，不反向写入.
 */

import type { FloatingTemplate } from './types';

/** 画布与 2D 上下文句柄. */
export interface MaskSurface {
  canvas: HTMLCanvasElement;
  ctx: CanvasRenderingContext2D;
}

/** 笔刷圆盘模板缓存（按半径复用，避免每次落笔重建路径）. */
export type StencilCache = Map<number, HTMLCanvasElement>;

/**
 * 创建指定尺寸的离屏蒙版画布.
 *
 * Args:
 *   w (number): 画布宽（像素）.
 *   h (number): 画布高（像素）.
 *
 * Returns:
 *   MaskSurface: 含画布与 2D 上下文；上下文启用 willReadFrequently 以加速回读.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:loadIndex`
 */
export function createMaskSurface(w: number, h: number): MaskSurface {
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, w);
  canvas.height = Math.max(1, h);
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) throw new Error('无法创建蒙版画布上下文');
  return { canvas, ctx };
}

/**
 * 将整幅蒙版清空为全透明.
 *
 * Args:
 *   ctx (CanvasRenderingContext2D): 蒙版上下文.
 *   w (number): 画布宽.
 *   h (number): 画布高.
 *
 * Returns:
 *   void: 无返回值.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:loadIndex`
 *   - `frontend/src/pages/paint/PaintPage.tsx:clearAll`
 */
export function clearMask(ctx: CanvasRenderingContext2D, w: number, h: number): void {
  ctx.clearRect(0, 0, w, h);
}

/**
 * 将整幅蒙版填充为指定 Alpha.
 *
 * Args:
 *   ctx (CanvasRenderingContext2D): 蒙版上下文.
 *   w (number): 画布宽.
 *   h (number): 画布高.
 *   value (number): 目标 Alpha（0-255）；为 0 时等价于清空.
 *
 * Returns:
 *   void: 无返回值.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:loadIndex`
 *   - `frontend/src/pages/paint/PaintPage.tsx:fillAll`
 *   - `frontend/src/pages/paint/PaintPage.tsx:restoreDefault`
 */
export function fillMask(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  value: number,
): void {
  ctx.clearRect(0, 0, w, h);
  if (value <= 0) return;
  ctx.save();
  ctx.globalAlpha = Math.min(255, value) / 255;
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, w, h);
  ctx.restore();
}

/**
 * 取指定半径的白色圆盘模板；按半径缓存复用.
 *
 * Args:
 *   cache (StencilCache): 半径到模板画布的映射.
 *   radius (number): 圆盘半径（画布像素）.
 *
 * Returns:
 *   HTMLCanvasElement: 透明底上的白色实心圆.
 *
 * Callers:
 *   - `frontend/src/pages/paint/mask.ts:stampDisc`
 */
function getStencil(cache: StencilCache, radius: number): HTMLCanvasElement {
  const cached = cache.get(radius);
  if (cached) return cached;
  const size = Math.max(2, Math.ceil(radius * 2 + 2));
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('无法创建笔刷模板画布');
  ctx.fillStyle = '#ffffff';
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, radius, 0, Math.PI * 2);
  ctx.fill();
  cache.set(radius, canvas);
  return canvas;
}

/**
 * 在指定位置落下一个圆盘笔刷，把圆内 Alpha 覆盖为给定值.
 *
 * 先用 destination-out 清除圆内既有 Alpha，再以 source-over 写入目标 Alpha，
 * 从而实现「覆盖」而非「叠加」，与任意 Alpha 值的笔刷语义一致.
 *
 * Args:
 *   ctx (CanvasRenderingContext2D): 蒙版上下文.
 *   cache (StencilCache): 半径模板缓存.
 *   cx (number): 圆心 x（画布坐标）.
 *   cy (number): 圆心 y（画布坐标）.
 *   radius (number): 半径（画布像素）.
 *   value (number): 目标 Alpha（0-255）.
 *
 * Returns:
 *   void: 无返回值.
 *
 * Callers:
 *   - `frontend/src/pages/paint/mask.ts:strokeLine`
 *   - `frontend/src/pages/paint/PaintPage.tsx:paintDisc`
 */
export function stampDisc(
  ctx: CanvasRenderingContext2D,
  cache: StencilCache,
  cx: number,
  cy: number,
  radius: number,
  value: number,
): void {
  const stencil = getStencil(cache, Math.max(0.5, radius));
  const left = cx - stencil.width / 2;
  const top = cy - stencil.height / 2;
  ctx.save();
  ctx.globalCompositeOperation = 'destination-out';
  ctx.drawImage(stencil, left, top);
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = Math.max(0, Math.min(255, value)) / 255;
  ctx.drawImage(stencil, left, top);
  ctx.restore();
}

/**
 * 沿两点间直线连续落笔，填满采样间隙.
 *
 * Args:
 *   ctx (CanvasRenderingContext2D): 蒙版上下文.
 *   cache (StencilCache): 半径模板缓存.
 *   x0 (number): 起点 x.
 *   y0 (number): 起点 y.
 *   x1 (number): 终点 x.
 *   y1 (number): 终点 y.
 *   radius (number): 半径（画布像素）.
 *   value (number): 目标 Alpha（0-255）.
 *
 * Returns:
 *   void: 无返回值.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:paintSegment`
 */
export function strokeLine(
  ctx: CanvasRenderingContext2D,
  cache: StencilCache,
  x0: number,
  y0: number,
  x1: number,
  y1: number,
  radius: number,
  value: number,
): void {
  const distance = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1);
  const spacing = Math.max(1, Math.floor(radius / 2));
  const steps = Math.max(1, Math.ceil(distance / spacing));
  for (let i = 0; i <= steps; i += 1) {
    const t = i / steps;
    stampDisc(
      ctx,
      cache,
      Math.round(x0 + (x1 - x0) * t),
      Math.round(y0 + (y1 - y0) * t),
      radius,
      value,
    );
  }
}

/**
 * 回读整幅蒙版的 Alpha 通道.
 *
 * Args:
 *   ctx (CanvasRenderingContext2D): 蒙版上下文.
 *   w (number): 画布宽.
 *   h (number): 画布高.
 *
 * Returns:
 *   Uint8Array: 长度 w*h 的 Alpha 值（自上而下行序）.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:pushHistory`
 *   - `frontend/src/pages/paint/PaintPage.tsx:saveMask`
 */
export function readAlpha(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
): Uint8Array {
  const image = ctx.getImageData(0, 0, w, h);
  const alpha = new Uint8Array(w * h);
  for (let i = 0; i < alpha.length; i += 1) alpha[i] = image.data[i * 4 + 3];
  return alpha;
}

/**
 * 用给定 Alpha 通道覆盖整幅蒙版.
 *
 * Args:
 *   ctx (CanvasRenderingContext2D): 蒙版上下文.
 *   w (number): 画布宽.
 *   h (number): 画布高.
 *   alpha (Uint8Array): 长度 w*h 的 Alpha 值.
 *
 * Returns:
 *   void: 无返回值.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:undo`
 *   - `frontend/src/pages/paint/PaintPage.tsx:redo`
 */
export function writeAlpha(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  alpha: Uint8Array,
): void {
  const image = ctx.createImageData(w, h);
  for (let i = 0; i < alpha.length; i += 1) {
    const offset = i * 4;
    image.data[offset] = 255;
    image.data[offset + 1] = 255;
    image.data[offset + 2] = 255;
    image.data[offset + 3] = alpha[i];
  }
  ctx.putImageData(image, 0, 0);
}

/**
 * 从指定像素出发做四邻域漫水填充，把连通同值区域改为目标 Alpha.
 *
 * Args:
 *   ctx (CanvasRenderingContext2D): 蒙版上下文.
 *   w (number): 画布宽.
 *   h (number): 画布高.
 *   x (number): 起点 x（画布坐标）.
 *   y (number): 起点 y（画布坐标）.
 *   value (number): 目标 Alpha（0-255）.
 *
 * Returns:
 *   boolean: 是否发生了像素改动；起点已等于目标值时返回 false.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:fillRegion`
 */
export function floodFill(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  x: number,
  y: number,
  value: number,
): boolean {
  const start = y * w + x;
  const image = ctx.getImageData(0, 0, w, h);
  const data = image.data;
  const target = data[start * 4 + 3];
  if (target === value) return false;
  const visited = new Uint8Array(w * h);
  const queue = new Int32Array(w * h);
  let head = 0;
  let tail = 0;
  queue[tail] = start;
  tail += 1;
  visited[start] = 1;
  let changed = false;
  while (head < tail) {
    const index = queue[head];
    head += 1;
    if (data[index * 4 + 3] !== target) continue;
    const offset = index * 4;
    data[offset] = 255;
    data[offset + 1] = 255;
    data[offset + 2] = 255;
    data[offset + 3] = value;
    changed = true;
    const cx = index % w;
    const cy = (index - cx) / w;
    if (cx > 0 && !visited[index - 1] && data[(index - 1) * 4 + 3] === target) {
      visited[index - 1] = 1;
      queue[tail] = index - 1;
      tail += 1;
    }
    if (cx < w - 1 && !visited[index + 1] && data[(index + 1) * 4 + 3] === target) {
      visited[index + 1] = 1;
      queue[tail] = index + 1;
      tail += 1;
    }
    if (cy > 0 && !visited[index - w] && data[(index - w) * 4 + 3] === target) {
      visited[index - w] = 1;
      queue[tail] = index - w;
      tail += 1;
    }
    if (cy < h - 1 && !visited[index + w] && data[(index + w) * 4 + 3] === target) {
      visited[index + w] = 1;
      queue[tail] = index + w;
      tail += 1;
    }
  }
  if (changed) ctx.putImageData(image, 0, 0);
  return changed;
}

/**
 * 回读画布局部矩形的蒙版，构造 RGB 恒白的 RGBA 数据.
 *
 * Args:
 *   ctx (CanvasRenderingContext2D): 蒙版上下文.
 *   x (number): 左边界（含）.
 *   y (number): 上边界（含）.
 *   w (number): 宽.
 *   h (number): 高.
 *
 * Returns:
 *   Uint8ClampedArray: 长度 w*h*4 的图案数据（A 为蒙版值）.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:confirmCapture`
 */
export function readMaskRegion(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
): Uint8ClampedArray {
  const image = ctx.getImageData(x, y, w, h);
  for (let i = 0; i < image.data.length; i += 4) {
    image.data[i] = 255;
    image.data[i + 1] = 255;
    image.data[i + 2] = 255;
  }
  return image.data;
}

/**
 * 把未确认模板按 min/max 合并到当前蒙版的可写交集区域.
 *
 * 模板允许拖出画布，故先求模板矩形与画布的交集，仅写回交集像素；
 * 图案按最近邻缩放采样.
 *
 * Args:
 *   ctx (CanvasRenderingContext2D): 蒙版上下文.
 *   w (number): 画布宽.
 *   h (number): 画布高.
 *   floating (FloatingTemplate): 未确认实例几何与图案.
 *   isMax (boolean): true 为 max 合并，false 为 min 合并.
 *
 * Returns:
 *   void: 无返回值.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:applyFloatingTemplate`
 */
export function applyTemplateRegion(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  floating: FloatingTemplate,
  isMax: boolean,
): void {
  const data = floating.data;
  if (!data || floating.w <= 0 || floating.h <= 0) return;
  const x0 = Math.max(0, Math.floor(floating.rx));
  const y0 = Math.max(0, Math.floor(floating.ry));
  const x1 = Math.min(w, Math.ceil(floating.rx + floating.rw));
  const y1 = Math.min(h, Math.ceil(floating.ry + floating.rh));
  const tw = x1 - x0;
  const th = y1 - y0;
  if (tw <= 0 || th <= 0) return;
  const region = ctx.getImageData(x0, y0, tw, th);
  for (let row = 0; row < th; row += 1) {
    for (let col = 0; col < tw; col += 1) {
      const u = (x0 + col + 0.5 - floating.rx) / floating.rw;
      const v = (y0 + row + 0.5 - floating.ry) / floating.rh;
      const tx = Math.max(0, Math.min(floating.w - 1, Math.floor(u * floating.w)));
      const ty = Math.max(0, Math.min(floating.h - 1, Math.floor(v * floating.h)));
      const source = data[(ty * floating.w + tx) * 4 + 3];
      const offset = (row * tw + col) * 4;
      const current = region.data[offset + 3];
      region.data[offset] = 255;
      region.data[offset + 1] = 255;
      region.data[offset + 2] = 255;
      region.data[offset + 3] = isMax
        ? Math.max(current, source)
        : Math.min(current, source);
    }
  }
  ctx.putImageData(region, x0, y0);
}

/**
 * 把 Alpha 通道编码为 PNG 字节；RGB 恒 0，后端仅读取 A 通道.
 *
 * Args:
 *   alpha (Uint8Array): 长度 w*h 的 Alpha 值.
 *   w (number): 画布宽.
 *   h (number): 画布高.
 *
 * Returns:
 *   Promise<Blob>: PNG 字节.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:saveMask`
 */
export function alphaToPngBlob(
  alpha: Uint8Array,
  w: number,
  h: number,
): Promise<Blob> {
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('无法创建蒙版导出画布');
  const image = ctx.createImageData(w, h);
  for (let i = 0; i < alpha.length; i += 1) {
    const offset = i * 4;
    image.data[offset] = 0;
    image.data[offset + 1] = 0;
    image.data[offset + 2] = 0;
    image.data[offset + 3] = alpha[i];
  }
  ctx.putImageData(image, 0, 0);
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error('PNG 编码失败'));
    }, 'image/png');
  });
}
