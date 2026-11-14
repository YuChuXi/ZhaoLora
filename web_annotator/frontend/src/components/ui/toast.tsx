import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * 全站统一的瞬态提示。
 *
 * Args:
 *   message (string | null): 提示文本；为空时不渲染。
 *
 * Returns:
 *   JSX.Element | null: 提示节点。
 *
 * Callers:
 *   - `app/AppShell.tsx:AppShell`
 *   - `pages/index/IndexPage.tsx:IndexPage`
 */
export function Toast({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="fixed bottom-5 left-5 z-[100] max-w-[70vw] rounded-md border border-border bg-[#16213e] px-3 py-2 text-xs text-foreground shadow-xl">
      {message}
    </div>
  );
}

/**
 * 提供提示文本与定时隐藏逻辑，重复调用会重置隐藏计时。
 *
 * Args:
 *   timeout (number): 自动隐藏毫秒数，默认 3200。
 *
 * Returns:
 *   { message: string | null; notify: (text: string) => void }: 当前提示与触发函数。
 *
 * Callers:
 *   - `app/AppShell.tsx:AppShell`
 *   - `pages/index/IndexPage.tsx:IndexPage`
 */
export function useNotifier(timeout = 3200) {
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<number>(undefined);

  const notify = useCallback(
    (text: string) => {
      setMessage(text);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setMessage(null), timeout);
    },
    [timeout],
  );

  useEffect(() => () => window.clearTimeout(timer.current), []);

  return { message, notify };
}
