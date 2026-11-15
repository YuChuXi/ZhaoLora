import { useEffect, useMemo, useRef, useState } from 'react';
import { api, apiPath, request } from '@/lib/api';
import type { BatchSnapshot } from '@/lib/types';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/controls';
import { Toolbar, ToolbarDivider } from '@/components/ui/toolbar';
import { useJob, useStatus } from '@/components/ui/status';
import { PageHint } from '@/components/ui/hint';
import { useApp } from '@/app/AppContext';
import { VideoFrameDecoder } from './decoder';
import { FrameChart } from './FrameCharts';
import { ThumbnailStrip } from './ThumbnailStrip';
import {
  frameTypeAt,
  formatTime,
  hasFrameTypes,
  readQueryPath,
  type FrameBitstream,
  type FrameSaveResponse,
  type VideoDetail,
  type VideoInfo,
} from './model';

/** 空白位流, 详情未加载时占位。 */
const EMPTY_BITS: FrameBitstream = { chars: '', sizes: [] };

/** 页面内使用的视频详情快照。 */
interface DetailState {
  total: number;
  width: number;
  height: number;
  duration: number;
  bits: FrameBitstream;
  saved: number[];
}

/** 键盘与滚轮共用的动作集合。 */
interface PageActions {
  switchVideo: (offset: number) => void;
  stepFrame: (step: number) => void;
  stepIFrame: (direction: number) => void;
  saveCurrent: () => void;
  deleteCurrent: () => void;
}

/** 长任务快照, 补充共享类型未声明的总帧数与取消标记。 */
type VideoJobSnapshot = BatchSnapshot & { total?: number; cancel_requested?: boolean };

/**
 * 将未知异常转换为可展示文本。
 *
 * Args:
 *   error (unknown): 捕获到的异常。
 *
 * Returns:
 *   string: 异常消息; 非 Error 时取字符串形式。
 *
 * Callers:
 *   - `VideoExtractPage`：请求失败提示。
 */
function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 视频抽帧页: 视频列表、逐帧预览、时间轴、存删帧与帧类型分析。
 *
 * Args:
 *   props (object): 无外部属性, 数据集与设置由 `useApp` 提供。
 *
 * Returns:
 *   JSX.Element: 抽帧页界面。
 *
 * Callers:
 *   - `main.tsx`：视频抽帧路由挂载。
 */
export function VideoExtractPage() {
  const app = useApp();
  const savedVideoSettings = (app.settings.video ?? {}) as { hide_b?: boolean };

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const decoderRef = useRef<VideoFrameDecoder | null>(null);
  const pathRef = useRef('');
  const indexRef = useRef(0);
  const detailRef = useRef<DetailState | null>(null);
  const videosRef = useRef<VideoInfo[]>([]);
  const hideBRef = useRef(savedVideoSettings.hide_b === true);
  const analyzingRef = useRef(false);
  const cancelRequestedRef = useRef(false);
  const unwatchRef = useRef<(() => void) | null>(null);
  const bootedRef = useRef(false);
  const actionsRef = useRef<PageActions | null>(null);

  const [videos, setVideos] = useState<VideoInfo[]>([]);
  const [path, setPath] = useState('');
  const [detail, setDetail] = useState<DetailState | null>(null);
  const [index, setIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [hideB, setHideB] = useState(savedVideoSettings.hide_b === true);
  const [analyzing, setAnalyzing] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<VideoJobSnapshot | null>(null);
  const [extra, setExtra] = useState('帧类型: 未分析');
  const [notice, setNotice] = useState<string | null>(null);
  const [extracting, setExtracting] = useState<{ done: number; total: number } | null>(null);

  pathRef.current = path;
  indexRef.current = index;
  detailRef.current = detail;
  videosRef.current = videos;
  hideBRef.current = hideB;

  const bits = detail?.bits ?? EMPTY_BITS;
  const total = detail?.total ?? 0;
  const saved = detail?.saved ?? [];
  const savedSet = useMemo(() => new Set(saved), [saved]);
  const checkedCount = checked.size;
  const vidIndex = videos.findIndex((video) => video.path === path);

  /**
   * 创建或复用 WebCodecs 解码控制器。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   VideoFrameDecoder: 解码控制器。
   *
   * Callers:
   *   - `VideoExtractPage.openVideo`：视频就绪后显示首帧。
   *   - `VideoExtractPage.gotoFrame`：切帧显示。
   */
  function ensureDecoder(): VideoFrameDecoder {
    if (decoderRef.current) return decoderRef.current;
    const canvas = canvasRef.current;
    if (!canvas) throw new Error('主预览画布未就绪');
    decoderRef.current = new VideoFrameDecoder({
      canvas,
      onPaint: (frame) => {
        indexRef.current = frame;
        setIndex(frame);
        setLoading(false);
      },
      onLoadingChange: setLoading,
      onError: (error) => app.notify(error.message),
    });
    return decoderRef.current;
  }

  /**
   * 停止当前帧类型分析的 SSE 订阅。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage.openVideo`：切换视频。
   *   - `VideoExtractPage.watchAnalysis`：任务终态。
   *   - `VideoExtractPage.toggleAnalysis`：用户停止。
   */
  function stopWatch(): void {
    unwatchRef.current?.();
    unwatchRef.current = null;
  }

  /**
   * 读取视频详情并更新页面数据。
   *
   * Args:
   *   targetPath (string): 视频相对路径。
   *
   * Returns:
   *   Promise<DetailState | null>: 解析后的详情; 请求期间已切换视频时返回 null。
   *
   * Callers:
   *   - `VideoExtractPage.openVideo`：切换视频。
   *   - `VideoExtractPage.watchAnalysis`：分析完成后回读位流。
   */
  async function loadDetail(targetPath: string): Promise<DetailState | null> {
    const data = await request<VideoDetail>(
      `${apiPath('/video/detail')}?path=${encodeURIComponent(targetPath)}`,
    );
    if (pathRef.current !== targetPath) return null;
    const next: DetailState = {
      total: data.total,
      width: data.width,
      height: data.height,
      duration: data.duration,
      bits: { chars: data.types.t, sizes: data.types.s },
      saved: data.saved,
    };
    detailRef.current = next;
    setDetail(next);
    if (!next.total) {
      app.notify('视频无法读取或总帧数为 0');
    } else {
      setExtra(hasFrameTypes(next.bits) ? '帧类型: 已分析' : '帧类型: 未分析');
    }
    return next;
  }

  /**
   * 订阅帧类型分析任务进度并在终态刷新位流。
   *
   * Args:
   *   id (string): 任务标识。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage.startAnalysis`：提交后订阅。
   *   - `VideoExtractPage.resumeAnalysis`：恢复未结束任务。
   */
  function watchAnalysis(id: string): void {
    stopWatch();
    unwatchRef.current = api.watchJob(id, (event) => {
      const next = event as VideoJobSnapshot;
      setSnapshot(next);
      if (next.cancel_requested) cancelRequestedRef.current = true;
      if (next.status === 'running' || next.status === 'queued') {
        setExtra(next.message || `正在分析帧类型 ${next.done ?? 0}/${next.total ?? 0}`);
        return;
      }
      analyzingRef.current = false;
      setAnalyzing(false);
      setJobId(null);
      stopWatch();
      if (next.status === 'done') {
        setExtra('分析: 完成');
        void (async () => {
          if (!pathRef.current) return;
          try {
            await loadDetail(pathRef.current);
          } catch (error) {
            app.notify(errorText(error));
          }
        })();
      } else if (next.status === 'error') {
        if (cancelRequestedRef.current) {
          setExtra('分析: 已取消');
        } else {
          setExtra('分析: 失败');
          app.notify(`帧类型分析失败: ${next.message ?? ''}`);
        }
      } else if (next.status === 'cancelled') {
        setExtra('分析: 已取消');
      }
    });
  }

  /**
   * 启动帧类型分析长任务。
   *
   * Args:
   *   targetPath (string): 视频相对路径。
   *
   * Returns:
   *   Promise<void>: 提交完成即返回。
   *
   * Callers:
   *   - `VideoExtractPage.openVideo`：位流未缓存时自动分析。
   *   - `VideoExtractPage.toggleAnalysis`：工具栏按钮。
   */
  async function startAnalysis(targetPath: string): Promise<void> {
    if (analyzingRef.current || !targetPath) return;
    analyzingRef.current = true;
    cancelRequestedRef.current = false;
    setAnalyzing(true);
    setJobId(null);
    setSnapshot(null);
    setExtra('帧类型: 提交中…');
    try {
      const data = await request<{ ok: boolean; job_id: string }>(
        `${apiPath('/video/analysis/start')}?path=${encodeURIComponent(targetPath)}`,
        { method: 'POST' },
      );
      if (!analyzingRef.current) {
        void api.cancelJob(data.job_id).catch((error) => app.notify(errorText(error)));
        return;
      }
      setJobId(data.job_id);
      watchAnalysis(data.job_id);
    } catch (error) {
      analyzingRef.current = false;
      setAnalyzing(false);
      setExtra('分析: 失败');
      app.notify(`帧类型分析失败: ${errorText(error)}`);
    }
  }

  /**
   * 切换帧类型分析的启动与停止。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage`：工具栏「帧类型 / 停止」按钮。
   */
  function toggleAnalysis(): void {
    if (analyzingRef.current) {
      if (jobId) {
        cancelRequestedRef.current = true;
        void api.cancelJob(jobId).catch((error) => {
          cancelRequestedRef.current = false;
          app.notify(errorText(error));
        });
      } else {
        stopWatch();
        analyzingRef.current = false;
        setAnalyzing(false);
        setExtra('分析: 已停止');
      }
      return;
    }
    void startAnalysis(pathRef.current);
  }

  /**
   * 恢复刷新页面后仍未结束的帧类型分析任务。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 查询完成即返回。
   *
   * Callers:
   *   - `VideoExtractPage`：初始化末尾。
   */
  async function resumeAnalysis(): Promise<void> {
    const active = await api.findActiveJob('video_analysis');
    if (!active?.job_id) return;
    analyzingRef.current = true;
    cancelRequestedRef.current = !!(active as VideoJobSnapshot).cancel_requested;
    setAnalyzing(true);
    setJobId(active.job_id);
    setExtra(active.message || '恢复中…');
    watchAnalysis(active.job_id);
  }

  /**
   * 打开指定视频: 重置解码与页面状态, 加载详情并显示首帧。
   *
   * Args:
   *   targetPath (string): 视频相对路径。
   *
   * Returns:
   *   Promise<void>: 首帧请求发出后返回。
   *
   * Callers:
   *   - `VideoExtractPage`：初始化。
   *   - `VideoExtractPage.switchVideo`：上一个 / 下一个视频。
   */
  async function openVideo(targetPath: string): Promise<void> {
    if (!targetPath) return;
    stopWatch();
    analyzingRef.current = false;
    cancelRequestedRef.current = false;
    setAnalyzing(false);
    setJobId(null);
    setSnapshot(null);
    decoderRef.current?.reset();
    pathRef.current = targetPath;
    indexRef.current = 0;
    setPath(targetPath);
    setIndex(0);
    detailRef.current = null;
    setDetail(null);
    setChecked(new Set());
    setExtracting(null);
    setExtra('帧类型: 未分析');
    setLoading(true);
    try {
      const next = await loadDetail(targetPath);
      if (!next) return;
      if (!next.total) {
        setLoading(false);
        return;
      }
      const decoder = ensureDecoder();
      decoder.setVideo(targetPath, next.total, next.duration);
      decoder.show(0);
      if (!hasFrameTypes(next.bits) && !analyzingRef.current) {
        void startAnalysis(targetPath);
      }
    } catch (error) {
      setLoading(false);
      app.notify(errorText(error));
    }
  }

  /**
   * 显示指定帧, 越界时钳位。
   *
   * Args:
   *   target (number): 目标帧索引。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage.stepFrame`：按步长切帧。
   *   - `VideoExtractPage.stepIFrame`：I 帧跳转。
   *   - `VideoExtractPage`：图表与缩略图点击。
   */
  function gotoFrame(target: number): void {
    const current = detailRef.current;
    if (!current?.total) return;
    const clamped = Math.max(0, Math.min(current.total - 1, target));
    if (clamped === indexRef.current) return;
    ensureDecoder().show(clamped);
  }

  /**
   * 按步长切帧; 隐藏 B 帧时步长按非 B 帧计数并跳过 B 帧。
   *
   * Args:
   *   step (number): 步长(正向后, 负向前)。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage`：工具栏按钮。
   *   - `VideoExtractPage`：主预览滚轮与键盘。
   */
  function stepFrame(step: number): void {
    const current = detailRef.current;
    if (!current?.total) return;
    if (!hideBRef.current) {
      gotoFrame(indexRef.current + step);
      return;
    }
    const direction = step > 0 ? 1 : -1;
    let remaining = Math.abs(step);
    let cursor = indexRef.current;
    while (remaining > 0) {
      cursor += direction;
      if (cursor < 0 || cursor >= current.total) break;
      if (current.bits.chars[cursor] !== 'B') remaining -= 1;
    }
    gotoFrame(cursor);
  }

  /**
   * 跳到下一个 / 上一个 I 帧; 无 I 帧时保持当前帧。
   *
   * Args:
   *   direction (number): 跳转方向(-1 向前, 1 向后)。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage`：Shift+滚轮。
   */
  function stepIFrame(direction: number): void {
    const current = detailRef.current;
    if (!current?.total) return;
    let cursor = indexRef.current + direction;
    while (cursor >= 0 && cursor < current.total) {
      if (current.bits.chars[cursor] === 'I') break;
      cursor += direction;
    }
    if (cursor < 0 || cursor >= current.total) return;
    gotoFrame(cursor);
  }

  /**
   * 切换到列表中的上一个 / 下一个视频。
   *
   * Args:
   *   offset (number): 偏移量(-1 上一个, 1 下一个)。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage`：工具栏按钮与键盘 A/D。
   */
  function switchVideo(offset: number): void {
    const list = videosRef.current;
    if (!list.length) return;
    const currentIndex = list.findIndex((video) => video.path === pathRef.current);
    const target = currentIndex + offset;
    if (target < 0 || target >= list.length) {
      app.notify('已是第一个/最后一个视频');
      return;
    }
    void openVideo(list[target].path);
  }

  /**
   * 用最新的已保存帧列表更新详情快照; 视频已切换时丢弃过期响应。
   *
   * Args:
   *   targetPath (string): 发起请求时的视频路径。
   *   indices (number[]): 已保存帧索引(升序)。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage.saveCurrent`：单帧保存。
   *   - `VideoExtractPage.deleteCurrent`：单帧删除。
   *   - `VideoExtractPage.extractChecked`：批量提取。
   */
  function applySaved(targetPath: string, indices: number[]): void {
    if (pathRef.current !== targetPath) return;
    const current = detailRef.current;
    if (!current) return;
    const next = { ...current, saved: indices };
    detailRef.current = next;
    setDetail(next);
  }

  /**
   * 保存当前帧 PNG 到 main 目录。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 请求结束即返回。
   *
   * Callers:
   *   - `VideoExtractPage`：工具栏、主预览左键、键盘 S/空格。
   */
  async function saveCurrent(): Promise<void> {
    const targetPath = pathRef.current;
    const current = detailRef.current;
    if (!targetPath || !current?.total) return;
    const targetIndex = indexRef.current;
    try {
      const res = await request<FrameSaveResponse>(apiPath('/video/frame/save'), {
        method: 'POST',
        body: JSON.stringify({ path: targetPath, index: targetIndex }),
      });
      applySaved(targetPath, res.saved);
      app.notify(`已存帧 ${targetIndex}`);
    } catch (error) {
      app.notify(`存帧失败: ${errorText(error)}`);
    }
  }

  /**
   * 删除当前帧已保存的 PNG。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 请求结束即返回。
   *
   * Callers:
   *   - `VideoExtractPage`：工具栏、主预览右键、键盘 X/Delete。
   */
  async function deleteCurrent(): Promise<void> {
    const targetPath = pathRef.current;
    const current = detailRef.current;
    if (!targetPath || !current?.total) return;
    const targetIndex = indexRef.current;
    try {
      const res = await request<FrameSaveResponse>(apiPath('/video/frame/delete'), {
        method: 'POST',
        body: JSON.stringify({ path: targetPath, index: targetIndex }),
      });
      applySaved(targetPath, res.saved);
      app.notify(`已删帧 ${targetIndex}`);
    } catch (error) {
      app.notify(`删帧失败: ${errorText(error)}`);
    }
  }

  /**
   * 批量提取缩略图条中勾选的帧, 已保存的帧跳过。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 全部请求结束即返回。
   *
   * Callers:
   *   - `VideoExtractPage`：工具栏「提取勾选」按钮。
   */
  async function extractChecked(): Promise<void> {
    const targetPath = pathRef.current;
    const current = detailRef.current;
    if (!targetPath || !current?.total) return;
    const savedIndices = new Set(current.saved);
    const pending = Array.from(checked)
      .sort((left, right) => left - right)
      .filter((frame) => !savedIndices.has(frame));
    if (!pending.length) {
      app.notify(checked.size ? '勾选帧均已保存' : '未勾选待提取帧');
      return;
    }
    setExtracting({ done: 0, total: pending.length });
    let latest = current.saved;
    try {
      for (let position = 0; position < pending.length; position += 1) {
        if (pathRef.current !== targetPath) {
          app.notify('视频已切换, 批量提取中止');
          return;
        }
        const res = await request<FrameSaveResponse>(apiPath('/video/frame/save'), {
          method: 'POST',
          body: JSON.stringify({ path: targetPath, index: pending[position] }),
        });
        latest = res.saved;
        applySaved(targetPath, latest);
        if (pathRef.current === targetPath) {
          setExtracting({ done: position + 1, total: pending.length });
        }
      }
      if (pathRef.current === targetPath) setChecked(new Set());
      app.notify(`已提取 ${pending.length} 帧`);
    } catch (error) {
      applySaved(targetPath, latest);
      app.notify(`批量提取失败: ${errorText(error)}`);
    } finally {
      setExtracting(null);
    }
  }

  /**
   * 切换单帧勾选状态。
   *
   * Args:
   *   frame (number): 帧索引。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ThumbnailStrip`：缩略图勾选框。
   */
  function toggleCheck(frame: number): void {
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(frame)) next.delete(frame);
      else next.add(frame);
      return next;
    });
  }

  /**
   * 切换隐藏 B 帧开关并持久化到设置键 `video`。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage`：工具栏隐藏 B 帧复选框。
   */
  function toggleHideB(): void {
    const next = !hideBRef.current;
    hideBRef.current = next;
    setHideB(next);
    app.saveSettings({ video: { hide_b: next } });
    if (next && detailRef.current?.bits.chars[indexRef.current] === 'B') {
      stepFrame(1);
    }
  }

  actionsRef.current = {
    switchVideo,
    stepFrame,
    stepIFrame,
    saveCurrent: () => void saveCurrent(),
    deleteCurrent: () => void deleteCurrent(),
  };

  useEffect(() => {
    if (bootedRef.current) return;
    bootedRef.current = true;
    void (async () => {
      if (typeof VideoDecoder === 'undefined' || typeof EncodedVideoChunk === 'undefined') {
        setNotice('当前浏览器不支持 WebCodecs, 无法逐帧解码');
        return;
      }
      try {
        const list = await request<VideoInfo[]>(apiPath('/videos'));
        videosRef.current = list;
        setVideos(list);
        if (!list.length) {
          setNotice('数据集 video 目录为空');
          return;
        }
        const initial = readQueryPath() || list[0].path;
        await openVideo(initial);
        if (!analyzingRef.current) await resumeAnalysis();
      } catch (error) {
        app.notify(errorText(error));
      }
    })();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || target?.isContentEditable) {
        return;
      }
      const actions = actionsRef.current;
      if (!actions) return;
      const key = event.key;
      let handled = true;
      if (key === 'a' || key === 'A') actions.switchVideo(-1);
      else if (key === 'd' || key === 'D') actions.switchVideo(1);
      else if (key === 'q' || key === 'Q') actions.stepFrame(-1);
      else if (key === 'e' || key === 'E') actions.stepFrame(1);
      else if (key === 'ArrowLeft') actions.stepFrame(-10);
      else if (key === 'ArrowRight') actions.stepFrame(10);
      else if (key === 'PageUp') actions.stepFrame(-100);
      else if (key === 'PageDown') actions.stepFrame(100);
      else if (key === 's' || key === 'S' || key === ' ') actions.saveCurrent();
      else if (key === 'x' || key === 'X' || key === 'Delete') actions.deleteCurrent();
      else handled = false;
      if (handled) event.preventDefault();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      if (event.shiftKey) {
        actionsRef.current?.stepIFrame(event.deltaY < 0 ? -1 : 1);
        return;
      }
      actionsRef.current?.stepFrame(event.deltaY < 0 ? -1 : 1);
    };
    stage.addEventListener('wheel', onWheel, { passive: false });
    return () => stage.removeEventListener('wheel', onWheel);
  }, []);

  useEffect(
    () => () => {
      unwatchRef.current?.();
      decoderRef.current?.destroy();
      decoderRef.current = null;
    },
    [],
  );

  const activeType = detail ? frameTypeAt(bits, index) : null;
  const positionText = total
    ? `${index + 1}/${total}${activeType ? ` · ${activeType.type}帧 ${activeType.size}KB` : ''}`
    : '0/0';
  const nameText = path
    ? `${vidIndex >= 0 ? `[${vidIndex + 1}/${videos.length}] ` : ''}${path.split('/').pop() ?? ''}`
      + ` · ${total} 帧`
      + (total ? ` · ${formatTime((index + 1) / (total / (detail?.duration || 1)))}` : '')
    : '加载中…';

  useStatus(
    [
      { kind: 'total', value: saved.length },
      ...(checkedCount ? [{ kind: 'selected' as const, value: checkedCount }] : []),
      {
        kind: 'resource',
        name: '分辨率',
        value: detail ? `${detail.width}×${detail.height}` : '-',
      },
    ],
    [positionText, nameText, extra].filter(Boolean).join(' · '),
  );
  useJob(snapshot ? { snapshot, onClear: () => setSnapshot(null) } : null);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <PageHint>
        左键存帧 · 右键删帧 · 滚轮调帧 · Shift+滚轮 按 I 帧跳 · A/D 切换视频 · Q/E 单帧 ·
        ←/→ 十帧 · S/空格 存帧 · X/Delete 删帧
      </PageHint>
      <Toolbar>
        <Button onClick={() => switchVideo(-1)} disabled={!videos.length} title="A: 上一个视频">
          ‹ 视频
        </Button>
        <Button onClick={() => switchVideo(1)} disabled={!videos.length} title="D: 下一个视频">
          视频 ›
        </Button>
        <ToolbarDivider />
        <Button onClick={() => stepFrame(-10)} disabled={!total} title="←: 后退 10 帧">
          -10
        </Button>
        <Button onClick={() => stepFrame(-1)} disabled={!total} title="Q: 上一帧">
          -1
        </Button>
        <Button onClick={() => stepFrame(1)} disabled={!total} title="E: 下一帧">
          +1
        </Button>
        <Button onClick={() => stepFrame(10)} disabled={!total} title="→: 前进 10 帧">
          +10
        </Button>
        <ToolbarDivider />
        <Button
          variant="primary"
          onClick={() => void saveCurrent()}
          disabled={!total}
          title="S/空格: 保存当前帧到 main 目录"
        >
          存帧
        </Button>
        <Button
          variant="danger"
          onClick={() => void deleteCurrent()}
          disabled={!total}
          title="X/Delete: 删除当前帧(已保存)"
        >
          删帧
        </Button>
        <Button
          onClick={() => void extractChecked()}
          disabled={!total || !checkedCount || !!extracting}
          title="将缩略图条中勾选的帧批量保存到 main 目录"
        >
          {extracting ? `提取中 ${extracting.done}/${extracting.total}` : `提取勾选${checkedCount ? ` (${checkedCount})` : ''}`}
        </Button>
        <Button
          variant="ghost"
          onClick={() => setChecked(new Set())}
          disabled={!checkedCount}
        >
          清除勾选
        </Button>
        <ToolbarDivider />
        <Button
          onClick={toggleAnalysis}
          disabled={!total}
          title="逐帧解码分析每帧编码类型(I/P/B)与包大小"
        >
          {analyzing ? '停止' : '帧类型'}
        </Button>
        <ToolbarDivider />
        <div
          className="flex select-none items-center gap-1.5 whitespace-nowrap text-xs text-muted-foreground"
          title="柱状图隐藏 B 帧柱并紧凑排列, 所有切帧操作(滚轮/键盘/按钮)跳过 B 帧, 只落在 I/P 帧"
        >
          <Checkbox checked={hideB} onCheckedChange={() => toggleHideB()} />
          <span className="cursor-pointer" onClick={toggleHideB}>
            隐藏B帧
          </span>
        </div>
      </Toolbar>

      <div
        ref={stageRef}
        className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-[#05070d]"
        onMouseDown={(event) => {
          if (event.button === 0) {
            event.preventDefault();
            void saveCurrent();
          } else if (event.button === 2) {
            event.preventDefault();
            void deleteCurrent();
          }
        }}
        onContextMenu={(event) => event.preventDefault()}
      >
        {notice ? (
          <p className="p-10 text-sm text-muted-foreground">{notice}</p>
        ) : (
          <>
            <canvas
              ref={canvasRef}
              className={`block max-h-full max-w-full select-none ${loading ? 'opacity-35' : ''}`}
            />
            {loading ? (
              <div className="absolute inset-0 flex items-center justify-center gap-2 text-sm text-muted-foreground">
                解码帧中…
              </div>
            ) : null}
          </>
        )}
      </div>

      <div className="flex h-24 flex-none">
        <FrameChart
          mode="global"
          total={total}
          duration={detail?.duration ?? 0}
          bits={bits}
          saved={saved}
          index={index}
          hideB={hideB}
          onSeek={gotoFrame}
        />
        <FrameChart
          mode="local"
          total={total}
          duration={detail?.duration ?? 0}
          bits={bits}
          saved={saved}
          index={index}
          hideB={hideB}
          onSeek={gotoFrame}
        />
      </div>

      <ThumbnailStrip
        path={path}
        total={total}
        width={detail?.width ?? 0}
        height={detail?.height ?? 0}
        index={index}
        saved={savedSet}
        checked={checked}
        onSeek={gotoFrame}
        onToggleCheck={toggleCheck}
      />
    </div>
  );
}
