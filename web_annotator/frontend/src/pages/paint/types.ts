/** 蒙版页的数据类型定义. */

/** 图片列表项（`GET /paint/images`）. */
export interface PaintImageItem {
  path: string;
  stem: string;
  has_mask: boolean;
  width: number;
  height: number;
  thumbnail_hash: string;
}

/** 单张图片的已解码视觉与蒙版图像. */
export interface LoadedEntry {
  image: HTMLImageElement;
  mask: HTMLImageElement | null;
  w: number;
  h: number;
}

/** 模板锚点：图案上的 0-1 分数坐标. */
export interface Anchor {
  x: number;
  y: number;
}

/** 本地模板：图案 RGBA 数据（RGB 恒白，A 为蒙版值）与预览 dataURL. */
export interface MaskTemplate {
  w: number;
  h: number;
  data: Uint8ClampedArray;
  url: string;
  anchors: Anchor[];
}

/** 未确认模板实例的几何、图案与锚点. */
export interface FloatingTemplate {
  capture: boolean;
  slot: number;
  rx: number;
  ry: number;
  rw: number;
  rh: number;
  data: Uint8ClampedArray | null;
  w: number;
  h: number;
  url: string | null;
  anchors: Anchor[];
}

/** 服务端模板项（base64 PNG）. */
export interface ServerTemplate {
  slot: number;
  w: number;
  h: number;
  image_b64: string;
  anchors: Anchor[];
}

/** 服务端模板全量集. */
export interface TemplatesPayload {
  templates: ServerTemplate[];
}

/** 原图像素坐标点. */
export interface Point {
  x: number;
  y: number;
}

/** 画布显示变换：缩放比与相对容器左上角的偏移. */
export interface CanvasView {
  scale: number;
  width: number;
  height: number;
  left: number;
  top: number;
}

/** 缩放与平移状态. */
export interface Camera {
  zoom: number;
  x: number;
  y: number;
}

/** 绘制工具. */
export type Tool = 'brush' | 'eraser' | 'line' | 'fill';

/** 画布背景样式. */
export type BgStyle = 'checker' | 'white' | 'black' | 'custom';

/** 蒙版页的输入参数（持久化于设置键 `paint`）. */
export interface PaintInputs {
  brushPct: number;
  leftAlpha: number;
  rightAlpha: number;
  defaultAlpha: number;
  bgStyle: BgStyle;
  bgColor: string;
}
