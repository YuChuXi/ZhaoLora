import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api, datasetName } from '@/lib/api';
import type { DirOption } from '@/lib/types';
import { AppContext } from './AppContext';
import { Nav } from '@/filter/Nav';

function pageOfPath(): string {
  const parts = window.location.pathname.split('/').filter(Boolean);
  return parts[0] === 'd' ? (parts[2] ?? 'filter') : 'filter';
}

export function AppShell({ children }: { children: ReactNode }) {
  const dataset = datasetName();
  const page = pageOfPath();
  const [dirs, setDirsState] = useState<string[]>(['main']);
  const [allDirs, setAllDirs] = useState<DirOption[]>([]);
  const [settings, setSettings] = useState<Record<string, unknown>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const toastTimer = useRef<number>();

  const notify = useCallback((text: string) => {
    setMessage(text);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setMessage(null), 3200);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [settingsResult, dirsResult] = await Promise.all([api.getSettings(), api.listDirs()]);
        if (cancelled) return;
        const values = settingsResult.settings || {};
        const names = dirsResult.dirs.map((dir) => dir.name);
        const saved = Array.isArray(values.dirs)
          ? values.dirs.filter((name): name is string => typeof name === 'string' && names.includes(name))
          : [];
        setSettings(values);
        setAllDirs(dirsResult.dirs);
        setDirsState(saved.length ? saved : ['main']);
        setReady(true);
      } catch (error) {
        if (!cancelled) setFatal(error instanceof Error ? error.message : String(error));
      }
    })();
    return () => {
      cancelled = true;
      window.clearTimeout(toastTimer.current);
    };
  }, []);

  const saveSettings = useCallback(
    (patch: Record<string, unknown>, datasetLevel = false) => {
      setSettings((current) => ({ ...current, ...patch }));
      void api.saveSettings(patch, datasetLevel).catch((error) => {
        notify(error instanceof Error ? error.message : String(error));
      });
    },
    [notify],
  );

  const setDirs = useCallback(
    (next: string[]) => {
      const list = next.length ? next : ['main'];
      setDirsState(list);
      saveSettings({ dirs: list }, true);
    },
    [saveSettings],
  );

  const value = useMemo(
    () => ({ dataset, page, dirs, allDirs, setDirs, settings, saveSettings, notify }),
    [dataset, page, dirs, allDirs, setDirs, settings, saveSettings, notify],
  );

  if (fatal) {
    return <div className="p-6 text-sm text-primary">{fatal}</div>;
  }

  return (
    <AppContext.Provider value={value}>
      <div className="min-h-screen">
        <Nav dataset={dataset} page={page} dirs={dirs} allDirs={allDirs} onDirsChange={setDirs} />
        {ready ? children : <div className="p-6 text-xs text-muted-foreground">加载中…</div>}
        {message ? (
          <div className="fixed bottom-5 left-5 z-[100] max-w-[70vw] rounded-md border border-border bg-[#16213e] px-3 py-2 text-xs text-foreground shadow-xl">
            {message}
          </div>
        ) : null}
      </div>
    </AppContext.Provider>
  );
}
