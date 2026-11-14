import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react';
import { errorMessage, getDoc, listDocs, type DocNavItem } from './homeApi';
import { TaskBoard } from './TaskBoard';
import { NavBar, NavLink } from '@/components/Nav';
import { Button } from '@/components/ui/button';
import { Toast, useNotifier } from '@/components/ui/toast';
import { cn } from '@/lib/utils';
import './index.css';

/**
 * 从地址栏读取当前文档标识。
 *
 * 无 doc 参数表示任务列表；`index` 与 `readme` 均映射到概览。
 *
 * Returns:
 *   string: 文档 slug；任务列表时为空字符串。
 *
 * Callers:
 *   - `pages/index/IndexPage.tsx:IndexPage` 初始化与 popstate 处理。
 */
function currentSlug(): string {
  const value = new URLSearchParams(window.location.search).get('doc');
  if (value == null || value === '') return '';
  if (value === 'index') return 'readme';
  return value.trim();
}

/**
 * 滚动到文档内指定锚点。
 *
 * Args:
 *   hash (string): 含 `#` 的锚点片段。
 *
 * Returns:
 *   void
 *
 * Callers:
 *   - `pages/index/IndexPage.tsx:navigateDoc`
 *   - `pages/index/IndexPage.tsx` 文档 HTML 渲染后的滚动。
 */
function scrollToHash(hash: string) {
  if (!hash) return;
  const id = decodeURIComponent(hash.slice(1));
  const target = document.getElementById(id);
  if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/**
 * 首页：自渲染顶栏与文档侧栏，任务列表与文档正文共用主区。
 *
 * 任务列表承载添加、补全、排序、拖拽重排、完成状态、归档恢复与旧设置导入；
 * 文档面板按 `?doc=` 切换，正文由服务端渲染为 HTML。
 *
 * Args:
 *   无。
 *
 * Returns:
 *   JSX.Element: 首页视图。
 *
 * Callers:
 *   - 首页路由 `/` 的页面注册表。
 */
export function IndexPage() {
  const [collapsed, setCollapsed] = useState(false);
  const [nav, setNav] = useState<DocNavItem[]>([]);
  const [slug, setSlug] = useState<string>(() => currentSlug());
  const [docHtml, setDocHtml] = useState('');
  const [loadedSlug, setLoadedSlug] = useState('');
  const [docError, setDocError] = useState<string | null>(null);
  const [docLoading, setDocLoading] = useState(() => currentSlug() !== '');
  const { message, notify } = useNotifier();

  const pendingHash = useRef('');

  useEffect(() => {
    const handleError = (event: ErrorEvent) => notify(`⚠ ${event.message}`);
    const handleRejection = (event: PromiseRejectionEvent) =>
      notify(`⚠ ${errorMessage(event.reason)}`);
    window.addEventListener('error', handleError);
    window.addEventListener('unhandledrejection', handleRejection);
    return () => {
      window.removeEventListener('error', handleError);
      window.removeEventListener('unhandledrejection', handleRejection);
    };
  }, [notify]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listDocs();
        if (!cancelled) setNav(data.nav ?? []);
      } catch (error) {
        if (!cancelled) notify(errorMessage(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [notify]);

  useEffect(() => {
    if (!slug) {
      setDocHtml('');
      setLoadedSlug('');
      setDocError(null);
      setDocLoading(false);
      pendingHash.current = '';
      document.title = '任务 · ZhaoLora 标注工具';
      return;
    }
    let cancelled = false;
    setDocLoading(true);
    setDocError(null);
    (async () => {
      try {
        const data = await getDoc(slug);
        if (cancelled) return;
        setDocHtml(data.html || '');
        setLoadedSlug(slug);
        setDocLoading(false);
        const title = (data.title || data.file || '文档').replace(/\.md$/, '');
        document.title = `${slug === 'readme' ? '概览' : title} · ZhaoLora 标注工具`;
      } catch (error) {
        if (cancelled) return;
        pendingHash.current = '';
        setDocError(`文档加载失败：${errorMessage(error)}`);
        setDocLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [notify, slug]);

  useEffect(() => {
    if (!slug || loadedSlug !== slug) return;
    const hash = pendingHash.current;
    if (!hash) return;
    pendingHash.current = '';
    scrollToHash(hash);
  }, [docHtml, loadedSlug, slug]);

  useEffect(() => {
    const handlePopState = () => setSlug(currentSlug());
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  /**
   * 切换文档：写入地址栏并更新当前 slug，同一文档时直接滚动锚点。
   *
   * Args:
   *   next (string): 目标文档 slug；空字符串表示任务列表。
   *   hash (string): 目标锚点片段，可为空。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - 侧栏「任务」、文档导航项与正文内文档链接的点击。
   */
  const navigateDoc = (next: string, hash = '') => {
    const url = new URL(window.location.href);
    if (!next) url.searchParams.delete('doc');
    else url.searchParams.set('doc', next);
    window.history.pushState(null, '', url);
    if (next === slug) {
      scrollToHash(hash);
      return;
    }
    if (next) {
      setDocLoading(true);
      setDocError(null);
    }
    pendingHash.current = hash;
    setSlug(next);
  };

  /**
   * 拦截文档正文内的 `?doc=` 链接，按单页方式切换并定位锚点。
   *
   * Args:
   *   event (ReactMouseEvent<HTMLElement>): 文章区域的点击事件。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - 文档文章容器的 onClick。
   */
  const handleArticleClick = (event: ReactMouseEvent<HTMLElement>) => {
    const anchor = (event.target as Element).closest('a[href^="?doc="]');
    if (!anchor) return;
    event.preventDefault();
    const href = anchor.getAttribute('href') ?? '';
    const url = new URL(href, `${window.location.origin}/`);
    navigateDoc(url.searchParams.get('doc') || '', url.hash || '');
  };

  return (
    <div className={collapsed ? 'home-page side-collapsed' : 'home-page'}>
      <NavBar>
        <Button
          variant="ghost"
          size="icon"
          className="my-1.5 mr-1"
          aria-controls="home-docs-side"
          aria-expanded={!collapsed}
          aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
          title={collapsed ? '展开侧栏' : '收起侧栏'}
          onClick={() => setCollapsed((value) => !value)}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
            className={cn('h-4 w-4 transition-transform', collapsed && 'rotate-180')}
          >
            <path d="M15 6l-6 6 6 6" />
          </svg>
        </Button>
        <NavLink
          href="/"
          active
          onClick={(event) => {
            event.preventDefault();
            navigateDoc('');
          }}
        >
          首页
        </NavLink>
      </NavBar>

      <div className="home-shell">
        <aside
          id="home-docs-side"
          className={collapsed ? 'home-side collapsed' : 'home-side'}
        >
          <div className="home-side-inner">
            <div className="home-side-top">
              <a
                className={slug ? 'home-side-tasks' : 'home-side-tasks active'}
                href="/"
                onClick={(event) => {
                  event.preventDefault();
                  navigateDoc('');
                }}
              >
                任务
              </a>
            </div>
            <div className="home-side-head">文档</div>
            <nav className="home-docs-nav">
              {nav.map((item) => (
                <a
                  key={item.slug}
                  className={item.slug === slug ? 'active' : undefined}
                  href={`/?doc=${encodeURIComponent(item.slug)}`}
                  onClick={(event) => {
                    event.preventDefault();
                    navigateDoc(item.slug);
                  }}
                >
                  {item.label}
                </a>
              ))}
            </nav>
          </div>
        </aside>

        <div className="home-main">
          <div className="home-pane" id="home-pane-tasks" hidden={!!slug}>
            <TaskBoard notify={notify} />
          </div>
          <div className="home-pane" id="home-pane-docs" hidden={!slug}>
            {docLoading ? (
              <article className="home-docs-article">
                <p className="home-docs-empty">加载中…</p>
              </article>
            ) : docError ? (
              <article className="home-docs-article">
                <p className="home-docs-empty">{docError}</p>
              </article>
            ) : (
              <article
                className="home-docs-article"
                onClick={handleArticleClick}
                dangerouslySetInnerHTML={{ __html: docHtml }}
              />
            )}
          </div>
        </div>
      </div>

      <Toast message={message} />
    </div>
  );
}
