import { useEffect } from 'react';

/**
 * 页面滚动容器。
 *
 * 全站滚动由 `AppShell` 的内容容器承担，窗口本身不滚动；需要滚动操作的
 * 逻辑一律经此函数取容器，不得直接操作 `window`。
 *
 * Returns:
 *   HTMLElement: 标记了 `data-scroll` 的内容容器。
 *
 * Callers:
 *   - `filter/Masonry.tsx:Masonry` 向上加载后的滚动补偿
 *   - `filter/useFilterPage.ts:useFilterPage` 搜索与跳页回到顶部
 *   - `lib/scroll.ts:restoreCardAnchor`
 *   - `lib/scroll.ts:useScrollLock`
 */
export function scrollContainer(): HTMLElement {
  const node = document.querySelector<HTMLElement>('[data-scroll]');
  if (!node) throw new Error('缺少页面滚动容器 [data-scroll]');
  return node;
}

/** 列数调整前的可见卡片锚点，用于重排后恢复阅读位置。 */
export interface CardAnchor {
  key: string;
  top: number;
}

/**
 * 记录首个可见卡片及其顶部偏移。
 *
 * Returns:
 *   CardAnchor | null: 锚点；当前没有可见卡片时为 null。
 *
 * Callers:
 *   - `filter/Toolbar.tsx:changeColumns`
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 */
export function captureCardAnchor(): CardAnchor | null {
  for (const card of document.querySelectorAll<HTMLElement>('.masonry-card')) {
    const rect = card.getBoundingClientRect();
    if (rect.bottom > 0 && card.dataset.key) return { key: card.dataset.key, top: rect.top };
  }
  return null;
}

/**
 * 列数变化后按锚点滚动，使同一张卡片保持在原有视觉位置。
 *
 * Args:
 *   anchor (CardAnchor | null): `captureCardAnchor` 记录的锚点。
 *
 * Returns:
 *   void
 *
 * Callers:
 *   - `filter/Toolbar.tsx:Toolbar` 列数变化后的布局副作用
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage` 列数变化后的布局副作用
 */
export function restoreCardAnchor(anchor: CardAnchor | null): void {
  if (!anchor) return;
  const card = document.querySelector<HTMLElement>(
    `.masonry-card[data-key="${CSS.escape(anchor.key)}"]`,
  );
  if (!card) return;
  const top = card.getBoundingClientRect().top;
  if (top !== anchor.top) scrollContainer().scrollBy(0, top - anchor.top);
}

/**
 * 模态视图打开期间锁定页面滚动，关闭后恢复原值。
 *
 * Args:
 *   locked (boolean): 是否锁定。
 *
 * Returns:
 *   void
 *
 * Callers:
 *   - `pages/scoring/ScoringPage.tsx:ScoringPage` 灯箱
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage` 单图模式
 */
export function useScrollLock(locked: boolean): void {
  useEffect(() => {
    if (!locked) return;
    const node = scrollContainer();
    const previous = node.style.overflow;
    node.style.overflow = 'hidden';
    return () => {
      node.style.overflow = previous;
    };
  }, [locked]);
}
