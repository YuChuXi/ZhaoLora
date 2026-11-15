import { useCallback, useEffect, useMemo } from 'react';
import { api, SOURCES, thumbUrl } from '@/lib/api';
import { scrollContainer } from '@/lib/scroll';
import type { DownloadEntry, LocalItem, Mode, Post, ScoreItem, SortKey } from '@/lib/types';
import { Button } from '@/components/ui/button';
import { useStatus } from '@/components/ui/status';
import { PageHint } from '@/components/ui/hint';
import { useApp } from '@/app/AppContext';
import { Masonry, type ViewItem } from './Masonry';
import type { SearchState } from './state';
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

/**
 * 远端图片的保存状态文本.
 *
 * Args:
 *   entry (DownloadEntry | undefined): 图片的保存状态; 无条目即未保存.
 *
 * Returns:
 *   string: 状态文本, 下载中优先展示进度.
 *
 * Callers:
 *   - `filter/FilterPage.tsx:cardLabel`
 */
function saveLabel(entry?: DownloadEntry): string {
  if (entry?.status === 'downloading') return entry.progress || '保存中…';
  if (entry?.status === 'done') return '已保存';
  if (entry?.status === 'error') return '失败';
  return '未保存';
}

/**
 * 远端卡片的附加文本: 保存状态、帖子形态与分辨率、文件名.
 *
 * Args:
 *   post (Post): 搜索结果帖子.
 *   entry (DownloadEntry | undefined): 保存状态.
 *
 * Returns:
 *   string: 卡片附加文本.
 *
 * Callers:
 *   - `filter/FilterPage.tsx:FilterPage`
 */
function cardLabel(post: Post, entry?: DownloadEntry): string {
  const kind =
    post.post_type === 'video'
      ? '视频'
      : post.post_type === 'cover'
        ? '封面'
        : post.is_forward
          ? '转发'
          : '';
  const resolution = post.width > 0 ? `${post.width}×${post.height} ` : '';
  return [saveLabel(entry), kind, `${resolution}${post.stem || post.id}`].filter(Boolean).join(' · ');
}

/**
 * 给出当前排序键对应的角标文本.
 *
 * 名称排序不显示角标；搜索排序显示相似度或标签评分；评分项排序显示全部评分项
 * 分值；其余排序显示该键的原始值。
 *
 * Args:
 *   item (LocalItem): 本地图片条目.
 *   sort (SortKey): 当前排序键.
 *   search (SearchState | null): 搜索排序的分值表.
 *   scoreItems (ScoreItem[]): 数据集评分项.
 *
 * Returns:
 *   string | undefined: 角标文本; 无需显示时为 undefined.
 *
 * Callers:
 *   - `filter/FilterPage.tsx:FilterPage`
 */
function sortBadge(
  item: LocalItem,
  sort: SortKey,
  search: SearchState | null,
  scoreItems: ScoreItem[],
): string | undefined {
  if (sort === 'search') {
    if (!search) return undefined;
    return `${search.prefix}${(search.scores[item.path] ?? 0).toFixed(3)}`;
  }
  if (sort === 'aesthetic') {
    return item.aesthetic === null ? '美学 —' : `美学 ${item.aesthetic.toFixed(3)}`;
  }
  if (sort === 'pixels') {
    if (item.width <= 0 || item.height <= 0) return '—';
    const megapixels = (item.width * item.height) / 1_000_000;
    return `${item.width}×${item.height} · ${megapixels.toFixed(1)}MP`;
  }
  if (sort === 'posted') return item.posted_at ? item.posted_at.slice(0, 10) : undefined;
  if (sort.startsWith('score:')) {
    const parts = scoreItems.map((entry) => {
      const value = item.scores?.[entry.key];
      return `${entry.label} ${typeof value === 'number' ? value.toFixed(3) : '—'}`;
    });
    return parts.length ? parts.join(' · ') : undefined;
  }
  return undefined;
}

export function FilterPage() {
  const app = useApp();
  const { state, actions, localItems } = useFilterPage(app.dirs);
  const selectedCount = useMemo(
    () => Object.values(state.selected).filter(Boolean).length,
    [state.selected],
  );
  const { saved, savedCount, savingCount } = useMemo(() => {
    const map: Record<string, boolean> = {};
    let savedTotal = 0;
    let savingTotal = 0;
    for (const post of state.remote.posts) {
      const entry = state.downloads[post.key];
      if (!entry) continue;
      if (entry.status === 'downloading') {
        savingTotal += 1;
        map[post.key] = true;
      } else if (entry.status === 'done') {
        savedTotal += 1;
        map[post.key] = true;
      }
    }
    return { saved: map, savedCount: savedTotal, savingCount: savingTotal };
  }, [state.remote.posts, state.downloads]);
  const trashedCount = useMemo(
    () => state.local.filter((item) => item.trashed).length,
    [state.local],
  );

  useStatus(
    state.mode === 'local'
      ? [
          { kind: 'total', value: state.local.length },
          { kind: 'selected', value: selectedCount },
          ...(state.showTrash && trashedCount
            ? [{ kind: 'part' as const, name: '回收站', value: trashedCount }]
            : []),
        ]
      : [
          { kind: 'part', name: '已收录', value: state.remote.stored },
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
        label: item.stem,
        badge: sortBadge(item, state.sort, search, state.scoreItems),
        kind: 'image' as const,
      }));
    }
    return state.remote.posts.map((post) => ({
      key: post.key,
      ratio: ratioOf(post),
      src: thumbUrl(post),
      label: cardLabel(post, state.downloads[post.key]),
      page: post.page,
      kind: post.is_video ? ('video' as const) : ('image' as const),
    }));
  }, [state.mode, state.remote.posts, state.downloads, localItems, search, state.sort, state.scoreItems]);

  const setVisiblePage = actions.setVisiblePage;
  useEffect(() => {
    if (state.mode === 'local') return;
    const host = scrollContainer();
    if (!host) return;
    let frame = 0;
    const sync = () => {
      frame = 0;
      const cards = host.querySelectorAll<HTMLElement>('[data-page]');
      if (!cards.length) return;
      const top = host.getBoundingClientRect().top;
      for (const card of Array.from(cards)) {
        if (card.getBoundingClientRect().bottom <= top) continue;
        const page = Number(card.dataset.page);
        if (page > 0) setVisiblePage(page);
        break;
      }
    };
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(sync);
    };
    host.addEventListener('scroll', onScroll, { passive: true });
    sync();
    return () => {
      host.removeEventListener('scroll', onScroll);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [setVisiblePage, state.mode, state.remote.posts]);

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
      <PageHint>
        {state.mode === 'local'
          ? '点击选中 · Shift 连选 · 右键相似 · Delete 删除 · A 全选 · D 清除 · Ctrl+I 反选 · Ctrl+Z 恢复'
          : SOURCES[state.mode].hint ?? '左键写入目录 · 右键大图'}
      </PageHint>
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
        artist={state.remote.fields.uid ?? ''}
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
