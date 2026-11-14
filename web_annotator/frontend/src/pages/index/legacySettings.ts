import {
  listTasks,
  readDatasetSettings,
  readGlobalSettings,
  writeDatasetSettings,
  writeGlobalSettings,
} from './homeApi';

/** 旧版全局设置键（目录选择、排序、两处列数）。 */
const LEGACY_FIXED_KEYS = [
  'annotator_work_dirs',
  'annotator_work_dir',
  'annotator_sort',
  'zhao_filter_cols',
  'zhao_tagcheck_cols',
];

/** 旧版任务级设置键前缀（描述规则与裁剪部位，后缀为任务名）。 */
const LEGACY_PREFIXES = [
  'zhao_caption_rules:',
  'zhao_caption_inject:',
  'crop_part_defaults:',
  'crop_auto_parts:',
  'crop_auto_parts_deny:',
];

/** 单个任务待写入数据集层的旧设置。 */
interface LegacyTaskPatch {
  rules?: unknown;
  inject?: unknown;
  part_defaults?: unknown;
  auto_parts?: unknown;
  denied_parts?: unknown;
}

/**
 * 扫描 localStorage 中实际残留的旧设置键。
 *
 * Returns:
 *   string[]: 旧设置键名列表；无残留时为空数组。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx` 启动时判断导入按钮是否展示。
 *   - `pages/index/legacySettings.ts:importLegacySettings`
 */
export function legacyKeys(): string[] {
  const keys: string[] = [];
  for (const key of LEGACY_FIXED_KEYS) {
    if (localStorage.getItem(key) != null) keys.push(key);
  }
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index);
    if (key == null) continue;
    if (LEGACY_PREFIXES.some((prefix) => key.startsWith(prefix))) keys.push(key);
  }
  return keys;
}

/**
 * 解析旧设置的 JSON 文本。
 *
 * 旧设置可能被手工写入或已损坏；损坏内容返回 null 并由导入流程跳过，
 * 从而保留原键以便用户修复后重试。
 *
 * Args:
 *   text (string | undefined): localStorage 中的原始文本。
 *
 * Returns:
 *   unknown: 解析结果；缺失或解析失败时为 null。
 *
 * Callers:
 *   - `pages/index/legacySettings.ts:importLegacySettings`
 */
function readLegacyJson(text: string | undefined): unknown {
  if (text == null) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/**
 * 把未知值规整为普通对象，非对象或数组时返回空对象。
 *
 * Args:
 *   value (unknown): 设置中的子键值。
 *
 * Returns:
 *   Record<string, unknown>: 可安全合并的子键容器。
 *
 * Callers:
 *   - `pages/index/legacySettings.ts:importLegacySettings`
 */
function asObject(value: unknown): Record<string, unknown> {
  if (value != null && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

/**
 * 将当前 localStorage 中的旧版设置导入服务端：全局参数合并写入全局缺省层，
 * 描述规则与裁剪部位按任务名分别合并写入对应任务的数据集层，全部成功后
 * 清除旧键；任一步失败即中止并保留旧键，可重试（重复写入同值无害）。
 *
 * Returns:
 *   Promise<number>: 覆盖到的任务数；无旧数据时为 0。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:importLegacy`
 */
export async function importLegacySettings(): Promise<number> {
  const keys = legacyKeys();
  if (!keys.length) return 0;

  const raw: Record<string, string> = {};
  for (const key of keys) {
    const value = localStorage.getItem(key);
    if (value != null) raw[key] = value;
  }

  // 全局参数：目录选择（work_dirs 优先，回退旧版单目录键）、排序、两处列数。
  const dirsRaw = raw['annotator_work_dirs'] ?? raw['annotator_work_dir'];
  const filterCols = Number.parseInt(raw['zhao_filter_cols'] ?? '', 10);
  const tagCols = Number.parseInt(raw['zhao_tagcheck_cols'] ?? '', 10);
  const globalPatch: Record<string, unknown> = {};
  if (dirsRaw != null) {
    const dirs = dirsRaw
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
    if (dirs.length) globalPatch.dirs = dirs;
  }
  if (raw['annotator_sort'] != null) globalPatch.sort = raw['annotator_sort'];
  if (filterCols >= 0) globalPatch.filter = { cols: filterCols };
  if (tagCols >= 0) globalPatch.tag_check = { cols: tagCols };

  // 任务键按后缀分组为数据集层 patch 的子键。
  const taskPatches: Record<string, LegacyTaskPatch> = {};
  for (const prefix of LEGACY_PREFIXES) {
    for (const key of Object.keys(raw)) {
      if (!key.startsWith(prefix)) continue;
      const name = key.slice(prefix.length);
      const value = readLegacyJson(raw[key]);
      if (!value) continue;
      const patch = taskPatches[name] ?? {};
      taskPatches[name] = patch;
      if (prefix === 'zhao_caption_rules:') patch.rules = value;
      else if (prefix === 'zhao_caption_inject:') patch.inject = value;
      else if (prefix === 'crop_part_defaults:') patch.part_defaults = value;
      else if (prefix === 'crop_auto_parts:') patch.auto_parts = value;
      else patch.denied_parts = value;
    }
  }

  if (Object.keys(globalPatch).length) {
    const current = await readGlobalSettings();
    const settings = current.settings ?? {};
    // 列数是 filter/tag_check 的子键，合并进全局层现值以避免覆盖其余子键。
    if (filterCols >= 0) {
      const filter = asObject(settings.filter);
      filter.cols = filterCols;
      globalPatch.filter = filter;
    }
    if (tagCols >= 0) {
      const tagCheck = asObject(settings.tag_check);
      tagCheck.cols = tagCols;
      globalPatch.tag_check = tagCheck;
    }
    await writeGlobalSettings(globalPatch);
  }

  // 已归档或已删除任务的旧键无处可写，随本次导入一并丢弃。
  const known = new Set<string>();
  const list = await listTasks('sort_order', 'asc');
  for (const task of list.tasks ?? []) known.add(task.name);

  let imported = 0;
  for (const name of Object.keys(taskPatches)) {
    if (!known.has(name)) continue;
    const current = await readDatasetSettings(name);
    const settings = current.settings ?? {};
    const legacy = taskPatches[name];
    const patch: Record<string, unknown> = {};

    if (legacy.rules != null || legacy.inject != null) {
      const caption = asObject(settings.caption);
      if (legacy.rules != null) caption.rules = legacy.rules;
      if (legacy.inject != null) caption.inject = legacy.inject;
      patch.caption = caption;
    }
    if (
      legacy.part_defaults != null ||
      legacy.auto_parts != null ||
      legacy.denied_parts != null
    ) {
      const crop = asObject(settings.crop);
      if (legacy.part_defaults != null) crop.part_defaults = legacy.part_defaults;
      if (legacy.auto_parts != null) crop.auto_parts = legacy.auto_parts;
      if (legacy.denied_parts != null) crop.denied_parts = legacy.denied_parts;
      patch.crop = crop;
    }

    if (Object.keys(patch).length) await writeDatasetSettings(name, patch);
    imported += 1;
  }

  for (const key of keys) localStorage.removeItem(key);
  return imported;
}
