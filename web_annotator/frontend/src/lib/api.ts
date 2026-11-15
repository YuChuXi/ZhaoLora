import type {
  BatchParams,
  BatchSnapshot,
  CaptionDirsData,
  CaptionImage,
  CaptionStartBody,
  DirOption,
  DownloadStatus,
  LocalItem,
  Mode,
  Post,
  ScoreItem,
  SearchResult,
  SearchScoreItem,
  SortKey,
} from './types';

const dataset = decodeURIComponent(
  window.location.pathname.match(/^\/d\/([^/]+)/)?.[1] ?? '',
);

export function apiPath(suffix: string): string {
  return `/api/d/${encodeURIComponent(dataset)}${suffix}`;
}

export function datasetName(): string {
  return dataset;
}

/**
 * 请求失败时携带 HTTP 状态码的错误。
 *
 * Args:
 *   status (number): HTTP 状态码，供调用方区分资源不存在与临时故障。
 *   detail (string): 响应体文本。
 *
 * Returns:
 *   ApiError: 错误实例。
 *
 * Callers:
 *   - `frontend/src/lib/api.ts:request`
 *   - `frontend/src/app/AppShell.tsx:AppShell`
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    detail: string,
  ) {
    super(`${status}: ${detail}`);
    this.name = 'ApiError';
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    throw new ApiError(res.status, await res.text());
  }
  return res.json() as Promise<T>;
}

export interface RemoteBarField {
  type: 'text' | 'select' | 'button';
  id?: string;
  label?: string;
  placeholder?: string;
  options?: Array<{ value: string; label: string }>;
  role?: 'search' | 'batch' | 'meta';
  className?: string;
}

/** 工具栏布局: 本地操作集或在线来源的检索集. */
export type SourceLayout = 'local' | 'search';

export interface ConditionOption {
  value: string;
  label: string;
}

/** 来源条件的一个字段声明; 决定条件对话框的控件与条件串序列化方式. */
export interface ConditionField {
  kind: 'chips' | 'range' | 'text';
  /** token 前缀; `range` 的上下限键为 `{key}Lo` / `{key}Hi`. */
  key: string;
  label: string;
  options: ConditionOption[];
  /** `chips` 序列化方式: repeat 逐项 / comma 逗号 OR / exclude 三选二排除. */
  join: 'repeat' | 'comma' | 'exclude';
  date: boolean;
  raw: boolean;
  placeholder: string;
  /** `range` 下限 token 前缀; 与 `hi_token` 同为空时按 `{key}:>=` / `{key}:<=` 输出. */
  lo_token: string;
  /** `range` 上限 token 前缀. */
  hi_token: string;
}

/** 来源的额外检索条件声明. */
export interface SourceCondition {
  fields: ConditionField[];
  /** 条件串初始值. */
  default: string;
}

export interface SourceDef {
  remote: boolean;
  layout: SourceLayout;
  label: string;
  fields: RemoteBarField[];
  requireTags?: boolean;
  noJump?: boolean;
  fixedAspect?: string;
  hint?: string;
  /** 预览侧栏以自然语言段落展示正文, 缺省按标签芯片展示. */
  preview?: 'text';
}

const BOORU_ORDER = [
  { value: '', label: '默认' },
  { value: 'score', label: '分数' },
  { value: 'id', label: 'ID 新→旧' },
  { value: 'id_asc', label: 'ID 旧→新' },
];

export const SOURCES: Record<Mode, SourceDef> = {
  local: { remote: false, layout: 'local', label: '本地', fields: [] },
  gelbooru: {
    remote: true,
    layout: 'search',
    label: 'Gelbooru',
    fields: [
      { type: 'text', id: 'tags', placeholder: '标签，空格分隔' },
      { type: 'select', id: 'order', label: '排序', options: BOORU_ORDER },
      { type: 'button', role: 'search', label: '搜索' },
      { type: 'button', role: 'batch', label: '批量下载' },
      { type: 'button', role: 'meta', label: '元数据' },
    ],
  },
  danbooru: {
    remote: true,
    layout: 'search',
    label: 'Danbooru',
    fields: [
      { type: 'text', id: 'tags', placeholder: '标签，空格分隔' },
      { type: 'select', id: 'order', label: '排序', options: BOORU_ORDER },
      { type: 'button', role: 'search', label: '搜索' },
      { type: 'button', role: 'batch', label: '批量下载' },
      { type: 'button', role: 'meta', label: '元数据' },
    ],
  },
  e621: {
    remote: true,
    layout: 'search',
    label: 'e621',
    fields: [
      { type: 'text', id: 'tags', placeholder: '标签，空格分隔' },
      { type: 'select', id: 'order', label: '排序', options: BOORU_ORDER },
      { type: 'button', role: 'search', label: '搜索' },
      { type: 'button', role: 'batch', label: '批量下载' },
      { type: 'button', role: 'meta', label: '元数据' },
    ],
  },
  bilibili: {
    remote: true,
    layout: 'search',
    label: 'Bilibili',
    requireTags: true,
    noJump: true,
    preview: 'text',
    hint: '输入 B站 UID 搜索 · 左键写入目录 · 右键大图',
    fields: [
      { type: 'text', id: 'uid', placeholder: 'B站 UID' },
      { type: 'button', role: 'search', label: '搜索' },
      { type: 'button', role: 'batch', label: '批量下载' },
      { type: 'button', role: 'meta', label: '元数据' },
    ],
  },
  'bilibili-video': {
    remote: true,
    layout: 'search',
    label: 'Bilibili视频',
    requireTags: true,
    noJump: true,
    preview: 'text',
    fixedAspect: '16 / 9',
    hint: '输入 B站 UID 搜索 · 左键写入目录 · 右键大图',
    fields: [
      { type: 'text', id: 'uid', placeholder: 'B站 UID' },
      { type: 'button', role: 'search', label: '搜索' },
      { type: 'button', role: 'batch', label: '批量下载' },
      { type: 'button', role: 'meta', label: '元数据' },
    ],
  },
  x: {
    remote: true,
    layout: 'search',
    label: 'X',
    requireTags: true,
    noJump: true,
    preview: 'text',
    hint: '输入 X 用户名搜索 · 左键写入目录 · 右键大图',
    fields: [
      { type: 'text', id: 'uid', placeholder: 'X 用户名' },
      { type: 'button', role: 'search', label: '搜索' },
      { type: 'button', role: 'batch', label: '批量下载' },
      { type: 'button', role: 'meta', label: '元数据' },
    ],
  },
  pixiv: {
    remote: true,
    layout: 'search',
    label: 'Pixiv',
    requireTags: true,
    preview: 'text',
    hint: '输入画师 ID 或标签搜索 · 左键写入目录 · 右键大图',
    fields: [
      { type: 'text', id: 'query', placeholder: '画师 ID 或标签' },
      {
        type: 'select',
        id: 'mode',
        label: '类型',
        options: [
          { value: 'user', label: '画师' },
          { value: 'tag', label: '标签' },
        ],
      },
      { type: 'button', role: 'search', label: '搜索' },
      { type: 'button', role: 'batch', label: '批量下载' },
      { type: 'button', role: 'meta', label: '元数据' },
    ],
  },
  mihuashi: {
    remote: true,
    layout: 'search',
    label: '米画师',
    requireTags: true,
    preview: 'text',
    hint: '输入画师 UID 搜索 · 左键写入目录 · 右键大图',
    fields: [
      { type: 'text', id: 'uid', placeholder: '画师 UID' },
      {
        type: 'select',
        id: 'mode',
        label: '板块',
        options: [
          { value: 'artworks', label: '精选作品' },
          { value: 'stalls', label: '上架橱窗' },
          { value: 'draw_types', label: '价目表' },
        ],
      },
      { type: 'button', role: 'search', label: '搜索' },
      { type: 'button', role: 'batch', label: '批量下载' },
      { type: 'button', role: 'meta', label: '元数据' },
    ],
  },
  video: {
    remote: true,
    layout: 'search',
    label: '视频',
    noJump: true,
    hint: '本地 video 目录 · 点击进入抽帧',
    fields: [{ type: 'button', role: 'search', label: '刷新' }],
  },
};

export function isRemote(mode: Mode): boolean {
  return mode !== 'local';
}

function proxy(source: string, kind: string | null, id: string, url: string): string {
  const params = new URLSearchParams();
  if (kind) params.set('kind', kind);
  if (id) params.set('id', id);
  params.set('url', url);
  return `${apiPath(`/online-sources/${source}-image`)}?${params.toString()}`;
}

export function thumbUrl(post: Post): string {
  switch (post.source) {
    case 'gelbooru':
      return proxy('gelbooru', 'thumb', post.id, post.preview_url || post.sample_url || '');
    case 'e621':
      return proxy('e621', 'thumb', post.id, post.preview_url || post.sample_url || '');
    case 'danbooru':
      return proxy('danbooru', 'thumb', post.id, post.preview_url || post.sample_url || '');
    case 'bilibili':
      return proxy('bilibili', 'thumb', '', post.preview_url || post.sample_url || '');
    case 'bilibili-video':
      return proxy('bilibili-video', 'thumb', '', post.preview_url || post.sample_url || '');
    case 'x':
      return proxy('x', 'thumb', '', post.preview_url || post.sample_url || '');
    case 'pixiv':
      return proxy('pixiv', 'thumb', '', post.preview_url || post.sample_url || '');
    case 'mihuashi': {
      const url = post.preview_url || post.sample_url || '';
      return proxy('mihuashi', 'thumb', '', url ? `${url}!artwork.large` : '');
    }
    case 'video':
      return `${apiPath('/video/cover')}?path=${encodeURIComponent(post.path || '')}`;
    default:
      return post.preview_url || post.sample_url || '';
  }
}

export function sampleUrl(post: Post): string {
  switch (post.source) {
    case 'gelbooru':
      return proxy('gelbooru', 'sample', post.id, post.sample_url || post.file_url || post.preview_url || '');
    case 'e621':
      return proxy('e621', 'sample', post.id, post.sample_url || post.file_url || post.preview_url || '');
    case 'danbooru':
      return proxy('danbooru', 'sample', post.id, post.sample_url || post.file_url || post.preview_url || '');
    case 'bilibili':
      return proxy('bilibili', null, '', post.sample_url || post.file_url || post.preview_url || '');
    case 'bilibili-video':
      return proxy('bilibili-video', null, '', post.sample_url || post.file_url || post.preview_url || '');
    case 'x':
      return proxy('x', null, '', post.sample_url || post.file_url || post.preview_url || '');
    case 'pixiv':
      return proxy('pixiv', null, '', post.sample_url || post.file_url || post.preview_url || '');
    case 'mihuashi':
      return proxy('mihuashi', null, '', post.sample_url || post.file_url || post.preview_url || '');
    default:
      return post.sample_url || post.file_url || post.preview_url || '';
  }
}

/**
 * 帖子在原站的页面地址.
 *
 * Args:
 *   post (Post): 搜索结果帖子.
 *   artist (string): 当前来源的查询值; 米画师橱窗与价目表条目无独立页面,
 *     需用画师 UID 跳主页.
 *
 * Returns:
 *   string: 原站地址; 来源无页面时为 `#`.
 *
 * Callers:
 *   - `filter/Dialogs.tsx:PreviewDialog`
 */
export function pageUrl(post: Post, artist = ''): string {
  switch (post.source) {
    case 'gelbooru':
      return `https://gelbooru.com/index.php?page=post&s=view&id=${post.id}`;
    case 'danbooru':
      return `https://danbooru.donmai.us/posts/${post.id}`;
    case 'e621':
      return `https://e621.net/posts/${post.id}`;
    case 'bilibili': {
      const i = post.id.lastIndexOf('_');
      return `https://t.bilibili.com/${i > 0 ? post.id.slice(0, i) : post.id}`;
    }
    case 'bilibili-video':
      return `https://www.bilibili.com/video/${post.id}`;
    case 'x': {
      const parts = post.id.split('_');
      return `https://x.com/${parts[0]}/status/${parts[1] ?? ''}`;
    }
    case 'pixiv': {
      const i = post.id.indexOf('_');
      return `https://www.pixiv.net/artworks/${i > 0 ? post.id.slice(0, i) : post.id}`;
    }
    case 'mihuashi':
      return post.id.startsWith('a')
        ? `https://www.mihuashi.com/artworks/${post.id.slice(1)}`
        : `https://www.mihuashi.com/profiles/${artist}`;
    default:
      return '#';
  }
}

export interface RemoteQuery {
  source: Mode;
  tags: string;
  extra: string;
  order: string;
  page: number;
  session: string;
  dir: string;
}

export interface DownloadBody {
  source: string;
  id: string;
  filename: string;
  dir: string;
  overwrite: boolean;
  post: Post;
}

export const api = {
  listDirs: () => request<{ dirs: DirOption[]; default: string }>(apiPath('/dirs')),
  listImages: (dirs: string[], showTrash: boolean) => {
    const params = new URLSearchParams({ dirs: dirs.join(','), show_trash: showTrash ? '1' : '0' });
    return request<LocalItem[]>(`${apiPath('/images')}?${params.toString()}`);
  },
  toggle: (path: string) =>
    request<{ selected: boolean }>(apiPath('/toggle'), {
      method: 'POST',
      body: JSON.stringify({ path }),
    }),
  selectRange: (paths: string[]) =>
    request<unknown>(apiPath('/select-range'), {
      method: 'POST',
      body: JSON.stringify({ paths }),
    }),
  selectAll: (dirs: string[], showTrash: boolean) =>
    request<unknown>(apiPath('/select-all'), {
      method: 'POST',
      body: JSON.stringify({ dirs, show_trash: showTrash }),
    }),
  deselectAll: () => request<unknown>(apiPath('/deselect-all'), { method: 'POST' }),
  applyDelete: () => request<unknown>(apiPath('/apply'), { method: 'POST' }),
  restore: (paths: string[]) =>
    request<unknown>(apiPath('/restore'), {
      method: 'POST',
      body: JSON.stringify({ paths }),
    }),
  getSettings: () => request<{ settings: Record<string, unknown> }>(apiPath('/settings')),
  saveSettings: (patch: Record<string, unknown>, datasetLevel: boolean) =>
    request<unknown>(datasetLevel ? apiPath('/settings') : '/api/settings', {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),
  onlineSources: () =>
    request<{
      sources: string[];
      conditions: Record<string, SourceCondition>;
      proxy: string;
    }>(apiPath('/online-sources/sources')),
  search: (body: RemoteQuery) =>
    request<SearchResult>(apiPath('/online-sources/search'), {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  download: (body: DownloadBody, signal?: AbortSignal) =>
    request<{ job_id?: string; path?: string; skipped?: boolean }>(apiPath('/online-sources/download'), {
      method: 'POST',
      body: JSON.stringify(body),
      signal,
    }),
  undownload: (body: DownloadBody) =>
    request<unknown>(apiPath('/online-sources/undownload'), {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  tagSuggest: (source: Mode, q: string, signal?: AbortSignal) => {
    const params = new URLSearchParams({ source, q, limit: '12' });
    return request<{ tags: Array<{ name: string; note: string; count: number }> }>(
      `${apiPath('/online-sources/tag-suggest')}?${params.toString()}`,
      { signal },
    );
  },
  scoreItems: () => request<{ items: ScoreItem[] }>(apiPath('/scoring/items')),
  scoringTagSuggest: (q: string, signal?: AbortSignal) => {
    const params = new URLSearchParams({ q, limit: '12' });
    return request<{ tags: Array<{ name: string; category: string }> }>(
      `${apiPath('/scoring/tags/suggest')}?${params.toString()}`,
      { signal },
    );
  },
  batchStart: (body: Record<string, unknown>) =>
    request<{ job_id: string }>(apiPath('/online-sources/batch/start'), {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  batchParams: (body: { limit?: number; conn?: number; rate_rps?: number }) =>
    request<{ job_id: string }>(apiPath('/online-sources/batch/params'), {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  regenerateCaptions: (body: Record<string, unknown>) =>
    request<{ ok: boolean; count: number }>(apiPath('/online-sources/regenerate-captions'), {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  refreshMetadata: (body: { dir: string }) =>
    request<{ job_id: string }>(apiPath('/online-sources/refresh-metadata'), {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  listVideos: () => request<{ videos: Post[] }>(`${apiPath('/videos')}?probe=1`),
  cancelJob: (jobId: string) =>
    request<unknown>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
  similar: (path: string, limit = 12) => {
    const params = new URLSearchParams({ path, limit: String(limit) });
    return request<{ similar: LocalItem[] }>(`${apiPath('/images/similar')}?${params.toString()}`);
  },
  captionDirs: () => request<CaptionDirsData>(apiPath('/caption/dirs')),
  captionList: (dirs: string[]) =>
    request<{ items: CaptionImage[] }>(apiPath('/caption/list'), {
      method: 'POST',
      body: JSON.stringify({ dirs }),
    }),
  captionStart: (body: CaptionStartBody) =>
    request<{ job_id: string }>(apiPath('/caption/start'), {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  captionDelete: (paths: string[]) =>
    request<{ deleted: number }>(apiPath('/caption/delete'), {
      method: 'POST',
      body: JSON.stringify({ paths }),
    }),
  findActiveJob: (kind: string) =>
    request<BatchSnapshot | null>(
      `/api/jobs/active?kind=${encodeURIComponent(kind)}&dataset=${encodeURIComponent(dataset)}`,
    ),
  semanticQuery: (query: string, dirs: string[]) =>
    request<{ job_id: string }>(apiPath('/tags/embedding/query'), {
      method: 'POST',
      body: JSON.stringify({ query, dirs }),
    }),
  tagQuery: (query: string, dirs: string[]) =>
    request<{ items: SearchScoreItem[]; missing: number }>(apiPath('/scoring/tag-query'), {
      method: 'POST',
      body: JSON.stringify({ query, dirs }),
    }),
  embeddingHistory: () =>
    request<{ queries: Array<{ query: string; time?: number }> }>(apiPath('/tags/embedding/history')),
  localImageUrl: (path: string) => `${apiPath('/image')}?path=${encodeURIComponent(path)}`,
  thumbHashUrl: (hash: string) =>
    `/d/${encodeURIComponent(dataset)}/thumb/${encodeURIComponent(hash)}.avif`,
  watchJob: (jobId: string, onEvent: (snap: BatchSnapshot) => void) => {
    let closed = false;
    let retry = 0;
    let source: EventSource | null = null;
    const connect = () => {
      if (closed) return;
      source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
      source.onmessage = (event) => {
        retry = 0;
        const snap = JSON.parse(event.data) as BatchSnapshot;
        onEvent(snap);
        if (snap.status === 'done' || snap.status === 'error' || snap.status === 'cancelled') {
          closed = true;
          source?.close();
        }
      };
      source.onerror = () => {
        source?.close();
        if (closed) return;
        const delay = Math.min(1000 * 2 ** retry, 10000);
        retry += 1;
        window.setTimeout(connect, delay);
      };
    };
    connect();
    return () => {
      closed = true;
      source?.close();
    };
  },
};

export function sortItems(
  items: LocalItem[],
  key: SortKey,
  scores?: Record<string, number>,
): LocalItem[] {
  const out = items.slice();
  if (key === 'search' && scores) {
    out.sort((a, b) => {
      const sa = scores[a.path];
      const sb = scores[b.path];
      const va = typeof sa === 'number' ? sa : Number.NEGATIVE_INFINITY;
      const vb = typeof sb === 'number' ? sb : Number.NEGATIVE_INFINITY;
      if (vb !== va) return vb - va;
      return a.stem.localeCompare(b.stem, 'en', { numeric: true });
    });
    return out;
  }
  if (key === 'posted') {
    out.sort((a, b) => {
      const ta = a.posted_at || '';
      const tb = b.posted_at || '';
      if (ta && tb && ta !== tb) return ta < tb ? 1 : -1;
      if (ta !== tb) return ta ? -1 : 1;
      return a.stem.localeCompare(b.stem, 'en', { numeric: true });
    });
  } else if (key === 'aesthetic') {
    out.sort((a, b) => {
      const da = typeof a.aesthetic === 'number' ? a.aesthetic : -1;
      const db = typeof b.aesthetic === 'number' ? b.aesthetic : -1;
      if (db !== da) return db - da;
      return a.stem.localeCompare(b.stem, 'en');
    });
  } else if (key === 'pixels') {
    out.sort((a, b) => {
      const pa = a.width * a.height;
      const pb = b.width * b.height;
      if (pb !== pa) return pb - pa;
      return a.stem.localeCompare(b.stem, 'en');
    });
  } else if (key.startsWith('score:')) {
    const name = key.slice(6);
    out.sort((a, b) => {
      const da = typeof a.scores?.[name] === 'number' ? a.scores[name] : -1;
      const db = typeof b.scores?.[name] === 'number' ? b.scores[name] : -1;
      if (db !== da) return db - da;
      return a.stem.localeCompare(b.stem, 'en', { numeric: true });
    });
  } else {
    out.sort((a, b) => a.stem.localeCompare(b.stem, 'en', { numeric: true }));
  }
  return out;
}

export function statusLabel(status?: DownloadStatus): string {
  if (status === 'downloading') return '保存中…';
  if (status === 'done') return '已保存';
  if (status === 'error') return '失败';
  return '未保存';
}
