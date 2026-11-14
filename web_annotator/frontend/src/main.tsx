import type { ComponentType } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import { AppShell } from './app/AppShell';
import { FilterPage } from './filter/FilterPage';
import { CaptionPage } from './pages/caption/CaptionPage';

const path = window.location.pathname;
const page = path.startsWith('/d/') ? (path.split('/')[3] || 'filter') : 'filter';

const PAGES: Record<string, ComponentType> = {
  filter: FilterPage,
  caption: CaptionPage,
};

const host = document.getElementById('root');
if (!host) throw new Error('缺少挂载节点 #root');

const Page = PAGES[page];
createRoot(host).render(<AppShell>{Page ? <Page /> : null}</AppShell>);
