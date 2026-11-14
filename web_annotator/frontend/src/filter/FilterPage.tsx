import { useCallback, useMemo } from 'react';
import { api, SOURCES, thumbUrl } from '@/lib/api';
import type { LocalItem, Mode, Post } from '@/lib/types';
import { Button } from '@/components/ui/button';
import { useStatus } from '@/components/ui/status';
import { useApp } from '@/app/AppContext';
import { Masonry, type ViewItem } from './Masonry';
import { Toolbar } from './Toolbar';
import { BatchDialog, MetaDialog, PreviewDialog, SearchDialog, SimilarDialog } from './Dialogs';
import { useFilterPage } from './useFilterPage';

function ratioOf(post: Post): number {
  if (post.width > 0 && post.height > 0 && !post.is_video) return post.height / post.width;
  const aspect = SOURCES[post.source as Mode]?.fixedAspect;
  if (aspect) {
    const [width, height] = aspect.split('/').map((part) => Number(part.trim()));
    if (width > 0 && height > 0) return height / width;
  }
  return 1;
}

function cardLabel(post: Post): string {
  const resolution = post.width > 0 ? `${post.width}×${post.height} ` : '';
  const kind = post.is_video ? '视频' : post.is_forward ? '转发' : '';
  return [kind, `${resolution}${post.stem || post.id}`].filter(Boolean).join(' · ');
}

export function FilterPage() {
  const app = useApp();
  const { state, actions, localItems } = useFilterPage(app.dirs);
  const selectedCount = useMemo(
    () => Object.values(state.selected).filter(Boolean).length,
    [state.selected],
  );
  const saved = useMemo(() => {
    const map: Record<string, boolean> = {};
    for (const post of state.remote.posts) {
      const entry = state.downloads[post.key];
      if (entry && entry.status !== 'error') map[post.key] = true;
    }
    return map;
  }, [state.remote.posts, state.downloads]);
  const savedCount = useMemo(() => Object.keys(saved).length, [saved]);
  const savingCount = useMemo(
    () =>
      state.remote.posts.filter((post) => state.downloads[post.key]?.status === 'downloading').length,
    [state.remote.posts, state.downloads],
  );

  useStatus(
    state.mode === 'local'
      ? [
          { kind: 'total', value: state.local.length },
          { kind: 'selected', value: selectedCount },
        ]
      : [
          { kind: 'total', value: state.remote.posts.length },
          { kind: 'part', name: '已保存', value: savedCount },
          ...(savingCount ? [{ kind: 'part' as const, name: '保存中', value: savingCount }] : []),
        ],
  );

  const search = state.search;
  const viewItems = useMemo<ViewItem[]>(() => {
    if (state.mode === 'local') {
      return localItems.map((item: LocalItem) => ({
        key: item.path,
        ratio: item.width > 0 && item.height > 0 ? item.height / item.width : 1,
        src: item.thumbnail_hash ? api.thumbHashUrl(item.thumbnail_hash) : api.localImageUrl(item.path),
        fallbackSrc: api.localImageUrl(item.path),
        label: search
          ? `${search.prefix}${(search.scores[item.path] ?? 0).toFixed(3)} ${item.stem}`
          : item.stem,
        kind: 'image' as const,
      }));
    }
    return state.remote.posts.map((post) => ({
      key: post.key,
      ratio: ratioOf(post),
      src: thumbUrl(post),
      label: cardLabel(post),
      kind: post.is_video ? ('video' as const) : ('image' as const),
    }));
  }, [state.mode, state.remote.posts, localItems, search]);

  const { openSimilar, openPreview } = actions;
  const openContext = useCallback(
    (key: string) => {
      if (state.mode === 'local') {
        openSimilar(key);
        return;
      }
      const post = state.remote.posts.find((item) => item.key === key);
      if (post) openPreview(post);
    },
    [openPreview, openSimilar, state.mode, state.remote.posts],
  );

  const previewPost = state.preview;

  return (
    <div>
      <Toolbar state={state} actions={actions} />

      {state.error ? (
        <div className="flex items-center justify-between border-b border-primary/40 bg-primary/10 px-3 py-1.5 text-xs text-primary">
          <span className="truncate">{state.error}</span>
          <Button variant="ghost" size="sm" onClick={actions.dismissError}>
            关闭
          </Button>
        </div>
      ) : null}

      <Masonry
        items={viewItems}
        fixedColumns={state.columns}
        selected={state.mode === 'local' ? state.selected : saved}
        downloads={state.downloads}
        hasMore={state.remote.hasMore}
        hasPrev={state.remote.hasPrev}
        loading={state.remote.loading}
        onCardClick={actions.cardClick}
        onCardContext={openContext}
        onLoadMore={actions.loadMore}
      />

      <PreviewDialog
        post={previewPost}
        entry={previewPost ? state.downloads[previewPost.key] : undefined}
        onClose={actions.closePreview}
        onToggle={(post) => actions.cardClick(post.key, false)}
        onTagSearch={(tag) => {
          actions.closePreview();
          actions.searchWith(tag);
        }}
      />

      <SimilarDialog similar={state.similar} onClose={actions.closeSimilar} onJump={actions.jumpTo} />

      <SearchDialog
        open={state.searchOpen}
        busy={state.searchBusy}
        message={state.searchMessage}
        onClose={actions.closeSearch}
        onSemantic={actions.runSemanticSearch}
        onTag={actions.runTagSearch}
      />

      <BatchDialog
        open={state.batchOpen}
        snapshot={state.batch}
        onClose={actions.closeBatch}
        onStart={actions.startBatch}
        onParams={actions.updateBatchParams}
      />

      <MetaDialog
        open={state.metaOpen}
        snapshot={state.meta}
        onClose={actions.closeMeta}
        onRegenerate={() =>
          actions.regenerateCaptions({
            limit: 0,
            conn: 0,
            rate: 0,
            anima: true,
            meta: false,
            underscores: false,
          })
        }
        onRefresh={actions.refreshMetadata}
      />
    </div>
  );
}
