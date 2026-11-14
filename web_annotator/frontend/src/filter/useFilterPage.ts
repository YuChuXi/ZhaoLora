import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';
import {
  api,
  isRemote,
  SOURCES,
  sortItems,
} from '@/lib/api';
import type { BatchParams, BatchSnapshot, LocalItem, Mode, Post } from '@/lib/types';
import { useApp } from '@/app/AppContext';
import {
  type Action,
  type PageState,
  type PersistedFilter,
  type PersistedSource,
  initialState,
  reducer,
} from './state';

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function tagsOf(mode: Mode, fields: Record<string, string>): string {
  if (mode === 'gelbooru' || mode === 'danbooru' || mode === 'e621') return fields.tags ?? '';
  if (mode === 'pixiv') return fields.query ?? '';
  if (mode === 'video') return '';
  return fields.uid ?? '';
}

function extraOf(mode: Mode, fields: Record<string, string>): string {
  if (mode === 'pixiv') return `mode:${fields.mode || 'search'}`;
  if (mode === 'mihuashi') return `mode:${fields.mode || 'artwork'}`;
  if (mode === 'bilibili') return fields.extra || 'type:image';
  if (mode === 'bilibili-video') return fields.extra || 'type:video';
  return fields.extra ?? '';
}

function parsePersisted(filter: unknown, sort: unknown, ui: unknown): PersistedFilter {
  const source = (filter && typeof filter === 'object' ? filter : {}) as Record<string, unknown>;
  const mode = typeof source.mode === 'string' && source.mode in SOURCES ? (source.mode as Mode) : 'local';
  const cols = typeof source.cols === 'number' ? source.cols : 0;
  const showTrash = !!source.show_trash;
  const sources =
    ui && typeof ui === 'object' ? (ui as PersistedFilter['sources']) : {};
  return {
    mode,
    cols,
    sort: typeof sort === 'string' ? (sort as PersistedFilter['sort']) : 'name',
    showTrash,
    dirs: [],
    sources,
  };
}

function filterNewPosts(posts: Post[], existing: Post[]): Post[] {
  const seen = new Set(existing.map((post) => post.key));
  return posts.filter((post) => {
    if (seen.has(post.key)) return false;
    seen.add(post.key);
    return true;
  });
}

export interface FilterActions {
  setMode: (mode: Mode) => void;
  setColumns: (columns: number) => void;
  setSort: (sort: PageState['sort']) => void;
  setTrash: (showTrash: boolean) => void;
  setField: (key: string, value: string) => void;
  search: () => void;
  searchWith: (tags: string) => void;
  loadMore: (up: boolean) => void;
  cardClick: (key: string, shiftKey: boolean) => void;
  openPreview: (post: Post) => void;
  closePreview: () => void;
  openSimilar: (path: string) => void;
  closeSimilar: () => void;
  jumpTo: (path: string) => void;
  toggleSelected: (key: string, on: boolean) => void;
  localSelectAll: () => void;
  localClearSelection: () => void;
  localInvert: () => void;
  localDelete: () => void;
  localRestore: () => void;
  selectRangeTo: (key: string) => void;
  openBatch: () => void;
  closeBatch: () => void;
  startBatch: (params: BatchParams) => void;
  updateBatchParams: (params: BatchParams) => void;
  openMeta: () => void;
  closeMeta: () => void;
  regenerateCaptions: (params: BatchParams) => void;
  refreshMetadata: () => void;
  dismissError: () => void;
}

export function useFilterPage(dirList: string[]): { state: PageState; actions: FilterActions; localItems: LocalItem[]; keys: string[] } {
  const app = useApp();
  const [state, dispatch] = useReducer(reducer, initialState);
  const stateRef = useRef(state);
  stateRef.current = state;
  const seq = useRef(0);
  const sourcesRef = useRef<Partial<Record<Mode, PersistedSource>>>({});
  const saveTimer = useRef<number>();
  const jobIds = useRef<Record<string, string>>({});
  const jobClose = useRef<(() => void) | null>(null);
  const initialized = useRef(false);

  const apply = useCallback((action: Action) => dispatch(action), []);

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    const persisted = parsePersisted(app.settings.filter, app.settings.sort, app.settings.booru_ui);
    sourcesRef.current = persisted.sources;
    apply({ type: 'init', dirs: dirList, allDirs: [], persisted });
  }, [app.settings, apply, dirList]);

  useEffect(() => {
    if (!state.ready) return;
    apply({ type: 'setDirs', dirs: dirList });
  }, [apply, dirList, state.ready]);

  const searchRemote = useCallback(
    async (mode: Mode, reset: boolean, id: number, up = false, overrideTags?: string) => {
      const current = stateRef.current;
      const def = SOURCES[mode];
      if (!def.remote) return;
      const fields = current.remote.fields;
      const tags = overrideTags ?? tagsOf(mode, fields);
      if (def.requireTags && !tags.trim()) {
        apply({ type: 'remoteError', error: '请输入搜索条件' });
        return;
      }
      const page = reset ? 1 : up ? Math.max(1, current.remote.pageLo - 1) : current.remote.pageHi + 1;
      apply({ type: 'remoteStart', reset });
      try {
        const result = await api.search({
          source: mode,
          tags,
          extra: extraOf(mode, fields),
          order: fields.order ?? '',
          page,
          session: reset ? '' : current.remote.session,
          dir: current.dirs[0] || 'main',
        });
        if (id !== seq.current) return;
        const posts = reset ? result.posts : filterNewPosts(result.posts, current.remote.posts);
        apply({
          type: 'remoteLoaded',
          posts,
          page: result.page,
          hasMore: result.has_more,
          session: result.session,
          reset,
          up,
        });
      } catch (error) {
        if (id !== seq.current) return;
        apply({ type: 'remoteError', error: messageOf(error) });
      }
    },
    [apply],
  );

  const loadLocal = useCallback(
    async (id: number) => {
      const current = stateRef.current;
      try {
        const items = await api.listImages(current.dirs, current.showTrash);
        if (id !== seq.current) return;
        apply({ type: 'localLoaded', items });
      } catch (error) {
        if (id !== seq.current) return;
        apply({ type: 'error', error: messageOf(error) });
      }
    },
    [apply],
  );

  const dirsKey = state.dirs.join(',');
  const trashDependency = state.mode === 'local' ? state.showTrash : false;

  useEffect(() => {
    if (!state.ready) return;
    const id = ++seq.current;
    if (state.mode === 'local') {
      void loadLocal(id);
    } else {
      void searchRemote(state.mode, true, id);
    }
  }, [state.ready, state.mode, dirsKey, trashDependency, loadLocal, searchRemote]);

  useEffect(() => {
    if (!state.ready) return;
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      const current = stateRef.current;
      const sources = {
        ...sourcesRef.current,
        [current.mode]: {
          fields: current.remote.fields,
          page: current.remote.pageHi,
        },
      };
      sourcesRef.current = sources;
      app.saveSettings(
        {
          filter: {
            mode: current.mode,
            cols: current.columns,
            show_trash: current.showTrash,
          },
          sort: current.sort,
          booru_ui: sources,
        },
        false,
      );
    }, 400);
    return () => window.clearTimeout(saveTimer.current);
  }, [
    state.ready,
    state.mode,
    state.columns,
    state.sort,
    state.showTrash,
    state.remote.fields,
    state.remote.pageHi,
  ]);

  useEffect(() => {
    return () => {
      jobClose.current?.();
    };
  }, []);

  const localItems = useMemo(() => sortItems(state.local, state.sort), [state.local, state.sort]);
  const keys = useMemo(() => {
    if (state.mode === 'local') return localItems.map((item) => item.path);
    return state.remote.posts.map((post) => post.key);
  }, [state.mode, localItems, state.remote.posts]);

  const notify = useCallback((error: string) => apply({ type: 'error', error }), [apply]);

  const watchDownload = useCallback(
    (key: string, jobId: string) => {
      jobIds.current[key] = jobId;
      jobClose.current?.();
      jobClose.current = api.watchJob(jobId, (snapshot: BatchSnapshot) => {
        if (snapshot.status === 'done') {
          delete jobIds.current[key];
          apply({ type: 'status', key, status: 'done' });
          apply({ type: 'toggle', key, on: true });
        } else if (snapshot.status === 'error' || snapshot.status === 'cancelled') {
          delete jobIds.current[key];
          apply({ type: 'status', key, status: snapshot.status === 'error' ? 'error' : 'idle' });
        } else {
          const text =
            snapshot.status === 'queued'
              ? '排队中'
              : snapshot.message || `${Math.round(snapshot.pct ?? 0)}%`;
          apply({ type: 'status', key, status: 'downloading', progress: text });
        }
      });
    },
    [apply],
  );

  const downloadPost = useCallback(
    async (post: Post) => {
      const current = stateRef.current;
      apply({ type: 'status', key: post.key, status: 'downloading', progress: '保存中…' });
      try {
        const result = await api.download({
          source: post.source,
          id: post.id,
          filename: post.filename,
          dir: current.dirs[0] || 'main',
          overwrite: false,
          post,
        });
        if (result.job_id) {
          watchDownload(post.key, result.job_id);
          return;
        }
        apply({ type: 'status', key: post.key, status: 'done' });
        apply({ type: 'toggle', key: post.key, on: true });
      } catch (error) {
        apply({ type: 'status', key: post.key, status: 'error' });
        notify(messageOf(error));
      }
    },
    [apply, notify, watchDownload],
  );

  const undownloadPost = useCallback(
    async (post: Post) => {
      const current = stateRef.current;
      try {
        await api.undownload({
          source: post.source,
          id: post.id,
          filename: post.filename,
          dir: current.dirs[0] || 'main',
          overwrite: false,
          post,
        });
        apply({ type: 'status', key: post.key, status: 'idle' });
        apply({ type: 'toggle', key: post.key, on: false });
      } catch (error) {
        notify(messageOf(error));
      }
    },
    [apply, notify],
  );

  const toggleSelected = useCallback(
    (key: string, on: boolean) => {
      apply({ type: 'toggle', key, on });
      if (stateRef.current.mode === 'local') {
        void api.toggle(key).catch((error) => {
          apply({ type: 'toggle', key, on: !on });
          notify(messageOf(error));
        });
      }
    },
    [apply, notify],
  );

  const selectRangeTo = useCallback(
    (key: string) => {
      const current = stateRef.current;
      const from = current.cursor ? keys.indexOf(current.cursor) : -1;
      const to = keys.indexOf(key);
      if (from < 0 || to < 0) {
        toggleSelected(key, true);
        return;
      }
      const slice = keys.slice(Math.min(from, to), Math.max(from, to) + 1);
      apply({ type: 'selectRange', keys: slice });
      if (current.mode === 'local') {
        void api.selectRange(slice).catch((error) => notify(messageOf(error)));
      }
    },
    [apply, keys, notify, toggleSelected],
  );

  const cardClick = useCallback(
    (key: string, shiftKey: boolean) => {
      const current = stateRef.current;
      if (current.mode === 'local') {
        if (shiftKey && current.cursor) {
          selectRangeTo(key);
          return;
        }
        toggleSelected(key, !current.selected[key]);
        return;
      }
      const post = current.remote.posts.find((item) => item.key === key);
      if (!post) return;
      const jobId = jobIds.current[key];
      if (jobId) {
        void api.cancelJob(jobId)
          .then(() => {
            delete jobIds.current[key];
            apply({ type: 'status', key, status: 'idle' });
          })
          .catch((error) => notify(messageOf(error)));
        return;
      }
      if (current.selected[key]) void undownloadPost(post);
      else void downloadPost(post);
    },
    [apply, downloadPost, notify, selectRangeTo, toggleSelected, undownloadPost],
  );

  const search = useCallback(() => {
    if (!isRemote(stateRef.current.mode)) return;
    const id = ++seq.current;
    void searchRemote(stateRef.current.mode, true, id);
  }, [searchRemote]);

  const searchWith = useCallback(
    (tags: string) => {
      const current = stateRef.current;
      if (!isRemote(current.mode)) return;
      apply({ type: 'field', key: 'tags', value: tags });
      const id = ++seq.current;
      void searchRemote(current.mode, true, id, false, tags);
    },
    [apply, searchRemote],
  );

  const loadMore = useCallback(
    (up: boolean) => {
      const current = stateRef.current;
      if (!isRemote(current.mode) || current.remote.loading) return;
      if (up && !current.remote.hasPrev) return;
      if (!up && !current.remote.hasMore) return;
      void searchRemote(current.mode, false, seq.current, up);
    },
    [searchRemote],
  );

  const openSimilar = useCallback(
    async (path: string) => {
      apply({ type: 'similar', similar: { path, items: [], loading: true } });
      try {
        const result = await api.similar(path);
        apply({ type: 'similar', similar: { path, items: result.similar, loading: false } });
      } catch (error) {
        apply({ type: 'similar', similar: null });
        notify(messageOf(error));
      }
    },
    [apply, notify],
  );

  const jumpTo = useCallback((path: string) => {
    const card = document.querySelector<HTMLElement>(`[data-key="${CSS.escape(path)}"]`);
    card?.scrollIntoView({ block: 'center' });
  }, []);

  const localSelectAll = useCallback(() => {
    const current = stateRef.current;
    apply({ type: 'selectAll', keys });
    void api.selectAll(current.dirs, current.showTrash).catch((error) => notify(messageOf(error)));
  }, [apply, keys, notify]);

  const localClearSelection = useCallback(() => {
    apply({ type: 'clearSelection' });
    void api.deselectAll().catch((error) => notify(messageOf(error)));
  }, [apply, notify]);

  const localInvert = useCallback(async () => {
    const current = stateRef.current;
    const next: string[] = [];
    for (const item of localItems) {
      if (current.selected[item.path]) {
        await api.toggle(item.path);
      } else {
        await api.toggle(item.path);
        next.push(item.path);
      }
    }
    apply({ type: 'selectAll', keys: next });
  }, [apply, localItems]);

  const localDelete = useCallback(async () => {
    try {
      await api.applyDelete();
      apply({ type: 'clearSelection' });
      const id = ++seq.current;
      await loadLocal(id);
    } catch (error) {
      notify(messageOf(error));
    }
  }, [apply, loadLocal, notify]);

  const localRestore = useCallback(async () => {
    const selected = Object.keys(stateRef.current.selected);
    if (!selected.length) return;
    try {
      await api.restore(selected);
      apply({ type: 'clearSelection' });
      const id = ++seq.current;
      await loadLocal(id);
    } catch (error) {
      notify(messageOf(error));
    }
  }, [apply, loadLocal, notify]);

  const startBatch = useCallback(
    async (params: BatchParams) => {
      const current = stateRef.current;
      if (!isRemote(current.mode)) return;
      try {
        const result = await api.batchStart({
          source: current.mode,
          tags: tagsOf(current.mode, current.remote.fields),
          extra: extraOf(current.mode, current.remote.fields),
          order: current.remote.fields.order ?? '',
          dir: current.dirs[0] || 'main',
          page: current.remote.pageLo,
          limit: params.limit,
          conn: params.conn,
          rate_rps: params.rate,
          anima_format: params.anima,
          include_meta_tags: params.meta,
          keep_underscores: params.underscores,
        });
        apply({ type: 'batchRunning', running: true });
        jobClose.current?.();
        jobClose.current = api.watchJob(result.job_id, (snapshot) => {
          apply({ type: 'batchSnapshot', snapshot });
          if (snapshot.status === 'done' || snapshot.status === 'error' || snapshot.status === 'cancelled') {
            apply({ type: 'batchRunning', running: false });
          }
        });
      } catch (error) {
        notify(messageOf(error));
      }
    },
    [apply, notify],
  );

  const updateBatchParams = useCallback(
    async (params: BatchParams) => {
      try {
        await api.batchParams({ limit: params.limit, conn: params.conn, rate_rps: params.rate });
      } catch (error) {
        notify(messageOf(error));
      }
    },
    [notify],
  );

  const regenerateCaptions = useCallback(
    async (params: BatchParams) => {
      try {
        const result = await api.regenerateCaptions({
          dir: stateRef.current.dirs[0] || 'main',
          anima_format: params.anima,
          include_meta_tags: params.meta,
          keep_underscores: params.underscores,
        });
        notify(`重生成 ${result.count} 条描述`);
      } catch (error) {
        notify(messageOf(error));
      }
    },
    [notify],
  );

  const refreshMetadata = useCallback(async () => {
    try {
      const result = await api.refreshMetadata({ dir: stateRef.current.dirs[0] || 'main' });
      jobClose.current?.();
      jobClose.current = api.watchJob(result.job_id, (snapshot) => {
        apply({ type: 'batchSnapshot', snapshot });
      });
      notify('元数据刷新已提交');
    } catch (error) {
      notify(messageOf(error));
    }
  }, [apply, notify]);

  const actions: FilterActions = {
    setMode: (mode) => apply({ type: 'setMode', mode }),
    setColumns: (columns) => apply({ type: 'setColumns', columns }),
    setSort: (sort) => apply({ type: 'setSort', sort }),
    setTrash: (showTrash) => apply({ type: 'setTrash', showTrash }),
    setField: (key, value) => apply({ type: 'field', key, value }),
    search,
    searchWith,
    loadMore,
    cardClick,
    openPreview: (post) => apply({ type: 'preview', post }),
    closePreview: () => apply({ type: 'preview', post: null }),
    openSimilar: (path) => void openSimilar(path),
    closeSimilar: () => apply({ type: 'similar', similar: null }),
    jumpTo,
    toggleSelected,
    localSelectAll,
    localClearSelection,
    localInvert: () => void localInvert(),
    localDelete: () => void localDelete(),
    localRestore: () => void localRestore(),
    selectRangeTo,
    openBatch: () => apply({ type: 'batchOpen', open: true }),
    closeBatch: () => apply({ type: 'batchOpen', open: false }),
    startBatch: (params) => void startBatch(params),
    updateBatchParams: (params) => void updateBatchParams(params),
    openMeta: () => apply({ type: 'metaOpen', open: true }),
    closeMeta: () => apply({ type: 'metaOpen', open: false }),
    regenerateCaptions: (params) => void regenerateCaptions(params),
    refreshMetadata: () => void refreshMetadata(),
    dismissError: () => apply({ type: 'error', error: null }),
  };

  return { state, actions, localItems, keys };
}
