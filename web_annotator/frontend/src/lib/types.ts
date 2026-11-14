export interface DirOption {
  name: string;
  label: string;
  image_count: number;
}

export interface LocalItem {
  path: string;
  stem: string;
  suffix: string;
  size: number;
  width: number;
  height: number;
  thumbnail_hash: string;
  selected: boolean;
  trashed: boolean;
  dir: string;
  aesthetic: number | null;
  scores: Record<string, number>;
  posted_at: string;
}

export interface Post {
  source: string;
  id: string;
  key: string;
  width: number;
  height: number;
  score: number;
  tags: string;
  preview_url: string;
  sample_url: string;
  file_url: string;
  rating: string;
  file_ext: string;
  downloaded: boolean;
  stem: string;
  filename: string;
  is_video: boolean;
  is_forward: boolean;
  post_type: string;
  page?: number;
  path?: string;
  name?: string;
  duration?: number;
}

export interface SearchResult {
  posts: Post[];
  has_more: boolean;
  session: string;
  page: number;
  count: number;
}

export type Mode = 'local' | 'gelbooru' | 'danbooru' | 'e621' | 'bilibili' | 'bilibili-video' | 'x' | 'pixiv' | 'mihuashi' | 'video';

export type SortKey = 'name' | 'posted' | 'aesthetic' | 'pixels' | 'search';

export type DownloadStatus = 'downloading' | 'done' | 'error';

/** 单张远端图片的保存状态与进度文本；无条目即未保存。 */
export interface DownloadEntry {
  status: DownloadStatus;
  progress: string;
}

export interface BatchSnapshot {
  job_id?: string;
  status?: string;
  done?: number;
  limit?: number;
  message?: string;
  pct?: number;
  log?: string[];
  result?: {
    items?: Array<{ path: string; similarity?: number; score?: number }>;
  };
}

export interface SearchScoreItem {
  path: string;
  similarity?: number;
  score?: number;
}

export interface BatchParams {
  limit: number;
  conn: number;
  rate: number;
  anima: boolean;
  meta: boolean;
  underscores: boolean;
}

export interface CaptionImage {
  path: string;
  stem: string;
  width: number;
  height: number;
  thumbnail_hash: string;
  dir: string;
  captions: string[];
}

export interface CaptionRule {
  text: string;
  pattern: string;
}

export interface CaptionGroupItem {
  text: string;
  weight: number;
}

export interface CaptionGroup {
  pattern: string;
  exclusive: boolean;
  items: CaptionGroupItem[];
}

export interface CaptionSettings {
  modes: string[];
  inject: {
    general?: boolean;
    general_threshold?: string;
    character?: boolean;
    character_threshold?: string;
    nsfw?: boolean;
    nsfw_threshold?: string;
  };
  force: boolean;
  rules: CaptionRule[];
  groups: CaptionGroup[];
}

export interface CaptionDirsData {
  dirs: DirOption[];
  ready: boolean;
  info: string;
  modes: string[];
  default_wd14_threshold: number;
  rule_templates: Array<{ label: string; text: string }>;
  filter_presets: Array<{ label: string; mode: string; pattern: string }>;
}

export interface CaptionStartBody {
  dirs: string[];
  paths: string[];
  modes: string[];
  general_threshold: number | null;
  character_threshold: number | null;
  nsfw_threshold: number | null;
  rules: CaptionRule[];
  groups: CaptionGroup[];
  force: boolean;
}
