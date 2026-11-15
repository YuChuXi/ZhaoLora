import type { ReactNode } from 'react';

/**
 * 页面右下角的操作提示条：列出当前页的鼠标与快捷键操作.
 *
 * 固定在全局状态栏上方，不拦截指针事件，内容滚动时位置不变。
 *
 * Args:
 *   children (ReactNode): 提示文本.
 *
 * Returns:
 *   JSX.Element: 提示节点.
 *
 * Callers:
 *   - `filter/FilterPage.tsx:FilterPage`
 *   - `pages/scoring/ScoringPage.tsx:ScoringPage`
 *   - `pages/dedupe/DedupePage.tsx:DedupePage`
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 *   - `pages/caption/CaptionPage.tsx:CaptionPage`
 *   - `pages/video-extract/VideoExtractPage.tsx:VideoExtractPage`
 */
export function PageHint({ children }: { children: ReactNode }) {
  return (
    <div className="pointer-events-none fixed bottom-9 right-3 z-20 max-w-[72vw] text-right text-xs leading-relaxed text-muted-foreground/60">
      {children}
    </div>
  );
}
