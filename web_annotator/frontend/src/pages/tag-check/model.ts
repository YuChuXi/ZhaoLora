import { api, apiPath } from '@/lib/api';

export type SepOption = 'any' | 'before' | 'after';
export type ModeOption = 'top' | 'bottom' | 'random';
export type CheckPosition = 'any' | 'top' | 'bottom' | 'before-sep' | 'after-sep' | 'bottom-before';
export type InsertPosition = 'top' | 'bottom' | 'before-sep' | 'after-sep';
export type Engine = 'similarity' | 'concept';

export interface TagCheckItem {
  path: string;
  stem: string;
  width: number;
  height: number;
  thumbnail_hash: string;
  dir: string;
  exists: boolean;
  position_ok: boolean;
  caption_preview: string;
  sim: number | null;
  ratio: number | null;
  mask: string | null;
}

export interface TagCheckResponseItem {
  path: string;
  stem: string;
  width: number;
  height: number;
  thumbnail_hash: string;
  dir: string;
  exists: boolean;
  position_ok: boolean;
  caption_preview: string;
}

export interface TagCheckResponse {
  tag: string;
  position: string;
  total: number;
  ok_count: number;
  bad_count: number;
  items: TagCheckResponseItem[];
}

export interface TagStatRow {
  tag: string;
  count: number;
  zh?: string;
}

export interface TagSuggestion {
  tag: string;
  count: number;
  zh: string;
  pos: number;
}

export interface TagStatsResponse {
  tags?: TagStatRow[];
}

export interface SemanticItem {
  path: string;
  stem?: string;
  similarity?: number;
  ratio?: number;
  mask_data?: string;
}

export interface JobSnapshot {
  status?: string;
  pct?: number;
  message?: string;
  result?: { items?: SemanticItem[] };
}

export interface BatchTagResponse {
  ok: boolean;
  matched_count: number;
  message: string;
}

export interface EmbOptions {
  engine: Engine;
  operator: 'gt' | 'lt';
  threshold: string;
  fuzzy: boolean;
  action: 'add' | 'delete';
}

export interface TagCheckSettings {
  position?: SepOption;
  mode?: ModeOption;
  apply_mask?: boolean;
  cols?: number;
  emb?: Partial<EmbOptions>;
}

export interface ParsedSettings {
  position: SepOption;
  mode: ModeOption;
  applyMask: boolean;
  cols: number;
  emb: EmbOptions;
}

/**
 * 读取并校验保存的 tag_check 设置，缺失或非法的字段回落到页面默认值。
 *
 * Args:
 *   raw (unknown): 设置对象中 `tag_check` 键的原始值。
 *
 * Returns:
 *   ParsedSettings: 页面初始化使用的完整设置。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 */
export function parseSettings(raw: unknown): ParsedSettings {
  const source = raw && typeof raw === 'object' ? (raw as TagCheckSettings) : {};
  const position: SepOption =
    source.position === 'before' || source.position === 'after' ? source.position : 'any';
  const mode: ModeOption =
    source.mode === 'bottom' || source.mode === 'random' ? source.mode : 'top';
  const cols =
    typeof source.cols === 'number' && source.cols >= 0 && source.cols <= 16 ? source.cols : 0;
  const embSource = source.emb && typeof source.emb === 'object' ? source.emb : {};
  const threshold =
    typeof embSource.threshold === 'string' && embSource.threshold.trim()
      ? embSource.threshold
      : '0.5';
  return {
    position,
    mode,
    applyMask: !!source.apply_mask,
    cols,
    emb: {
      engine: embSource.engine === 'concept' ? 'concept' : 'similarity',
      operator: embSource.operator === 'lt' ? 'lt' : 'gt',
      threshold,
      fuzzy: !!embSource.fuzzy,
      action: embSource.action === 'delete' ? 'delete' : 'add',
    },
  };
}

/**
 * 由分隔位置与模式选项计算标签检查位置值。
 *
 * Args:
 *   sep (SepOption): 分隔位置选择（任意 / `|||` 前 / `|||` 后）。
 *   mode (ModeOption): 位置模式选择（置顶 / 置底 / 任意）。
 *
 * Returns:
 *   CheckPosition: 后端检查接口使用的位置值。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckPage.tsx:runCheck`
 *   - `pages/tag-check/TagCheckPage.tsx:onCardClick`
 *   - `pages/tag-check/TagCheckPage.tsx:editSingle`
 */
export function checkPositionOf(sep: SepOption, mode: ModeOption): CheckPosition {
  if (mode === 'random') {
    if (sep === 'before') return 'before-sep';
    if (sep === 'after') return 'after-sep';
    return 'any';
  }
  if (sep === 'any') return mode === 'top' ? 'top' : 'bottom';
  if (sep === 'before') return mode === 'top' ? 'top' : 'bottom-before';
  return mode === 'top' ? 'after-sep' : 'bottom';
}

/**
 * 由分隔位置与模式选项计算标签插入位置值。
 *
 * 任意模式追加到所选区域末尾：任意位置→全局末尾，`|||` 前→头部末尾，
 * `|||` 后→正文末尾。
 *
 * Args:
 *   sep (SepOption): 分隔位置选择。
 *   mode (ModeOption): 位置模式选择。
 *
 * Returns:
 *   InsertPosition: 后端写入接口使用的位置值。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckPage.tsx:onCardClick`
 *   - `pages/tag-check/TagCheckPage.tsx:editSingle`
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 */
export function insertPositionOf(sep: SepOption, mode: ModeOption): InsertPosition {
  if (mode === 'random') return sep === 'before' ? 'before-sep' : 'bottom';
  if (sep === 'any') return mode === 'top' ? 'top' : 'bottom';
  if (sep === 'before') return mode === 'top' ? 'top' : 'before-sep';
  return mode === 'top' ? 'after-sep' : 'bottom';
}

/**
 * 生成工具栏与语义对话框展示的位置描述。
 *
 * Args:
 *   sep (SepOption): 分隔位置选择。
 *   mode (ModeOption): 位置模式选择。
 *
 * Returns:
 *   string: 形如 `在 ||| 前 · 置顶` 的描述文本。
 *
 * Callers:
 *   - `pages/tag-check/SemanticDialog.tsx:SemanticDialog`
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 */
export function positionText(sep: SepOption, mode: ModeOption): string {
  const sepLabel = sep === 'before' ? '在 ||| 前' : sep === 'after' ? '在 ||| 后' : '任意位置';
  const modeLabel = mode === 'top' ? '置顶' : mode === 'bottom' ? '置底' : '任意';
  return `${sepLabel} · ${modeLabel}`;
}

/**
 * 归一化标签用于补全匹配：小写并将空白替换为下划线。
 *
 * Args:
 *   value (string): 原始标签或查询文本。
 *
 * Returns:
 *   string: 归一化后的字符串。
 *
 * Callers:
 *   - `pages/tag-check/model.ts:matchTags`
 *   - `pages/tag-check/TagAutocomplete.tsx:highlightTag`
 */
export function normalizeTag(value: string): string {
  return value.toLowerCase().replace(/\s+/g, '_');
}

/**
 * 从标签统计中按子串匹配补全候选，先按匹配位置、再按出现次数排序。
 *
 * Args:
 *   tags (TagStatRow[]): 标签统计列表。
 *   query (string): 用户输入。
 *   limit (number): 最多返回条数。
 *
 * Returns:
 *   TagSuggestion[]: 补全候选，含匹配位置。
 *
 * Callers:
 *   - `pages/tag-check/TagAutocomplete.tsx:TagAutocomplete`
 */
export function matchTags(tags: TagStatRow[], query: string, limit: number): TagSuggestion[] {
  const normalized = normalizeTag(query.trim());
  if (!normalized) {
    return tags.slice(0, limit).map((item) => ({
      tag: item.tag,
      count: item.count,
      zh: item.zh ?? '',
      pos: 0,
    }));
  }
  const matched: TagSuggestion[] = [];
  for (const item of tags) {
    const index = normalizeTag(item.tag).indexOf(normalized);
    if (index >= 0) {
      matched.push({ tag: item.tag, count: item.count, zh: item.zh ?? '', pos: index });
    }
  }
  matched.sort((a, b) => (a.pos !== b.pos ? a.pos - b.pos : b.count - a.count));
  return matched.slice(0, limit);
}

/**
 * 将后端检查结果转换为带前端语义指标的状态项。
 *
 * Args:
 *   item (TagCheckResponseItem): 后端返回的单图检查结果。
 *   previous (TagCheckItem): 可选的既有状态项，用于保留相似度、掩码占比与掩码图。
 *
 * Returns:
 *   TagCheckItem: 页面使用的状态项。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckPage.tsx:runCheck`
 *   - `pages/tag-check/TagCheckPage.tsx:onCardClick`
 *   - `pages/tag-check/TagCheckPage.tsx:editSingle`
 */
export function toUiItem(item: TagCheckResponseItem, previous?: TagCheckItem): TagCheckItem {
  return {
    ...item,
    sim: previous?.sim ?? null,
    ratio: previous?.ratio ?? null,
    mask: previous?.mask ?? null,
  };
}

/**
 * 计算卡片缩略图 URL，无缩略图时回落到原图。
 *
 * Args:
 *   item (TagCheckItem): 图片状态项。
 *
 * Returns:
 *   string: 缩略图地址。
 *
 * Callers:
 *   - `pages/tag-check/model.ts:imageSource`
 *   - `pages/tag-check/Filmstrip.tsx:Filmstrip`
 *   - `pages/tag-check/SingleView.tsx:SingleView`
 */
export function thumbnailSource(item: TagCheckItem): string {
  return item.thumbnail_hash
    ? api.thumbHashUrl(item.thumbnail_hash)
    : api.localImageUrl(item.path);
}

/**
 * 计算瀑布流卡片图片 URL，开启蒙板预览时使用后端合成图。
 *
 * Args:
 *   item (TagCheckItem): 图片状态项。
 *   applyMask (boolean): 是否显示蒙板合成图。
 *
 * Returns:
 *   string: 图片地址。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckMasonry.tsx:TagCheckMasonry`
 */
export function imageSource(item: TagCheckItem, applyMask: boolean): string {
  if (applyMask) {
    return `${apiPath('/paint/mask-preview')}?path=${encodeURIComponent(item.path)}`;
  }
  return thumbnailSource(item);
}

/**
 * 读取当前引擎对应的排序指标。
 *
 * Args:
 *   item (TagCheckItem): 图片状态项。
 *   engine (Engine): 语义检索引擎。
 *
 * Returns:
 *   number | null: 相似度或掩码占比；无值时为 null。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckPage.tsx:applySort`
 *   - `pages/tag-check/SemanticDialog.tsx:SemanticDialog`
 */
export function metricOf(item: TagCheckItem, engine: Engine): number | null {
  return engine === 'concept' ? item.ratio : item.sim;
}

/**
 * 将异常转换为可展示的文本。
 *
 * Args:
 *   error (unknown): 捕获的异常。
 *
 * Returns:
 *   string: 异常文本。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 *   - `pages/tag-check/SemanticDialog.tsx:SemanticDialog`
 */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
