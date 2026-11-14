import type { ComponentType } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import { AppShell } from './app/AppShell';
import { FilterPage } from './filter/FilterPage';
import { CaptionPage } from './pages/caption/CaptionPage';
import { CropPage } from './pages/crop/CropPage';
import { DedupePage } from './pages/dedupe/DedupePage';
import { IndexPage } from './pages/index/IndexPage';
import { PaintPage } from './pages/paint/PaintPage';
import { ReadyPage } from './pages/ready/ReadyPage';
import { ScoringPage } from './pages/scoring/ScoringPage';
import { TagCheckPage } from './pages/tag-check/TagCheckPage';
import { TagsPage } from './pages/tags/TagsPage';
import { VideoExtractPage } from './pages/video-extract/VideoExtractPage';

const PAGES: Record<string, ComponentType> = {
  filter: FilterPage,
  caption: CaptionPage,
  scoring: ScoringPage,
  dedupe: DedupePage,
  tags: TagsPage,
  ready: ReadyPage,
  'tag-check': TagCheckPage,
  crop: CropPage,
  paint: PaintPage,
  video_extract: VideoExtractPage,
};

const TITLES: Record<string, string> = {
  filter: '筛选',
  caption: '描述',
  scoring: '评分',
  dedupe: '去重',
  tags: '标签',
  ready: '就绪',
  'tag-check': '标签检查',
  crop: '裁剪',
  paint: '蒙版',
  video_extract: '视频抽帧',
};

const host = document.getElementById('root');
if (!host) throw new Error('缺少挂载节点 #root');

const path = window.location.pathname;
const parts = path.split('/').filter(Boolean);
const isHome = parts.length === 0;
const datasetPage = parts[0] === 'd' ? (parts[2] || 'filter') : 'filter';
const Page = PAGES[datasetPage];

if (!isHome) document.title = `${TITLES[datasetPage] ?? '标注'} · ${decodeURIComponent(parts[1] ?? '')}`;

createRoot(host).render(
  isHome ? <IndexPage /> : <AppShell>{Page ? <Page /> : null}</AppShell>,
);
