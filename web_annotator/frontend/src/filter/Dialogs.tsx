import { useEffect, useState } from 'react';
import { api, pageUrl, sampleUrl, statusLabel } from '@/lib/api';
import type { BatchParams, BatchSnapshot, DownloadStatus, LocalItem, Post } from '@/lib/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/controls';
import { Dialog } from '@/components/ui/dialog';
import type { SimilarState } from './state';

export function PreviewDialog({
  post,
  selected,
  status,
  onClose,
  onToggle,
  onTagSearch,
}: {
  post: Post | null;
  selected: boolean;
  status?: DownloadStatus;
  onClose: () => void;
  onToggle: (post: Post) => void;
  onTagSearch: (tag: string) => void;
}) {
  if (!post) return null;
  const tags = (post.tags || '').split(/\s+/).filter(Boolean);
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={`${post.source} · ${post.stem || post.id}`}
      className="w-[min(960px,94vw)]"
    >
      <div className="flex max-h-[70vh] gap-4">
        <div className="flex min-w-0 flex-1 items-center justify-center overflow-hidden rounded-md bg-[#05070d]">
          <img src={sampleUrl(post)} alt={post.stem || post.id} className="max-h-[68vh] max-w-full object-contain" />
        </div>
        <div className="flex w-72 shrink-0 flex-col gap-3 text-xs text-muted-foreground">
          <div>
            <div className="text-foreground">{post.stem || post.id}</div>
            <div>分辨率 {post.width > 0 ? `${post.width}×${post.height}` : '-'}</div>
            <div>分数 {post.score ?? '-'} · rating {post.rating || '-'}</div>
            <div>状态 {status ? statusLabel(status) : selected ? '已保存' : '未保存'}</div>
          </div>
          <div className="flex flex-wrap gap-1.5 overflow-auto">
            {tags.map((tag) => (
              <button
                key={tag}
                type="button"
                className="max-w-full break-all rounded border border-border bg-[#0a1a30] px-2 py-1 text-[11px] text-foreground hover:border-primary"
                onClick={() => onTagSearch(tag)}
              >
                {tag}
              </button>
            ))}
          </div>
          <div className="mt-auto flex flex-wrap gap-2">
            <a
              className="inline-flex h-7 items-center rounded-md border border-border px-3 text-xs text-foreground hover:bg-accent"
              href={pageUrl(post)}
              target="_blank"
              rel="noreferrer"
            >
              打开原站
            </a>
            <Button variant={selected ? 'default' : 'primary'} onClick={() => onToggle(post)}>
              {selected ? '移除' : '下载'}
            </Button>
          </div>
        </div>
      </div>
    </Dialog>
  );
}

export function SimilarDialog({
  similar,
  onClose,
  onJump,
}: {
  similar: SimilarState | null;
  onClose: () => void;
  onJump: (path: string) => void;
}) {
  if (!similar) return null;
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()} title="色调相似" className="w-[min(720px,94vw)]">
      {similar.loading ? (
        <div className="py-8 text-center text-xs text-muted-foreground">加载中…</div>
      ) : (
        <div className="grid grid-cols-3 gap-2">
          {similar.items.map((item: LocalItem) => (
            <button
              key={item.path}
              type="button"
              className="overflow-hidden rounded border border-border text-left hover:border-primary"
              onClick={() => {
                onJump(item.path);
                onClose();
              }}
            >
              <img
                src={item.thumbnail_hash ? api.thumbHashUrl(item.thumbnail_hash) : api.localImageUrl(item.path)}
                alt={item.stem}
                loading="lazy"
                className="block aspect-square w-full object-cover"
              />
              <div className="px-2 py-1 text-[11px] text-muted-foreground">
                {item.width > 0 ? `${item.width}×${item.height} · ` : ''}
                {item.stem}
              </div>
            </button>
          ))}
        </div>
      )}
    </Dialog>
  );
}

export function BatchDialog({
  open,
  running,
  snapshot,
  onClose,
  onStart,
  onParams,
}: {
  open: boolean;
  running: boolean;
  snapshot: BatchSnapshot | null;
  onClose: () => void;
  onStart: (params: BatchParams) => void;
  onParams: (params: BatchParams) => void;
}) {
  const [params, setParams] = useState<BatchParams>({
    limit: 50,
    conn: 2,
    rate: 0,
    anima: true,
    meta: false,
    underscores: false,
  });

  useEffect(() => {
    if (!running) return;
    const timer = window.setTimeout(() => onParams(params), 300);
    return () => window.clearTimeout(timer);
  }, [params, running, onParams]);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title="批量下载"
      footer={
        <>
          <Button onClick={onClose}>关闭</Button>
          <Button variant="primary" disabled={running} onClick={() => onStart(params)}>
            {running ? '运行中…' : '开始'}
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2 text-xs text-muted-foreground">
        <span>数量</span>
        <Input
          type="number"
          min={1}
          value={params.limit}
          onChange={(event) => setParams({ ...params, limit: Number(event.target.value) || 1 })}
        />
        <span>连接</span>
        <Input
          type="number"
          min={1}
          max={8}
          value={params.conn}
          onChange={(event) => setParams({ ...params, conn: Number(event.target.value) || 1 })}
        />
        <span>限速</span>
        <Input
          type="number"
          min={0}
          step={0.5}
          value={params.rate}
          onChange={(event) => setParams({ ...params, rate: Number(event.target.value) || 0 })}
        />
        <span>格式</span>
        <label className="flex items-center gap-2">
          <Checkbox checked={params.anima} onCheckedChange={(checked) => setParams({ ...params, anima: checked })} />
          Anima 格式
        </label>
        <span />
        <label className="flex items-center gap-2">
          <Checkbox checked={params.meta} onCheckedChange={(checked) => setParams({ ...params, meta: checked })} />
          包含元数据标签
        </label>
        <span />
        <label className="flex items-center gap-2">
          <Checkbox
            checked={params.underscores}
            onCheckedChange={(checked) => setParams({ ...params, underscores: checked })}
          />
          保留下划线
        </label>
      </div>
      {snapshot ? (
        <div className="mt-3 space-y-1 text-xs text-muted-foreground">
          <div>
            {snapshot.status || 'running'} · {snapshot.done ?? 0}/{snapshot.limit ?? params.limit}
          </div>
          <div className="h-1 overflow-hidden rounded bg-border">
            <div
              className="h-full bg-primary transition-all"
              style={{ width: `${Math.min(100, snapshot.pct ?? 0)}%` }}
            />
          </div>
          {snapshot.message ? <div>{snapshot.message}</div> : null}
        </div>
      ) : null}
    </Dialog>
  );
}

export function MetaDialog({
  open,
  running,
  onClose,
  onRegenerate,
  onRefresh,
}: {
  open: boolean;
  running: boolean;
  onClose: () => void;
  onRegenerate: () => void;
  onRefresh: () => void;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title="元数据工具"
      footer={<Button onClick={onClose}>关闭</Button>}
    >
      <div className="flex flex-col gap-2 text-xs text-muted-foreground">
        <Button variant="primary" disabled={running} onClick={onRegenerate}>
          重生成 Caption
        </Button>
        <Button disabled={running} onClick={onRefresh}>
          刷新元数据
        </Button>
      </div>
    </Dialog>
  );
}
