import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

/**
 * 全站统一工具栏容器：吸附于页面滚动容器顶部，只承载操作与参数入口。
 *
 * 统计、资源状态与摘要由全站底部 `StatusBar` 承担，任务进度同样在其中呈现；
 * 工具栏只放操作与参数入口，不得再承载状态信息。
 *
 * Args:
 *   children (ReactNode): 工具栏内容，按分组顺序排列.
 *   className (string | undefined): 页面附加类名，仅用于必要的布局差异.
 *
 * Returns:
 *   JSX.Element: 工具栏节点.
 *
 * Callers:
 *   - `frontend/src/filter/Toolbar.tsx:Toolbar`
 *   - `frontend/src/pages/scoring/ScoringPage.tsx:ScoringPage`
 *   - `frontend/src/pages/dedupe/DedupePage.tsx:DedupePage`
 *   - `frontend/src/pages/crop/CropPage.tsx:CropPage`
 *   - `frontend/src/pages/paint/PaintPage.tsx:PaintPage`
 *   - `frontend/src/pages/tags/TagsPage.tsx:TagsPage`
 *   - `frontend/src/pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 *   - `frontend/src/pages/caption/CaptionPage.tsx:CaptionPage`
 *   - `frontend/src/pages/ready/ReadyPage.tsx:ReadyPage`
 *   - `frontend/src/pages/video-extract/VideoExtractPage.tsx:VideoExtractPage`
 */
export function Toolbar({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        'sticky top-0 z-30 flex shrink-0 flex-wrap items-center gap-2 border-b border-border bg-background/95 px-2 py-2 backdrop-blur',
        className,
      )}
    >
      {children}
    </div>
  );
}

/**
 * 工具栏内的语义分组，组内元素保持统一间距.
 *
 * Args:
 *   children (ReactNode): 同组控件.
 *   className (string | undefined): 附加类名.
 *
 * Returns:
 *   JSX.Element: 分组节点.
 *
 * Callers:
 *   - 各页面工具栏.
 */
export function ToolbarGroup({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('flex items-center gap-2', className)}>{children}</div>;
}

/**
 * 工具栏分组之间的竖线分隔.
 *
 * Args:
 *   无.
 *
 * Returns:
 *   JSX.Element: 分隔节点.
 *
 * Callers:
 *   - 各页面工具栏.
 */
export function ToolbarDivider() {
  return <span className="mx-1 h-5 w-px bg-border" />;
}
