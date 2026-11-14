import { createContext, useContext } from 'react';
import type { DirOption } from '@/lib/types';

export interface AppContextValue {
  dataset: string;
  page: string;
  dirs: string[];
  allDirs: DirOption[];
  setDirs: (dirs: string[]) => void;
  settings: Record<string, unknown>;
  saveSettings: (patch: Record<string, unknown>, datasetLevel?: boolean) => void;
  notify: (message: string) => void;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const value = useContext(AppContext);
  if (!value) throw new Error('AppContext 未挂载');
  return value;
}
