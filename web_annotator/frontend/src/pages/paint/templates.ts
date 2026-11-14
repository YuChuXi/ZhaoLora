/** 蒙版模板的编解码与持久化载荷构造. */

import type { Anchor, MaskTemplate, ServerTemplate, TemplatesPayload } from './types';

/**
 * 把 RGBA 图案数据编码为 PNG dataURL.
 *
 * Args:
 *   data (Uint8ClampedArray): 长度 w*h*4 的图案数据.
 *   w (number): 图案宽.
 *   h (number): 图案高.
 *
 * Returns:
 *   string: `data:image/png;base64,...` 形式的 dataURL.
 *
 * Callers:
 *   - `frontend/src/pages/paint/templates.ts:templateFromRegion`
 *   - `frontend/src/pages/paint/templates.ts:decodeServerTemplate`
 */
export function dataUrlFromRgba(data: Uint8ClampedArray, w: number, h: number): string {
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('无法创建模板预览画布');
  const image = ctx.createImageData(w, h);
  image.data.set(data);
  ctx.putImageData(image, 0, 0);
  return canvas.toDataURL('image/png');
}

/**
 * 由捕获区域的 RGBA 数据构造本地模板.
 *
 * Args:
 *   data (Uint8ClampedArray): 长度 w*h*4 的图案数据（RGB 恒白，A 为蒙版值）.
 *   w (number): 图案宽.
 *   h (number): 图案高.
 *   anchors (Anchor[]): 模板锚点（0-1 分数坐标）.
 *
 * Returns:
 *   MaskTemplate: 含图案数据、预览 dataURL 与锚点的模板.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:confirmCapture`
 */
export function templateFromRegion(
  data: Uint8ClampedArray,
  w: number,
  h: number,
  anchors: Anchor[],
): MaskTemplate {
  return {
    w,
    h,
    data,
    url: dataUrlFromRgba(data, w, h),
    anchors: anchors.map((anchor) => ({ ...anchor })),
  };
}

/**
 * 解码服务端模板项为本地模板.
 *
 * Args:
 *   item (ServerTemplate): 服务端模板项（base64 PNG 与锚点）.
 *
 * Returns:
 *   Promise<MaskTemplate>: 图案 RGBA 数据、dataURL 与锚点.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx` 模板加载副作用.
 */
export async function decodeServerTemplate(item: ServerTemplate): Promise<MaskTemplate> {
  const url = `data:image/png;base64,${item.image_b64}`;
  const image = new Image();
  image.src = url;
  await image.decode();
  const canvas = document.createElement('canvas');
  canvas.width = item.w;
  canvas.height = item.h;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('无法创建模板解码画布');
  ctx.drawImage(image, 0, 0, item.w, item.h);
  return {
    w: item.w,
    h: item.h,
    data: ctx.getImageData(0, 0, item.w, item.h).data,
    url,
    anchors: item.anchors.map((anchor) => ({ ...anchor })),
  };
}

/**
 * 把本地模板槽位转换为服务端全量覆盖载荷.
 *
 * Args:
 *   slots (Array<MaskTemplate | null>): 长度 10 的模板槽位.
 *
 * Returns:
 *   TemplatesPayload: 仅含非空槽位的服务端载荷.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:persistTemplates`
 */
export function buildTemplatePayload(slots: Array<MaskTemplate | null>): TemplatesPayload {
  const templates: ServerTemplate[] = [];
  slots.forEach((template, slot) => {
    if (!template) return;
    templates.push({
      slot,
      w: template.w,
      h: template.h,
      image_b64: template.url.replace(/^data:image\/png;base64,/, ''),
      anchors: template.anchors.map((anchor) => ({ ...anchor })),
    });
  });
  return { templates };
}
