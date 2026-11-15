/** 蒙版页：Alpha 绘制、模板与 BiRefNet/SAM3 抠图. */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { api, apiPath, request } from '@/lib/api';
import { useApp } from '@/app/AppContext';
import { Button } from '@/components/ui/button';
import { Slider } from '@/components/ui/controls';
import { Input, Select } from '@/components/ui/input';
import { Toolbar, ToolbarDivider } from '@/components/ui/toolbar';
import { useJob, useStatus } from '@/components/ui/status';
import { cn } from '@/lib/utils';
import {
  alphaToPngBlob,
  applyTemplateRegion,
  clearMask,
  createMaskSurface,
  fillMask,
  readAlpha,
  readMaskRegion,
  stampDisc,
  strokeLine,
  type MaskSurface,
  type StencilCache,
} from './mask';
import { buildTemplatePayload, decodeServerTemplate, templateFromRegion } from './templates';
import { slotKey, TemplateDialog, type MergeMode } from './TemplateDialog';
import { MaskGenerateDialog } from './MaskGenerateDialog';
import type {
  BgStyle,
  CanvasView,
  FloatingTemplate,
  LoadedEntry,
  MaskTemplate,
  PaintImageItem,
  PaintInputs,
  Point,
  TemplatesPayload,
} from './types';

/** 邻域预加载半径（前后各取该数量的图片）. */
const PRELOAD_RADIUS = 15;

/** 缩放手柄到可动边的映射. */
const RESIZE_HV: Record<string, { h: 'l' | 'r' | null; v: 't' | 'b' | null }> = {
  nw: { h: 'l', v: 't' },
  n: { h: null, v: 't' },
  ne: { h: 'r', v: 't' },
  w: { h: 'l', v: null },
  e: { h: 'r', v: null },
  sw: { h: 'l', v: 'b' },
  s: { h: null, v: 'b' },
  se: { h: 'r', v: 'b' },
};

/** 缩放手柄样式. */
const HANDLE_STYLE: Record<string, CSSProperties> = {
  nw: { position: 'absolute', left: 0, top: 0, width: 12, height: 12, cursor: 'nwse-resize', zIndex: 3 },
  n: {
    position: 'absolute',
    left: '50%',
    top: 0,
    width: 46,
    height: 10,
    transform: 'translateX(-50%)',
    cursor: 'ns-resize',
    zIndex: 3,
  },
  ne: { position: 'absolute', right: 0, top: 0, width: 12, height: 12, cursor: 'nesw-resize', zIndex: 3 },
  w: {
    position: 'absolute',
    left: 0,
    top: '50%',
    width: 10,
    height: 46,
    transform: 'translateY(-50%)',
    cursor: 'ew-resize',
    zIndex: 3,
  },
  e: {
    position: 'absolute',
    right: 0,
    top: '50%',
    width: 10,
    height: 46,
    transform: 'translateY(-50%)',
    cursor: 'ew-resize',
    zIndex: 3,
  },
  sw: { position: 'absolute', left: 0, bottom: 0, width: 12, height: 12, cursor: 'nesw-resize', zIndex: 3 },
  s: {
    position: 'absolute',
    left: '50%',
    bottom: 0,
    width: 46,
    height: 10,
    transform: 'translateX(-50%)',
    cursor: 'ns-resize',
    zIndex: 3,
  },
  se: {
    position: 'absolute',
    right: 0,
    bottom: 0,
    width: 12,
    height: 12,
    cursor: 'nwse-resize',
    zIndex: 3,
    background: 'linear-gradient(135deg, transparent 40%, #f33 41%, #f33 59%, transparent 60%)',
  },
};

/** 未确认模板的拖拽状态. */
interface FloatDrag {
  mode: 'move' | 'resize' | 'anchor';
  anchor: number;
  sx: number;
  sy: number;
  x0: number;
  y0: number;
  w0: number;
  h0: number;
  h: 'l' | 'r' | null;
  v: 't' | 'b' | null;
}

/** 绘制进行中的状态. */
interface StrokeState {
  mode: 'none' | 'brush' | 'line' | 'capture';
  value: number;
  start: Point | null;
  last: Point | null;
}

/** 蒙版生成任务的快照. */
interface JobSnapshot {
  status?: string;
  done?: number;
  total?: number;
  failed?: number;
  message?: string;
}

/**
 * 解析 Alpha 输入框的数值，空串或非法输入回退到该字段的默认值.
 *
 * Args:
 *   value (string): 输入框文本.
 *   fallback (number): 空串或非法输入时的回退值.
 *
 * Returns:
 *   number: 限制在 0-255 的整数.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:PaintPage` 左右键与默认 Alpha 输入框.
 */
function alphaInput(value: string, fallback: number): number {
  const parsed = Number.parseInt(value, 10);
  return Number.isNaN(parsed) ? fallback : clamp(0, 255, parsed);
}

/**
 * 把数值限制在闭区间内.
 *
 * Args:
 *   min (number): 下界.
 *   max (number): 上界.
 *   value (number): 待限制的值.
 *
 * Returns:
 *   number: 限制后的值.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx` 各输入与缩放路径.
 */
function clamp(min: number, max: number, value: number): number {
  return Math.min(max, Math.max(min, value));
}

/**
 * 按图像尺寸与百分比计算笔刷半径.
 *
 * Args:
 *   w (number): 图像宽.
 *   h (number): 图像高.
 *   pct (number): 笔刷百分比（相对 sqrt(w*h)）.
 *
 * Returns:
 *   number: 半径（图像像素），最小为 2.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:PaintPage`
 */
function brushRadiusFrom(w: number, h: number, pct: number): number {
  if (!w || !h) return 2;
  return Math.max(2, Math.round((Math.sqrt(w * h) * pct) / 100));
}

/**
 * 把任意值解析为有限数值.
 *
 * Args:
 *   value (unknown): 候选值.
 *   fallback (number): 非法时的默认值.
 *
 * Returns:
 *   number: 解析结果.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:readPaintSettings`
 */
function numberOr(value: unknown, fallback: number): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

/**
 * 从设置中的 `paint` 键读取输入参数.
 *
 * Args:
 *   value (unknown): `app.settings.paint`.
 *
 * Returns:
 *   PaintInputs: 补全默认值后的输入参数.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:PaintPage` 初始状态.
 */
function readPaintSettings(value: unknown): PaintInputs {
  const source = value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
  const style = source.bg_style;
  const bgStyle: BgStyle =
    style === 'white' || style === 'black' || style === 'custom' || style === 'checker'
      ? style
      : 'checker';
  return {
    brushPct: clamp(0.5, 20, numberOr(source.brush_pct, 3)),
    leftAlpha: clamp(0, 255, Math.round(numberOr(source.left_alpha, 0))),
    rightAlpha: clamp(0, 255, Math.round(numberOr(source.right_alpha, 255))),
    defaultAlpha: clamp(0, 255, Math.round(numberOr(source.default_alpha, 255))),
    bgStyle,
    bgColor: typeof source.bg_color === 'string' ? source.bg_color : '#ffffff',
  };
}

/**
 * 把输入参数序列化为设置值.
 *
 * Args:
 *   inputs (PaintInputs): 当前输入参数.
 *
 * Returns:
 *   Record<string, unknown>: 写入 `paint` 键的对象.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:patchInputs`
 */
function settingsPayload(inputs: PaintInputs): Record<string, unknown> {
  return {
    default_alpha: inputs.defaultAlpha,
    brush_pct: inputs.brushPct,
    left_alpha: inputs.leftAlpha,
    right_alpha: inputs.rightAlpha,
    bg_style: inputs.bgStyle,
    bg_color: inputs.bgColor,
  };
}

/**
 * 计算画布背景 CSS.
 *
 * Args:
 *   inputs (PaintInputs): 当前输入参数.
 *
 * Returns:
 *   string: 棋盘格或纯色背景.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:PaintPage`
 */
function backgroundCss(inputs: PaintInputs): string {
  if (inputs.bgStyle === 'checker') {
    const factor = inputs.defaultAlpha / 255;
    const v0 = Math.round(204 * factor);
    const v1 = Math.round(153 * factor);
    return `repeating-conic-gradient(rgb(${v0},${v0},${v0}) 0 25%, rgb(${v1},${v1},${v1}) 0 50%) 0 0 / 24px 24px`;
  }
  if (inputs.bgStyle === 'white') return '#ffffff';
  if (inputs.bgStyle === 'black') return '#000000';
  return inputs.bgColor;
}

/**
 * 请求二进制资源并返回 Blob.
 *
 * Args:
 *   url (string): 资源地址.
 *
 * Returns:
 *   Promise<Blob>: 响应体.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:loadEntry`
 *   - `frontend/src/pages/paint/PaintPage.tsx:reloadCurrentMask`
 */
async function fetchBlob(url: string): Promise<Blob> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`加载失败: ${response.status}`);
  return response.blob();
}

/**
 * 把 Blob 解码为 Image.
 *
 * Args:
 *   blob (Blob): 图像字节.
 *
 * Returns:
 *   Promise<HTMLImageElement>: 解码完成的图像.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:loadEntry`
 *   - `frontend/src/pages/paint/PaintPage.tsx:reloadCurrentMask`
 *   - `frontend/src/pages/paint/PaintPage.tsx:saveMask`
 */
async function decodeBlob(blob: Blob): Promise<HTMLImageElement> {
  const url = URL.createObjectURL(blob);
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    return image;
  } finally {
    URL.revokeObjectURL(url);
  }
}

/**
 * 计算按住 Shift 时以实例中心为锚点的缩放几何.
 *
 * Args:
 *   drag (FloatDrag): 拖拽起始状态.
 *   ox (number): 图像域水平位移.
 *   oy (number): 图像域垂直位移.
 *
 * Returns:
 *   { rx: number; ry: number; rw: number; rh: number }: 新几何.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:handleFloatingPointerMove`
 */
function shiftResize(
  drag: FloatDrag,
  ox: number,
  oy: number,
): { rx: number; ry: number; rw: number; rh: number } {
  const ratio = drag.w0 / drag.h0;
  const cx = drag.x0 + drag.w0 / 2;
  const cy = drag.y0 + drag.h0 / 2;
  let mx: number | null = null;
  let my: number | null = null;
  if (drag.h === 'l') mx = drag.x0 + ox;
  else if (drag.h === 'r') mx = drag.x0 + drag.w0 + ox;
  if (drag.v === 't') my = drag.y0 + oy;
  else if (drag.v === 'b') my = drag.y0 + drag.h0 + oy;
  let w: number;
  let h: number;
  if (mx !== null && my !== null) {
    const dx = Math.abs(mx - cx);
    const dy = Math.abs(my - cy);
    if (dx * ratio > dy) {
      w = Math.max(4, 2 * dx);
      h = w / ratio;
    } else {
      h = Math.max(4, 2 * dy);
      w = h * ratio;
    }
  } else if (my !== null) {
    h = Math.max(4, 2 * Math.abs(my - cy));
    w = drag.w0;
  } else {
    w = Math.max(4, 2 * Math.abs((mx ?? cx) - cx));
    h = drag.h0;
  }
  return { rx: cx - w / 2, ry: cy - h / 2, rw: w, rh: h };
}

/**
 * 蒙版页：绘制、撤销重做、模板、缩放平移与蒙版生成.
 *
 * Args:
 *   无；数据集、目录与设置由 ``useApp`` 提供.
 *
 * Returns:
 *   JSX.Element: 蒙版工作区界面.
 *
 * Callers:
 *   - 前端页面路由表挂载.
 */
export function PaintPage() {
  const app = useApp();
  const { notify, saveSettings } = app;
  const sourceDir = app.dirs[0] ?? 'main';

  const [images, setImages] = useState<PaintImageItem[]>([]);
  const itemsRef = useRef<PaintImageItem[]>([]);
  const [index, setIndexState] = useState(0);
  const indexRef = useRef(0);
  const [inputs, setInputs] = useState<PaintInputs>(() => readPaintSettings(app.settings.paint));
  const inputsRef = useRef(inputs);
  const [size, setSizeState] = useState({ w: 0, h: 0 });
  const sizeRef = useRef(size);
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const viewportRef = useRef(viewport);
  const [cacheVersion, setCacheVersion] = useState(0);
  const [imageTick, setImageTick] = useState(0);
  const [filmstripOpen, setFilmstripOpen] = useState(false);
  const [templates, setTemplates] = useState<Array<MaskTemplate | null>>(() =>
    new Array(10).fill(null),
  );
  const templatesRef = useRef(templates);
  const [mergeMode, setMergeModeState] = useState<MergeMode>('min');
  const mergeModeRef = useRef<MergeMode>(mergeMode);
  const [floating, setFloatingState] = useState<FloatingTemplate | null>(null);
  const floatingRef = useRef<FloatingTemplate | null>(floating);
  const [captureMode, setCaptureModeState] = useState(false);
  const captureModeRef = useRef(false);
  const captureStartRef = useRef<Point | null>(null);
  const [generateOpen, setGenerateOpen] = useState(false);
  const [templateOpen, setTemplateOpen] = useState(false);
  const [progress, setProgress] = useState<{ pct: number; text: string } | null>(null);
  const jobStopRef = useRef<(() => void) | null>(null);
  const jobTimerRef = useRef<number | undefined>();

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const displayCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const composeRef = useRef<HTMLCanvasElement | null>(null);
  const baseImageRef = useRef<HTMLImageElement | null>(null);
  const maskSurfaceRef = useRef<MaskSurface | null>(null);
  const stencilRef = useRef<StencilCache>(new Map());
  const cacheRef = useRef<Map<string, LoadedEntry>>(new Map());
  const pendingRef = useRef<Map<string, Promise<LoadedEntry>>>(new Map());
  const loadTokenRef = useRef(0);
  const listTokenRef = useRef(0);
  const preloadSeqRef = useRef(0);
  const preloadErrorRef = useRef(false);
  const modifiedRef = useRef(false);
  const renderScheduledRef = useRef(false);
  const rafRef = useRef<number | null>(null);
  const redModeRef = useRef(false);
  const hoverRef = useRef<Point | null>(null);
  const brushPreviewRef = useRef<HTMLDivElement | null>(null);
  const lineSvgRef = useRef<SVGSVGElement | null>(null);
  const lineRef = useRef<SVGLineElement | null>(null);
  const captureRectRef = useRef<HTMLDivElement | null>(null);
  const strokeRef = useRef<StrokeState>({ mode: 'none', value: 0, start: null, last: null });
  const strokeMoveRef = useRef<((event: PointerEvent) => void) | null>(null);
  const strokeUpRef = useRef<((event: PointerEvent) => void) | null>(null);
  const dragRef = useRef<FloatDrag | null>(null);

  /**
   * 同步图片列表到引用与状态.
   *
   * Args:
   *   next (PaintImageItem[]): 新的图片列表.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 列表加载、保存与模板/生成任务完成路径.
   */
  const applyItems = useCallback((next: PaintImageItem[]) => {
    itemsRef.current = next;
    setImages(next);
  }, []);

  /**
   * 同步当前索引到引用与状态.
   *
   * Args:
   *   next (number): 新的索引.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:loadIndex`
   */
  const applyIndex = useCallback((next: number) => {
    indexRef.current = next;
    setIndexState(next);
  }, []);

  /**
   * 同步当前图像工作尺寸到引用与状态.
   *
   * Args:
   *   next ({ w: number; h: number }): 新的尺寸.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:loadIndex`
   */
  const applySize = useCallback((next: { w: number; h: number }) => {
    sizeRef.current = next;
    setSizeState(next);
  }, []);

  /**
   * 同步未确认模板实例到引用与状态.
   *
   * Args:
   *   next (FloatingTemplate | null): 新实例或 null.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 模板放置、拖动、锚点与应用路径.
   */
  const applyFloatingState = useCallback((next: FloatingTemplate | null) => {
    floatingRef.current = next;
    setFloatingState(next);
  }, []);

  /**
   * 同步模板槽位到引用与状态.
   *
   * Args:
   *   next (Array<MaskTemplate | null>): 新的 10 个槽位.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 模板加载、捕获、删除与锚点同步路径.
   */
  const applyTemplates = useCallback((next: Array<MaskTemplate | null>) => {
    templatesRef.current = next;
    setTemplates(next);
  }, []);

  /**
   * 同步框选模式到引用与状态.
   *
   * Args:
   *   next (boolean): 是否处于框选模式.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 模板框选开始/结束/取消路径.
   */
  const setCaptureMode = useCallback((next: boolean) => {
    captureModeRef.current = next;
    setCaptureModeState(next);
  }, []);

  /**
   * 同步模板合并方式.
   *
   * Args:
   *   next (MergeMode): min 或 max.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 模板对话框合并方式选择.
   */
  const setMergeMode = useCallback((next: MergeMode) => {
    mergeModeRef.current = next;
    setMergeModeState(next);
  }, []);

  /**
   * 合并输入参数并按需持久化到设置键 `paint`.
   *
   * Args:
   *   patch (Partial<PaintInputs>): 待合并字段.
   *   persist (boolean): 是否立即写回设置.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 工具栏各输入控件与滚轮笔刷调节.
   */
  const patchInputs = useCallback(
    (patch: Partial<PaintInputs>, persist = false) => {
      const next = { ...inputsRef.current, ...patch };
      inputsRef.current = next;
      setInputs(next);
      if (persist) saveSettings({ paint: settingsPayload(next) });
    },
    [saveSettings],
  );

  /**
   * 把异常转为可读文本并提示.
   *
   * Args:
   *   error (unknown): 捕获的异常.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 加载、保存与任务完成路径的错误处理.
   */
  const reportError = useCallback(
    (error: unknown) => notify(error instanceof Error ? error.message : String(error)),
    [notify],
  );

  /**
   * 取当前图片列表项.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   PaintImageItem | null: 当前项；列表为空时为 null.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx` 保存、切图与任务完成路径.
   */
  const currentItem = (): PaintImageItem | null => itemsRef.current[indexRef.current] ?? null;

  const view = useMemo<CanvasView>(() => {
    const { w, h } = size;
    if (!w || !h || !viewport.width || !viewport.height) {
      return { scale: 1, width: 0, height: 0, left: 0, top: 0 };
    }
    const scale = Math.min(viewport.width / w, viewport.height / h);
    const width = w * scale;
    const height = h * scale;
    return {
      scale,
      width,
      height,
      left: (viewport.width - width) / 2,
      top: (viewport.height - height) / 2,
    };
  }, [size, viewport]);
  const viewRef = useRef(view);
  viewRef.current = view;

  const brushRadius = brushRadiusFrom(size.w, size.h, inputs.brushPct);
  const brushRadiusRef = useRef(brushRadius);
  brushRadiusRef.current = brushRadius;

  /**
   * 合成 RGB 与蒙版并按当前缩放平移绘制到视口画布；红色模式覆盖图片区域.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:scheduleRender`
   */
  const drawDisplay = useCallback(() => {
    renderScheduledRef.current = false;
    const canvas = displayCanvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const viewportSize = viewportRef.current;
    const currentView = viewRef.current;
    const ratio = window.devicePixelRatio || 1;
    const vw = Math.max(1, viewportSize.width);
    const vh = Math.max(1, viewportSize.height);
    const pixelWidth = Math.round(vw * ratio);
    const pixelHeight = Math.round(vh * ratio);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, vw, vh);
    const { w, h } = sizeRef.current;
    const base = baseImageRef.current;
    const mask = maskSurfaceRef.current;
    if (!w || !h || !base || !mask) return;
    let compose = composeRef.current;
    if (!compose || compose.width !== w || compose.height !== h) {
      compose = document.createElement('canvas');
      compose.width = w;
      compose.height = h;
      composeRef.current = compose;
    }
    const composeCtx = compose.getContext('2d');
    if (!composeCtx) return;
    composeCtx.clearRect(0, 0, w, h);
    composeCtx.globalCompositeOperation = 'source-over';
    if (redModeRef.current) {
      composeCtx.fillStyle = 'rgba(255, 0, 0, 0.5)';
      composeCtx.fillRect(0, 0, w, h);
      composeCtx.globalCompositeOperation = 'destination-out';
      composeCtx.drawImage(mask.canvas, 0, 0, w, h);
      composeCtx.globalCompositeOperation = 'source-over';
      ctx.drawImage(base, currentView.left, currentView.top, currentView.width, currentView.height);
    } else {
      composeCtx.drawImage(base, 0, 0, w, h);
      composeCtx.globalCompositeOperation = 'destination-in';
      composeCtx.drawImage(mask.canvas, 0, 0, w, h);
      composeCtx.globalCompositeOperation = 'source-over';
    }
    ctx.drawImage(
      compose,
      currentView.left,
      currentView.top,
      currentView.width,
      currentView.height,
    );
  }, []);

  /**
   * 在下一帧调度一次显示合成.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 蒙版改动、切图与模板操作路径.
   */
  const scheduleRender = useCallback(() => {
    if (renderScheduledRef.current) return;
    renderScheduledRef.current = true;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      drawDisplay();
    });
  }, [drawDisplay]);

  /**
   * 更新笔刷圆形预览.
   *
   * Args:
   *   point (Point | null): 图像坐标；null 时隐藏.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 指针移动、缩放与笔刷尺寸变化路径.
   */
  const updateBrushPreview = useCallback((point: Point | null) => {
    const element = brushPreviewRef.current;
    if (!element) return;
    const currentView = viewRef.current;
    if (!point || !sizeRef.current.w || !sizeRef.current.h) {
      element.style.display = 'none';
      return;
    }
    const radius = brushRadiusRef.current * currentView.scale;
    if (radius < 2) {
      element.style.display = 'none';
      return;
    }
    element.style.display = 'block';
    element.style.width = `${radius * 2}px`;
    element.style.height = `${radius * 2}px`;
    element.style.left = `${currentView.left + (point.x + 0.5) * currentView.scale - radius}px`;
    element.style.top = `${currentView.top + (point.y + 0.5) * currentView.scale - radius}px`;
  }, []);

  /**
   * 更新直线模式的红色橡皮筋预览.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 直线拖拽、缩放与笔刷尺寸变化路径.
   */
  const updateLinePreview = useCallback(() => {
    const svg = lineSvgRef.current;
    const line = lineRef.current;
    const state = strokeRef.current;
    if (!svg || !line) return;
    if (state.mode !== 'line' || !state.start || !state.last) {
      svg.style.display = 'none';
      return;
    }
    const currentView = viewRef.current;
    svg.style.display = 'block';
    line.setAttribute('x1', String(currentView.left + (state.start.x + 0.5) * currentView.scale));
    line.setAttribute('y1', String(currentView.top + (state.start.y + 0.5) * currentView.scale));
    line.setAttribute('x2', String(currentView.left + (state.last.x + 0.5) * currentView.scale));
    line.setAttribute('y2', String(currentView.top + (state.last.y + 0.5) * currentView.scale));
    line.setAttribute(
      'stroke-width',
      String(Math.max(1, brushRadiusRef.current * 2 * currentView.scale)),
    );
  }, []);

  /**
   * 更新框选选区矩形的显示.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 框选拖拽路径.
   */
  const updateCaptureRect = useCallback(() => {
    const element = captureRectRef.current;
    const state = strokeRef.current;
    if (!element) return;
    const start = captureStartRef.current;
    if (!captureModeRef.current || !start || !state.last) {
      element.style.display = 'none';
      return;
    }
    const currentView = viewRef.current;
    const x0 = Math.min(start.x, state.last.x);
    const y0 = Math.min(start.y, state.last.y);
    const x1 = Math.max(start.x, state.last.x);
    const y1 = Math.max(start.y, state.last.y);
    element.style.display = 'block';
    element.style.left = `${currentView.left + x0 * currentView.scale}px`;
    element.style.top = `${currentView.top + y0 * currentView.scale}px`;
    element.style.width = `${(x1 - x0 + 1) * currentView.scale}px`;
    element.style.height = `${(y1 - y0 + 1) * currentView.scale}px`;
  }, []);

  /**
   * 把视口坐标换算为图像像素坐标.
   *
   * Args:
   *   clientX (number): 视口横坐标.
   *   clientY (number): 视口纵坐标.
   *   clampToImage (boolean): 是否在超出画布时钳位到边缘.
   *
   * Returns:
   *   Point | null: 图像坐标；未加载或越界且不钳位时为 null.
   *
   * Callers:
   *   - 指针按下、移动、框选与模板锚点路径.
   */
  const origFromClient = useCallback(
    (clientX: number, clientY: number, clampToImage: boolean): Point | null => {
      const canvas = displayCanvasRef.current;
      const { w, h } = sizeRef.current;
      if (!canvas || !w || !h) return null;
      const view = viewRef.current;
      if (!view.scale) return null;
      const rect = canvas.getBoundingClientRect();
      const left = rect.left + view.left;
      const top = rect.top + view.top;
      const right = left + view.width;
      const bottom = top + view.height;
      if (!clampToImage && (clientX < left || clientX > right || clientY < top || clientY > bottom)) {
        return null;
      }
      const x = (clientX - left) / view.scale;
      const y = (clientY - top) / view.scale;
      return {
        x: clamp(0, w - 1, Math.floor(x)),
        y: clamp(0, h - 1, Math.floor(y)),
      };
    },
    [],
  );

  /**
   * 在图像坐标落一个圆盘笔刷.
   *
   * Args:
   *   point (Point): 图像坐标.
   *   value (number): 目标 Alpha.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:startStroke`
   */
  const paintDisc = useCallback(
    (point: Point, value: number) => {
      const surface = maskSurfaceRef.current;
      if (!surface) return;
      stampDisc(surface.ctx, stencilRef.current, point.x, point.y, brushRadiusRef.current, value);
      scheduleRender();
    },
    [scheduleRender],
  );

  /**
   * 沿两点绘制连续笔触.
   *
   * Args:
   *   from (Point): 起点.
   *   to (Point): 终点.
   *   value (number): 目标 Alpha.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:finishStroke`
   *   - 绘制拖拽移动路径.
   */
  const paintSegment = useCallback(
    (from: Point, to: Point, value: number) => {
      const surface = maskSurfaceRef.current;
      if (!surface) return;
      strokeLine(
        surface.ctx,
        stencilRef.current,
        from.x,
        from.y,
        to.x,
        to.y,
        brushRadiusRef.current,
        value,
      );
      scheduleRender();
    },
    [scheduleRender],
  );

  /**
   * 清空整幅蒙版为全透明.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 工具栏「全透明」按钮.
   */
  const clearAll = useCallback(() => {
    const surface = maskSurfaceRef.current;
    const { w, h } = sizeRef.current;
    if (!surface || !w || !h) return;
    clearMask(surface.ctx, w, h);
    modifiedRef.current = true;
    scheduleRender();
  }, [scheduleRender]);

  /**
   * 把整幅蒙版填充为指定 Alpha.
   *
   * Args:
   *   value (number): 目标 Alpha.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 工具栏「全不透明」按钮.
   */
  const fillAll = useCallback(
    (value: number) => {
      const surface = maskSurfaceRef.current;
      const { w, h } = sizeRef.current;
      if (!surface || !w || !h) return;
      fillMask(surface.ctx, w, h, value);
      modifiedRef.current = true;
      scheduleRender();
    },
    [scheduleRender],
  );

  /**
   * 恢复为默认 Alpha 的整幅蒙版.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 工具栏「恢复默认」按钮.
   */
  const restoreDefault = useCallback(() => {
    const surface = maskSurfaceRef.current;
    const { w, h } = sizeRef.current;
    if (!surface || !w || !h) return;
    fillMask(surface.ctx, w, h, inputsRef.current.defaultAlpha);
    modifiedRef.current = true;
    scheduleRender();
  }, [scheduleRender]);

  /**
   * 读取当前鼠标按键对应的绘制 Alpha.
   *
   * Args:
   *   button (number): 0 为左键，其余为右键.
   *
   * Returns:
   *   number: 左/右键 Alpha（0-255）.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:startStroke`
   */
  const alphaFor = (button: number): number =>
    button === 0 ? inputsRef.current.leftAlpha : inputsRef.current.rightAlpha;

  /**
   * 保存当前蒙版到 `mask/`.
   *
   * Args:
   *   force (boolean): 是否忽略脏检查强制保存.
   *
   * Returns:
   *   Promise<void>: 保存完成.
   *
   * Callers:
   *   - 工具栏「保存」、切图与快捷键.
   */
  const saveMask = useCallback(
    async (force: boolean) => {
      const item = currentItem();
      const surface = maskSurfaceRef.current;
      const { w, h } = sizeRef.current;
      if (!item || !surface || !w || !h) return;
      if (!force && !modifiedRef.current && item.has_mask) return;
      const alpha = readAlpha(surface.ctx, w, h);
      const blob = await alphaToPngBlob(alpha, w, h);
      const response = await fetch(
        `${apiPath('/paint/upload-mask')}?path=${encodeURIComponent(item.path)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'image/png' },
          body: blob,
        },
      );
      if (!response.ok) {
        throw new Error(`保存失败: ${response.status} ${await response.text()}`);
      }
      const result = (await response.json()) as { saved: string; size: number[] };
      if (currentItem()?.path === item.path) modifiedRef.current = false;
      applyItems(
        itemsRef.current.map((entry) =>
          entry.path === item.path ? { ...entry, has_mask: true } : entry,
        ),
      );
      const cached = cacheRef.current.get(item.path);
      if (cached) cached.mask = await decodeBlob(blob);
      notify(`已保存: ${result.saved}`);
    },
    [applyItems, notify],
  );

  /**
   * 只重新加载当前图片的蒙版并重绘.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   Promise<void>: 重载完成.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:handleMaskDone`
   */
  const reloadCurrentMask = useCallback(async () => {
    const item = currentItem();
    const surface = maskSurfaceRef.current;
    const { w, h } = sizeRef.current;
    if (!item || !surface || !item.has_mask || !w || !h) return;
    const blob = await fetchBlob(
      `${apiPath('/paint/mask')}?path=${encodeURIComponent(item.path)}`,
    );
    const image = await decodeBlob(blob);
    surface.ctx.clearRect(0, 0, w, h);
    surface.ctx.drawImage(image, 0, 0, w, h);
    const cached = cacheRef.current.get(item.path);
    if (cached) {
      cached.mask = image;
      cached.w = w;
      cached.h = h;
    }
    modifiedRef.current = false;
    scheduleRender();
    setCacheVersion((version) => version + 1);
  }, [scheduleRender]);

  /**
   * 蒙版生成完成后的就地刷新.
   *
   * Args:
   *   batch (boolean): 是否为批量任务.
   *
   * Returns:
   *   Promise<void>: 刷新完成.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:watchMaskJob`
   */
  const handleMaskDone = useCallback(
    async (batch: boolean) => {
      if (batch) {
        cacheRef.current.clear();
        setCacheVersion((version) => version + 1);
        const path = currentItem()?.path ?? null;
        const list = await request<PaintImageItem[]>(
          `${apiPath('/paint/images')}?dir=${encodeURIComponent(sourceDir)}`,
        );
        applyItems(list);
        if (!path) return;
        const nextIndex = list.findIndex((entry) => entry.path === path);
        if (nextIndex < 0) return;
        applyIndex(nextIndex);
        await reloadCurrentMask();
        return;
      }
      const item = currentItem();
      if (!item) return;
      applyItems(
        itemsRef.current.map((entry) =>
          entry.path === item.path ? { ...entry, has_mask: true } : entry,
        ),
      );
      await reloadCurrentMask();
    },
    [applyIndex, applyItems, reloadCurrentMask, sourceDir],
  );

  /**
   * 订阅蒙版生成任务的进度并对终态做刷新.
   *
   * Args:
   *   jobId (string): 任务标识.
   *   batch (boolean): 是否为批量任务.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/MaskGenerateDialog.tsx` 的启动回调.
   */
  const watchMaskJob = useCallback(
    (jobId: string, batch: boolean) => {
      jobStopRef.current?.();
      if (jobTimerRef.current) window.clearTimeout(jobTimerRef.current);
      setProgress({ pct: 0, text: '准备中' });
      jobStopRef.current = api.watchJob(jobId, (snapshot) => {
        const snap = snapshot as unknown as JobSnapshot;
        const total = snap.total ?? 0;
        const done = snap.done ?? 0;
        if (total > 0) {
          setProgress({
            pct: Math.round((done / total) * 100),
            text: `${done}/${total}${snap.failed ? ` 失败${snap.failed}` : ''}`,
          });
        }
        if (snap.status === 'done') {
          setProgress({ pct: 100, text: `完成 ${done}/${total}` });
          jobTimerRef.current = window.setTimeout(() => setProgress(null), 1500);
          void handleMaskDone(batch).catch(reportError);
          return;
        }
        if (snap.status === 'error') {
          setProgress({ pct: 0, text: `失败: ${snap.message ?? '未知错误'}` });
          jobTimerRef.current = window.setTimeout(() => setProgress(null), 5000);
          return;
        }
        if (snap.status === 'cancelled') {
          jobTimerRef.current = window.setTimeout(() => setProgress(null), 3000);
        }
      });
    },
    [handleMaskDone, reportError],
  );

  /**
   * 加载单张图片的视觉与蒙版.
   *
   * Args:
   *   item (PaintImageItem): 列表项.
   *
   * Returns:
   *   Promise<LoadedEntry>: 解码后的视觉、蒙版与工作尺寸.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:preloadEntry`
   */
  const loadEntry = useCallback(async (item: PaintImageItem): Promise<LoadedEntry> => {
    const visual = fetchBlob(`${apiPath('/paint/image')}?path=${encodeURIComponent(item.path)}`);
    const mask = item.has_mask
      ? fetchBlob(`${apiPath('/paint/mask')}?path=${encodeURIComponent(item.path)}`)
      : Promise.resolve(null);
    const [visualBlob, maskBlob] = await Promise.all([visual, mask]);
    const image = await decodeBlob(visualBlob);
    const maskImage = maskBlob ? await decodeBlob(maskBlob) : null;
    return {
      image,
      mask: maskImage,
      w: Math.max(1, item.width),
      h: Math.max(1, item.height),
    };
  }, []);

  /**
   * 预加载单张图片，命中缓存或进行中的请求时直接复用.
   *
   * Args:
   *   item (PaintImageItem): 列表项.
   *
   * Returns:
   *   Promise<LoadedEntry>: 解码后的条目.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:loadIndex`
   *   - `frontend/src/pages/paint/PaintPage.tsx:preloadNeighbors`
   */
  const preloadEntry = useCallback(
    async (item: PaintImageItem): Promise<LoadedEntry> => {
      const cached = cacheRef.current.get(item.path);
      if (cached) return cached;
      const pending = pendingRef.current.get(item.path);
      if (pending) return pending;
      const promise = loadEntry(item)
        .then((entry) => {
          cacheRef.current.set(item.path, entry);
          pendingRef.current.delete(item.path);
          setCacheVersion((version) => version + 1);
          return entry;
        })
        .catch((error) => {
          pendingRef.current.delete(item.path);
          throw error;
        });
      pendingRef.current.set(item.path, promise);
      return promise;
    },
    [loadEntry],
  );

  /**
   * 后台预加载当前索引前后邻域的图片.
   *
   * Args:
   *   index (number): 当前索引.
   *
   * Returns:
   *   Promise<void>: 预加载结束.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:loadIndex`
   */
  const preloadNeighbors = useCallback(
    async (index: number) => {
      const seq = preloadSeqRef.current + 1;
      preloadSeqRef.current = seq;
      for (let delta = 1; delta <= PRELOAD_RADIUS; delta += 1) {
        if (seq !== preloadSeqRef.current) return;
        const forward = index + delta;
        if (forward < itemsRef.current.length) {
          try {
            await preloadEntry(itemsRef.current[forward]);
          } catch (error) {
            if (!preloadErrorRef.current) {
              preloadErrorRef.current = true;
              reportError(error);
            }
          }
        }
        if (seq !== preloadSeqRef.current) return;
        const backward = index - delta;
        if (backward >= 0) {
          try {
            await preloadEntry(itemsRef.current[backward]);
          } catch (error) {
            if (!preloadErrorRef.current) {
              preloadErrorRef.current = true;
              reportError(error);
            }
          }
        }
      }
    },
    [preloadEntry, reportError],
  );

  /**
   * 卸载绘制期间的窗口级指针跟踪.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 切图、框选结束与组件卸载路径.
   */
  const stopStrokeTracking = useCallback(() => {
    if (strokeMoveRef.current) {
      window.removeEventListener('pointermove', strokeMoveRef.current);
      strokeMoveRef.current = null;
    }
    if (strokeUpRef.current) {
      window.removeEventListener('pointerup', strokeUpRef.current);
      window.removeEventListener('pointercancel', strokeUpRef.current);
      strokeUpRef.current = null;
    }
  }, []);

  /**
   * 终止进行中的绘制、框选与模板实例.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:loadIndex`
   *   - 组件卸载与目录切换.
   */
  const cancelInteraction = useCallback(() => {
    stopStrokeTracking();
    strokeRef.current = { mode: 'none', value: 0, start: null, last: null };
    captureStartRef.current = null;
    setCaptureMode(false);
    applyFloatingState(null);
    dragRef.current = null;
    hoverRef.current = null;
    if (brushPreviewRef.current) brushPreviewRef.current.style.display = 'none';
    if (lineSvgRef.current) lineSvgRef.current.style.display = 'none';
    if (captureRectRef.current) captureRectRef.current.style.display = 'none';
  }, [applyFloatingState, setCaptureMode, stopStrokeTracking]);

  /**
   * 加载指定索引的图片并初始化蒙版画布.
   *
   * Args:
   *   target (number): 目标索引（自动钳位）.
   *
   * Returns:
   *   Promise<void>: 加载完成.
   *
   * Callers:
   *   - 初始列表加载、上一张/下一张、跳过与预览条点击.
   */
  const loadIndex = useCallback(
    async (target: number) => {
      const list = itemsRef.current;
      if (!list.length) return;
      const nextIndex = clamp(0, list.length - 1, target);
      const token = loadTokenRef.current + 1;
      loadTokenRef.current = token;
      cancelInteraction();
      const item = list[nextIndex];
      let entry: LoadedEntry;
      try {
        entry = await preloadEntry(item);
      } catch (error) {
        if (token === loadTokenRef.current) reportError(error);
        return;
      }
      if (token !== loadTokenRef.current) return;
      const surface = createMaskSurface(entry.w, entry.h);
      if (entry.mask) surface.ctx.drawImage(entry.mask, 0, 0, entry.w, entry.h);
      else fillMask(surface.ctx, entry.w, entry.h, inputsRef.current.defaultAlpha);
      maskSurfaceRef.current = surface;
      baseImageRef.current = entry.image;
      applySize({ w: entry.w, h: entry.h });
      applyIndex(nextIndex);
      modifiedRef.current = false;
      setImageTick((tick) => tick + 1);
      scheduleRender();
      void preloadNeighbors(nextIndex);
    },
    [
      applyIndex,
      applySize,
      cancelInteraction,
      preloadEntry,
      preloadNeighbors,
      reportError,
      scheduleRender,
    ],
  );

  /**
   * 拉取当前目录的图片列表并加载第一张.
   *
   * Args:
   *   dir (string): 子目录名.
   *
   * Returns:
   *   Promise<void>: 加载完成.
   *
   * Callers:
   *   - 组件目录副作用.
   */
  const loadImageList = useCallback(
    async (dir: string) => {
      const token = listTokenRef.current + 1;
      listTokenRef.current = token;
      const list = await request<PaintImageItem[]>(
        `${apiPath('/paint/images')}?dir=${encodeURIComponent(dir)}`,
      );
      if (token !== listTokenRef.current) return;
      cacheRef.current.clear();
      pendingRef.current.clear();
      preloadErrorRef.current = false;
      setCacheVersion((version) => version + 1);
      applyItems(list);
      if (!list.length) {
        maskSurfaceRef.current = null;
        baseImageRef.current = null;
        applySize({ w: 0, h: 0 });
        setImageTick((tick) => tick + 1);
        notify('没有图片');
        return;
      }
      await loadIndex(0);
    },
    [applyItems, applySize, loadIndex, notify],
  );

  /**
   * 保存当前帧后切换到上一张.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   Promise<void>: 切换完成.
   *
   * Callers:
   *   - 工具栏「上一张」与快捷键 ←/A.
   */
  const goPrev = useCallback(async () => {
    if (!itemsRef.current.length) return;
    const current = indexRef.current;
    await saveMask(false).catch(reportError);
    if (current > 0) await loadIndex(current - 1);
  }, [loadIndex, reportError, saveMask]);

  /**
   * 保存当前帧后切换到下一张.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   Promise<void>: 切换完成.
   *
   * Callers:
   *   - 工具栏「下一张」与快捷键 →/D.
   */
  const goNext = useCallback(async () => {
    const list = itemsRef.current;
    if (!list.length) return;
    const current = indexRef.current;
    await saveMask(false).catch(reportError);
    if (current < list.length - 1) await loadIndex(current + 1);
  }, [loadIndex, reportError, saveMask]);

  /**
   * 不保存当前帧直接跳到下一张.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   Promise<void>: 切换完成.
   *
   * Callers:
   *   - 工具栏「跳过」与快捷键 X.
   */
  const goSkip = useCallback(async () => {
    const list = itemsRef.current;
    if (!list.length) return;
    const current = indexRef.current;
    if (current < list.length - 1) await loadIndex(current + 1);
  }, [loadIndex]);

  /**
   * 把模板槽位全量写入服务端.
   *
   * Args:
   *   slots (Array<MaskTemplate | null>): 模板槽位.
   *
   * Returns:
   *   Promise<void>: 写入完成.
   *
   * Callers:
   *   - 模板捕获、删除与锚点同步路径.
   */
  const persistTemplates = useCallback(async (slots: Array<MaskTemplate | null>) => {
    await request(apiPath('/paint/templates'), {
      method: 'PUT',
      body: JSON.stringify(buildTemplatePayload(slots)),
    });
  }, []);

  /**
   * 把指定槽位模板作为未确认实例放到画布中心.
   *
   * Args:
   *   slot (number): 模板槽位 0-9.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 快捷键 1-9/0.
   */
  const placeTemplate = useCallback(
    (slot: number) => {
      const template = templatesRef.current[slot];
      const { w, h } = sizeRef.current;
      if (!template || !maskSurfaceRef.current || !w || !h) return;
      const rw = Math.min(template.w, w);
      const rh = Math.min(template.h, h);
      applyFloatingState({
        capture: false,
        slot,
        rx: Math.round((w - rw) / 2),
        ry: Math.round((h - rh) / 2),
        rw,
        rh,
        data: template.data,
        w: template.w,
        h: template.h,
        url: template.url,
        anchors: template.anchors.map((anchor) => ({ ...anchor })),
      });
    },
    [applyFloatingState],
  );

  /**
   * 把实例锚点同步回所属模板槽位并持久化.
   *
   * Args:
   *   source (FloatingTemplate): 含最新锚点的实例.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:addAnchor`
   *   - `frontend/src/pages/paint/PaintPage.tsx:removeAnchor`
   */
  const syncAnchors = useCallback(
    (source: FloatingTemplate) => {
      if (source.capture) return;
      const slots = [...templatesRef.current];
      const template = slots[source.slot];
      if (!template) return;
      slots[source.slot] = {
        ...template,
        anchors: source.anchors.map((anchor) => ({ ...anchor })),
      };
      applyTemplates(slots);
      void persistTemplates(slots).catch(reportError);
    },
    [applyTemplates, persistTemplates, reportError],
  );

  /**
   * 移除指定索引的锚点.
   *
   * Args:
   *   index (number): 锚点索引.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 实例锚点单击与拖拽抬笔.
   */
  const removeAnchor = useCallback(
    (index: number) => {
      const source = floatingRef.current;
      if (!source) return;
      const next = { ...source, anchors: source.anchors.filter((_, i) => i !== index) };
      applyFloatingState(next);
      syncAnchors(next);
    },
    [applyFloatingState, syncAnchors],
  );

  /**
   * 在指针位置添加锚点；命中既有锚点时改为移除.
   *
   * Args:
   *   clientX (number): 视口横坐标.
   *   clientY (number): 视口纵坐标.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:handleFloatingPointerUp`
   */
  const addAnchor = useCallback(
    (clientX: number, clientY: number) => {
      const source = floatingRef.current;
      const canvas = displayCanvasRef.current;
      const { w, h } = sizeRef.current;
      const view = viewRef.current;
      if (!source || !canvas || !w || !h || !view.scale) return;
      const rect = canvas.getBoundingClientRect();
      const scaleX = view.scale;
      const scaleY = view.scale;
      const left = rect.left + view.left;
      const top = rect.top + view.top;
      const fx = ((clientX - left) / scaleX - source.rx) / source.rw;
      const fy = ((clientY - top) / scaleY - source.ry) / source.rh;
      for (let i = 0; i < source.anchors.length; i += 1) {
        const anchor = source.anchors[i];
        if (
          Math.hypot(
            (anchor.x - fx) * source.rw * scaleX,
            (anchor.y - fy) * source.rh * scaleY,
          ) <= 10
        ) {
          removeAnchor(i);
          return;
        }
      }
      if (source.anchors.length >= 2) {
        notify('锚点已满 (最多 2 个)');
        return;
      }
      const next = { ...source, anchors: [...source.anchors, { x: fx, y: fy }] };
      applyFloatingState(next);
      syncAnchors(next);
    },
    [applyFloatingState, notify, removeAnchor, syncAnchors],
  );

  /**
   * 删除指定槽位模板并持久化.
   *
   * Args:
   *   slot (number): 槽位索引.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 模板对话框删除按钮.
   */
  const deleteTemplate = useCallback(
    (slot: number) => {
      const slots = [...templatesRef.current];
      slots[slot] = null;
      applyTemplates(slots);
      void persistTemplates(slots).catch(reportError);
    },
    [applyTemplates, persistTemplates, reportError],
  );

  /**
   * 确认框选结果：按最终几何抠取图案并存入预定槽位.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 确定键 Enter/空格（框选待确认实例）.
   */
  const confirmCapture = useCallback(() => {
    const source = floatingRef.current;
    const surface = maskSurfaceRef.current;
    const { w, h } = sizeRef.current;
    if (!source || !surface || !w || !h) return;
    const x0 = Math.max(0, Math.floor(source.rx));
    const y0 = Math.max(0, Math.floor(source.ry));
    const x1 = Math.min(w, Math.ceil(source.rx + source.rw));
    const y1 = Math.min(h, Math.ceil(source.ry + source.rh));
    const tw = x1 - x0;
    const th = y1 - y0;
    if (tw < 2 || th < 2) {
      notify('模板区域过小, 请调整后重试');
      return;
    }
    const data = readMaskRegion(surface.ctx, x0, y0, tw, th);
    const template = templateFromRegion(data, tw, th, source.anchors);
    const slots = [...templatesRef.current];
    slots[source.slot] = template;
    applyTemplates(slots);
    void persistTemplates(slots).catch(reportError);
    notify(`已保存模板到槽位 ${source.slot + 1} (按 ${slotKey(source.slot)} 放置)`);
    applyFloatingState(null);
  }, [applyFloatingState, applyTemplates, notify, persistTemplates, reportError]);

  /**
   * 应用未确认模板；框选待确认实例则改为确认保存.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 确定键 Enter/空格.
   */
  const applyFloatingTemplate = useCallback(() => {
    const source = floatingRef.current;
    const surface = maskSurfaceRef.current;
    const { w, h } = sizeRef.current;
    if (!source) return;
    if (source.capture) {
      confirmCapture();
      return;
    }
    if (!surface || !w || !h) return;
    const x0 = Math.max(0, Math.floor(source.rx));
    const y0 = Math.max(0, Math.floor(source.ry));
    const x1 = Math.min(w, Math.ceil(source.rx + source.rw));
    const y1 = Math.min(h, Math.ceil(source.ry + source.rh));
    if (x0 >= x1 || y0 >= y1) {
      applyFloatingState(null);
      return;
    }
    applyTemplateRegion(surface.ctx, w, h, source, mergeModeRef.current === 'max');
    modifiedRef.current = true;
    applyFloatingState(null);
    scheduleRender();
  }, [applyFloatingState, confirmCapture, scheduleRender]);

  /**
   * 结束框选并按几何生成待确认模板实例.
   *
   * Args:
   *   x0 (number): 起点 x.
   *   y0 (number): 起点 y.
   *   x1 (number): 终点 x.
   *   y1 (number): 终点 y.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:finishStroke`
   */
  const beginCaptureConfirm = useCallback(
    (x0: number, y0: number, x1: number, y1: number) => {
      const { w, h } = sizeRef.current;
      if (!maskSurfaceRef.current || !w || !h) return;
      const px0 = Math.max(0, Math.floor(Math.min(x0, x1)));
      const py0 = Math.max(0, Math.floor(Math.min(y0, y1)));
      const px1 = Math.min(w, Math.ceil(Math.max(x0, x1)));
      const py1 = Math.min(h, Math.ceil(Math.max(y0, y1)));
      const pw = px1 - px0;
      const ph = py1 - py0;
      if (pw < 2 || ph < 2) {
        notify('框选区域过小');
        return;
      }
      let slot = -1;
      for (let i = 0; i < 10; i += 1) {
        if (!templatesRef.current[i]) {
          slot = i;
          break;
        }
      }
      if (slot === -1) {
        notify('模板已满 (最多 10 个)');
        return;
      }
      applyFloatingState({
        capture: true,
        slot,
        rx: px0,
        ry: py0,
        rw: pw,
        rh: ph,
        data: null,
        w: 0,
        h: 0,
        url: null,
        anchors: [],
      });
      notify(`单击加锚点(≤2), 拖动/缩放调整区域, Enter 确认保存到槽位 ${slot + 1}`);
    },
    [applyFloatingState, notify],
  );

  /**
   * 进入模板框选模式.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 模板对话框「框选当前图区域」按钮.
   */
  const beginTemplateCapture = useCallback(() => {
    if (!maskSurfaceRef.current) {
      notify('当前没有可框选的画布');
      return;
    }
    setTemplateOpen(false);
    setCaptureMode(true);
    applyFloatingState(null);
    notify('在图上拖拽框选区域 (Esc 取消)');
  }, [applyFloatingState, notify, setCaptureMode]);

  /**
   * 取消模板框选模式.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 快捷键 Esc.
   */
  const cancelCapture = useCallback(() => {
    setCaptureMode(false);
    captureStartRef.current = null;
    stopStrokeTracking();
    strokeRef.current = { mode: 'none', value: 0, start: null, last: null };
    if (captureRectRef.current) captureRectRef.current.style.display = 'none';
  }, [setCaptureMode, stopStrokeTracking]);

  /**
   * 处理未确认模板上的指针按下.
   *
   * Args:
   *   event (ReactPointerEvent<HTMLDivElement>): 指针事件.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 模板实例容器的 pointerdown.
   */
  const handleFloatingPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const source = floatingRef.current;
    if (!source) return;
    event.preventDefault();
    event.stopPropagation();
    const target = event.target as HTMLElement;
    const anchorAttr = target.getAttribute('data-anchor');
    let hv: { h: 'l' | 'r' | null; v: 't' | 'b' | null } | null = null;
    if (anchorAttr === null) {
      const resizeKey = target.getAttribute('data-resize');
      if (resizeKey && RESIZE_HV[resizeKey]) hv = RESIZE_HV[resizeKey];
    }
    dragRef.current = {
      mode: anchorAttr !== null ? 'anchor' : hv ? 'resize' : 'move',
      anchor: anchorAttr !== null ? Number.parseInt(anchorAttr, 10) : -1,
      sx: event.clientX,
      sy: event.clientY,
      x0: source.rx,
      y0: source.ry,
      w0: source.rw,
      h0: source.rh,
      h: hv ? hv.h : null,
      v: hv ? hv.v : null,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  /**
   * 处理未确认模板的拖动/缩放/锚点对齐.
   *
   * Args:
   *   event (ReactPointerEvent<HTMLDivElement>): 指针事件.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 模板实例容器的 pointermove.
   */
  const handleFloatingPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    const source = floatingRef.current;
    const canvas = displayCanvasRef.current;
    const { w, h } = sizeRef.current;
    const view = viewRef.current;
    if (!drag || !source || !canvas || !w || !h || !view.scale) return;
    event.stopPropagation();
    const rect = canvas.getBoundingClientRect();
    const scaleX = view.scale;
    const scaleY = view.scale;
    if (!scaleX || !scaleY) return;
    const ox = (event.clientX - drag.sx) / scaleX;
    const oy = (event.clientY - drag.sy) / scaleY;
    if (drag.mode === 'anchor') {
      const list = source.anchors;
      const moving = list[drag.anchor];
      if (!moving) return;
      const fixed = list[drag.anchor === 0 ? 1 : 0];
      const px = (event.clientX - rect.left - view.left) / scaleX;
      const py = (event.clientY - rect.top - view.top) / scaleY;
      const ax = source.rx + moving.x * source.rw;
      const ay = source.ry + moving.y * source.rh;
      if (fixed) {
        const fx = source.rx + fixed.x * source.rw;
        const fy = source.ry + fixed.y * source.rh;
        const sx = ax !== fx ? (px - fx) / (ax - fx) : 1;
        const sy = ay !== fy ? (py - fy) / (ay - fy) : 1;
        const safeX = Math.max(4 / source.rw, sx);
        const safeY = Math.max(4 / source.rh, sy);
        applyFloatingState({
          ...source,
          rx: fx + (source.rx - fx) * safeX,
          ry: fy + (source.ry - fy) * safeY,
          rw: source.rw * safeX,
          rh: source.rh * safeY,
        });
      } else {
        applyFloatingState({ ...source, rx: source.rx + px - ax, ry: source.ry + py - ay });
      }
      return;
    }
    if (drag.mode === 'move') {
      applyFloatingState({ ...source, rx: drag.x0 + ox, ry: drag.y0 + oy });
      return;
    }
    if (event.shiftKey) {
      applyFloatingState({ ...source, ...shiftResize(drag, ox, oy) });
      return;
    }
    const right = drag.x0 + drag.w0;
    const bottom = drag.y0 + drag.h0;
    let rx: number;
    let rw: number;
    if (drag.h === null) {
      rx = drag.x0;
      rw = drag.w0;
    } else if (drag.h === 'l') {
      rx = Math.min(drag.x0 + ox, right - 4);
      rw = right - rx;
    } else {
      rx = drag.x0;
      rw = Math.max(4, drag.w0 + ox);
    }
    let ry: number;
    let rh: number;
    if (drag.v === null) {
      ry = drag.y0;
      rh = drag.h0;
    } else if (drag.v === 't') {
      ry = Math.min(drag.y0 + oy, bottom - 4);
      rh = bottom - ry;
    } else {
      ry = drag.y0;
      rh = Math.max(4, drag.h0 + oy);
    }
    applyFloatingState({ ...source, rx, ry, rw, rh });
  };

  /**
   * 处理未确认模板的抬笔：无位移单击增删锚点.
   *
   * Args:
   *   event (ReactPointerEvent<HTMLDivElement>): 指针事件.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 模板实例容器的 pointerup.
   */
  const handleFloatingPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    dragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (!drag || drag.mode === 'resize') return;
    if (Math.hypot(event.clientX - drag.sx, event.clientY - drag.sy) >= 4) return;
    if (drag.mode === 'anchor') removeAnchor(drag.anchor);
    else addAnchor(event.clientX, event.clientY);
  };

  /**
   * 结束一次进行中的笔触或框选.
   *
   * Args:
   *   state (StrokeState): 结束时的笔触状态.
   *   point (Point | null): 结束位置.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:startStroke` 注册的窗口 pointerup.
   */
  const finishStroke = (state: StrokeState, point: Point | null) => {
    if (state.mode === 'capture') {
      stopStrokeTracking();
      const start = captureStartRef.current;
      captureStartRef.current = null;
      setCaptureMode(false);
      if (captureRectRef.current) captureRectRef.current.style.display = 'none';
      strokeRef.current = { mode: 'none', value: 0, start: null, last: null };
      if (start && point) beginCaptureConfirm(start.x, start.y, point.x, point.y);
      return;
    }
    if (state.mode === 'line' && state.start && point) {
      paintSegment(state.start as Point, point, state.value);
      modifiedRef.current = true;
    }
    if (state.mode === 'brush') modifiedRef.current = true;
    stopStrokeTracking();
    strokeRef.current = { mode: 'none', value: 0, start: null, last: null };
    updateBrushPreview(null);
    if (lineSvgRef.current) lineSvgRef.current.style.display = 'none';
    scheduleRender();
  };

  /**
   * 注册窗口级指针跟踪，保证移出画布后仍可继续绘制.
   *
   * Args:
   *   pointerId (number): 触发按下的指针标识.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - `frontend/src/pages/paint/PaintPage.tsx:startStroke`
   */
  const attachStrokeTracking = (pointerId: number) => {
    const move = (event: PointerEvent) => {
      if (event.pointerId !== pointerId) return;
      const state = strokeRef.current;
      if (state.mode === 'none') return;
      const point = origFromClient(event.clientX, event.clientY, true);
      if (!point) return;
      const previous = state.last;
      state.last = point;
      if (state.mode === 'capture') {
        updateCaptureRect();
        return;
      }
      if (state.mode === 'line') {
        updateBrushPreview(point);
        updateLinePreview();
        return;
      }
      updateBrushPreview(point);
      if (previous) paintSegment(previous, point, state.value);
    };
    const up = (event: PointerEvent) => {
      if (event.pointerId !== pointerId) return;
      const state = strokeRef.current;
      if (state.mode === 'none') return;
      const point = origFromClient(event.clientX, event.clientY, true) ?? state.last;
      finishStroke(state, point);
    };
    stopStrokeTracking();
    strokeMoveRef.current = move;
    strokeUpRef.current = up;
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
  };

  /**
   * 开始一次绘制、框选或直线操作.
   *
   * Args:
   *   event (ReactPointerEvent<HTMLDivElement>): 指针按下事件.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 画布容器的 pointerdown.
   */
  const startStroke = (event: ReactPointerEvent<HTMLDivElement>) => {
    const surface = maskSurfaceRef.current;
    const point = origFromClient(event.clientX, event.clientY, false);
    if (!surface || !point) return;
    if (captureModeRef.current) {
      strokeRef.current = { mode: 'capture', value: 0, start: point, last: point };
      captureStartRef.current = point;
      updateCaptureRect();
      attachStrokeTracking(event.pointerId);
      return;
    }
    const value = alphaFor(event.button);
    if (event.shiftKey) {
      strokeRef.current = { mode: 'line', value, start: point, last: point };
      updateLinePreview();
      attachStrokeTracking(event.pointerId);
      return;
    }
    paintDisc(point, value);
    strokeRef.current = { mode: 'brush', value, start: point, last: point };
    modifiedRef.current = true;
    attachStrokeTracking(event.pointerId);
  };

  /**
   * 处理画布容器的指针按下.
   *
   * Args:
   *   event (ReactPointerEvent<HTMLDivElement>): 指针事件.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 画布容器的 pointerdown.
   */
  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!maskSurfaceRef.current) return;
    if (event.button !== 0 && event.button !== 1 && event.button !== 2) return;
    event.preventDefault();
    startStroke(event);
  };

  /**
   * 处理画布容器的指针移动，仅负责悬停笔刷预览.
   *
   * Args:
   *   event (ReactPointerEvent<HTMLDivElement>): 指针事件.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 画布容器的 pointermove.
   */
  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (strokeRef.current.mode !== 'none') return;
    const point = origFromClient(event.clientX, event.clientY, false);
    hoverRef.current = point;
    updateBrushPreview(point);
  };

  /**
   * 处理指针离开画布容器.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 画布容器的 pointerleave.
   */
  const handlePointerLeave = () => {
    if (strokeRef.current.mode !== 'none') return;
    hoverRef.current = null;
    updateBrushPreview(null);
  };

  useEffect(() => {
    let cancelled = false;
    loadImageList(sourceDir).catch((error) => {
      if (!cancelled) reportError(error);
    });
    return () => {
      cancelled = true;
      listTokenRef.current += 1;
      loadTokenRef.current += 1;
      cancelInteraction();
    };
  }, [cancelInteraction, loadImageList, reportError, sourceDir]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const payload = await request<TemplatesPayload>(apiPath('/paint/templates'));
      const decoded = await Promise.all(
        payload.templates.map(
          async (item) => [item.slot, await decodeServerTemplate(item)] as const,
        ),
      );
      if (cancelled) return;
      const slots: Array<MaskTemplate | null> = new Array(10).fill(null);
      for (const [slot, template] of decoded) {
        if (slot >= 0 && slot < 10) slots[slot] = template;
      }
      applyTemplates(slots);
    })().catch((error) => {
      if (!cancelled) reportError(error);
    });
    return () => {
      cancelled = true;
    };
  }, [applyTemplates, reportError]);

  useEffect(() => {
    const target = wrapRef.current;
    if (!target) return;
    const update = () => {
      const next = { width: target.clientWidth, height: target.clientHeight };
      viewportRef.current = next;
      setViewport(next);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(target);
    window.addEventListener('resize', update);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', update);
    };
  }, []);

  useEffect(() => {
    const target = wrapRef.current;
    if (!target) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const direction = event.deltaY < 0 ? 1 : -1;
      const current = inputsRef.current.brushPct;
      patchInputs({
        brushPct: clamp(0.5, 20, current + direction * Math.max(0.1, current * 0.15)),
      });
      updateBrushPreview(hoverRef.current);
      updateLinePreview();
    };
    target.addEventListener('wheel', onWheel, { passive: false });
    return () => target.removeEventListener('wheel', onWheel);
  }, [patchInputs, updateBrushPreview, updateLinePreview]);

  const redMode = captureMode || Boolean(floating?.capture);

  useEffect(() => {
    redModeRef.current = redMode;
    drawDisplay();
  }, [drawDisplay, redMode]);

  useEffect(() => {
    scheduleRender();
  }, [imageTick, scheduleRender, size.h, size.w, view]);

  useEffect(() => {
    updateBrushPreview(hoverRef.current);
    updateLinePreview();
  }, [inputs.brushPct, updateBrushPreview, updateLinePreview, view]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
      if (event.key === 'Escape') {
        if (captureModeRef.current) {
          cancelCapture();
          notify('已取消框选');
        } else if (floatingRef.current) {
          const wasCapture = floatingRef.current.capture;
          applyFloatingState(null);
          notify(wasCapture ? '已取消框选' : '已取消模板');
        }
        return;
      }
      if (event.key === ' ' || event.key === 'Enter') {
        if (floatingRef.current) {
          event.preventDefault();
          applyFloatingTemplate();
        }
        return;
      }
      let slot: number | null = null;
      if (event.key.length === 1 && event.key >= '1' && event.key <= '9') {
        slot = Number.parseInt(event.key, 10) - 1;
      } else if (event.key === '0') {
        slot = 9;
      }
      if (slot !== null) {
        const source = floatingRef.current;
        if (source && source.capture) {
          applyFloatingState({ ...source, slot });
          notify(`确认后将保存到槽位 ${slot + 1}`);
        } else if (!captureModeRef.current && templatesRef.current[slot]) {
          placeTemplate(slot);
        }
        event.preventDefault();
        return;
      }
      if (event.ctrlKey || event.metaKey) {
        if (event.key === 's' || event.key === 'S') {
          event.preventDefault();
          void saveMask(true).catch(reportError);
        } else if (event.key === 'a' || event.key === 'A') {
          event.preventDefault();
          void goPrev();
        } else if (event.key === 'd' || event.key === 'D') {
          event.preventDefault();
          void goNext();
        }
        return;
      }
      if (event.key === 'ArrowLeft' || event.key === 'a' || event.key === 'A') {
        event.preventDefault();
        void goPrev();
      } else if (event.key === 'ArrowRight' || event.key === 'd' || event.key === 'D') {
        event.preventDefault();
        void goNext();
      } else if (event.key === 's' || event.key === 'S') {
        event.preventDefault();
        void saveMask(true).catch(reportError);
      } else if (event.key === 'x' || event.key === 'X') {
        void goSkip();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [
    applyFloatingState,
    applyFloatingTemplate,
    cancelCapture,
    goNext,
    goPrev,
    goSkip,
    notify,
    placeTemplate,
    reportError,
    saveMask,
  ]);

  useEffect(() => {
    return () => {
      stopStrokeTracking();
      jobStopRef.current?.();
      if (jobTimerRef.current) window.clearTimeout(jobTimerRef.current);
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [stopStrokeTracking]);

  const current = images[index] ?? null;
  const doneCount = images.filter((item) => item.has_mask).length;
  const buffer = useMemo(() => {
    void cacheVersion;
    const position = indexRef.current;
    let ready = 0;
    let want = 0;
    const list = itemsRef.current;
    for (let delta = -PRELOAD_RADIUS; delta <= PRELOAD_RADIUS; delta += 1) {
      const neighbor = position + delta;
      if (neighbor < 0 || neighbor >= list.length) continue;
      want += 1;
      if (cacheRef.current.has(list[neighbor].path)) ready += 1;
    }
    return { ready, want, total: cacheRef.current.size };
  }, [cacheVersion, images, index]);

  let overlay: JSX.Element | null = null;
  if (floating) {
    const overlayStyle: CSSProperties = {
      left: view.left + floating.rx * view.scale,
      top: view.top + floating.ry * view.scale,
      width: Math.max(1, floating.rw * view.scale),
      height: Math.max(1, floating.rh * view.scale),
      border: '1.5px solid #f33',
      boxSizing: 'border-box',
      cursor: 'move',
      zIndex: 12,
      userSelect: 'none',
      touchAction: 'none',
    };
    const patternStyle = {
      position: 'absolute',
      inset: 0,
      pointerEvents: 'none',
      zIndex: 0,
      background: 'rgba(255, 0, 0, 0.5)',
      WebkitMaskImage: floating.url
        ? `linear-gradient(#000, #000), url("${floating.url}")`
        : undefined,
      WebkitMaskComposite: 'xor',
      maskImage: floating.url
        ? `linear-gradient(#000, #000), url("${floating.url}")`
        : undefined,
      maskComposite: 'exclude',
      maskSize: '100% 100%, 100% 100%',
      maskMode: 'alpha',
    } as CSSProperties;
    overlay = (
      <div
        className="absolute"
        style={overlayStyle}
        onPointerDown={handleFloatingPointerDown}
        onPointerMove={handleFloatingPointerMove}
        onPointerUp={handleFloatingPointerUp}
        onPointerCancel={handleFloatingPointerUp}
      >
        {floating.url ? <div style={patternStyle} /> : null}
        {['nw', 'n', 'ne', 'w', 'e', 'sw', 's', 'se'].map((kind) => (
          <span key={kind} data-resize={kind} style={HANDLE_STYLE[kind]} />
        ))}
        {floating.anchors.map((anchor, anchorIndex) => (
          <span
            key={anchorIndex}
            data-anchor={anchorIndex}
            title={`锚点 ${anchorIndex + 1}: 拖动对齐(实例随之平移缩放), 单击移除`}
            style={{
              position: 'absolute',
              zIndex: 4,
              left: `${anchor.x * 100}%`,
              top: `${anchor.y * 100}%`,
              width: 12,
              height: 12,
              transform: 'translate(-50%, -50%)',
              borderRadius: '50%',
              background: '#fa0',
              border: '1.5px solid #fff',
              boxShadow: '0 0 0 1px rgba(0,0,0,.6)',
              cursor: 'move',
            }}
          />
        ))}
      </div>
    );
  }

  useJob(progress ? { snapshot: { status: 'running', pct: progress.pct, message: progress.text } } : null);
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
      { kind: 'part', name: '已标注', value: doneCount },
      {
        kind: 'resource',
        name: '缓冲',
        value: `${buffer.ready}/${buffer.want}（缓存 ${buffer.total}）`,
      },
      { kind: 'resource', name: '尺寸', value: size.w ? `${size.w}×${size.h}` : '-' },
    ],
    [current?.stem ?? '', current ? (current.has_mask ? '已标注' : '待标注') : '']
      .filter(Boolean)
      .join(' · '),
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <Toolbar>
        <Button onClick={() => void goPrev()} title="←/A">
          上一张
        </Button>
        <Button onClick={() => void goNext()} title="→/D">
          下一张
        </Button>
        <Button onClick={() => void goSkip()} title="X">
          跳过
        </Button>
        <Button
          variant="primary"
          onClick={() => void saveMask(true).catch(reportError)}
          title="S"
        >
          保存
        </Button>
        <ToolbarDivider />
        <Button
          onClick={() => setGenerateOpen(true)}
          title="生成蒙版(BiRefNet / SAM3)到 mask/"
        >
          生成蒙版
        </Button>
        <Button
          onClick={() => setTemplateOpen(true)}
          title="蒙板模板: 框选当前图区域, Enter/空格 确认; 1-9/0 放置, Enter/空格 应用, Esc 取消"
        >
          模板
        </Button>
        <ToolbarDivider />
        <Button size="sm" onClick={clearAll} title="全透明">
          全透明
        </Button>
        <Button size="sm" onClick={() => fillAll(255)} title="全不透明">
          全不透明
        </Button>
        <Button size="sm" onClick={restoreDefault} title="恢复默认 Alpha">
          恢复默认
        </Button>
        <ToolbarDivider />
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          笔刷
          <Slider
            value={inputs.brushPct}
            min={0.5}
            max={20}
            step={0.1}
            onChange={(value) => patchInputs({ brushPct: value })}
            onCommit={() => patchInputs({}, true)}
          />
          <span className="w-24">
            {inputs.brushPct.toFixed(1)}% ~{brushRadius}px
          </span>
        </label>
        <label className="flex items-center gap-1 text-xs text-muted-foreground" title="左键/右键绘制 Alpha 值，⇄ 快捷交换">
          Alpha
          <Input
            type="number"
            min={0}
            max={255}
            className="w-14"
            value={inputs.leftAlpha}
            title="左键 Alpha"
            onChange={(event) =>
              patchInputs({ leftAlpha: alphaInput(event.target.value, 0) })
            }
            onBlur={() => patchInputs({}, true)}
          />
          <Button
            size="sm"
            title="交换左右键 Alpha"
            onClick={() =>
              patchInputs(
                { leftAlpha: inputsRef.current.rightAlpha, rightAlpha: inputsRef.current.leftAlpha },
                true,
              )
            }
          >
            ⇄
          </Button>
          <Input
            type="number"
            min={0}
            max={255}
            className="w-14"
            value={inputs.rightAlpha}
            title="右键 Alpha"
            onChange={(event) =>
              patchInputs({ rightAlpha: alphaInput(event.target.value, 255) })
            }
            onBlur={() => patchInputs({}, true)}
          />
        </label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          默认 Alpha
          <Input
            type="number"
            min={0}
            max={255}
            className="w-14"
            value={inputs.defaultAlpha}
            onChange={(event) =>
              patchInputs({ defaultAlpha: alphaInput(event.target.value, 255) })
            }
            onBlur={() => patchInputs({}, true)}
          />
        </label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          背景
          <Select
            value={inputs.bgStyle}
            onChange={(event) =>
              patchInputs({ bgStyle: event.target.value as BgStyle }, true)
            }
          >
            <option value="checker">棋盘格</option>
            <option value="white">纯白</option>
            <option value="black">纯黑</option>
            <option value="custom">自定义</option>
          </Select>
          {inputs.bgStyle === 'custom' ? (
            <input
              type="color"
              value={inputs.bgColor}
              title="自定义背景色"
              className="h-7 w-7 rounded border border-border bg-[#0a1a30] p-0"
              onChange={(event) => patchInputs({ bgColor: event.target.value }, true)}
            />
          ) : null}
        </label>
      </Toolbar>

      <div
        ref={wrapRef}
        className="relative min-h-0 flex-1 touch-none overflow-hidden bg-[#222]"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerLeave={handlePointerLeave}
        onContextMenu={(event) => event.preventDefault()}
      >
        <div
          className="pointer-events-none absolute"
          style={{
            left: view.left,
            top: view.top,
            width: view.width,
            height: view.height,
            background: backgroundCss(inputs),
          }}
        />
        <canvas ref={displayCanvasRef} className="absolute inset-0 block h-full w-full" />
        <div
          ref={brushPreviewRef}
          className="pointer-events-none absolute z-10 hidden rounded-full border border-white bg-white/10 shadow-[0_0_0_1px_#000]"
        />
        <svg ref={lineSvgRef} className="pointer-events-none absolute inset-0 z-10 hidden">
          <line ref={lineRef} stroke="rgba(255,0,0,0.3)" strokeLinecap="round" />
        </svg>
        <div
          ref={captureRectRef}
          className="pointer-events-none absolute z-[12] hidden border border-dashed border-[#f33] bg-red-500/15"
        />
        <span className="pointer-events-none absolute bottom-2 right-3 z-10 whitespace-nowrap text-xs text-white/30">
          Esc 取消 · Enter/空格 应用 · 1-9/0 模板 · ←/A 上一张 · →/D 下一张 · S 保存 · X
          跳过 · Shift+拖拽 直线 · 滚轮 笔刷
        </span>
        {overlay}
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
                item.width && item.height
                  ? { aspectRatio: `${item.width} / ${item.height}` }
                  : undefined
              }
              className={cn(
                'h-32 w-auto shrink-0 cursor-pointer rounded-sm border-2 bg-[#0a1a30]',
                position === index
                  ? 'border-[#00cc66]'
                  : 'border-transparent hover:border-muted-foreground',
              )}
              onClick={() => void loadIndex(position)}
            />
          ))}
        </div>
      ) : null}

      <MaskGenerateDialog
        open={generateOpen}
        onOpenChange={setGenerateOpen}
        currentPath={current?.path ?? null}
        dir={sourceDir}
        notify={notify}
        onStarted={watchMaskJob}
      />
      <TemplateDialog
        open={templateOpen}
        onOpenChange={setTemplateOpen}
        templates={templates}
        mergeMode={mergeMode}
        onMergeMode={setMergeMode}
        onDelete={deleteTemplate}
        onCapture={beginTemplateCapture}
      />
    </div>
  );
}
