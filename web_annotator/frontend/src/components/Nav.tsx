import type { Mode } from '@/lib/types';

const TABS = [
  { page: 'scoring', label: '评分' },
  { page: 'filter', label: '筛选' },
  { page: 'dedupe', label: '去重' },
  { page: 'crop', label: '裁剪' },
  { page: 'paint', label: '蒙版' },
  { page: 'tags', label: '标签' },
  { page: 'tag-check', label: '标签检查' },
  { page: 'caption', label: '描述' },
  { page: 'ready', label: '就绪' },
];

export function Nav({ dataset, page }: { dataset: string; page: string }) {
  const base = `/d/${encodeURIComponent(dataset)}`;
  return (
    <nav className="sticky top-0 z-40 flex items-stretch overflow-x-auto border-b border-border bg-[#0f1729] px-2">
      <a
        className="border-b-2 border-transparent px-3.5 py-2.5 text-[13px] text-muted-foreground hover:text-foreground"
        href="/"
      >
        首页
      </a>
      {TABS.map((tab) => (
        <a
          key={tab.page}
          className={
            tab.page === page
              ? 'border-b-2 border-primary px-3.5 py-2.5 text-[13px] font-medium text-foreground'
              : 'border-b-2 border-transparent px-3.5 py-2.5 text-[13px] text-muted-foreground hover:text-foreground'
          }
          href={`${base}/${tab.page}`}
        >
          {tab.label}
        </a>
      ))}
    </nav>
  );
}

export function modeLabel(mode: Mode): string {
  return TABS.find((tab) => tab.page === mode)?.label ?? mode;
}
