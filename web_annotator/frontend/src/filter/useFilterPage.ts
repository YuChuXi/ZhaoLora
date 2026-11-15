import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';
import {
  api,
  datasetName,
  isRemote,
  SOURCES,
  sortItems,
  type SourceCondition,
} from '@/lib/api';
import type { BatchParams, BatchSnapshot, LocalItem, Mode, Post } from '@/lib/types';
import { scrollContainer } from '@/lib/scroll';
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

function extraOf(
  mode: Mode,
  fields: Record<string, string>,
  conditions: Record<string, SourceCondition>,
): string {
  if (mode === 'pixiv') return `mode:${fields.mode || 'user'}`;
  if (mode === 'mihuashi') return `mode:${fields.mode || 'artworks'}`;
  if (mode === 'bilibili' || mode === 'bilibili-video') {
    return fields.extra ?? conditions[mode]?.default ?? '';
  }
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

/** 单张图片正在执行的保存动作：同步请求的中止函数，或异步任务的订阅与标识。 */
interface DownloadTask {
  abort: () => void;
  jobId?: string;
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
  jumpPage: (page: number) => void;
  setVisiblePage: (page: number) => void;
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
  openSearch: () => void;
  closeSearch: () => void;
  runSemanticSearch: (query: string) => void;
  runTagSearch: (query: string) => void;
  clearSearch: () => void;
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
  /** 在途增量翻页所属的序列号; 同一序列只允许一个请求. */
  const paging = useRef<number | null>(null);
  const sourcesRef = useRef<Partial<Record<Mode, PersistedSource>>>({});
  const saveTimer = useRef<number>();
  const downloadTasks = useRef(new Map<string, DownloadTask>());
  const batchClose = useRef<(() => void) | null>(null);
  const metaClose = useRef<(() => void) | null>(null);
  const searchStop = useRef<(() => void) | null>(null);
  const initialized = useRef(false);
  const resumed = useRef(false);

  const apply = useCallback((action: Action) => dispatch(action), []);

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    const persisted = parsePersisted(app.settings.filter, app.settings.sort, app.settings.booru_ui);
    sourcesRef.current = persisted.sources;
    apply({ type: 'init', dirs: dirList, allDirs: [], persisted });
    void api
      .onlineSources()
      .then((result) => apply({ type: 'conditions', conditions: result.conditions }))
      .catch((error) => apply({ type: 'error', error: messageOf(error) }));
    void api
      .scoreItems()
      .then((result) => apply({ type: 'scoreItems', items: result.items }))
      .catch((error) => apply({ type: 'error', error: messageOf(error) }));
  }, [app.settings, apply, dirList]);

  useEffect(() => {
    if (!state.ready) return;
    apply({ type: 'setDirs', dirs: dirList });
  }, [apply, dirList, state.ready]);

  useEffect(() => {
    if (!state.ready || !state.scoreItems.length || !state.sort.startsWith('score:')) return;
    const key = state.sort.slice(6);
    if (!state.scoreItems.some((item) => item.key === key)) {
      apply({ type: 'setSort', sort: 'name' });
    }
  }, [apply, state.ready, state.scoreItems, state.sort]);

  const searchRemote = useCallback(
    async (
      mode: Mode,
      reset: boolean,
      id: number,
      up = false,
      overrideTags?: string,
      startPage?: number,
    ) => {
      const current = stateRef.current;
      const def = SOURCES[mode];
      if (!def.remote) return;
      const fields = current.remote.fields;
      const tags = overrideTags ?? tagsOf(mode, fields);
      if (def.requireTags && !tags.trim()) {
        apply({ type: 'remoteError', error: '请输入搜索条件' });
        return;
      }
      const page = reset
        ? startPage ?? 1
        : up
          ? Math.max(1, current.remote.pageLo - 1)
          : current.remote.pageHi + 1;
      apply({ type: 'remoteStart', reset });
      try {
        const result = await api.search({
          source: mode,
          tags,
          extra: extraOf(mode, fields, current.conditions),
          order: fields.order ?? '',
          page,
          session: reset ? '' : current.remote.session,
          dir: current.dirs[0] || 'main',
        });
        if (id !== seq.current) return;
        const fetched = result.posts.map((post) => ({ ...post, page: result.page }));
        const posts = reset ? fetched : filterNewPosts(fetched, current.remote.posts);
        apply({
          type: 'remoteLoaded',
          posts,
          page: result.page,
          hasMore: result.has_more,
          session: result.session,
          reset,
          up,
          stored: result.stored,
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
    scrollContainer()?.scrollTo({ top: 0 });
    if (state.mode === 'local') {
      void loadLocal(id);
    } else {
      void searchRemote(state.mode, true, id, false, undefined, state.remote.pageLo);
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
          page: current.remote.visiblePage,
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
          sort: current.sort === 'search' ? 'name' : current.sort,
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
    state.remote.visiblePage,
  ]);

  useEffect(() => {
    return () => {
      for (const task of downloadTasks.current.values()) task.abort();
      downloadTasks.current.clear();
      batchClose.current?.();
      metaClose.current?.();
      searchStop.current?.();
    };
  }, []);

  const localItems = useMemo(
    () => sortItems(state.local, state.sort, state.search?.scores),
    [state.local, state.sort, state.search],
  );
  const keys = useMemo(() => {
    if (state.mode === 'local') return localItems.map((item) => item.path);
    return state.remote.posts.map((post) => post.key);
  }, [state.mode, localItems, state.remote.posts]);

  const notify = useCallback((message: string) => app.notify(message), [app]);

  /**
   * 重新读取工作目录已收录的图片总数。
   *
   * 下载、移除与批量任务结束后调用，保证状态栏「已收录」与磁盘一致；
   * 本地模式不涉及该计数。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `filter/useFilterPage.ts:watchDownload`
   *   - `filter/useFilterPage.ts:undownloadPost`
   *   - `filter/useFilterPage.ts:watchBatch`
   */
  const refreshStored = useCallback(async () => {
    const current = stateRef.current;
    if (!isRemote(current.mode)) return;
    try {
      const result = await api.listDirs();
      const name = current.dirs[0] || 'main';
      const hit = result.dirs.find((dir) => dir.name === name);
      apply({ type: 'stored', stored: hit?.image_count ?? 0 });
    } catch (error) {
      notify(messageOf(error));
    }
  }, [apply, notify]);

  /**
   * 订阅单张图片的下载任务，终态写回卡片状态。
   *
   * 每张图片持有独立的 SSE 连接，互不影响；重复触发同一张图片时先关闭旧连接。
   *
   * Args:
   *   key (string): 图片键。
   *   jobId (string): 后端下载任务标识。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `filter/useFilterPage.ts:downloadPost`
   */
  const watchDownload = useCallback(
    (key: string, jobId: string) => {
      downloadTasks.current.get(key)?.abort();
      const close = api.watchJob(jobId, (snapshot: BatchSnapshot) => {
        if (snapshot.status === 'done') {
          downloadTasks.current.delete(key);
          apply({ type: 'download', key, entry: { status: 'done', progress: '' } });
          void refreshStored();
          return;
        }
        if (snapshot.status === 'error') {
          downloadTasks.current.delete(key);
          apply({ type: 'download', key, entry: { status: 'error', progress: '' } });
          return;
        }
        if (snapshot.status === 'cancelled') {
          downloadTasks.current.delete(key);
          apply({ type: 'downloadClear', key });
          return;
        }
        apply({
          type: 'download',
          key,
          entry: {
            status: 'downloading',
            progress:
              snapshot.status === 'queued'
                ? '排队中'
                : snapshot.message || `${Math.round(snapshot.pct ?? 0)}%`,
          },
        });
      });
      downloadTasks.current.set(key, { abort: close, jobId });
    },
    [apply, refreshStored],
  );

  /**
   * 打断单张图片正在进行的保存，并清除其卡片状态。
   *
   * 同步请求经 `AbortController` 断开连接，服务端据断连取消下载任务；异步任务
   * 关闭订阅并提交协作取消。
   *
   * Args:
   *   key (string): 图片键。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `filter/useFilterPage.ts:cardClick`
   */
  const cancelDownload = useCallback(
    (key: string) => {
      const task = downloadTasks.current.get(key);
      downloadTasks.current.delete(key);
      task?.abort();
      if (task?.jobId) void api.cancelJob(task.jobId);
      apply({ type: 'downloadClear', key });
    },
    [apply],
  );

  const downloadPost = useCallback(
    async (post: Post) => {
      const current = stateRef.current;
      const controller = new AbortController();
      downloadTasks.current.set(post.key, { abort: () => controller.abort() });
      apply({ type: 'download', key: post.key, entry: { status: 'downloading', progress: '保存中…' } });
      try {
        const result = await api.download(
          {
            source: post.source,
            id: post.id,
            filename: post.filename,
            dir: current.dirs[0] || 'main',
            overwrite: false,
            post,
          },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        if (result.job_id) {
          watchDownload(post.key, result.job_id);
          return;
        }
        downloadTasks.current.delete(post.key);
        apply({ type: 'download', key: post.key, entry: { status: 'done', progress: '' } });
        void refreshStored();
      } catch (error) {
        if (controller.signal.aborted) return;
        downloadTasks.current.delete(post.key);
        apply({ type: 'download', key: post.key, entry: { status: 'error', progress: '' } });
        notify(messageOf(error));
      }
    },
    [apply, notify, refreshStored, watchDownload],
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
        apply({ type: 'downloadClear', key: post.key });
        void refreshStored();
      } catch (error) {
        notify(messageOf(error));
      }
    },
    [apply, notify, refreshStored],
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
      if (post.source === 'video' && post.path) {
        window.location.href = `/d/${encodeURIComponent(datasetName())}/video_extract?path=${encodeURIComponent(post.path)}`;
        return;
      }
      const entry = current.downloads[key];
      if (entry?.status === 'downloading') {
        cancelDownload(key);
        return;
      }
      if (entry?.status === 'done') void undownloadPost(post);
      else void downloadPost(post);
    },
    [cancelDownload, downloadPost, selectRangeTo, toggleSelected, undownloadPost],
  );

  const search = useCallback(() => {
    if (!isRemote(stateRef.current.mode)) return;
    const id = ++seq.current;
    scrollContainer()?.scrollTo({ top: 0 });
    void searchRemote(stateRef.current.mode, true, id);
  }, [searchRemote]);

  const searchWith = useCallback(
    (tags: string) => {
      const current = stateRef.current;
      if (!isRemote(current.mode)) return;
      apply({ type: 'field', key: 'tags', value: tags });
      const id = ++seq.current;
      scrollContainer()?.scrollTo({ top: 0 });
      void searchRemote(current.mode, true, id, false, tags);
    },
    [apply, searchRemote],
  );

  /**
   * 加载上一页或下一页.
   *
   * Args:
   *   up (boolean): 是否加载上一页.
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `filter/useFilterPage.ts:FilterActions`
   *   - `filter/Toolbar.tsx:SearchSection` 的上一页/下一页按钮.
   *   - `filter/Masonry.tsx:Masonry` 的顶部与底部哨兵.
   */
  const loadMore = useCallback(
    (up: boolean) => {
      const current = stateRef.current;
      const id = seq.current;
      if (paging.current === id) return;
      if (!isRemote(current.mode) || current.remote.loading) return;
      if (up && !current.remote.hasPrev) return;
      if (!up && !current.remote.hasMore) return;
      paging.current = id;
      void searchRemote(current.mode, false, id, up).finally(() => {
        if (paging.current === id) paging.current = null;
      });
    },
    [searchRemote],
  );

  const jumpPage = useCallback(
    (page: number) => {
      const current = stateRef.current;
      if (!isRemote(current.mode) || current.remote.loading) return;
      const id = ++seq.current;
      scrollContainer()?.scrollTo({ top: 0 });
      void searchRemote(current.mode, true, id, false, undefined, Math.max(1, page));
    },
    [searchRemote],
  );

  const setVisiblePage = useCallback(
    (page: number) => apply({ type: 'visiblePage', page }),
    [apply],
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
    const next: string[] = [];
    for (const item of localItems) {
      const result = await api.toggle(item.path);
      if (result.selected) next.push(item.path);
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

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
      const current = stateRef.current;
      if (current.mode !== 'local') return;
      const key = event.key.toLowerCase();
      const modified = event.ctrlKey || event.metaKey;
      if (modified && !event.altKey) {
        if (event.shiftKey) {
          if (key !== 'a') return;
          event.preventDefault();
          localClearSelection();
          return;
        }
        if (key === 'a') {
          event.preventDefault();
          localSelectAll();
        } else if (key === 'i') {
          event.preventDefault();
          void localInvert();
        } else if (key === 'z') {
          event.preventDefault();
          void localRestore();
        }
        return;
      }
      if (event.altKey || event.shiftKey) return;
      if (key === 'a') {
        event.preventDefault();
        localSelectAll();
        return;
      }
      if (key === 'd') {
        event.preventDefault();
        localClearSelection();
        return;
      }
      if (event.key === 'Delete' && Object.values(current.selected).some(Boolean)) {
        event.preventDefault();
        void localDelete();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [localClearSelection, localDelete, localInvert, localRestore, localSelectAll]);

  /**
   * 订阅批量下载任务, 终态刷新工作目录计数。
   *
   * Args:
   *   jobId (string): 批量任务标识.
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `filter/useFilterPage.ts:startBatch`
   *   - `filter/useFilterPage.ts:useFilterPage`（刷新页面后恢复进行中的任务）
   */
  const watchBatch = useCallback(
    (jobId: string) => {
      batchClose.current?.();
      batchClose.current = api.watchJob(jobId, (snapshot) => {
        apply({ type: 'batch', snapshot });
        if (
          snapshot.status === 'done' ||
          snapshot.status === 'cancelled' ||
          snapshot.status === 'error'
        ) {
          void refreshStored();
        }
      });
    },
    [apply, refreshStored],
  );

  const startBatch = useCallback(
    async (params: BatchParams) => {
      const current = stateRef.current;
      if (!isRemote(current.mode)) return;
      try {
        const result = await api.batchStart({
          source: current.mode,
          tags: tagsOf(current.mode, current.remote.fields),
          extra: extraOf(current.mode, current.remote.fields, current.conditions),
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
        apply({ type: 'batch', snapshot: { job_id: result.job_id, status: 'queued' } });
        watchBatch(result.job_id);
      } catch (error) {
        notify(messageOf(error));
      }
    },
    [apply, notify, watchBatch],
  );

  useEffect(() => {
    if (!state.ready || resumed.current) return;
    resumed.current = true;
    let open = true;
    api
      .findActiveJob('online_batch')
      .then((snapshot) => {
        if (!open || !snapshot?.job_id) return;
        apply({ type: 'batch', snapshot });
        watchBatch(snapshot.job_id);
        notify('已恢复批量下载进度');
      })
      .catch((error) => apply({ type: 'error', error: messageOf(error) }));
    return () => {
      open = false;
    };
  }, [apply, notify, state.ready, watchBatch]);

  const runSemanticSearch = useCallback(
    async (query: string) => {
      const current = stateRef.current;
      apply({ type: 'searchBusy', busy: true });
      apply({ type: 'searchMessage', message: '提交中…' });
      try {
        const result = await api.semanticQuery(query, current.dirs);
        searchStop.current?.();
        searchStop.current = api.watchJob(result.job_id, (snap) => {
          if (snap.status === 'done') {
            const scores: Record<string, number> = {};
            for (const item of snap.result?.items ?? []) scores[item.path] = item.similarity ?? 0;
            apply({ type: 'searchResult', search: { prefix: 'S', query, scores, missing: 0 } });
            apply({ type: 'setSort', sort: 'search' });
            apply({ type: 'searchOpen', open: false });
            searchStop.current?.();
            searchStop.current = null;
          } else if (snap.status === 'error' || snap.status === 'cancelled') {
            apply({ type: 'searchBusy', busy: false });
            apply({ type: 'searchMessage', message: snap.message || '搜索失败' });
          } else {
            apply({ type: 'searchMessage', message: snap.message || `${Math.round(snap.pct ?? 0)}%` });
          }
        });
      } catch (error) {
        apply({ type: 'searchBusy', busy: false });
        apply({ type: 'searchMessage', message: messageOf(error) });
      }
    },
    [apply],
  );

  const runTagSearch = useCallback(
    async (query: string) => {
      const current = stateRef.current;
      apply({ type: 'searchBusy', busy: true });
      apply({ type: 'searchMessage', message: '评分中…' });
      try {
        const result = await api.tagQuery(query, current.dirs);
        const scores: Record<string, number> = {};
        for (const item of result.items) scores[item.path] = item.score ?? 0;
        apply({
          type: 'searchResult',
          search: { prefix: 'T', query, scores, missing: result.missing },
        });
        apply({ type: 'setSort', sort: 'search' });
        apply({ type: 'searchOpen', open: false });
        if (!result.items.length) {
          apply({ type: 'searchMessage', message: '没有可用分值，请先在评分页建立 WD14 缓存' });
        }
      } catch (error) {
        apply({ type: 'searchBusy', busy: false });
        apply({ type: 'searchMessage', message: messageOf(error) });
      }
    },
    [apply],
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
      apply({ type: 'meta', snapshot: { job_id: result.job_id, status: 'queued' } });
      metaClose.current?.();
      metaClose.current = api.watchJob(result.job_id, (snapshot) => {
        apply({ type: 'meta', snapshot });
      });
      notify('元数据刷新已提交');
    } catch (error) {
      notify(messageOf(error));
    }
  }, [apply, notify]);

  const actions: FilterActions = {
    setMode: (mode) => {
      for (const task of downloadTasks.current.values()) task.abort();
      downloadTasks.current.clear();
      const saved = sourcesRef.current[mode];
      apply({ type: 'setMode', mode, fields: saved?.fields, page: saved?.page });
    },
    setColumns: (columns) => apply({ type: 'setColumns', columns }),
    setSort: (sort) => apply({ type: 'setSort', sort }),
    setTrash: (showTrash) => apply({ type: 'setTrash', showTrash }),
    setField: (key, value) => apply({ type: 'field', key, value }),
    search,
    searchWith,
    loadMore,
    jumpPage,
    setVisiblePage,
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
    openSearch: () => apply({ type: 'searchOpen', open: true }),
    closeSearch: () => apply({ type: 'searchOpen', open: false }),
    runSemanticSearch: (query) => void runSemanticSearch(query),
    runTagSearch: (query) => void runTagSearch(query),
    clearSearch: () => apply({ type: 'searchClear' }),
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
