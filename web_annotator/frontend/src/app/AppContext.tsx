import { createContext, useContext } from 'react';
import type { DirOption } from '@/lib/types';
import type { JobState, StatusSegData } from '@/components/ui/status';

export interface AppContextValue {
  dataset: string;
  page: string;
  dirs: string[];
  allDirs: DirOption[];
  setDirs: (dirs: string[]) => void;
  settings: Record<string, unknown>;
  saveSettings: (patch: Record<string, unknown>, datasetLevel?: boolean) => void;
  notify: (message: string) => void;
  dirsDisabled: boolean;
  setDirsDisabled: (disabled: boolean) => void;
  setStatus: (segs: StatusSegData[], summary: string) => void;
  setJob: (job: JobState | null) => void;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const value = useContext(AppContext);
  if (!value) throw new Error('AppContext 未挂载');
  return value;
}
