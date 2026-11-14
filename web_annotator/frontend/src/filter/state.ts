import type {
  BatchSnapshot,
  DirOption,
  DownloadStatus,
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
  status: Record<string, DownloadStatus>;
  progress: Record<string, string>;
  preview: Post | null;
  similar: SimilarState | null;
  batchOpen: boolean;
  batchRunning: boolean;
  batchSnapshot: BatchSnapshot | null;
  metaOpen: boolean;
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
  status: {},
  progress: {},
  preview: null,
  similar: null,
  batchOpen: false,
  batchRunning: false,
  batchSnapshot: null,
  metaOpen: false,
  error: null,
};

export type Action =
  | { type: 'init'; dirs: string[]; allDirs: DirOption[]; persisted: PersistedFilter }
  | { type: 'setMode'; mode: Mode }
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
  | { type: 'status'; key: string; status: DownloadStatus; progress?: string }
  | { type: 'batchOpen'; open: boolean }
  | { type: 'batchSnapshot'; snapshot: BatchSnapshot | null }
  | { type: 'batchRunning'; running: boolean }
  | { type: 'metaOpen'; open: boolean }
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
        status: {},
        progress: {},
        preview: null,
        similar: null,
        remote: { ...emptyRemote(), fields: {} },
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
      if (action.reset) {
        const selected: Record<string, boolean> = {};
        for (const post of action.posts) {
          if (post.downloaded) selected[post.key] = true;
        }
        return {
          ...state,
          remote: {
            ...state.remote,
            loading: false,
            posts: action.posts,
            pageLo: action.page,
            pageHi: action.page,
            hasMore: action.hasMore,
            hasPrev: action.page > 1,
            session: action.session,
          },
          selected,
        };
      }
      return {
        ...state,
        remote: {
          ...state.remote,
          loading: false,
          posts: action.up ? [...action.posts, ...state.remote.posts] : [...state.remote.posts, ...action.posts],
          pageLo: action.up ? action.page : state.remote.pageLo,
          pageHi: action.up ? state.remote.pageHi : action.page,
          hasMore: action.up ? state.remote.hasMore : action.hasMore,
          hasPrev: action.up ? action.page > 1 : state.remote.hasPrev,
          session: action.session || state.remote.session,
        },
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
    case 'status':
      return {
        ...state,
        status: { ...state.status, [action.key]: action.status },
        progress: action.progress
          ? { ...state.progress, [action.key]: action.progress }
          : state.progress,
      };
    case 'batchOpen':
      return { ...state, batchOpen: action.open };
    case 'batchSnapshot':
      return { ...state, batchSnapshot: action.snapshot };
    case 'batchRunning':
      return { ...state, batchRunning: action.running };
    case 'metaOpen':
      return { ...state, metaOpen: action.open };
    case 'error':
      return { ...state, error: action.error };
    default:
      return state;
  }
}

export function visibleKeys(state: PageState): string[] {
  if (state.mode === 'local') return state.local.map((item) => item.path);
  return state.remote.posts.map((post) => post.key);
}
