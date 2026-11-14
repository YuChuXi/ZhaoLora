import { request } from '@/lib/api';

/** 任务排序字段。 */
export type SortField = 'sort_order' | 'name' | 'image_count' | 'created';

/** 任务排序方向。 */
export type SortOrder = 'asc' | 'desc';

/** 首页任务卡片数据。 */
export interface TaskItem {
  name: string;
  completed: boolean;
  image_count: number;
  preview_hashes: string[];
  report_ready: boolean;
  note: string;
  trigger_word: string;
  created: number;
  sort_order: number;
  archived: boolean;
  archive_base: string;
  archived_at: number | null;
}

/** 任务列表响应。 */
export interface TaskListData {
  tasks: TaskItem[];
}

/** 未登记候选目录响应。 */
export interface TaskCandidatesData {
  candidates: string[];
}

/** 任务写操作回执。 */
export interface TaskOkData {
  ok: boolean;
  name: string;
  completed: boolean;
  note: string;
  trigger_word: string;
}

/** 长任务提交回执。 */
export interface JobSubmitData {
  ok?: boolean;
  job_id: string;
}

/** 长任务快照（活跃查询与 SSE 事件共用）。 */
export interface JobSnapshot {
  job_id?: string;
  status?: string;
  dataset?: string;
  message?: string;
  phase?: string;
  pct?: number | null;
  done?: number;
  total?: number;
}

/** 文档导航项。 */
export interface DocNavItem {
  slug: string;
  label: string;
}

/** 文档导航响应。 */
export interface DocsNavData {
  nav: DocNavItem[];
}

/** 单篇文档内容。 */
export interface DocContentData {
  slug: string;
  file: string;
  title: string;
  html: string;
}

/** 设置读取响应。 */
export interface SettingsData {
  settings: Record<string, unknown>;
}

/**
 * 提取异常的可读文本。
 *
 * Args:
 *   error (unknown): 捕获到的异常。
 *
 * Returns:
 *   string: 异常消息或字符串化结果。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx` 的任务、候选与导入流程。
 *   - `pages/index/IndexPage.tsx` 的文档加载流程。
 *   - `pages/index/TaskCard.tsx` 的内联编辑保存失败提示。
 */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 读取任务列表，排序由服务端完成。
 *
 * Args:
 *   sortBy (SortField): 排序字段。
 *   sortOrder (SortOrder): 排序方向。
 *
 * Returns:
 *   Promise<TaskListData>: 任务卡片列表。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:reloadTasks`
 *   - `pages/index/legacySettings.ts:importLegacySettings`
 */
export function listTasks(sortBy: SortField, sortOrder: SortOrder): Promise<TaskListData> {
  const params = new URLSearchParams({ sort_by: sortBy, sort_order: sortOrder });
  return request<TaskListData>(`/api/tasks?${params.toString()}`);
}

/**
 * 读取任务根下尚未登记、名称合法的候选目录。
 *
 * Returns:
 *   Promise<TaskCandidatesData>: 候选目录名列表。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:loadCandidates`
 */
export function listCandidates(): Promise<TaskCandidatesData> {
  return request<TaskCandidatesData>('/api/tasks/candidates');
}

/**
 * 登记新任务并创建必要目录。
 *
 * Args:
 *   name (string): 任务目录名。
 *
 * Returns:
 *   Promise<TaskOkData>: 新任务回执。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:submitTask`
 */
export function addTask(name: string): Promise<TaskOkData> {
  return request<TaskOkData>('/api/tasks', {
    method: 'POST',
    body: JSON.stringify({ name }),
  });
}

/**
 * 标记任务完成或重新打开。
 *
 * Args:
 *   name (string): 任务名。
 *   completed (boolean): True 标记完成，False 重新打开。
 *
 * Returns:
 *   Promise<TaskOkData>: 最新任务回执。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:changeCompleted`
 */
export function setTaskCompleted(name: string, completed: boolean): Promise<TaskOkData> {
  const action = completed ? 'complete' : 'reopen';
  return request<TaskOkData>(`/api/tasks/${encodeURIComponent(name)}/${action}`, {
    method: 'POST',
    body: '{}',
  });
}

/**
 * 修改任务备注。
 *
 * Args:
 *   name (string): 任务名。
 *   note (string): 备注文本。
 *
 * Returns:
 *   Promise<TaskOkData>: 最新任务回执。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:saveNote`
 */
export function setTaskNote(name: string, note: string): Promise<TaskOkData> {
  return request<TaskOkData>(`/api/tasks/${encodeURIComponent(name)}/note`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  });
}

/**
 * 修改任务触发词。
 *
 * Args:
 *   name (string): 任务名。
 *   triggerWord (string): 触发词文本。
 *
 * Returns:
 *   Promise<TaskOkData>: 最新任务回执。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:saveTrigger`
 */
export function setTaskTrigger(name: string, triggerWord: string): Promise<TaskOkData> {
  return request<TaskOkData>(`/api/tasks/${encodeURIComponent(name)}/trigger-word`, {
    method: 'POST',
    body: JSON.stringify({ trigger_word: triggerWord }),
  });
}

/**
 * 提交任务归档长任务。
 *
 * Args:
 *   name (string): 任务名。
 *
 * Returns:
 *   Promise<JobSubmitData>: 含 job_id 的提交回执。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:archiveTask`
 */
export function archiveTask(name: string): Promise<JobSubmitData> {
  return request<JobSubmitData>(`/api/tasks/${encodeURIComponent(name)}/archive`, {
    method: 'POST',
  });
}

/**
 * 提交归档恢复长任务。
 *
 * Args:
 *   base (string): 归档基本名。
 *
 * Returns:
 *   Promise<JobSubmitData>: 含 job_id 的提交回执。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:restoreTask`
 */
export function restoreTask(base: string): Promise<JobSubmitData> {
  return request<JobSubmitData>(`/api/archived/${encodeURIComponent(base)}/restore`, {
    method: 'POST',
  });
}

/**
 * 按给定任务名顺序重设手动排序。
 *
 * Args:
 *   order (string[]): 按目标顺序排列的任务名。
 *
 * Returns:
 *   Promise<unknown>: 服务端回执。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:handleDrop`
 */
export function reorderTasks(order: string[]): Promise<unknown> {
  return request<unknown>('/api/tasks/reorder', {
    method: 'POST',
    body: JSON.stringify({ order }),
  });
}

/**
 * 读取文档导航。
 *
 * Returns:
 *   Promise<DocsNavData>: 文档导航项列表。
 *
 * Callers:
 *   - `pages/index/IndexPage.tsx` 启动时加载侧栏导航。
 */
export function listDocs(): Promise<DocsNavData> {
  return request<DocsNavData>('/api/docs');
}

/**
 * 读取并渲染单篇文档。
 *
 * Args:
 *   slug (string): 文档标识。
 *
 * Returns:
 *   Promise<DocContentData>: 含已渲染 HTML 的文档内容。
 *
 * Callers:
 *   - `pages/index/IndexPage.tsx` 的文档切换流程。
 */
export function getDoc(slug: string): Promise<DocContentData> {
  return request<DocContentData>(`/api/docs/${encodeURIComponent(slug)}`);
}

/**
 * 查询未结束的归档类长任务，供刷新后重连进度。
 *
 * Returns:
 *   Promise<JobSnapshot | null>: 活跃任务快照；无活跃任务时为 null。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:reconnectArchive`
 */
export async function findActiveArchiveJob(): Promise<JobSnapshot | null> {
  const snapshot = await request<JobSnapshot>('/api/jobs/active?kind=archive');
  if (!snapshot.job_id || snapshot.status === 'idle') return null;
  return snapshot;
}

/**
 * 读取全局缺省层设置。
 *
 * Returns:
 *   Promise<SettingsData>: 全局设置全量。
 *
 * Callers:
 *   - `pages/index/legacySettings.ts:importLegacySettings`
 */
export function readGlobalSettings(): Promise<SettingsData> {
  return request<SettingsData>('/api/settings');
}

/**
 * 按键合并写入全局缺省层设置。
 *
 * Args:
 *   patch (Record<string, unknown>): 待合并的键值映射。
 *
 * Returns:
 *   Promise<SettingsData>: 写入后的全局设置。
 *
 * Callers:
 *   - `pages/index/legacySettings.ts:importLegacySettings`
 */
export function writeGlobalSettings(patch: Record<string, unknown>): Promise<SettingsData> {
  return request<SettingsData>('/api/settings', {
    method: 'PUT',
    body: JSON.stringify(patch),
  });
}

/**
 * 读取任务数据集层设置。
 *
 * Args:
 *   name (string): 任务名。
 *
 * Returns:
 *   Promise<SettingsData>: 合并视图设置。
 *
 * Callers:
 *   - `pages/index/legacySettings.ts:importLegacySettings`
 */
export function readDatasetSettings(name: string): Promise<SettingsData> {
  return request<SettingsData>(`/api/d/${encodeURIComponent(name)}/settings`);
}

/**
 * 按键合并写入任务数据集层设置。
 *
 * Args:
 *   name (string): 任务名。
 *   patch (Record<string, unknown>): 待合并的键值映射。
 *
 * Returns:
 *   Promise<SettingsData>: 写入后的数据集层设置。
 *
 * Callers:
 *   - `pages/index/legacySettings.ts:importLegacySettings`
 */
export function writeDatasetSettings(
  name: string,
  patch: Record<string, unknown>,
): Promise<SettingsData> {
  return request<SettingsData>(`/api/d/${encodeURIComponent(name)}/settings`, {
    method: 'PUT',
    body: JSON.stringify(patch),
  });
}
