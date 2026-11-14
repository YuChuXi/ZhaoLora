import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import type { DirOption } from '@/lib/types';
import { cn } from '@/lib/utils';
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

/**
 * 全站唯一顶栏容器：外观与高度由内容决定，不参与页面的尺寸计算。
 *
 * Args:
 *   children (ReactNode): 顶栏条目与右侧附加控件。
 *
 * Returns:
 *   JSX.Element: 顶栏节点。
 *
 * Callers:
 *   - `components/Nav.tsx:Nav`
 *   - `pages/index/IndexPage.tsx:IndexPage`
 */
export function NavBar({ children }: { children: ReactNode }) {
  return (
    <nav className="flex shrink-0 items-stretch overflow-x-auto border-b border-border bg-[#0f1729] px-2">
      {children}
    </nav>
  );
}

/**
 * 顶栏条目。
 *
 * Args:
 *   href (string): 目标地址。
 *   active (boolean | undefined): 是否为当前页。
 *   children (ReactNode): 条目文本。
 *   onClick (function | undefined): 点击回调，用于单页内跳转。
 *
 * Returns:
 *   JSX.Element: 链接节点。
 *
 * Callers:
 *   - `components/Nav.tsx:Nav`
 *   - `pages/index/IndexPage.tsx:IndexPage`
 */
export function NavLink({
  href,
  active,
  children,
  onClick,
}: {
  href: string;
  active?: boolean;
  children: ReactNode;
  onClick?: (event: ReactMouseEvent<HTMLAnchorElement>) => void;
}) {
  return (
    <a
      className={cn(
        'flex items-center border-b-2 px-3.5 py-2.5 text-[13px]',
        active
          ? 'border-primary font-medium text-foreground'
          : 'border-transparent text-muted-foreground hover:text-foreground',
      )}
      href={href}
      onClick={onClick}
    >
      {children}
    </a>
  );
}

/**
 * 目录选择器：多选或单选，菜单经 Portal 挂载以避免被顶栏裁切。
 *
 * Args:
 *   dirs (string[]): 当前选中目录。
 *   allDirs (DirOption[]): 可选目录。
 *   onChange (function): 目录变化回调。
 *   single (boolean | undefined): 单选模式。
 *   disabled (boolean | undefined): 任务运行期间禁止切换。
 *
 * Returns:
 *   JSX.Element: 选择器节点。
 *
 * Callers:
 *   - `components/Nav.tsx:Nav`
 */
function DirectoryPicker({
  dirs,
  allDirs,
  onChange,
  single,
  disabled,
}: {
  dirs: string[];
  allDirs: DirOption[];
  onChange: (dirs: string[]) => void;
  single?: boolean;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; right: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const label = single ? (dirs[0] || 'main') : dirs.length === 1 ? dirs[0] : `${dirs.length} 个目录`;

  useLayoutEffect(() => {
    if (!open) return;
    const update = () => {
      const rect = buttonRef.current?.getBoundingClientRect();
      if (rect) setPosition({ top: rect.bottom + 4, right: window.innerWidth - rect.right });
    };
    update();
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update, true);
      window.removeEventListener('resize', update);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (menuRef.current?.contains(target) || buttonRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const toggle = (name: string, checked: boolean) => {
    if (single) {
      onChange(checked ? [name] : ['main']);
      setOpen(false);
      return;
    }
    let next = checked ? [...dirs, name] : dirs.filter((dir) => dir !== name);
    if (!next.length) next = ['main'];
    onChange(next);
  };

  return (
    <div className="relative">
      <Button
        ref={buttonRef}
        variant="ghost"
        size="sm"
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        title={dirs.join(', ')}
      >
        {label} ▾
      </Button>
      {open && position
        ? createPortal(
            <div
              ref={menuRef}
              className="fixed z-[100] max-h-[70vh] w-60 overflow-y-auto rounded-md border border-border bg-[#16213e] p-1.5 shadow-xl"
              style={{ top: position.top, right: position.right }}
            >
              {allDirs.length === 0 ? <div className="px-2 py-1 text-xs text-muted-foreground">暂无目录</div> : null}
              {allDirs.map((dir) => (
                <label
                  key={dir.name}
                  className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-accent"
                >
                  <Checkbox
                    checked={dirs.includes(dir.name)}
                    disabled={disabled}
                    onCheckedChange={(checked) => toggle(dir.name, checked)}
                  />
                  <span className="flex-1 truncate">{dir.label || dir.name}</span>
                  <span className="text-[10px] text-muted-foreground">{dir.image_count || ''}</span>
                </label>
              ))}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}

/**
 * 数据集页的完整顶栏：首页入口、功能标签与目录选择器。
 *
 * Args:
 *   dataset (string): 当前任务名。
 *   page (string): 当前页面标识。
 *   dirs (string[]): 选中目录。
 *   allDirs (DirOption[]): 可选目录。
 *   onDirsChange (function): 目录变化回调。
 *   showDirs (boolean | undefined): 是否展示目录选择器。
 *   single (boolean | undefined): 单目录模式。
 *   disabled (boolean | undefined): 禁止切换目录。
 *
 * Returns:
 *   JSX.Element: 顶栏节点。
 *
 * Callers:
 *   - `app/AppShell.tsx:AppShell`
 */
export function Nav({
  dataset,
  page,
  dirs,
  allDirs,
  onDirsChange,
  showDirs = true,
  single = false,
  disabled = false,
}: {
  dataset: string;
  page: string;
  dirs: string[];
  allDirs: DirOption[];
  onDirsChange: (dirs: string[]) => void;
  showDirs?: boolean;
  single?: boolean;
  disabled?: boolean;
}) {
  const base = `/d/${encodeURIComponent(dataset)}`;
  return (
    <NavBar>
      <NavLink href="/">首页</NavLink>
      {TABS.map((tab) => (
        <NavLink key={tab.page} href={`${base}/${tab.page}`} active={tab.page === page}>
          {tab.label}
        </NavLink>
      ))}
      <div className="ml-auto flex items-center">
        {showDirs ? (
          <DirectoryPicker
            dirs={dirs}
            allDirs={allDirs}
            onChange={onDirsChange}
            single={single}
            disabled={disabled}
          />
        ) : null}
      </div>
    </NavBar>
  );
}
