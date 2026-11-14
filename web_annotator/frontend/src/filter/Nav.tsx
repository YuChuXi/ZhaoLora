import { useState } from 'react';
import type { DirOption } from '@/lib/types';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/controls';

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

function DirectoryPicker({
  dirs,
  allDirs,
  onChange,
}: {
  dirs: string[];
  allDirs: DirOption[];
  onChange: (dirs: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const label = dirs.length === 1 ? dirs[0] : `${dirs.length} 个目录`;

  const toggle = (name: string, checked: boolean) => {
    let next = checked ? [...dirs, name] : dirs.filter((dir) => dir !== name);
    if (!next.length) next = ['main'];
    onChange(next);
  };

  return (
    <div className="relative">
      <Button variant="ghost" size="sm" onClick={() => setOpen((value) => !value)} title={dirs.join(', ')}>
        {label} ▾
      </Button>
      {open ? (
        <div className="absolute right-0 top-full z-50 mt-1 w-60 rounded-md border border-border bg-[#16213e] p-1.5 shadow-xl">
          {allDirs.length === 0 ? <div className="px-2 py-1 text-xs text-muted-foreground">暂无目录</div> : null}
          {allDirs.map((dir) => (
            <label
              key={dir.name}
              className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-accent"
            >
              <Checkbox
                checked={dirs.includes(dir.name)}
                onCheckedChange={(checked) => toggle(dir.name, checked)}
              />
              <span className="flex-1 truncate">{dir.label || dir.name}</span>
              <span className="text-[10px] text-muted-foreground">{dir.image_count || ''}</span>
            </label>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function Nav({
  dataset,
  page,
  dirs,
  allDirs,
  onDirsChange,
}: {
  dataset: string;
  page: string;
  dirs: string[];
  allDirs: DirOption[];
  onDirsChange: (dirs: string[]) => void;
}) {
  const base = `/d/${encodeURIComponent(dataset)}`;
  return (
    <nav className="sticky top-0 z-40 flex h-[41px] shrink-0 items-stretch overflow-x-auto border-b border-border bg-[#0f1729] px-2">
      <a
        className="flex items-center border-b-2 border-transparent px-3.5 text-[13px] text-muted-foreground hover:text-foreground"
        href="/"
      >
        首页
      </a>
      {TABS.map((tab) => (
        <a
          key={tab.page}
          className={
            tab.page === page
              ? 'flex items-center border-b-2 border-primary px-3.5 text-[13px] font-medium text-foreground'
              : 'flex items-center border-b-2 border-transparent px-3.5 text-[13px] text-muted-foreground hover:text-foreground'
          }
          href={`${base}/${tab.page}`}
        >
          {tab.label}
        </a>
      ))}
      <div className="ml-auto flex items-center">
        <DirectoryPicker dirs={dirs} allDirs={allDirs} onChange={onDirsChange} />
      </div>
    </nav>
  );
}
