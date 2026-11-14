import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ApiError, api, datasetName } from '@/lib/api';
import type { DirOption } from '@/lib/types';
import { AppContext } from './AppContext';
import { Nav } from '@/components/Nav';
import { StatusBar, type JobState, type StatusSegData } from '@/components/ui/status';
import { Toast, useNotifier } from '@/components/ui/toast';

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
  const [fatal, setFatal] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [dirsDisabled, setDirsDisabled] = useState(false);
  const { message, notify } = useNotifier();
  const [statusSegs, setStatusSegs] = useState<StatusSegData[]>([]);
  const [statusSummary, setStatusSummary] = useState('');
  const [job, setJobState] = useState<JobState | null>(null);
  const statusSignature = useRef('');
  const jobSignature = useRef('');

  const setStatus = useCallback((segs: StatusSegData[], summary: string) => {
    const signature = JSON.stringify({ segs, summary });
    if (statusSignature.current === signature) return;
    statusSignature.current = signature;
    setStatusSegs(segs);
    setStatusSummary(summary);
  }, []);

  const setJob = useCallback((next: JobState | null) => {
    const signature = next ? JSON.stringify(next.snapshot) : '';
    if (jobSignature.current === signature) return;
    jobSignature.current = signature;
    setJobState(next);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [settingsResult, dirsResult] = await Promise.allSettled([
        api.getSettings(),
        api.listDirs(),
      ]);
      if (cancelled) return;
      if (dirsResult.status === 'rejected') {
        const reason = dirsResult.reason;
        if (reason instanceof ApiError && reason.status === 404) {
          setFatal(reason.message);
          return;
        }
        notify(reason instanceof Error ? reason.message : String(reason));
      } else {
        setAllDirs(dirsResult.value.dirs);
      }
      if (settingsResult.status === 'rejected') {
        const reason = settingsResult.reason;
        notify(reason instanceof Error ? reason.message : String(reason));
      } else {
        setSettings(settingsResult.value.settings || {});
      }
      const values =
        settingsResult.status === 'fulfilled' ? settingsResult.value.settings || {} : {};
      const names =
        dirsResult.status === 'fulfilled'
          ? dirsResult.value.dirs.map((dir) => dir.name)
          : [];
      const saved = Array.isArray(values.dirs)
        ? values.dirs.filter((name): name is string => typeof name === 'string' && names.includes(name))
        : [];
      setDirsState(saved.length ? saved : ['main']);
      setReady(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [notify]);

  const saveChain = useRef<Promise<unknown>>(Promise.resolve());
  const saveSettings = useCallback(
    (patch: Record<string, unknown>, datasetLevel = false) => {
      setSettings((current) => ({ ...current, ...patch }));
      saveChain.current = saveChain.current
        .then(() => api.saveSettings(patch, datasetLevel))
        .catch((error) => {
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
    () => ({
      dataset,
      page,
      dirs,
      allDirs,
      setDirs,
      settings,
      saveSettings,
      notify,
      dirsDisabled,
      setDirsDisabled,
      setStatus,
      setJob,
    }),
    [
      dataset,
      page,
      dirs,
      allDirs,
      setDirs,
      settings,
      saveSettings,
      notify,
      dirsDisabled,
      setStatus,
      setJob,
    ],
  );

  const hideDirs = page === 'ready' || page === 'video_extract';
  const singleDir = page === 'crop' || page === 'paint';

  useEffect(() => {
    if (!ready || !singleDir || dirs.length <= 1) return;
    setDirs([dirs[0]]);
  }, [ready, singleDir, dirs, setDirs]);

  if (fatal) {
    return <div className="p-6 text-sm text-primary">{fatal}</div>;
  }

  return (
    <AppContext.Provider value={value}>
      <div className="flex h-dvh flex-col overflow-hidden">
        <Nav
          dataset={dataset}
          page={page}
          dirs={dirs}
          allDirs={allDirs}
          onDirsChange={setDirs}
          showDirs={!hideDirs}
          single={singleDir}
          disabled={dirsDisabled}
        />
        <div data-scroll className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          {ready ? children : <div className="p-6 text-xs text-muted-foreground">加载中…</div>}
        </div>
        <StatusBar segs={statusSegs} summary={statusSummary} job={job} />
        <Toast message={message} />
      </div>
    </AppContext.Provider>
  );
}
