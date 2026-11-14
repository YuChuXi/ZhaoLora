import type {
  BatchSnapshot,
  DirOption,
  DownloadEntry,
  LocalItem,
  Mode,
  Post,
  SortKey,
} from '@/lib/types';

export interface PersistedSource {
  fields: Record<string, string>;
  page: number;
}

export interface PersistedFilter {
  mode: Mode;
  cols: number;
  sort: SortKey;
  showTrash: boolean;
  dirs: string[];
  sources: Partial<Record<Mode, PersistedSource>>;
}

export interface RemoteState {
  fields: Record<string, string>;
  pageLo: number;
  pageHi: number;
  session: string;
  hasMore: boolean;
  hasPrev: boolean;
  loading: boolean;
  posts: Post[];
}

export interface SimilarState {
  path: string;
  items: LocalItem[];
  loading: boolean;
}

export interface SearchState {
  prefix: 'S' | 'T';
  query: string;
  scores: Record<string, number>;
  missing: number;
}

export interface PageState {
  ready: boolean;
  mode: Mode;
  dirs: string[];
  allDirs: DirOption[];
  columns: number;
  sort: SortKey;
  showTrash: boolean;
  local: LocalItem[];
  localLoaded: boolean;
  selected: Record<string, boolean>;
  cursor: string | null;
  remote: RemoteState;
  /** 远端图片的保存状态；无条目即未保存，与本地模式的 `selected` 无关。 */
  downloads: Record<string, DownloadEntry>;
  preview: Post | null;
  similar: SimilarState | null;
  search: SearchState | null;
  searchOpen: boolean;
  searchBusy: boolean;
  searchMessage: string;
  batchOpen: boolean;
  batch: BatchSnapshot | null;
  metaOpen: boolean;
  meta: BatchSnapshot | null;
  error: string | null;
}

export const emptyRemote = (): RemoteState => ({
  fields: {},
  pageLo: 1,
  pageHi: 1,
  session: '',
  hasMore: false,
  hasPrev: false,
  loading: false,
  posts: [],
});

export const initialPersisted: PersistedFilter = {
  mode: 'local',
  cols: 0,
  sort: 'name',
  showTrash: false,
  dirs: ['main'],
  sources: {},
};

export const initialState: PageState = {
  ready: false,
  mode: 'local',
  dirs: ['main'],
  allDirs: [],
  columns: 0,
  sort: 'name',
  showTrash: false,
  local: [],
  localLoaded: false,
  selected: {},
  cursor: null,
  remote: emptyRemote(),
  downloads: {},
  preview: null,
  similar: null,
  search: null,
  searchOpen: false,
  searchBusy: false,
  searchMessage: '',
  batchOpen: false,
  batch: null,
  metaOpen: false,
  meta: null,
  error: null,
};

export type Action =
  | { type: 'init'; dirs: string[]; allDirs: DirOption[]; persisted: PersistedFilter }
  | { type: 'setMode'; mode: Mode; fields?: Record<string, string> }
  | { type: 'setDirs'; dirs: string[] }
  | { type: 'setColumns'; columns: number }
  | { type: 'setSort'; sort: SortKey }
  | { type: 'setTrash'; showTrash: boolean }
  | { type: 'localLoaded'; items: LocalItem[] }
  | { type: 'field'; key: string; value: string }
  | { type: 'remoteStart'; reset: boolean }
  | { type: 'remoteLoaded'; posts: Post[]; page: number; hasMore: boolean; session: string; reset: boolean; up: boolean }
  | { type: 'remoteError'; error: string }
  | { type: 'toggle'; key: string; on: boolean }
  | { type: 'selectRange'; keys: string[] }
  | { type: 'selectAll'; keys: string[] }
  | { type: 'clearSelection' }
  | { type: 'preview'; post: Post | null }
  | { type: 'similar'; similar: SimilarState | null }
  | { type: 'searchOpen'; open: boolean }
  | { type: 'searchBusy'; busy: boolean }
  | { type: 'searchMessage'; message: string }
  | { type: 'searchResult'; search: SearchState }
  | { type: 'searchClear' }
  | { type: 'download'; key: string; entry: DownloadEntry }
  | { type: 'downloadClear'; key: string }
  | { type: 'batchOpen'; open: boolean }
  | { type: 'batch'; snapshot: BatchSnapshot | null }
  | { type: 'metaOpen'; open: boolean }
  | { type: 'meta'; snapshot: BatchSnapshot | null }
  | { type: 'error'; error: string | null };

function selectedFrom(items: LocalItem[], selected: Record<string, boolean>): Record<string, boolean> {
  const next: Record<string, boolean> = {};
  for (const item of items) {
    if (selected[item.path] || item.selected) next[item.path] = true;
  }
  return next;
}

function modeFields(persisted: PersistedFilter, mode: Mode): PersistedSource {
  return persisted.sources[mode] ?? { fields: {}, page: 1 };
}

/**
 * 由服务端返回的已保存标记与在途任务重建图片状态表。
 *
 * 在途任务优先于服务端标记：图片正在下载时，即使服务端尚未记录已完成，也保留进度。
 *
 * Args:
 *   current (Record<string, DownloadEntry>): 变更前的状态表。
 *   posts (Post[]): 本次返回的图片。
 *
 * Returns:
 *   Record<string, DownloadEntry>: 仅覆盖本次返回图片的状态条目。
 *
 * Callers:
 *   - `filter/state.ts:reducer`（`remoteLoaded` 分支）
 */
function downloadsFrom(
  current: Record<string, DownloadEntry>,
  posts: Post[],
): Record<string, DownloadEntry> {
  const next: Record<string, DownloadEntry> = {};
  for (const post of posts) {
    const live = current[post.key];
    if (live?.status === 'downloading') next[post.key] = live;
    else if (post.downloaded) next[post.key] = { status: 'done', progress: '' };
  }
  return next;
}

export function reducer(state: PageState, action: Action): PageState {
  switch (action.type) {
    case 'init': {
      const source = modeFields(action.persisted, action.persisted.mode);
      return {
        ...state,
        ready: true,
        mode: action.persisted.mode,
        dirs: action.dirs,
        allDirs: action.allDirs,
        columns: action.persisted.cols,
        sort: action.persisted.sort,
        showTrash: action.persisted.showTrash,
        remote: {
          ...emptyRemote(),
          fields: { ...source.fields },
          pageLo: source.page,
          pageHi: source.page,
        },
      };
    }
    case 'setMode': {
      if (action.mode === state.mode) return state;
      return {
        ...state,
        mode: action.mode,
        selected: {},
        cursor: null,
        downloads: {},
        preview: null,
        similar: null,
        search: null,
        searchMessage: '',
        remote: { ...emptyRemote(), fields: { ...(action.fields ?? {}) } },
        local: [],
        localLoaded: false,
        error: null,
      };
    }
    case 'setDirs':
      return { ...state, dirs: action.dirs, cursor: null };
    case 'setColumns':
      return { ...state, columns: action.columns };
    case 'setSort':
      return { ...state, sort: action.sort };
    case 'setTrash':
      return { ...state, showTrash: action.showTrash, cursor: null };
    case 'localLoaded': {
      const selected = selectedFrom(action.items, state.selected);
      return { ...state, local: action.items, localLoaded: true, selected };
    }
    case 'field':
      return {
        ...state,
        remote: { ...state.remote, fields: { ...state.remote.fields, [action.key]: action.value } },
      };
    case 'remoteStart':
      return {
        ...state,
        error: null,
        remote: action.reset
          ? { ...state.remote, loading: true, posts: [], session: '', hasMore: false, hasPrev: false }
          : { ...state.remote, loading: true },
      };
    case 'remoteLoaded': {
      const downloads = downloadsFrom(state.downloads, action.posts);
      return {
        ...state,
        remote: {
          ...state.remote,
          loading: false,
          posts: action.reset
            ? action.posts
            : action.up
              ? [...action.posts, ...state.remote.posts]
              : [...state.remote.posts, ...action.posts],
          pageLo: action.reset || action.up ? action.page : state.remote.pageLo,
          pageHi: action.reset || !action.up ? action.page : state.remote.pageHi,
          hasMore: action.reset || !action.up ? action.hasMore : state.remote.hasMore,
          hasPrev: action.reset ? action.page > 1 : action.up ? action.page > 1 : state.remote.hasPrev,
          session: action.session || state.remote.session,
        },
        downloads: action.reset ? downloads : { ...state.downloads, ...downloads },
      };
    }
    case 'remoteError':
      return { ...state, error: action.error, remote: { ...state.remote, loading: false } };
    case 'toggle':
      return {
        ...state,
        selected: { ...state.selected, [action.key]: action.on },
        cursor: action.key,
      };
    case 'selectAll': {
      const selected: Record<string, boolean> = {};
      for (const key of action.keys) selected[key] = true;
      return { ...state, selected };
    }
    case 'selectRange': {
      const selected = { ...state.selected };
      for (const key of action.keys) selected[key] = true;
      return { ...state, selected };
    }
    case 'clearSelection':
      return { ...state, selected: {}, cursor: null };
    case 'preview':
      return { ...state, preview: action.post };
    case 'similar':
      return { ...state, similar: action.similar };
    case 'searchOpen':
      return { ...state, searchOpen: action.open };
    case 'searchBusy':
      return { ...state, searchBusy: action.busy };
    case 'searchMessage':
      return { ...state, searchMessage: action.message };
    case 'searchResult':
      return { ...state, search: action.search, searchBusy: false, searchMessage: '' };
    case 'searchClear':
      return { ...state, search: null, searchMessage: '', searchBusy: false, sort: 'name' };
    case 'download':
      return { ...state, downloads: { ...state.downloads, [action.key]: action.entry } };
    case 'downloadClear': {
      if (!state.downloads[action.key]) return state;
      const downloads = { ...state.downloads };
      delete downloads[action.key];
      return { ...state, downloads };
    }
    case 'batchOpen':
      return { ...state, batchOpen: action.open };
    case 'batch':
      return { ...state, batch: action.snapshot };
    case 'metaOpen':
      return { ...state, metaOpen: action.open };
    case 'meta':
      return { ...state, meta: action.snapshot };
    case 'error':
      return { ...state, error: action.error };
    default:
      return state;
  }
}

/**
 * 判断长任务快照是否处于运行阶段（已提交或执行中）。
 *
 * Args:
 *   snapshot (BatchSnapshot | null): 任务快照。
 *
 * Returns:
 *   boolean: 处于排队或运行中返回 true。
 *
 * Callers:
 *   - `filter/Dialogs.tsx:BatchDialog`
 *   - `filter/Dialogs.tsx:MetaDialog`
 */
export function isRunning(snapshot: BatchSnapshot | null): boolean {
  return snapshot?.status === 'queued' || snapshot?.status === 'running';
}
