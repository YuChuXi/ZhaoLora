import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { api, apiPath, request } from '@/lib/api';
import { useApp } from '@/app/AppContext';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/controls';
import { Toolbar, ToolbarDivider } from '@/components/ui/toolbar';
import { useStatus } from '@/components/ui/status';
import { cn } from '@/lib/utils';

interface MatchConfig {
  side_min: number;
  side_max: number;
  side_step: number;
  zooms: number[];
  max_output_pixels: number;
}

interface CropImageItem {
  path: string;
  stem: string;
  width: number;
  height: number;
  thumbnail_hash: string;
}

interface CropPartRecord {
  crop?: number[] | null;
  size?: number[] | null;
}

interface CropImageState {
  stem: string;
  parts: Record<string, CropPartRecord>;
}

interface CropPageConfig {
  parts: string[];
}

interface CropDefault {
  target_w: number;
  target_h: number;
  zoom: number;
}

interface StoredAutoPart {
  name?: string;
  enabled?: boolean;
}

interface StoredCropSettings {
  part_defaults?: Record<string, CropDefault>;
  auto_parts?: Array<string | StoredAutoPart>;
  denied_parts?: string[];
}

interface Candidate {
  crop_w: number;
  crop_h: number;
  target_w: number;
  target_h: number;
  zoom: number;
}

interface CropBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  target_w: number;
  target_h: number;
  zoom: number;
  name: string;
  saved: boolean;
}

type DragMode =
  | 'move'
  | 'resize_tl'
  | 'resize_tr'
  | 'resize_bl'
  | 'resize_br'
  | 'resize_left'
  | 'resize_right'
  | 'resize_top'
  | 'resize_bottom';

type HitMode = DragMode | 'new';

interface HitResult {
  idx: number;
  mode: HitMode;
}

interface DragState {
  mode: HitMode;
  idx: number;
  start: { x: number; y: number };
  base: CropBox | null;
}

interface BoxRect {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

interface CanvasView {
  scale: number;
  width: number;
  height: number;
  left: number;
  top: number;
}

const DEFAULT_MATCH: MatchConfig = {
  side_min: 512,
  side_max: 1536 * 2,
  side_step: 128,
  zooms: [1.0, 1.5, 2.0, 2.5, 3.0, 4.0],
  max_output_pixels: 1536 * 1536,
};

const HANDLE_TOL = 10;
const NEAR_RADIUS = 2;

/**
 * 把任意边长吸附到配置的等距网格上。
 *
 * Args:
 *   cfg (MatchConfig): 边长网格配置。
 *   x (number): 待吸附边长。
 *
 * Returns:
 *   number: 夹在 ``[side_min, side_max]`` 内的网格边长。
 *
 * Callers:
 *   - `fitPixelLimit`
 *   - `chooseBest`
 */
function snapSide(cfg: MatchConfig, x: number): number {
  const t = Math.round((x - cfg.side_min) / cfg.side_step);
  return Math.max(cfg.side_min, Math.min(cfg.side_max, cfg.side_min + t * cfg.side_step));
}

/**
 * 求指定缩放比下能放进给定原图尺寸的最大网格边长。
 *
 * Args:
 *   cfg (MatchConfig): 边长网格配置。
 *   maxCrop (number): 可用的最大原图边长。
 *   z (number): 输出缩放比。
 *
 * Returns:
 *   number | null: 满足 ``round(side * z) <= maxCrop`` 的最大网格边长；无解时为 null。
 *
 * Callers:
 *   - `chooseBest`
 */
function maxSideFitting(cfg: MatchConfig, maxCrop: number, z: number): number | null {
  if (maxCrop < 1 || z <= 0) return null;
  let s = Math.min(cfg.side_max, Math.floor(maxCrop / z));
  s = cfg.side_min + Math.floor((s - cfg.side_min) / cfg.side_step) * cfg.side_step;
  while (s >= cfg.side_min) {
    if (Math.round(s * z) <= maxCrop) return s;
    s -= cfg.side_step;
  }
  return null;
}

/**
 * 取目标边长附近的网格候选，并始终包含上下界。
 *
 * Args:
 *   cfg (MatchConfig): 边长网格配置。
 *   ideal (number): 期望边长。
 *   maxS (number): 当前允许的最大网格边长。
 *
 * Returns:
 *   number[]: 升序去重后的候选边长。
 *
 * Callers:
 *   - `chooseBest`
 */
function sideNeighbors(cfg: MatchConfig, ideal: number, maxS: number): number[] {
  if (maxS < cfg.side_min) return [];
  const upper = cfg.side_min + Math.floor((Math.min(cfg.side_max, maxS) - cfg.side_min) / cfg.side_step) * cfg.side_step;
  if (upper < cfg.side_min) return [];
  const base = Math.max(cfg.side_min, Math.min(upper, snapSide(cfg, ideal)));
  const values = new Set<number>([cfg.side_min, upper, base]);
  for (let d = -NEAR_RADIUS; d <= NEAR_RADIUS; d++) {
    if (d === 0) continue;
    const s = base + d * cfg.side_step;
    if (s >= cfg.side_min && s <= upper) values.add(s);
  }
  return Array.from(values).sort((a, b) => a - b);
}

/**
 * 按输出像素总量上限收缩目标尺寸。
 *
 * Args:
 *   cfg (MatchConfig): 边长网格配置。
 *   tw (number): 期望输出宽。
 *   th (number): 期望输出高。
 *
 * Returns:
 *   [number, number] | null: 收缩后的目标尺寸；无法满足下限或像素上限时为 null。
 *
 * Callers:
 *   - `chooseBest`
 */
function fitPixelLimit(cfg: MatchConfig, tw: number, th: number): [number, number] | null {
  if (tw * th <= cfg.max_output_pixels) return [tw, th];
  const scale = Math.sqrt(cfg.max_output_pixels / (tw * th));
  let tw2 = snapSide(cfg, tw * scale);
  let th2 = snapSide(cfg, th * scale);
  while (tw2 >= cfg.side_min && th2 >= cfg.side_min && tw2 * th2 > cfg.max_output_pixels) {
    if (tw2 >= th2) tw2 -= cfg.side_step;
    else th2 -= cfg.side_step;
  }
  if (tw2 < cfg.side_min || th2 < cfg.side_min || tw2 * th2 > cfg.max_output_pixels) return null;
  return [tw2, th2];
}

/**
 * 在缩放桶与网格边长的组合中选出最贴近期望尺寸的候选。
 *
 * Args:
 *   cfg (MatchConfig): 边长网格配置。
 *   maxW (number): 可用原图最大宽。
 *   maxH (number): 可用原图最大高。
 *   desiredW (number): 期望原图宽。
 *   desiredH (number): 期望原图高。
 *   weightW (number): 宽度误差权重，默认 1。
 *   weightH (number): 高度误差权重，默认 1。
 *
 * Returns:
 *   Candidate | null: 最优候选；无可行组合时为 null。
 *
 * Callers:
 *   - `CropPage`
 */
function chooseBest(
  cfg: MatchConfig,
  maxW: number,
  maxH: number,
  desiredW: number,
  desiredH: number,
  weightW = 1,
  weightH = 1,
): Candidate | null {
  const bestByCrop: Record<string, { s0: number; s1: number; s2: number; cand: Candidate }> = {};
  for (const z of cfg.zooms) {
    const maxTw = maxSideFitting(cfg, maxW, z);
    const maxTh = maxSideFitting(cfg, maxH, z);
    if (maxTw == null || maxTh == null) continue;
    const tws = sideNeighbors(cfg, desiredW / z, maxTw);
    const ths = sideNeighbors(cfg, desiredH / z, maxTh);
    for (const tw0 of tws) {
      for (const th0 of ths) {
        const fitted = fitPixelLimit(cfg, tw0, th0);
        if (!fitted) continue;
        const [tw, th] = fitted;
        if (tw > maxTw || th > maxTh) continue;
        const cw = Math.round(tw * z);
        const ch = Math.round(th * z);
        if (cw < 1 || ch < 1 || cw > maxW || ch > maxH) continue;
        const score0 = weightW * Math.abs(cw - desiredW) + weightH * Math.abs(ch - desiredH);
        const score1 = -(tw * th);
        const score2 = -z;
        const key = `${cw}x${ch}`;
        const prev = bestByCrop[key];
        if (
          !prev ||
          score0 < prev.s0 ||
          (score0 === prev.s0 && (score1 < prev.s1 || (score1 === prev.s1 && score2 < prev.s2)))
        ) {
          bestByCrop[key] = {
            s0: score0,
            s1: score1,
            s2: score2,
            cand: { crop_w: cw, crop_h: ch, target_w: tw, target_h: th, zoom: z },
          };
        }
      }
    }
  }
  let best: Candidate | null = null;
  let bs0 = 0;
  let bs1 = 0;
  let bs2 = 0;
  for (const key of Object.keys(bestByCrop)) {
    const entry = bestByCrop[key];
    if (
      !best ||
      entry.s0 < bs0 ||
      (entry.s0 === bs0 && (entry.s1 < bs1 || (entry.s1 === bs1 && entry.s2 < bs2)))
    ) {
      best = entry.cand;
      bs0 = entry.s0;
      bs1 = entry.s1;
      bs2 = entry.s2;
    }
  }
  return best;
}

/**
 * 校验并还原部位预设中的固定输出规格。
 *
 * Args:
 *   cfg (MatchConfig): 边长网格配置。
 *   maxW (number): 原图宽。
 *   maxH (number): 原图高。
 *   tw (number): 预设目标宽。
 *   th (number): 预设目标高。
 *   zoom (number): 预设缩放比。
 *
 * Returns:
 *   Candidate | null: 合法时返回固定候选，否则为 null。
 *
 * Callers:
 *   - `CropPage`
 */
function findByTargetZoom(
  cfg: MatchConfig,
  maxW: number,
  maxH: number,
  tw: number,
  th: number,
  zoom: number,
): Candidate | null {
  if (tw < cfg.side_min || th < cfg.side_min || tw > cfg.side_max || th > cfg.side_max) return null;
  if ((tw - cfg.side_min) % cfg.side_step !== 0 || (th - cfg.side_min) % cfg.side_step !== 0) return null;
  if (!cfg.zooms.some((z) => Math.abs(z - zoom) < 1e-4)) return null;
  if (tw * th > cfg.max_output_pixels) return null;
  const cw = Math.round(tw * zoom);
  const ch = Math.round(th * zoom);
  if (cw > maxW || ch > maxH) return null;
  return { crop_w: cw, crop_h: ch, target_w: tw, target_h: th, zoom };
}

/**
 * 构造选区对象；拖动或缩放重建时默认标记为未保存。
 *
 * Args:
 *   x1 (number): 原图左上横坐标。
 *   y1 (number): 原图左上纵坐标。
 *   x2 (number): 原图右下横坐标。
 *   y2 (number): 原图右下纵坐标。
 *   cand (Candidate | null): 吸附候选，null 时按框尺寸初始化输出规格。
 *   name (string): 部位名。
 *
 * Returns:
 *   CropBox: 新建的选区对象。
 *
 * Callers:
 *   - `CropPage`
 *   - `applySnap`
 */
function makeCrop(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  cand: Candidate | null,
  name: string,
): CropBox {
  return {
    x1,
    y1,
    x2,
    y2,
    target_w: cand ? cand.target_w : Math.min(x2 - x1, 1536),
    target_h: cand ? cand.target_h : Math.min(y2 - y1, 1536),
    zoom: cand ? cand.zoom : 1.0,
    name: name || '',
    saved: false,
  };
}

/**
 * 解析侧栏「宽x高@缩放」文本。
 *
 * Args:
 *   text (string): 用户输入的规格文本。
 *
 * Returns:
 *   CropDefault | null: 合法规格；格式错误时为 null。
 *
 * Callers:
 *   - `CropPage`
 */
function parseDefaultSpec(text: string): CropDefault | null {
  const match = String(text || '').trim().match(/^(\d+)\s*[x×]\s*(\d+)\s*@\s*([\d.]+)\s*x?$/i);
  if (!match) return null;
  const tw = Number.parseInt(match[1], 10);
  const th = Number.parseInt(match[2], 10);
  const zoom = Number.parseFloat(match[3]);
  if (!(tw > 0 && th > 0 && zoom > 0)) return null;
  return { target_w: tw, target_h: th, zoom };
}

/**
 * 把部位预设格式化为输入框文本。
 *
 * Args:
 *   value (CropDefault | null): 部位预设。
 *
 * Returns:
 *   string: ``宽x高@缩放`` 文本；无预设时为空串。
 *
 * Callers:
 *   - `CropPage`
 */
function formatDefaultSpec(value: CropDefault | null): string {
  if (!value) return '';
  return `${value.target_w}x${value.target_h}@${Number(value.zoom).toFixed(1)}`;
}

/**
 * 把设置中的任意值收敛为裁剪设置对象。
 *
 * Args:
 *   value (unknown): ``settings.crop`` 的原始值。
 *
 * Returns:
 *   StoredCropSettings: 可安全读取的裁剪设置对象。
 *
 * Callers:
 *   - `CropPage`
 */
function normalizeCropSettings(value: unknown): StoredCropSettings {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return value as StoredCropSettings;
}

/**
 * 读取设置中的部位预设映射。
 *
 * Args:
 *   value (unknown): ``settings.crop.part_defaults`` 的原始值。
 *
 * Returns:
 *   Record<string, CropDefault>: 部位名到预设的映射。
 *
 * Callers:
 *   - `CropPage`
 */
function readPartDefaults(value: unknown): Record<string, CropDefault> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return value as Record<string, CropDefault>;
}

/**
 * 读取设置中的永久删除部位清单。
 *
 * Args:
 *   value (unknown): ``settings.crop.denied_parts`` 的原始值。
 *
 * Returns:
 *   string[]: 仅保留字符串项的永久删除部位名。
 *
 * Callers:
 *   - `CropPage`
 */
function readDeniedParts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((name): name is string => typeof name === 'string') : [];
}

/**
 * 取路径的最后一段作为文件名。
 *
 * Args:
 *   path (string): 数据集根相对路径。
 *
 * Returns:
 *   string: 文件名。
 *
 * Callers:
 *   - `CropPage`
 */
function basename(path: string): string {
  return path.split(/[/\\]/).pop() || path;
}

/**
 * 把选区坐标夹进原图范围并保证宽高至少为 1 像素。
 *
 * Args:
 *   w (number): 原图宽。
 *   h (number): 原图高。
 *   x1 (number): 左上横坐标。
 *   y1 (number): 左上纵坐标。
 *   x2 (number): 右下横坐标。
 *   y2 (number): 右下纵坐标。
 *
 * Returns:
 *   BoxRect: 夹取后的矩形。
 *
 * Callers:
 *   - `applySnap`
 *   - `CropPage`
 */
function clampBox(w: number, h: number, x1: number, y1: number, x2: number, y2: number): BoxRect {
  const cx1 = Math.max(0, Math.min(w - 1, x1));
  const cy1 = Math.max(0, Math.min(h - 1, y1));
  const cx2 = Math.max(cx1 + 1, Math.min(w, x2));
  const cy2 = Math.max(cy1 + 1, Math.min(h, y2));
  return { x1: cx1, y1: cy1, x2: cx2, y2: cy2 };
}

/**
 * 将界面像素换算为画布缓冲像素，保证缩小显示后线宽与手柄仍可见。
 *
 * Args:
 *   scale (number): 画布 CSS 尺寸与原图像素尺寸之比。
 *   n (number): 界面像素值。
 *
 * Returns:
 *   number: 画布缓冲像素值，最小为 1。
 *
 * Callers:
 *   - `hitTestBox`
 *   - `CropPage`
 */
function uiPx(scale: number, n: number): number {
  return Math.max(1, n / Math.max(1e-6, scale));
}

/**
 * 命中测试单个选区的角、边与内部。
 *
 * Args:
 *   box (CropBox): 待测试选区。
 *   rx (number): 原图像素横坐标。
 *   ry (number): 原图像素纵坐标。
 *   scale (number): 画布显示缩放比。
 *
 * Returns:
 *   DragMode | null: 命中的拖动模式；未命中为 null。
 *
 * Callers:
 *   - `CropPage`
 */
function hitTestBox(box: CropBox, rx: number, ry: number, scale: number): DragMode | null {
  const left = Math.min(box.x1, box.x2);
  const right = Math.max(box.x1, box.x2);
  const top = Math.min(box.y1, box.y2);
  const bottom = Math.max(box.y1, box.y2);
  const edgeTol = uiPx(scale, Math.max(8, HANDLE_TOL));
  const cornerTol = uiPx(scale, Math.max(16, HANDLE_TOL * 1.8));

  const corners: Array<{ mode: DragMode; x: number; y: number }> = [
    { mode: 'resize_tl', x: left, y: top },
    { mode: 'resize_tr', x: right, y: top },
    { mode: 'resize_bl', x: left, y: bottom },
    { mode: 'resize_br', x: right, y: bottom },
  ];
  let bestCorner: DragMode | null = null;
  let bestCornerD = Number.POSITIVE_INFINITY;
  for (const corner of corners) {
    const distance = Math.hypot(rx - corner.x, ry - corner.y);
    if (distance < bestCornerD) {
      bestCornerD = distance;
      bestCorner = corner.mode;
    }
  }
  if (bestCorner && bestCornerD <= cornerTol) return bestCorner;

  let nearL = Math.abs(rx - left) <= edgeTol && top <= ry && ry <= bottom;
  let nearR = Math.abs(rx - right) <= edgeTol && top <= ry && ry <= bottom;
  let nearT = Math.abs(ry - top) <= edgeTol && left <= rx && rx <= right;
  let nearB = Math.abs(ry - bottom) <= edgeTol && left <= rx && rx <= right;

  if (bestCornerD <= edgeTol * 1.25) {
    nearL = nearL && !(bestCorner === 'resize_tl' || bestCorner === 'resize_bl');
    nearR = nearR && !(bestCorner === 'resize_tr' || bestCorner === 'resize_br');
    nearT = nearT && !(bestCorner === 'resize_tl' || bestCorner === 'resize_tr');
    nearB = nearB && !(bestCorner === 'resize_bl' || bestCorner === 'resize_br');
  }

  if (nearL) return 'resize_left';
  if (nearR) return 'resize_right';
  if (nearT) return 'resize_top';
  if (nearB) return 'resize_bottom';

  const inside = left <= rx && rx <= right && top <= ry && ry <= bottom;
  return inside ? 'move' : null;
}

/**
 * 将拖动模式映射为鼠标指针样式。
 *
 * Args:
 *   mode (HitMode | null): 命中模式。
 *
 * Returns:
 *   string: CSS cursor 值。
 *
 * Callers:
 *   - `CropPage`
 */
function cursorFor(mode: HitMode | null): string {
  if (mode === 'move') return 'move';
  if (mode === 'resize_tl' || mode === 'resize_br') return 'nwse-resize';
  if (mode === 'resize_tr' || mode === 'resize_bl') return 'nesw-resize';
  if (mode === 'resize_left' || mode === 'resize_right') return 'ew-resize';
  if (mode === 'resize_top' || mode === 'resize_bottom') return 'ns-resize';
  return 'crosshair';
}

/**
 * 按吸附候选修正选区并夹进原图范围。
 *
 * Args:
 *   box (CropBox): 拖动后的选区。
 *   cand (Candidate | null): 吸附候选。
 *   mode (DragMode): 拖动模式。
 *   w (number): 原图宽。
 *   h (number): 原图高。
 *
 * Returns:
 *   CropBox: 吸附修正后的选区。
 *
 * Callers:
 *   - `CropPage`
 */
function applySnap(box: CropBox, cand: Candidate | null, mode: DragMode, w: number, h: number): CropBox {
  if (!cand) return box;
  const sw = cand.crop_w;
  const sh = cand.crop_h;
  let { x1, y1, x2, y2 } = box;
  if (mode === 'move') {
    x2 = Math.min(w, x1 + sw);
    y2 = Math.min(h, y1 + sh);
    x1 = x2 - sw;
    y1 = y2 - sh;
  } else if (mode === 'resize_left') {
    x1 = x2 - sw;
    y2 = y1 + sh;
  } else if (mode === 'resize_right') {
    x2 = x1 + sw;
    y2 = y1 + sh;
  } else if (mode === 'resize_top') {
    y1 = y2 - sh;
    x2 = x1 + sw;
  } else if (mode === 'resize_bottom') {
    y2 = y1 + sh;
    x2 = x1 + sw;
  } else if (mode === 'resize_tl') {
    x1 = x2 - sw;
    y1 = y2 - sh;
  } else if (mode === 'resize_tr') {
    x2 = x1 + sw;
    y1 = y2 - sh;
  } else if (mode === 'resize_bl') {
    x1 = x2 - sw;
    y2 = y1 + sh;
  } else if (mode === 'resize_br') {
    x2 = x1 + sw;
    y2 = y1 + sh;
  } else {
    x2 = Math.min(w, x1 + sw);
    y2 = Math.min(h, y1 + sh);
  }
  const clamped = clampBox(w, h, x1, y1, x2, y2);
  return makeCrop(clamped.x1, clamped.y1, clamped.x2, clamped.y2, cand, box.name);
}

/**
 * 在离屏画布上绘制棋盘格底纹。
 *
 * Args:
 *   ctx (CanvasRenderingContext2D): 目标上下文。
 *   width (number): 绘制宽。
 *   height (number): 绘制高。
 *
 * Returns:
 *   void: 无返回值。
 *
 * Callers:
 *   - `CropPage`
 */
function drawChecker(ctx: CanvasRenderingContext2D, width: number, height: number): void {
  const grid = 16;
  for (let y = 0; y < height; y += grid) {
    for (let x = 0; x < width; x += grid) {
      const light = ((x / grid + y / grid) % 2) === 0;
      ctx.fillStyle = light ? '#cccccc' : '#eeeeee';
      ctx.fillRect(x, y, Math.min(grid, width - x), Math.min(grid, height - y));
    }
  }
}

/**
 * 解码裁剪端点返回的图片二进制。
 *
 * Args:
 *   blob (Blob): 图片字节。
 *
 * Returns:
 *   Promise<HTMLImageElement>: 解码完成的原图对象。
 *
 * Callers:
 *   - `CropPage`
 */
function decodeImageBlob(blob: Blob): Promise<HTMLImageElement> {
  const url = URL.createObjectURL(blob);
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('图片解码失败'));
    };
    image.src = url;
  });
}

/**
 * 部位预设输入框；失焦或回车时提交，非法输入恢复为已保存值。
 *
 * Args:
 *   props.value (string): 已保存的规格文本。
 *   props.placeholder (string): 占位文本。
 *   props.onCommit (function): 提交回调，返回是否接受输入。
 *
 * Returns:
 *   JSX.Element: 受控输入框。
 *
 * Callers:
 *   - `CropPage`
 */
function SpecInput({
  value,
  placeholder,
  onCommit,
}: {
  value: string;
  placeholder?: string;
  onCommit: (text: string) => boolean;
}) {
  const [text, setText] = useState(value);

  useEffect(() => {
    setText(value);
  }, [value]);

  return (
    <Input
      className="w-28 text-xs"
      value={text}
      placeholder={placeholder}
      spellCheck={false}
      onChange={(event) => setText(event.target.value)}
      onBlur={() => {
        if (!onCommit(text)) setText(value);
      }}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          event.currentTarget.blur();
        }
      }}
    />
  );
}

/**
 * 选区部位名输入框；失焦时提交改名。
 *
 * Args:
 *   props.value (string): 选区当前部位名。
 *   props.onCommit (function): 提交回调。
 *
 * Returns:
 *   JSX.Element: 受控输入框。
 *
 * Callers:
 *   - `CropPage`
 */
function CropNameInput({ value, onCommit }: { value: string; onCommit: (text: string) => void }) {
  const [text, setText] = useState(value);

  useEffect(() => {
    setText(value);
  }, [value]);

  return (
    <Input
      className="mt-1 w-full"
      placeholder="部位名"
      value={text}
      onClick={(event) => event.stopPropagation()}
      onChange={(event) => setText(event.target.value)}
      onBlur={() => {
        const next = text.trim();
        setText(next);
        onCommit(next);
      }}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          event.currentTarget.blur();
        }
      }}
    />
  );
}

/**
 * 裁剪页：多选区吸附裁剪、整图输出与部位预设管理。
 *
 * Args:
 *   无；数据集、目录与设置由 ``useApp`` 提供。
 *
 * Returns:
 *   JSX.Element: 裁剪工作区界面。
 *
 * Callers:
 *   - `frontend/src/main.tsx`: 页面路由表挂载。
 */
export function CropPage() {
  const app = useApp();
  const { notify, saveSettings } = app;
  const sourceDir = app.dirs[0] ?? 'main';

  const [images, setImages] = useState<CropImageItem[]>([]);
  const imagesRef = useRef<CropImageItem[]>([]);
  const [index, setIndex] = useState(0);
  const indexRef = useRef(0);
  const [crops, setCrops] = useState<CropBox[]>([]);
  const cropsRef = useRef<CropBox[]>([]);
  const [selected, setSelected] = useState(-1);
  const selectedRef = useRef(-1);
  const [noCrop, setNoCrop] = useState(false);
  const noCropRef = useRef(false);
  const [partName, setPartName] = useState('');
  const partNameRef = useRef('');
  const [partAdd, setPartAdd] = useState('');
  const [autoParts, setAutoParts] = useState<string[]>([]);
  const autoPartsRef = useRef<string[]>([]);
  const cropSettingsRef = useRef<StoredCropSettings>(normalizeCropSettings(app.settings.crop));
  const deniedPartsRef = useRef<string[]>(readDeniedParts(cropSettingsRef.current.denied_parts));
  const [partDefaults, setPartDefaults] = useState<Record<string, CropDefault>>(() =>
    readPartDefaults(normalizeCropSettings(app.settings.crop).part_defaults),
  );
  const partDefaultsRef = useRef(partDefaults);
  partDefaultsRef.current = partDefaults;

  const [image, setImage] = useState<HTMLImageElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const sizeRef = useRef({ w: 0, h: 0 });
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const [filmstripOpen, setFilmstripOpen] = useState(false);

  const cfgRef = useRef<MatchConfig>(DEFAULT_MATCH);
  const baseRef = useRef<HTMLCanvasElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const dragRafRef = useRef<number | null>(null);
  const lastPointerRef = useRef<{ x: number; y: number } | null>(null);
  const loadTokenRef = useRef(0);

  const view = useMemo<CanvasView>(() => {
    if (!size.w || !size.h || !viewport.width || !viewport.height) {
      return { scale: 1, width: 0, height: 0, left: 0, top: 0 };
    }
    const scale = Math.min(viewport.width / size.w, viewport.height / size.h);
    const width = Math.max(1, Math.floor(size.w * scale));
    const height = Math.max(1, Math.floor(size.h * scale));
    return {
      scale,
      width,
      height,
      left: Math.floor((viewport.width - width) / 2),
      top: Math.floor((viewport.height - height) / 2),
    };
  }, [size.w, size.h, viewport.width, viewport.height]);
  const viewRef = useRef(view);
  viewRef.current = view;

  /**
   * 同步更新选区状态与引用。
   *
   * Args:
   *   next (CropBox[]): 新的选区列表。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `CropPage` 内部各选区变更路径。
   */
  const applyCrops = useCallback((next: CropBox[]) => {
    cropsRef.current = next;
    setCrops(next);
  }, []);

  /**
   * 同步更新当前选中索引。
   *
   * Args:
   *   next (number): 新的选中索引，-1 表示无选中。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `CropPage` 内部各选中变更路径。
   */
  const selectCrop = useCallback((next: number) => {
    selectedRef.current = next;
    setSelected(next);
  }, []);

  /**
   * 同步更新整图输出开关。
   *
   * Args:
   *   next (boolean): 是否整图输出。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `CropPage`
   */
  const setNoCropBoth = useCallback((next: boolean) => {
    noCropRef.current = next;
    setNoCrop(next);
  }, []);

  /**
   * 同步更新整图输出部位名。
   *
   * Args:
   *   next (string): 部位名。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `CropPage`
   */
  const setPartNameBoth = useCallback((next: string) => {
    partNameRef.current = next;
    setPartName(next);
  }, []);

  /**
   * 把裁剪设置补丁写入数据集层并同步本地引用。
   *
   * Args:
   *   patch (Partial<StoredCropSettings>): 待合并的设置字段。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `savePartDefaults`
   *   - `saveAutoParts`
   *   - `saveDeniedParts`
   */
  const persistCrop = useCallback(
    (patch: Partial<StoredCropSettings>) => {
      const next: StoredCropSettings = { ...cropSettingsRef.current, ...patch };
      cropSettingsRef.current = next;
      saveSettings({ crop: next }, true);
    },
    [saveSettings],
  );

  /**
   * 读取某部位的默认裁切预设，未单独设置时使用全局预设。
   *
   * Args:
   *   name (string): 部位名，空串表示全局。
   *
   * Returns:
   *   CropDefault | null: 命中的预设；无预设时为 null。
   *
   * Callers:
   *   - `resolveDefaultCand`
   */
  const getPartDefault = useCallback((name: string): CropDefault | null => {
    const map = partDefaultsRef.current;
    const key = name || '';
    if (map[key] && typeof map[key] === 'object') return map[key];
    if (key && map[''] && typeof map[''] === 'object') return map[''];
    return null;
  }, []);

  /**
   * 持久化部位预设映射。
   *
   * Args:
   *   map (Record<string, CropDefault>): 全量部位预设。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `commitPartDefault`
   */
  const savePartDefaults = useCallback(
    (map: Record<string, CropDefault>) => {
      partDefaultsRef.current = map;
      setPartDefaults(map);
      persistCrop({ part_defaults: map });
    },
    [persistCrop],
  );

  /**
   * 提交侧栏某部位或全局的默认裁切规格。
   *
   * Args:
   *   part (string): 部位名，空串表示全局。
   *   text (string): 输入框文本。
   *
   * Returns:
   *   boolean: 是否接受本次输入。
   *
   * Callers:
   *   - `SpecInput` 的提交回调
   */
  const commitPartDefault = useCallback(
    (part: string, text: string): boolean => {
      const map = { ...partDefaultsRef.current };
      const trimmed = text.trim();
      if (!trimmed) {
        delete map[part];
        savePartDefaults(map);
        notify('已清除默认裁切');
        return true;
      }
      const spec = parseDefaultSpec(trimmed);
      if (!spec) {
        notify('格式：宽x高@缩放，如 1536x1536@1.0');
        return false;
      }
      map[part] = spec;
      savePartDefaults(map);
      notify('已保存默认裁切');
      return true;
    },
    [notify, savePartDefaults],
  );

  /**
   * 从设置与服务器部位目录重建自动部位清单。
   *
   * Args:
   *   serverParts (string[]): 服务器已有的部位目录名。
   *   dir (string): 当前源目录名，不得兼作部位。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `CropPage` 启动与源目录切换副作用。
   */
  const loadParts = useCallback((serverParts: string[], dir: string) => {
    const state = cropSettingsRef.current;
    const saved = Array.isArray(state.auto_parts) ? state.auto_parts : null;
    const denied = readDeniedParts(state.denied_parts);
    deniedPartsRef.current = denied;
    const result: string[] = [];
    const seen = new Set<string>();
    const pushName = (name?: string) => {
      if (!name || seen.has(name) || denied.includes(name)) return;
      if (name === dir) return;
      seen.add(name);
      result.push(name);
    };
    if (saved) {
      for (const entry of saved) {
        if (typeof entry === 'string') pushName(entry);
        else if (entry && typeof entry === 'object' && entry.name && entry.enabled !== false) pushName(entry.name);
      }
    }
    if (Array.isArray(serverParts)) {
      for (const name of serverParts) pushName(name);
    }
    autoPartsRef.current = result;
    setAutoParts(result);
  }, []);

  /**
   * 持久化自动部位清单。
   *
   * Args:
   *   parts (string[]): 全量自动部位名。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `registerAutoPart`
   *   - `unregisterAutoPart`
   */
  const saveAutoParts = useCallback(
    (parts: string[]) => {
      autoPartsRef.current = parts;
      setAutoParts(parts);
      persistCrop({ auto_parts: parts });
    },
    [persistCrop],
  );

  /**
   * 持久化永久删除部位清单。
   *
   * Args:
   *   parts (string[]): 全量永久删除部位名。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `registerAutoPart`
   *   - `unregisterAutoPart`
   */
  const saveDeniedParts = useCallback(
    (parts: string[]) => {
      deniedPartsRef.current = parts;
      persistCrop({ denied_parts: parts });
    },
    [persistCrop],
  );

  /**
   * 登记自动部位：解除永久删除并写入设置。
   *
   * Args:
   *   name (string): 部位名。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `addPart`
   *   - `renameCrop`
   */
  const registerAutoPart = useCallback(
    (name: string) => {
      if (!name) return;
      saveDeniedParts(deniedPartsRef.current.filter((entry) => entry !== name));
      if (autoPartsRef.current.includes(name)) return;
      saveAutoParts([...autoPartsRef.current, name]);
    },
    [saveAutoParts, saveDeniedParts],
  );

  /**
   * 注销自动部位，可选写入永久删除清单。
   *
   * Args:
   *   name (string): 部位名。
   *   permanent (boolean): 是否同时永久删除。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `renameCrop`
   *   - `removeCropPermanently`
   */
  const unregisterAutoPart = useCallback(
    (name: string, permanent: boolean) => {
      if (!name) return;
      saveAutoParts(autoPartsRef.current.filter((entry) => entry !== name));
      if (permanent && !deniedPartsRef.current.includes(name)) {
        saveDeniedParts([...deniedPartsRef.current, name]);
      }
    },
    [saveAutoParts, saveDeniedParts],
  );

  /**
   * 按部位预设解析默认候选，空部位名使用全局预设。
   *
   * Args:
   *   name (string): 部位名。
   *
   * Returns:
   *   Candidate | null: 默认候选；原图尺寸未知时为 null。
   *
   * Callers:
   *   - `makeDefaultSizedCrop`
   */
  const resolveDefaultCand = useCallback(
    (name: string): Candidate | null => {
      const { w, h } = sizeRef.current;
      if (!w || !h) return null;
      const pref = getPartDefault(name || '');
      if (pref) {
        const fixed = findByTargetZoom(cfgRef.current, w, h, pref.target_w, pref.target_h, pref.zoom);
        if (fixed) return fixed;
      }
      return chooseBest(cfgRef.current, w, h, w, h);
    },
    [getPartDefault],
  );

  /**
   * 按默认参数生成居中选区，多个选区按序号错开。
   *
   * Args:
   *   name (string): 部位名。
   *   index (number): 选区序号，用于错开位置。
   *
   * Returns:
   *   CropBox: 新建选区。
   *
   * Callers:
   *   - `initDefaultCrop`
   *   - `ensurePartCrop`
   *   - `addCrop`
   */
  const makeDefaultSizedCrop = useCallback(
    (name: string, index: number): CropBox => {
      const { w, h } = sizeRef.current;
      const init = resolveDefaultCand(name);
      if (!init) return makeCrop(0, 0, w, h, null, name || '');
      const shift = (index || 0) * 40;
      let x1 = Math.floor((w - init.crop_w) / 2) + shift;
      let y1 = Math.floor((h - init.crop_h) / 2) + shift;
      x1 = Math.max(0, Math.min(w - init.crop_w, x1));
      y1 = Math.max(0, Math.min(h - init.crop_h, y1));
      return makeCrop(x1, y1, x1 + init.crop_w, y1 + init.crop_h, init, name || '');
    },
    [resolveDefaultCand],
  );

  /**
   * 生成本图初始选区：优先还原保存记录，其余自动部位补默认框。
   *
   * Args:
   *   savedParts (Record<string, CropPartRecord>): ``/crop/state`` 返回的部位记录。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `CropPage` 的图片加载与清空选区路径。
   */
  const initDefaultCrop = useCallback(
    (savedParts?: Record<string, CropPartRecord>) => {
      if (!sizeRef.current.w || !sizeRef.current.h) {
        applyCrops([]);
        selectCrop(-1);
        return;
      }
      const list: CropBox[] = [];
      if (savedParts) {
        for (const name of Object.keys(savedParts)) {
          const record = savedParts[name];
          if (!record.crop || record.crop.length !== 4) continue;
          const [cx1, cy1, cx2, cy2] = record.crop;
          const tw = record.size ? record.size[0] : cx2 - cx1;
          const th = record.size ? record.size[1] : cy2 - cy1;
          const box = makeCrop(cx1, cy1, cx2, cy2, {
            target_w: tw,
            target_h: th,
            zoom: tw / Math.max(1, cx2 - cx1),
            crop_w: cx2 - cx1,
            crop_h: cy2 - cy1,
          }, name);
          box.saved = true;
          list.push(box);
        }
      }
      for (const part of autoPartsRef.current) {
        if (!list.some((box) => box.name === part)) {
          list.push(makeDefaultSizedCrop(part, list.length));
        }
      }
      if (!list.length) list.push(makeDefaultSizedCrop('', 0));
      applyCrops(list);
      selectCrop(0);
    },
    [applyCrops, makeDefaultSizedCrop, selectCrop],
  );

  /**
   * 移除指定索引的选区并修正选中项。
   *
   * Args:
   *   idx (number): 选区索引。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `deleteCrop`
   *   - `removeCropPermanently`
   */
  const removeCropAt = useCallback(
    (idx: number) => {
      const list = cropsRef.current;
      if (idx < 0 || idx >= list.length) return;
      const next = list.filter((_, position) => position !== idx);
      applyCrops(next);
      if (!next.length) selectCrop(-1);
      else selectCrop(Math.min(idx, next.length - 1));
    },
    [applyCrops, selectCrop],
  );

  /**
   * 删除选区及同部位的所有选区，并永久注销该部位。
   *
   * Args:
   *   idx (number): 选区索引。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `deleteCropPermanently`
   */
  const removeCropPermanently = useCallback(
    (idx: number) => {
      const list = cropsRef.current;
      if (idx < 0 || idx >= list.length) return;
      const name = (list[idx].name || '').trim();
      removeCropAt(idx);
      if (!name) return;
      for (let i = cropsRef.current.length - 1; i >= 0; i--) {
        if (cropsRef.current[i].name === name) removeCropAt(i);
      }
      unregisterAutoPart(name, true);
    },
    [removeCropAt, unregisterAutoPart],
  );

  /**
   * 若当前图尚无该部位的选区则补一个默认框并选中。
   *
   * Args:
   *   name (string): 部位名。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `addPart`
   */
  const ensurePartCrop = useCallback(
    (name: string) => {
      if (cropsRef.current.some((box) => box.name === name)) return;
      const next = [...cropsRef.current, makeDefaultSizedCrop(name, cropsRef.current.length)];
      applyCrops(next);
      selectCrop(next.length - 1);
    },
    [applyCrops, makeDefaultSizedCrop, selectCrop],
  );

  /**
   * 构建离屏底图缓存，把棋盘格与原图合成一次供后续拖拽重绘。
   *
   * Args:
   *   source (HTMLImageElement): 原图。
   *   w (number): 原图宽。
   *   h (number): 原图高。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `loadIndex`
   */
  const buildBase = useCallback((source: HTMLImageElement, w: number, h: number) => {
    let base = baseRef.current;
    if (!base) {
      base = document.createElement('canvas');
      baseRef.current = base;
    }
    if (base.width !== w || base.height !== h) {
      base.width = w;
      base.height = h;
    }
    const ctx = base.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, w, h);
    drawChecker(ctx, w, h);
    ctx.drawImage(source, 0, 0);
  }, []);

  /**
   * 重绘画布：底图、选区边框、角点与标签。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `CropPage` 的重绘副作用与拖拽路径。
   */
  const redraw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const cw = canvas.width;
    const ch = canvas.height;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    ctx.fillStyle = '#222';
    ctx.fillRect(0, 0, cw, ch);
    const current = imageRef.current;
    if (!current || !sizeRef.current.w) return;
    const base = baseRef.current;
    if (!base) return;
    ctx.drawImage(base, 0, 0);
    if (noCropRef.current) return;

    const scale = viewRef.current.scale;
    const lw = uiPx(scale, 2.5);
    const lwInactive = uiPx(scale, 1.5);
    const hs = uiPx(scale, 5);
    const fontPx = uiPx(scale, 12);
    const labelH = uiPx(scale, 30);
    const labelGap = uiPx(scale, 32);

    for (let i = 0; i < cropsRef.current.length; i++) {
      const box = cropsRef.current[i];
      const { x1, y1, x2, y2 } = box;
      const active = i === selectedRef.current;
      const isSaved = !!box.saved;
      ctx.strokeStyle = isSaved ? '#e05555' : '#00cc66';
      ctx.lineWidth = active ? lw : lwInactive;
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);

      const corners: Array<[number, number]> = [
        [x1, y1],
        [x2, y1],
        [x1, y2],
        [x2, y2],
      ];
      ctx.fillStyle = isSaved ? '#e05555' : '#00cc66';
      for (const [cx, cy] of corners) {
        ctx.fillRect(cx - hs, cy - hs, hs * 2, hs * 2);
      }

      const label = `${i + 1}${box.name ? ` ${box.name}` : ''}`;
      const info = `${box.x2 - box.x1}x${box.y2 - box.y1} → ${box.target_w}x${box.target_h}@${Number(box.zoom).toFixed(1)}x`;
      ctx.font = `${fontPx}px sans-serif`;
      ctx.fillStyle = isSaved ? '#330a0a' : '#003320';
      const tw = Math.max(ctx.measureText(label).width, ctx.measureText(info).width) + uiPx(scale, 8);
      const ly = Math.max(0, y1 - labelGap);
      ctx.fillRect(x1, ly, tw, labelH);
      ctx.fillStyle = isSaved ? '#ffb0b0' : '#a0ffc0';
      ctx.fillText(label, x1 + uiPx(scale, 4), ly + fontPx);
      ctx.fillText(info, x1 + uiPx(scale, 4), ly + fontPx * 2 + uiPx(scale, 2));
    }
  }, []);

  /**
   * 把画布 CSS 坐标换算为原图像素坐标。
   *
   * Args:
   *   sx (number): 相对画布左上角的 CSS 横坐标。
   *   sy (number): 相对画布左上角的 CSS 纵坐标。
   *
   * Returns:
   *   { x: number; y: number }: 夹进原图范围后的像素坐标。
   *
   * Callers:
   *   - `doDrag`
   *   - `onPointerDown`
   *   - `onPointerMove`
   */
  const cssToRaw = useCallback((sx: number, sy: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    const { w, h } = sizeRef.current;
    const rx = Math.round((sx / Math.max(1e-6, rect.width)) * w);
    const ry = Math.round((sy / Math.max(1e-6, rect.height)) * h);
    return {
      x: Math.max(0, Math.min(w - 1, rx)),
      y: Math.max(0, Math.min(h - 1, ry)),
    };
  }, []);

  /**
   * 全局命中测试：角点优先，其次当前选区，再按倒序命中其他选区。
   *
   * Args:
   *   rx (number): 原图像素横坐标。
   *   ry (number): 原图像素纵坐标。
   *
   * Returns:
   *   HitResult: 命中的选区索引与拖动模式。
   *
   * Callers:
   *   - `onPointerDown`
   *   - `onPointerMove`
   */
  const hitTest = useCallback((rx: number, ry: number): HitResult => {
    const scale = viewRef.current.scale;
    const cornerTol = uiPx(scale, Math.max(16, HANDLE_TOL * 1.8));
    let best: HitResult | null = null;
    let bestD = Number.POSITIVE_INFINITY;
    for (let i = cropsRef.current.length - 1; i >= 0; i--) {
      const box = cropsRef.current[i];
      const left = Math.min(box.x1, box.x2);
      const right = Math.max(box.x1, box.x2);
      const top = Math.min(box.y1, box.y2);
      const bottom = Math.max(box.y1, box.y2);
      const points: Array<{ mode: DragMode; x: number; y: number }> = [
        { mode: 'resize_tl', x: left, y: top },
        { mode: 'resize_tr', x: right, y: top },
        { mode: 'resize_bl', x: left, y: bottom },
        { mode: 'resize_br', x: right, y: bottom },
      ];
      for (const point of points) {
        const distance = Math.hypot(rx - point.x, ry - point.y);
        if (distance < bestD) {
          bestD = distance;
          best = { idx: i, mode: point.mode };
        }
      }
    }
    if (best && bestD <= cornerTol) return best;

    const active = selectedRef.current;
    if (active >= 0 && active < cropsRef.current.length) {
      const mode = hitTestBox(cropsRef.current[active], rx, ry, scale);
      if (mode) return { idx: active, mode };
    }
    for (let i = cropsRef.current.length - 1; i >= 0; i--) {
      if (i === active) continue;
      const mode = hitTestBox(cropsRef.current[i], rx, ry, scale);
      if (mode) return { idx: i, mode };
    }
    return { idx: -1, mode: 'new' };
  }, []);

  /**
   * 更新画布容器鼠标指针样式。
   *
   * Args:
   *   mode (HitMode | null): 命中模式。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `onPointerDown`
   *   - `onPointerMove`
   */
  const setCursor = useCallback((mode: HitMode | null) => {
    if (wrapRef.current) wrapRef.current.style.cursor = cursorFor(mode);
  }, []);

  /**
   * 执行一次拖拽更新：新建、移动或缩放选区。
   *
   * Args:
   *   clientX (number): 指针视口横坐标。
   *   clientY (number): 指针视口纵坐标。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `onPointerMove` 的逐帧回调。
   */
  const doDrag = useCallback(
    (clientX: number, clientY: number) => {
      const drag = dragRef.current;
      if (!drag) return;
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const raw = cssToRaw(clientX - rect.left, clientY - rect.top);
      const { w: iw, h: ih } = sizeRef.current;
      let cand: Candidate | null = null;
      let newBox: BoxRect | null = null;
      const name = drag.base ? drag.base.name || '' : '';

      if (drag.mode === 'new') {
        const sx0 = drag.start.x;
        const sy0 = drag.start.y;
        const desiredW = Math.abs(raw.x - sx0);
        const desiredH = Math.abs(raw.y - sy0);
        if (desiredW < 2 || desiredH < 2) return;
        const signX = raw.x >= sx0 ? 1 : -1;
        const signY = raw.y >= sy0 ? 1 : -1;
        const maxW = signX > 0 ? iw - sx0 : sx0;
        const maxH = signY > 0 ? ih - sy0 : sy0;
        cand = chooseBest(cfgRef.current, maxW, maxH, desiredW, desiredH);
        if (!cand) return;
        const cw = cand.crop_w;
        const ch = cand.crop_h;
        const x1 = signX > 0 ? sx0 : sx0 - cw;
        const y1 = signY > 0 ? sy0 : sy0 - ch;
        const clamped = clampBox(iw, ih, x1, y1, x1 + cw, y1 + ch);
        const box = makeCrop(clamped.x1, clamped.y1, clamped.x2, clamped.y2, cand, '');
        if (drag.idx < 0) {
          const next = [...cropsRef.current, box];
          applyCrops(next);
          drag.idx = next.length - 1;
          selectCrop(drag.idx);
        } else {
          const next = [...cropsRef.current];
          next[drag.idx] = box;
          applyCrops(next);
        }
        return;
      }

      const base = drag.base;
      if (drag.idx < 0 || !base) return;
      const bx1 = base.x1;
      const by1 = base.y1;
      const bx2 = base.x2;
      const by2 = base.y2;
      const curW = Math.max(1, bx2 - bx1);
      const curH = Math.max(1, by2 - by1);

      if (drag.mode === 'move') {
        const dx = raw.x - drag.start.x;
        const dy = raw.y - drag.start.y;
        let nx1 = bx1 + dx;
        let ny1 = by1 + dy;
        let nx2 = bx2 + dx;
        let ny2 = by2 + dy;
        if (nx1 < 0) {
          nx2 -= nx1;
          nx1 = 0;
        }
        if (ny1 < 0) {
          ny2 -= ny1;
          ny1 = 0;
        }
        if (nx2 > iw) {
          nx1 -= nx2 - iw;
          nx2 = iw;
        }
        if (ny2 > ih) {
          ny1 -= ny2 - ih;
          ny2 = ih;
        }
        newBox = clampBox(iw, ih, nx1, ny1, nx2, ny2);
        cand = chooseBest(cfgRef.current, iw, ih, nx2 - nx1, ny2 - ny1);
      } else if (drag.mode === 'resize_left') {
        cand = chooseBest(cfgRef.current, Math.max(1, bx2), ih, Math.max(1, bx2 - raw.x), curH, 1, 4);
        if (!cand) return;
        newBox = { x1: bx2 - cand.crop_w, y1: by1, x2: bx2, y2: by1 + cand.crop_h };
      } else if (drag.mode === 'resize_right') {
        cand = chooseBest(cfgRef.current, Math.max(1, iw - bx1), ih, Math.max(1, raw.x - bx1), curH, 1, 4);
        if (!cand) return;
        newBox = { x1: bx1, y1: by1, x2: bx1 + cand.crop_w, y2: by1 + cand.crop_h };
      } else if (drag.mode === 'resize_top') {
        cand = chooseBest(cfgRef.current, iw, Math.max(1, by2), curW, Math.max(1, by2 - raw.y), 4, 1);
        if (!cand) return;
        newBox = { x1: bx1, y1: by2 - cand.crop_h, x2: bx1 + cand.crop_w, y2: by2 };
      } else if (drag.mode === 'resize_bottom') {
        cand = chooseBest(cfgRef.current, iw, Math.max(1, ih - by1), curW, Math.max(1, raw.y - by1), 4, 1);
        if (!cand) return;
        newBox = { x1: bx1, y1: by1, x2: bx1 + cand.crop_w, y2: by1 + cand.crop_h };
      } else if (drag.mode === 'resize_tl') {
        cand = chooseBest(
          cfgRef.current,
          Math.max(1, bx2),
          Math.max(1, by2),
          Math.max(1, bx2 - raw.x),
          Math.max(1, by2 - raw.y),
        );
        if (!cand) return;
        newBox = { x1: bx2 - cand.crop_w, y1: by2 - cand.crop_h, x2: bx2, y2: by2 };
      } else if (drag.mode === 'resize_tr') {
        cand = chooseBest(
          cfgRef.current,
          Math.max(1, iw - bx1),
          Math.max(1, by2),
          Math.max(1, raw.x - bx1),
          Math.max(1, by2 - raw.y),
        );
        if (!cand) return;
        newBox = { x1: bx1, y1: by2 - cand.crop_h, x2: bx1 + cand.crop_w, y2: by2 };
      } else if (drag.mode === 'resize_bl') {
        cand = chooseBest(
          cfgRef.current,
          Math.max(1, bx2),
          Math.max(1, ih - by1),
          Math.max(1, bx2 - raw.x),
          Math.max(1, raw.y - by1),
        );
        if (!cand) return;
        newBox = { x1: bx2 - cand.crop_w, y1: by1, x2: bx2, y2: by1 + cand.crop_h };
      } else {
        cand = chooseBest(
          cfgRef.current,
          Math.max(1, iw - bx1),
          Math.max(1, ih - by1),
          Math.max(1, raw.x - bx1),
          Math.max(1, raw.y - by1),
        );
        if (!cand) return;
        newBox = { x1: bx1, y1: by1, x2: bx1 + cand.crop_w, y2: by1 + cand.crop_h };
      }

      if (!newBox) return;
      const snappedBase = makeCrop(newBox.x1, newBox.y1, newBox.x2, newBox.y2, cand, name);
      let finalCand = cand;
      if (!finalCand) {
        const width = snappedBase.x2 - snappedBase.x1;
        const height = snappedBase.y2 - snappedBase.y1;
        finalCand = chooseBest(cfgRef.current, width, height, width, height);
      }
      const next = [...cropsRef.current];
      next[drag.idx] = applySnap(snappedBase, finalCand, drag.mode, iw, ih);
      applyCrops(next);
    },
    [applyCrops, cssToRaw, selectCrop],
  );

  /**
   * 指针按下：命中选区或开始新建选区。
   *
   * Args:
   *   event (ReactPointerEvent<HTMLCanvasElement>): 画布指针事件。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 画布元素 ``onPointerDown``。
   */
  const onPointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!imageRef.current || noCropRef.current) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const raw = cssToRaw(event.clientX - rect.left, event.clientY - rect.top);
    const hit = hitTest(raw.x, raw.y);
    dragRef.current = {
      mode: hit.mode,
      idx: hit.idx,
      start: raw,
      base: hit.idx >= 0 ? { ...cropsRef.current[hit.idx] } : null,
    };
    if (hit.idx >= 0) selectCrop(hit.idx);
    event.currentTarget.setPointerCapture(event.pointerId);
    setCursor(hit.mode);
  };

  /**
   * 指针移动：拖拽时逐帧更新选区，空闲时更新指针样式。
   *
   * Args:
   *   event (ReactPointerEvent<HTMLDivElement>): 画布容器指针事件。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 画布容器 ``onPointerMove``。
   */
  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (noCropRef.current || !imageRef.current) return;
    if (dragRef.current) {
      lastPointerRef.current = { x: event.clientX, y: event.clientY };
      if (dragRafRef.current === null) {
        dragRafRef.current = requestAnimationFrame(() => {
          dragRafRef.current = null;
          const pointer = lastPointerRef.current;
          if (pointer) doDrag(pointer.x, pointer.y);
        });
      }
      return;
    }
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const raw = cssToRaw(event.clientX - rect.left, event.clientY - rect.top);
    const hit = hitTest(raw.x, raw.y);
    setCursor(hit.mode);
  };

  /**
   * 指针抬起：结束拖拽并清理逐帧句柄。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 画布容器 ``onPointerUp`` 与 ``onPointerCancel``。
   */
  const onPointerUp = () => {
    dragRef.current = null;
    lastPointerRef.current = null;
    if (dragRafRef.current !== null) {
      cancelAnimationFrame(dragRafRef.current);
      dragRafRef.current = null;
    }
  };

  /**
   * 加载指定索引的原图与部位状态并重建选区。
   *
   * Args:
   *   idx (number): 图片索引。
   *   list (CropImageItem[]): 可选图片清单，默认使用当前清单。
   *
   * Returns:
   *   Promise<void>: 加载完成。
   *
   * Callers:
   *   - `loadImages`
   *   - `goPrev`
   *   - `goNext`
   *   - `saveAndNext`
   */
  const loadIndex = useCallback(
    async (idx: number, list?: CropImageItem[]) => {
      const items = list ?? imagesRef.current;
      if (!items.length) return;
      const next = Math.max(0, Math.min(items.length - 1, idx));
      indexRef.current = next;
      setIndex(next);
      const meta = items[next];
      const token = ++loadTokenRef.current;
      const [blob, state] = await Promise.all([
        fetch(`${apiPath('/crop/image')}?path=${encodeURIComponent(meta.path)}`).then(async (res) => {
          if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
          return res.blob();
        }),
        request<CropImageState>(`${apiPath('/crop/state')}?path=${encodeURIComponent(meta.path)}`),
      ]);
      const decoded = await decodeImageBlob(blob);
      if (token !== loadTokenRef.current) return;
      const w = decoded.naturalWidth;
      const h = decoded.naturalHeight;
      imageRef.current = decoded;
      sizeRef.current = { w, h };
      setImage(decoded);
      setSize({ w, h });
      setNoCropBoth(false);
      initDefaultCrop(state.parts);
      buildBase(decoded, w, h);
    },
    [buildBase, initDefaultCrop, setNoCropBoth],
  );

  /**
   * 拉取源目录图片清单并从第一张开始加载。
   *
   * Args:
   *   dir (string): 源目录名。
   *
   * Returns:
   *   Promise<void>: 列表与首张加载完成。
   *
   * Callers:
   *   - `CropPage` 启动与源目录切换副作用。
   */
  const loadImages = useCallback(
    async (dir: string) => {
      const list = await request<CropImageItem[]>(
        `${apiPath('/crop/images')}?dir=${encodeURIComponent(dir)}`,
      );
      imagesRef.current = list;
      setImages(list);
      indexRef.current = 0;
      setIndex(0);
      if (list.length > 0) await loadIndex(0, list);
      else notify('没有图片');
    },
    [loadIndex, notify],
  );

  /**
   * 切换到上一张图片。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 工具栏上一张按钮
   *   - 键盘快捷键
   */
  const goPrev = useCallback(() => {
    if (indexRef.current > 0) {
      void loadIndex(indexRef.current - 1).catch((error) =>
        notify(error instanceof Error ? error.message : String(error)),
      );
    } else {
      notify('已是第一张');
    }
  }, [loadIndex, notify]);

  /**
   * 切换到下一张图片。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 工具栏下一张按钮
   *   - 键盘快捷键
   */
  const goNext = useCallback(() => {
    if (indexRef.current + 1 < imagesRef.current.length) {
      void loadIndex(indexRef.current + 1).catch((error) =>
        notify(error instanceof Error ? error.message : String(error)),
      );
    } else {
      notify('已是最后一张');
    }
  }, [loadIndex, notify]);

  /**
   * 保存当前图裁剪结果并进入下一张。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 保存与跳转完成。
   *
   * Callers:
   *   - 工具栏保存按钮
   *   - 键盘快捷键
   */
  const saveAndNext = useCallback(async () => {
    const items = imagesRef.current;
    if (!items.length || !imageRef.current) return;
    const current = indexRef.current;
    const meta = items[current];
    const whole = noCropRef.current;
    const part = partNameRef.current.trim();
    if (whole && !part) {
      notify('整图输出需填写部位名');
      return;
    }
    const payload = whole
      ? []
      : cropsRef.current.map((box) => ({
          x1: box.x1,
          y1: box.y1,
          x2: box.x2,
          y2: box.y2,
          target_w: box.target_w,
          target_h: box.target_h,
          zoom: box.zoom,
          name: box.name || '',
        }));
    if (!whole && !payload.length) {
      notify('请至少保留一个选区');
      return;
    }
    try {
      const result = await request<{ count: number }>(apiPath('/crop/save'), {
        method: 'POST',
        body: JSON.stringify({
          path: meta.path,
          no_crop_resize: whole,
          part_name: part,
          crops: payload,
        }),
      });
      notify(`已保存 ${result.count} 张`);
      if (current + 1 < items.length) {
        await loadIndex(current + 1);
      } else if (!whole) {
        applyCrops(cropsRef.current.map((box) => ({ ...box, saved: true })));
      }
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    }
  }, [applyCrops, loadIndex, notify]);

  /**
   * 新建一个默认尺寸选区并选中。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 工具栏新建选区按钮
   *   - 键盘快捷键
   */
  const addCrop = useCallback(() => {
    if (noCropRef.current || !sizeRef.current.w) return;
    const next = [...cropsRef.current, makeDefaultSizedCrop('', cropsRef.current.length)];
    applyCrops(next);
    selectCrop(next.length - 1);
  }, [applyCrops, makeDefaultSizedCrop, selectCrop]);

  /**
   * 删除当前选中选区（仅当前图）。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 键盘快捷键
   */
  const delSelected = useCallback(() => {
    const active = selectedRef.current;
    if (noCropRef.current || active < 0 || active >= cropsRef.current.length) return;
    removeCropAt(active);
  }, [removeCropAt]);

  /**
   * 按偏移循环切换选中选区。
   *
   * Args:
   *   delta (number): 偏移量，1 为下一个，-1 为上一个。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 画布滚轮
   */
  const cycleSelected = useCallback(
    (delta: number) => {
      if (noCropRef.current || cropsRef.current.length < 2) return;
      const count = cropsRef.current.length;
      const active = selectedRef.current;
      selectCrop(active < 0 ? 0 : (active + delta + count) % count);
    },
    [selectCrop],
  );

  /**
   * 清空当前图的编辑态选区并恢复默认网格框。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 工具栏清空选区按钮
   *   - 键盘快捷键
   */
  const clearCrops = useCallback(() => {
    if (noCropRef.current) return;
    initDefaultCrop();
  }, [initDefaultCrop]);

  /**
   * 切换整图输出模式并同步部位名。
   *
   * Args:
   *   next (boolean): 是否启用整图输出。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 工具栏整图输出复选框
   */
  const toggleNoCrop = useCallback(
    (next: boolean) => {
      setNoCropBoth(next);
      if (next) {
        const active = selectedRef.current;
        if (!partNameRef.current && active >= 0 && cropsRef.current[active]) {
          setPartNameBoth(cropsRef.current[active].name || '');
        }
      } else if (!cropsRef.current.length) {
        initDefaultCrop();
      }
    },
    [initDefaultCrop, setNoCropBoth, setPartNameBoth],
  );

  /**
   * 提交选区改名并同步自动部位登记。
   *
   * Args:
   *   idx (number): 选区索引。
   *   raw (string): 新部位名。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - `CropNameInput` 的提交回调
   */
  const renameCrop = useCallback(
    (idx: number, raw: string) => {
      const list = cropsRef.current;
      if (idx < 0 || idx >= list.length) return;
      const box = list[idx];
      const oldName = box.name || '';
      const newName = raw.trim();
      if (oldName === newName) return;
      const next = [...list];
      next[idx] = { ...box, name: newName, saved: false };
      applyCrops(next);
      if (oldName && !next.some((entry) => entry.name === oldName)) {
        unregisterAutoPart(oldName, false);
      }
      if (newName) registerAutoPart(newName);
    },
    [applyCrops, registerAutoPart, unregisterAutoPart],
  );

  /**
   * 删除指定索引选区（仅当前图）。
   *
   * Args:
   *   idx (number): 选区索引。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 选区列表删除按钮
   */
  const deleteCrop = useCallback((idx: number) => removeCropAt(idx), [removeCropAt]);

  /**
   * 永久删除指定索引选区及其部位。
   *
   * Args:
   *   idx (number): 选区索引。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 选区列表永久删除按钮
   */
  const deleteCropPermanently = useCallback(
    (idx: number) => {
      const box = cropsRef.current[idx];
      const name = box ? (box.name || '').trim() : '';
      removeCropPermanently(idx);
      notify(name ? `已移除部位 ${name}` : '已移除选区');
    },
    [notify, removeCropPermanently],
  );

  /**
   * 新增自动部位，并在当前图补默认选区。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void: 无返回值。
   *
   * Callers:
   *   - 侧栏添加按钮
   *   - 部位输入框回车
   */
  const addPart = useCallback(() => {
    const name = partAdd.trim();
    if (!/^\w[\w-]{0,63}$/.test(name)) {
      notify('部位名无效');
      return;
    }
    registerAutoPart(name);
    setPartAdd('');
    if (!noCropRef.current && sizeRef.current.w) {
      ensurePartCrop(name);
    }
    notify(`已添加 ${name}`);
  }, [ensurePartCrop, notify, partAdd, registerAutoPart]);

  useEffect(() => {
    const target = wrapRef.current;
    if (!target) return;
    /**
     * 记录画布容器尺寸，供缩放与居中计算。
     *
     * Args:
     *   无。
     *
     * Returns:
     *   void: 无返回值。
     *
     * Callers:
     *   - 容器尺寸观察与窗口尺寸变化。
     */
    const updateViewport = () => {
      setViewport({ width: target.clientWidth, height: target.clientHeight });
    };
    updateViewport();
    const observer = new ResizeObserver(updateViewport);
    observer.observe(target);
    window.addEventListener('resize', updateViewport);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', updateViewport);
    };
  }, []);

  useEffect(() => {
    redraw();
  }, [redraw, image, view, crops, selected, noCrop]);

  useEffect(() => {
    if (wrapRef.current) wrapRef.current.style.cursor = noCrop ? 'default' : 'crosshair';
  }, [noCrop]);

  useEffect(() => {
    const target = wrapRef.current;
    if (!target) return;
    /**
     * 滚轮循环切换选中选区。
     *
     * Args:
     *   event (WheelEvent): 滚轮事件。
     *
     * Returns:
     *   void: 无返回值。
     *
     * Callers:
     *   - 画布容器滚轮监听。
     */
    const onWheel = (event: WheelEvent) => {
      if (noCropRef.current || cropsRef.current.length < 2 || dragRef.current) return;
      event.preventDefault();
      cycleSelected(event.deltaY > 0 ? 1 : -1);
    };
    target.addEventListener('wheel', onWheel, { passive: false });
    return () => target.removeEventListener('wheel', onWheel);
  }, [cycleSelected]);

  useEffect(() => {
    /**
     * 全局快捷键：保存、换图、新建、删除与清空。
     *
     * Args:
     *   event (KeyboardEvent): 键盘事件。
     *
     * Returns:
     *   void: 无返回值。
     *
     * Callers:
     *   - 文档键盘监听。
     */
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      const tag = target.tagName;
      const inField = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
      const interactive =
        inField ||
        target.isContentEditable ||
        Boolean(target.closest('button, [role="checkbox"]'));
      const key = event.key;
      const code = event.code;

      if ((event.ctrlKey || event.metaKey) && (key === 's' || key === 'S' || code === 'KeyS')) {
        event.preventDefault();
        if (inField) target.blur();
        void saveAndNext();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && (key === 'n' || key === 'N' || code === 'KeyN')) {
        event.preventDefault();
        if (inField) target.blur();
        goNext();
        return;
      }
      if (interactive) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;

      if (key === 'Delete' || key === 'Backspace' || key === 'e' || key === 'E' || code === 'KeyE') {
        event.preventDefault();
        delSelected();
      } else if (key === 's' || key === 'S' || code === 'KeyS') {
        event.preventDefault();
        void saveAndNext();
      } else if (key === ' ' || code === 'Space') {
        event.preventDefault();
        goNext();
      } else if (key === 'ArrowLeft' || key === 'a' || key === 'A' || code === 'KeyA') {
        event.preventDefault();
        goPrev();
      } else if (key === 'ArrowRight' || key === 'd' || key === 'D' || code === 'KeyD') {
        event.preventDefault();
        goNext();
      } else if (key === 'q' || key === 'Q' || code === 'KeyQ') {
        event.preventDefault();
        addCrop();
      } else if (key === 'c' || key === 'C' || code === 'KeyC') {
        event.preventDefault();
        clearCrops();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [addCrop, clearCrops, delSelected, goNext, goPrev, saveAndNext]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const config = await request<CropPageConfig>(apiPath('/crop/config'));
        if (cancelled) return;
        loadParts(config.parts ?? [], sourceDir);
        const match = await request<MatchConfig>(apiPath('/crop/candidates'));
        if (cancelled) return;
        cfgRef.current = match;
        await loadImages(sourceDir);
      } catch (error) {
        if (!cancelled) notify(error instanceof Error ? error.message : String(error));
      }
    })();
    return () => {
      cancelled = true;
      loadTokenRef.current += 1;
    };
  }, [loadImages, loadParts, notify, sourceDir]);

  const current = images[index];
  const activeCrop = selected >= 0 ? crops[selected] : null;
  const live = !current
    ? ''
    : noCrop
      ? ` | 整图输出 · ${size.w}×${size.h}`
      : activeCrop
        ? ` | ${activeCrop.x2 - activeCrop.x1}×${activeCrop.y2 - activeCrop.y1} → ${activeCrop.target_w}×${activeCrop.target_h} @${Number(activeCrop.zoom).toFixed(1)}x`
        : '';

  useStatus(
    [
      { kind: 'total', value: images.length },
      {
        kind: 'nav',
        name: '位置',
        value: images.length ? `${index + 1}/${images.length}` : '0/0',
        onClick: () => setFilmstripOpen((value) => !value),
        active: filmstripOpen,
      },
      { kind: 'part', name: '选区', value: crops.length },
      {
        kind: 'resource',
        name: '尺寸',
        value: current && size.w ? `${size.w}×${size.h}` : '-',
      },
    ],
    [
      current ? basename(current.path) : '',
      autoParts.length ? `自动：${autoParts.join(', ')}` : '',
      live.replace(/^\s*\|\s*/, ''),
    ]
      .filter(Boolean)
      .join(' · '),
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <Toolbar>
        <Button onClick={goPrev} title="←/A">
          上一张
        </Button>
        <Button onClick={goNext} title="→/D/空格">
          下一张
        </Button>
        <Button variant="primary" onClick={() => void saveAndNext()} title="S">
          保存
        </Button>
        <ToolbarDivider />
        <Button onClick={addCrop} title="Q">
          新建选区
        </Button>
        <Button onClick={clearCrops} title="C">
          清空选区
        </Button>
        <ToolbarDivider />
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Checkbox checked={noCrop} onCheckedChange={toggleNoCrop} />
          整图输出
        </label>
        {noCrop ? (
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            部位
            <Input
              className="w-20"
              placeholder="head"
              value={partName}
              onChange={(event) => setPartNameBoth(event.target.value)}
            />
          </label>
        ) : null}
      </Toolbar>

      <div className="flex min-h-0 flex-1">
        <div
          ref={wrapRef}
          className="relative flex-1 touch-none overflow-hidden bg-[#222]"
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          <canvas
            ref={canvasRef}
            width={size.w || 1}
            height={size.h || 1}
            className="absolute block touch-none"
            style={{
              width: size.w ? view.width : 0,
              height: size.h ? view.height : 0,
              left: view.left,
              top: view.top,
            }}
            onPointerDown={onPointerDown}
          />
        </div>

        <aside className="flex basis-1/4 min-w-0 max-w-sm shrink-0 flex-col overflow-hidden border-l border-border bg-secondary">
          <h3 className="px-3 pb-1.5 pt-2.5 text-sm font-semibold text-muted-foreground">选区</h3>
          <div className="flex gap-1.5 px-2 pb-2">
            <Input
              className="min-w-0 flex-1"
              placeholder="部位名"
              value={partAdd}
              onChange={(event) => setPartAdd(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  addPart();
                }
              }}
            />
            <Button onClick={addPart}>添加</Button>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
            {noCrop ? (
              <div className="rounded border border-border px-2.5 py-2 text-xs">
                <div>
                  整图输出 · {size.w}×{size.h}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">写入对应部位目录</div>
              </div>
            ) : crops.length === 0 ? (
              <div className="py-1 text-xs text-muted-foreground">暂无选区</div>
            ) : (
              crops.map((box, position) => (
                <div
                  key={position}
                  className={cn(
                    'mb-1.5 flex cursor-pointer items-start gap-2 rounded border px-2.5 py-2 text-xs',
                    position === selected
                      ? 'border-[#00ffff] shadow-[0_0_0_1px_#00ffff]'
                      : 'border-border hover:border-[#4a6a9a]',
                  )}
                  onClick={() => selectCrop(position)}
                >
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-semibold">
                      #{position + 1}
                      {box.name ? ` · ${box.name}` : ''}
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {box.x2 - box.x1}x{box.y2 - box.y1} → {box.target_w}x{box.target_h} @
                      {Number(box.zoom).toFixed(1)}x
                    </div>
                    <CropNameInput value={box.name} onCommit={(text) => renameCrop(position, text)} />
                  </div>
                  <div className="flex shrink-0 flex-col gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-5 px-2 text-xs"
                      title="仅当前图"
                      onClick={(event) => {
                        event.stopPropagation();
                        deleteCrop(position);
                      }}
                    >
                      删除
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-5 px-2 text-xs"
                      title="取消自动应用"
                      onClick={(event) => {
                        event.stopPropagation();
                        deleteCropPermanently(position);
                      }}
                    >
                      永久
                    </Button>
                  </div>
                </div>
              ))
            )}
          </div>

          <div className="border-t border-border px-3 py-2 text-xs text-muted-foreground">
            <div>默认裁切（本地）</div>
            <label className="mt-1.5 flex items-center gap-1.5">
              全局
              <SpecInput
                value={formatDefaultSpec(partDefaults[''] ?? null)}
                placeholder="1536x1536@1.0"
                onCommit={(text) => commitPartDefault('', text)}
              />
            </label>
            {autoParts.map((name) => (
              <label key={name} className="mt-1.5 flex items-center gap-1.5">
                {name}
                <SpecInput
                  value={formatDefaultSpec(partDefaults[name] ?? null)}
                  placeholder="1536x1536@1.0"
                  onCommit={(text) => commitPartDefault(name, text)}
                />
              </label>
            ))}
          </div>

          <div className="border-t border-border px-3 py-2 text-xs leading-relaxed text-muted-foreground">
            S 保存并下张 · 空格/→/D 下张 · ←/A 上张 · Q 新建 · E 删除 · C 清空
          </div>
        </aside>
      </div>

      {filmstripOpen ? (
        <div className="flex shrink-0 items-center gap-1.5 overflow-x-auto border-t border-border bg-secondary px-3 py-2">
          {images.map((item, position) => (
            <img
              key={item.path}
              src={
                item.thumbnail_hash
                  ? api.thumbHashUrl(item.thumbnail_hash)
                  : api.localImageUrl(item.path)
              }
              alt={item.stem}
              loading="lazy"
              title={item.stem}
              style={
                item.width && item.height ? { aspectRatio: `${item.width} / ${item.height}` } : undefined
              }
              className={cn(
                'h-32 w-auto shrink-0 cursor-pointer rounded-sm border-2 bg-[#0a1a30]',
                position === index
                  ? 'border-[#00cc66]'
                  : 'border-transparent hover:border-muted-foreground',
              )}
              onClick={() =>
                void loadIndex(position).catch((error) =>
                  notify(error instanceof Error ? error.message : String(error)),
                )
              }
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
