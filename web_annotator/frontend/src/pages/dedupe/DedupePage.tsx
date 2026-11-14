import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiPath, request } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input, Select } from '@/components/ui/input';
import { Dialog } from '@/components/ui/dialog';
import { Toolbar, ToolbarDivider } from '@/components/ui/toolbar';
import { useJob, useStatus } from '@/components/ui/status';
import { useApp } from '@/app/AppContext';

type Algorithm = 'mse' | 'fft' | 'orb';

const HIST_LABELS: Record<string, string> = {
  mse: 'MSE',
  fft: 'FFT 余弦',
  orb: 'ORB 匹配',
};

interface DedupeMember {
  path: string;
  stem: string;
  width: number;
  height: number;
  blockiness: number;
  area: number;
  is_keep: boolean;
  similarity: number | null;
  thumbnail_hash: string;
}

interface DedupeGroup {
  keep: string;
  size: number;
  members: DedupeMember[];
  suggested_remove: string[];
}

interface DedupeScanResult {
  algorithm: string;
  groups: DedupeGroup[];
  histogram: string | null;
}

interface DedupeJobEvent {
  status: string;
  message?: string;
  done?: number;
  total?: number;
  pct?: number | null;
  phase?: string;
  cancel_requested?: boolean;
  result?: unknown;
}

interface PreviewState {
  members: DedupeMember[];
  index: number;
}

/**
 * 从设置值中读取非空文本。
 *
 * Args:
 *   value (unknown): 设置中的原始值。
 *   fallback (string): 无有效文本时的缺省值。
 *
 * Returns:
 *   string: 字符串或有限数值的文本形式；否则返回缺省值。
 *
 * Callers:
 *   - `DedupePage`：恢复算法阈值与分辨率比。
 */
function savedText(value: unknown, fallback: string): string {
  if (typeof value === 'string' && value) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return fallback;
}

/**
 * 将未知异常转换为可展示的文本。
 *
 * Args:
 *   error (unknown): 捕获到的异常。
 *
 * Returns:
 *   string: 异常消息；非 Error 时取字符串形式。
 *
 * Callers:
 *   - `DedupePage`：请求失败提示。
 */
function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 以科学计数法尾数步进 MSE 阈值。
 *
 * 阈值范围限定为 `{1..9}e-9` 至 `{1..9}e0`，尾数满 9 进位、低于 1 借位。
 *
 * Args:
 *   value (string): 当前阈值文本。
 *   direction (number): +1 增大，-1 减小。
 *
 * Returns:
 *   string: 步进后的 `尾数e指数` 文本。
 *
 * Callers:
 *   - `DedupePage`：MSE 输入框方向键与步进按钮。
 */
function stepMseValue(value: string, direction: number): string {
  let current = Number.parseFloat(value);
  if (!(current > 0)) current = 1e-4;
  let exponent = Math.floor(Math.log10(current));
  let mantissa = Math.round(current / 10 ** exponent);
  if (mantissa < 1) mantissa = 1;
  if (mantissa > 9) mantissa = 9;
  if (direction > 0) {
    mantissa += 1;
    if (mantissa > 9) {
      mantissa = 1;
      exponent += 1;
    }
  } else {
    mantissa -= 1;
    if (mantissa < 1) {
      mantissa = 9;
      exponent -= 1;
    }
  }
  if (exponent > 0) exponent = 0;
  if (exponent < -9) exponent = -9;
  return `${mantissa}e${exponent}`;
}

/**
 * 构造成员的相似度说明文本。
 *
 * Args:
 *   member (DedupeMember): 去重组成员。
 *   algorithm (string): 扫描使用的算法。
 *
 * Returns:
 *   string: 无相似度时为空串；FFT/ORB 显示百分比，MSE 显示五位小数。
 *
 * Callers:
 *   - `DedupeCard`：卡片信息行。
 */
function similarityText(member: DedupeMember, algorithm: string): string {
  if (typeof member.similarity !== 'number') return '';
  if (algorithm === 'fft' || algorithm === 'orb') {
    return `相似 ${Math.round(member.similarity * 100)}%`;
  }
  return `MSE ${member.similarity.toFixed(5)}`;
}

/**
 * 按建议删除集合构造选中映射。
 *
 * Args:
 *   groups (DedupeGroup[]): 扫描得到的重复组。
 *
 * Returns:
 *   Record<string, boolean>: 建议删除路径到选中状态的映射。
 *
 * Callers:
 *   - `DedupePage`：扫描完成与「建议删除」。
 */
function suggestSelection(groups: DedupeGroup[]): Record<string, boolean> {
  const selected: Record<string, boolean> = {};
  for (const group of groups) {
    for (const path of group.suggested_remove) selected[path] = true;
  }
  return selected;
}

/**
 * 去重页成员卡片：缩略图、建议保留标记、分辨率与相似度，支持点击选中与预览。
 *
 * Args:
 *   member (DedupeMember): 成员数据。
 *   selected (boolean): 是否已选中。
 *   algorithm (string): 扫描算法，用于相似度文本。
 *   onToggle (function): 点击回调，入参为路径与 Shift 状态。
 *   onPreview (function): 右键或长按预览回调。
 *
 * Returns:
 *   JSX.Element: 固定宽度成员卡片。
 *
 * Callers:
 *   - `DedupePage`：重复组成员行。
 */
function DedupeCard({
  member,
  selected,
  algorithm,
  onToggle,
  onPreview,
}: {
  member: DedupeMember;
  selected: boolean;
  algorithm: string;
  onToggle: (path: string, shiftKey: boolean) => void;
  onPreview: () => void;
}) {
  const timer = useRef<number>();
  const longPressed = useRef(false);

  /**
   * 清除长按预览计时器。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `DedupeCard`：pointerup / pointercancel / pointermove 事件。
   */
  const cancelLongPress = () => {
    if (timer.current) {
      window.clearTimeout(timer.current);
      timer.current = undefined;
    }
  };

  const resolution = member.width && member.height ? `${member.width}×${member.height}` : '';
  const meta = [resolution, similarityText(member, algorithm)].filter(Boolean).join(' · ');

  return (
    <div
      className={
        member.is_keep
          ? 'masonry-card w-[180px] flex-none shadow-[inset_0_0_0_1px_rgba(74,222,128,0.35)]'
          : 'masonry-card w-[180px] flex-none'
      }
      data-selected={selected ? 'true' : 'false'}
      data-kind="image"
      title={member.path}
      onClick={(event) => {
        if (longPressed.current) {
          longPressed.current = false;
          return;
        }
        onToggle(member.path, event.shiftKey);
      }}
      onContextMenu={(event) => {
        event.preventDefault();
        onPreview();
      }}
      onPointerDown={(event) => {
        if (event.pointerType !== 'touch') return;
        timer.current = window.setTimeout(() => {
          longPressed.current = true;
          onPreview();
        }, 550);
      }}
      onPointerUp={cancelLongPress}
      onPointerCancel={cancelLongPress}
      onPointerMove={cancelLongPress}
    >
      <img
        src={
          member.thumbnail_hash
            ? api.thumbHashUrl(member.thumbnail_hash)
            : api.localImageUrl(member.path)
        }
        alt={member.stem || member.path}
        loading="lazy"
        decoding="async"
        draggable={false}
        style={{ height: 140, aspectRatio: 'auto' }}
      />
      {member.is_keep ? (
        <div className="absolute left-1.5 top-1.5 rounded bg-black/55 px-1.5 py-0.5 text-[10px] text-green-300">
          建议保留
        </div>
      ) : null}
      <div className="px-2 py-1.5 text-[11px] leading-snug text-muted-foreground">
        <div>{meta}</div>
        <div className="overflow-hidden text-ellipsis whitespace-nowrap">
          {member.stem || member.path}
        </div>
      </div>
      <span className="card-check">✓</span>
    </div>
  );
}

/**
 * 去重成员大图预览：先显示缩略图再替换原图，支持循环翻页与选中切换。
 *
 * Args:
 *   members (DedupeMember[]): 所在组的全部成员。
 *   index (number): 当前成员下标。
 *   selected (boolean): 当前成员是否已选中。
 *   onStep (function): 翻页回调，入参为 +1 / -1。
 *   onToggle (function): 切换当前成员选中状态。
 *   onClose (function): 关闭预览。
 *
 * Returns:
 *   JSX.Element: 预览对话框。
 *
 * Callers:
 *   - `DedupePage`：成员卡片右键或长按。
 */
function DedupePreview({
  members,
  index,
  selected,
  onStep,
  onToggle,
  onClose,
}: {
  members: DedupeMember[];
  index: number;
  selected: boolean;
  onStep: (delta: number) => void;
  onToggle: () => void;
  onClose: () => void;
}) {
  const member = members[index];
  const [src, setSrc] = useState(() =>
    member
      ? member.thumbnail_hash
        ? api.thumbHashUrl(member.thumbnail_hash)
        : api.localImageUrl(member.path)
      : '',
  );

  useEffect(() => {
    if (!member) return;
    setSrc(
      member.thumbnail_hash
        ? api.thumbHashUrl(member.thumbnail_hash)
        : api.localImageUrl(member.path),
    );
    let cancelled = false;
    const full = new Image();
    full.onload = () => {
      if (!cancelled) setSrc(full.src);
    };
    full.src = api.localImageUrl(member.path);
    return () => {
      cancelled = true;
    };
  }, [member]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'ArrowLeft' || event.key === 'a' || event.key === 'A') {
        event.preventDefault();
        onStep(-1);
      } else if (event.key === 'ArrowRight' || event.key === 'd' || event.key === 'D') {
        event.preventDefault();
        onStep(1);
      } else if (event.key === ' ' || event.key === 'Enter') {
        const tag = (event.target as HTMLElement | null)?.tagName?.toLowerCase();
        if (tag === 'button') return;
        event.preventDefault();
        onToggle();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onStep, onToggle]);

  if (!member) return null;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="预览"
      className="w-[min(960px,96vw)]"
      footer={
        <>
          <Button onClick={onToggle}>{selected ? '取消' : '选中'}</Button>
          <Button onClick={onClose}>关闭</Button>
        </>
      }
    >
      <div className="relative overflow-hidden rounded-xl border border-border bg-[#0a101c]">
        <img
          src={src}
          alt={member.stem || member.path}
          className="block max-h-[min(72vh,820px)] w-full bg-[#05070d] object-contain"
        />
        {members.length > 1 ? (
          <>
            <button
              type="button"
              title="←"
              className="absolute left-2.5 top-1/2 h-16 w-[42px] -translate-y-1/2 rounded-lg bg-black/45 text-3xl leading-none text-white hover:bg-primary/80"
              onClick={() => onStep(-1)}
            >
              ‹
            </button>
            <button
              type="button"
              title="→"
              className="absolute right-2.5 top-1/2 h-16 w-[42px] -translate-y-1/2 rounded-lg bg-black/45 text-3xl leading-none text-white hover:bg-primary/80"
              onClick={() => onStep(1)}
            >
              ›
            </button>
          </>
        ) : null}
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-2.5 text-[13px] text-muted-foreground">
        <span>
          {index + 1}/{members.length}
        </span>
        <span>{member.width && member.height ? `${member.width}×${member.height}` : '-'}</span>
        <b className="text-foreground">{member.stem || member.path}</b>
        <span>{member.is_keep ? '建议保留' : ''}</span>
      </div>
    </Dialog>
  );
}

/**
 * 近似去重页：扫描重复组、建议删除、成员选择与删除到回收站。
 *
 * Args:
 *   props (object): 无外部属性，数据集目录与设置由 `useApp` 提供。
 *
 * Returns:
 *   JSX.Element: 去重页界面。
 *
 * Callers:
 *   - `main.tsx`：去重路由挂载。
 */
export function DedupePage() {
  const { dirs, notify, saveSettings, setDirsDisabled, settings } = useApp();
  const saved = (settings.dedupe ?? {}) as Record<string, unknown>;
  const dirKey = dirs.join(',');

  const savedAlgorithm: Algorithm =
    saved.alg === 'fft' || saved.alg === 'orb' ? saved.alg : 'mse';

  const [algorithm, setAlgorithm] = useState<Algorithm>(savedAlgorithm);
  const [mseText, setMseText] = useState(savedText(saved.mse, '1e-4'));
  const [simValues, setSimValues] = useState<{ fft: string; orb: string }>({
    fft:
      savedAlgorithm === 'fft' && typeof saved.sim === 'string' ? saved.sim : '0.95',
    orb:
      savedAlgorithm === 'orb' && typeof saved.sim === 'string' ? saved.sim : '0.05',
  });
  const [ratioText, setRatioText] = useState(savedText(saved.ratio, '1.05'));

  const [groups, setGroups] = useState<DedupeGroup[]>([]);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [resultAlgorithm, setResultAlgorithm] = useState<Algorithm>(savedAlgorithm);
  const [histogram, setHistogram] = useState<string | null>(null);
  const [emptyText, setEmptyText] = useState('暂无结果');
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [snapshot, setSnapshot] = useState<DedupeJobEvent | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [conditionsOpen, setConditionsOpen] = useState(false);

  const lastDirs = useRef(dirKey);
  const resumed = useRef(false);

  const running = snapshot?.status === 'running' || snapshot?.status === 'queued';

  const selectedCount = useMemo(
    () => Object.values(selected).filter(Boolean).length,
    [selected],
  );

  /**
   * 将去重参数写入全局设置键 `dedupe`。
   *
   * Args:
   *   override (object): 需要同步覆盖的算法、阈值或分辨率比。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `DedupePage`：算法切换与扫描提交。
   */
  const persistDedupe = useCallback(
    (
      override?: Partial<{ alg: Algorithm; mse: string; sim: string; ratio: string }>,
    ) => {
      const nextAlgorithm = override?.alg ?? algorithm;
      const currentSim =
        override?.sim
        ?? (nextAlgorithm === 'mse' ? simValues.fft : simValues[nextAlgorithm]);
      saveSettings({
        dedupe: {
          alg: nextAlgorithm,
          mse: override?.mse ?? mseText,
          sim: currentSim,
          ratio: override?.ratio ?? ratioText,
        },
      });
    },
    [algorithm, mseText, ratioText, saveSettings, simValues],
  );

  /**
   * 恢复活动扫描任务的监听。
   *
   * Returns:
   *   Promise<void>: 存在未结束任务时写入快照与任务标识。
   *
   * Callers:
   *   - `DedupePage`：首次挂载与目录变化后。
   */
  const resumeActive = useCallback(async () => {
    const active = await api.findActiveJob('dedupe_scan');
    if (!active?.job_id || active.status === 'idle') return;
    setSnapshot(active as unknown as DedupeJobEvent);
    setJobId(active.job_id);
  }, []);

  useEffect(() => {
    if (resumed.current) return;
    resumed.current = true;
    void resumeActive().catch((error) => notify(errorText(error)));
  }, [notify, resumeActive]);

  useEffect(() => {
    if (lastDirs.current === dirKey) return;
    lastDirs.current = dirKey;
    setGroups([]);
    setSelected({});
    setCursor(null);
    setHistogram(null);
    setPreview(null);
    setEmptyText('目录已切换');
    if (jobId) return;
    void resumeActive().catch((error) => notify(errorText(error)));
  }, [dirKey, jobId, notify, resumeActive]);

  useEffect(() => {
    if (!jobId) return;
    return api.watchJob(jobId, (raw) => {
      const snap = raw as unknown as DedupeJobEvent;
      setSnapshot(snap);
      if (snap.status === 'done') {
        setJobId(null);
        const result = snap.result as DedupeScanResult | undefined;
        if (!result) {
          setGroups([]);
          setEmptyText('未发现相似组');
          return;
        }
        const nextGroups = result.groups ?? [];
        setGroups(nextGroups);
        setResultAlgorithm(
          result.algorithm === 'fft' || result.algorithm === 'orb'
            ? result.algorithm
            : 'mse',
        );
        setHistogram(result.histogram ?? null);
        setSelected(suggestSelection(nextGroups));
        setCursor(null);
        setEmptyText(nextGroups.length ? '' : '未发现相似组');
        notify(`完成 ${nextGroups.length} 组`);
      } else if (snap.status === 'error' || snap.status === 'cancelled') {
        setJobId(null);
        notify(snap.message || (snap.status === 'error' ? '扫描失败' : '已取消'));
      }
    });
  }, [jobId, notify]);

  useEffect(() => {
    if (!running) return;
    setDirsDisabled(true);
    return () => setDirsDisabled(false);
  }, [running, setDirsDisabled]);

  /**
   * 提交去重扫描任务。
   *
   * Returns:
   *   Promise<void>: 提交成功后进入任务监听。
   *
   * Callers:
   *   - `DedupePage`：扫描按钮。
   */
  const scan = useCallback(async () => {
    if (!dirs.length) {
      notify('请选择目录');
      return;
    }
    const mseValue = Number.parseFloat(mseText);
    const simValue = Number.parseFloat(algorithm === 'orb' ? simValues.orb : simValues.fft);
    const ratioValue = Number.parseFloat(ratioText);
    if (algorithm === 'mse' && !(mseValue > 0)) {
      notify('MSE 无效');
      return;
    }
    if (algorithm !== 'mse' && !(simValue > 0 && simValue <= 1)) {
      notify('相似度无效');
      return;
    }
    if (!(ratioValue >= 1)) {
      notify('分辨率比无效');
      return;
    }
    setPreview(null);
    setSnapshot({ status: 'queued', message: '等待执行' });
    persistDedupe();
    setSubmitting(true);
    try {
      const submitted = await request<{ job_id: string }>(apiPath('/dedupe/scan'), {
        method: 'POST',
        body: JSON.stringify({
          dirs,
          algorithm,
          mse_threshold: mseValue,
          sim_threshold: simValue,
          similar_res_ratio: ratioValue,
        }),
      });
      setJobId(submitted.job_id);
    } catch (error) {
      setSnapshot(null);
      notify(errorText(error));
    } finally {
      setSubmitting(false);
    }
  }, [algorithm, dirs, mseText, notify, persistDedupe, ratioText, simValues]);

  /**
   * 请求取消排队中的扫描任务。
   *
   * Returns:
   *   Promise<void>: 取消请求完成后提示。
   *
   * Callers:
   *   - `DedupePage`：取消按钮。
   */
  const cancelScan = useCallback(async () => {
    if (!jobId) return;
    try {
      await api.cancelJob(jobId);
      notify('正在取消…');
    } catch (error) {
      notify(errorText(error));
    }
  }, [jobId, notify]);

  /**
   * 切换成员选中态；按住 Shift 时从上次锚点在组内连选。
   *
   * Args:
   *   path (string): 成员相对路径。
   *   shiftKey (boolean): 是否按住 Shift。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `DedupeCard`：卡片点击。
   *   - `DedupePage`：预览对话框切换选中。
   */
  const togglePath = useCallback(
    (path: string, shiftKey: boolean) => {
      setSelected((current) => {
        if (shiftKey && cursor) {
          const members = groups.find((group) =>
            group.members.some((member) => member.path === path),
          )?.members;
          const keys = members?.map((member) => member.path) ?? [];
          const from = keys.indexOf(cursor);
          const to = keys.indexOf(path);
          if (from >= 0 && to >= 0) {
            const next = { ...current };
            for (const key of keys.slice(Math.min(from, to), Math.max(from, to) + 1)) {
              next[key] = true;
            }
            return next;
          }
        }
        const next = { ...current };
        if (next[path]) delete next[path];
        else next[path] = true;
        return next;
      });
      setCursor(path);
    },
    [cursor, groups],
  );

  /**
   * 将某一组的建议保留之外成员全部选中，建议保留成员取消选中。
   *
   * Args:
   *   group (DedupeGroup): 目标重复组。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `DedupePage`：组头「仅留建议」。
   */
  const keepOnlySuggested = useCallback((group: DedupeGroup) => {
    setSelected((current) => {
      const next = { ...current };
      for (const member of group.members) {
        if (member.is_keep) delete next[member.path];
        else next[member.path] = true;
      }
      return next;
    });
  }, []);

  /**
   * 全组按建议删除集合选中。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `DedupePage`：建议删除按钮。
   */
  const suggestAll = useCallback(() => {
    setSelected(suggestSelection(groups));
    setCursor(null);
  }, [groups]);

  /**
   * 清空全部选中。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `DedupePage`：全不选按钮。
   */
  const clearSelection = useCallback(() => {
    setSelected({});
    setCursor(null);
  }, []);

  /**
   * 将选中成员移入回收站，并从当前分组中移除。
   *
   * Returns:
   *   Promise<void>: 删除完成后更新分组与选中状态。
   *
   * Callers:
   *   - `DedupePage`：删除按钮。
   */
  const applyDelete = useCallback(async () => {
    const paths = Object.keys(selected).filter((path) => selected[path]);
    if (!paths.length) {
      notify('未选择图片');
      return;
    }
    if (!window.confirm(`删除 ${paths.length} 张到回收站？`)) return;
    setPreview(null);
    try {
      const result = await request<{ moved: number }>(apiPath('/dedupe/apply'), {
        method: 'POST',
        body: JSON.stringify({ paths, dirs }),
      });
      const gone = new Set(paths);
      setGroups((current) =>
        current
          .map((group) => {
            const members = group.members.filter((member) => !gone.has(member.path));
            return {
              ...group,
              members,
              size: members.length,
              suggested_remove: group.suggested_remove.filter((path) => !gone.has(path)),
            };
          })
          .filter((group) => group.size >= 2),
      );
      setSelected({});
      setCursor(null);
      notify(`已删除 ${result.moved} 张`);
    } catch (error) {
      notify(`删除失败: ${errorText(error)}`);
    }
  }, [dirs, notify, selected]);

  /**
   * 在预览组内循环翻页。
   *
   * Args:
   *   delta (number): +1 下一张，-1 上一张。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `DedupePage`：预览箭头与键盘。
   */
  const stepPreview = useCallback((delta: number) => {
    setPreview((current) => {
      if (!current || current.members.length < 2) return current;
      const length = current.members.length;
      return { ...current, index: (current.index + delta + length) % length };
    });
  }, []);

  /**
   * 切换预览中当前成员的选中状态。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `DedupePage`：预览选中按钮与空格 / 回车。
   */
  const togglePreview = useCallback(() => {
    if (!preview) return;
    const member = preview.members[preview.index];
    if (member) togglePath(member.path, false);
  }, [preview, togglePath]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a' && !event.shiftKey) {
        event.preventDefault();
        const next: Record<string, boolean> = {};
        for (const group of groups) {
          for (const member of group.members) next[member.path] = true;
        }
        setSelected(next);
      } else if (event.key === 'Delete') {
        event.preventDefault();
        void applyDelete();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [applyDelete, groups]);

  const previewSelected = preview
    ? !!selected[preview.members[preview.index]?.path]
    : false;

  useStatus([
    { kind: 'total', value: groups.length },
    { kind: 'selected', value: selectedCount },
  ]);
  useJob(
    snapshot
      ? {
          snapshot,
          onCancel: () => void cancelScan(),
          canCancel: snapshot.status === 'queued',
          cancelling: Boolean(snapshot.cancel_requested),
          onClear: () => setSnapshot(null),
        }
      : null,
  );

  return (
    <div>
      <Toolbar>
        <Button variant="primary" onClick={() => void scan()} disabled={!!jobId || submitting}>
          扫描
        </Button>

        <ToolbarDivider />

        <Button
          onClick={() => setConditionsOpen(true)}
          title={`算法 ${algorithm.toUpperCase()} · ${
            algorithm === 'mse' ? 'MSE' : '相似度'
          } ${algorithm === 'mse' ? mseText : simValues[algorithm]} · 分辨率比 ${ratioText}`}
        >
          扫描条件
        </Button>

        <ToolbarDivider />

        <Button onClick={clearSelection}>全不选</Button>
        <Button onClick={suggestAll}>建议删除</Button>
        <Button variant="danger" disabled={!selectedCount} onClick={() => void applyDelete()}>
          删除
        </Button>
      </Toolbar>

      <Dialog
        open={conditionsOpen}
        onOpenChange={setConditionsOpen}
        title="扫描条件"
        footer={<Button onClick={() => setConditionsOpen(false)}>关闭</Button>}
      >
        <div className="flex flex-col gap-3 text-xs text-muted-foreground">
          <label className="flex items-center gap-2">
            <span className="w-16">算法</span>
            <Select
              value={algorithm}
              onChange={(event) => {
                const next: Algorithm =
                  event.target.value === 'fft'
                    ? 'fft'
                    : event.target.value === 'orb'
                      ? 'orb'
                      : 'mse';
                setAlgorithm(next);
                persistDedupe({ alg: next });
              }}
            >
              <option value="mse">MSE（像素）</option>
              <option value="fft">FFT（低频）</option>
              <option value="orb">ORB（局部）</option>
            </Select>
          </label>

          {algorithm === 'mse' ? (
            <label className="flex items-center gap-2">
              <span className="w-16">
                {algorithm === 'mse' ? 'MSE 阈值' : '相似度'}
              </span>
              <Input
                type="number"
                className="flex-1"
                value={mseText}
                min="1e-9"
                max="1"
                step="any"
                onChange={(event) => setMseText(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
                  event.preventDefault();
                  setMseText(stepMseValue(mseText, event.key === 'ArrowUp' ? 1 : -1));
                }}
              />
              <span className="inline-flex flex-col gap-px">
                <button
                  type="button"
                  tabIndex={-1}
                  aria-label="增大阈值"
                  className="rounded border border-border bg-secondary px-1.5 text-[8px] leading-none text-muted-foreground hover:text-foreground"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => setMseText(stepMseValue(mseText, 1))}
                >
                  ▲
                </button>
                <button
                  type="button"
                  tabIndex={-1}
                  aria-label="减小阈值"
                  className="rounded border border-border bg-secondary px-1.5 text-[8px] leading-none text-muted-foreground hover:text-foreground"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => setMseText(stepMseValue(mseText, -1))}
                >
                  ▼
                </button>
              </span>
            </label>
          ) : (
            <label className="flex items-center gap-2">
              <span className="w-16">相似度</span>
              <Input
                type="number"
                className="flex-1"
                value={simValues[algorithm]}
                min="0"
                max="1"
                step="0.01"
                onChange={(event) =>
                  setSimValues((current) => ({ ...current, [algorithm]: event.target.value }))
                }
              />
            </label>
          )}

          <label className="flex items-center gap-2">
            <span className="w-16">分辨率比</span>
            <Input
              type="number"
              className="flex-1"
              value={ratioText}
              min="1"
              max="10"
              step="0.01"
              onChange={(event) => setRatioText(event.target.value)}
            />
          </label>
        </div>
      </Dialog>

      {histogram ? (
        <div className="mx-4 mb-3 mt-3 rounded-[10px] border border-border bg-secondary/40 px-3 py-2">
          <div className="pb-1.5 text-xs text-muted-foreground">
            两两相似度分布（{HIST_LABELS[resultAlgorithm] ?? resultAlgorithm}）
          </div>
          <img
            src={histogram}
            alt="两两相似度直方图"
            className="mx-auto block h-auto w-full max-w-[720px]"
          />
        </div>
      ) : null}

      {groups.length ? (
        <div className="flex flex-col gap-4 px-4 pb-10 pt-3">
          {groups.map((group, index) => (
            <section
              key={`${group.keep}-${index}`}
              className="overflow-hidden rounded-[10px] border border-border bg-secondary/40"
            >
              <div className="flex flex-wrap items-center gap-2.5 border-b border-border px-3 py-2 text-[13px] text-muted-foreground">
                <span>
                  组 <b className="text-foreground">#{index + 1}</b>
                </span>
                <span>{group.size} 张</span>
                <span>
                  建议保留 <b className="text-foreground">{group.keep || '-'}</b>
                </span>
                <Button size="sm" onClick={() => keepOnlySuggested(group)}>
                  仅留建议
                </Button>
              </div>
              <div className="flex flex-nowrap gap-2.5 overflow-x-auto p-2.5">
                {group.members.map((member, memberIndex) => (
                  <DedupeCard
                    key={member.path}
                    member={member}
                    selected={!!selected[member.path]}
                    algorithm={resultAlgorithm}
                    onToggle={togglePath}
                    onPreview={() =>
                      setPreview({ members: group.members, index: memberIndex })
                    }
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <div className="px-5 py-16 text-center text-sm text-muted-foreground">
          {running ? snapshot?.message || '扫描中…' : emptyText}
        </div>
      )}

      {preview ? (
        <DedupePreview
          key={preview.members[preview.index]?.path}
          members={preview.members}
          index={preview.index}
          selected={previewSelected}
          onStep={stepPreview}
          onToggle={togglePreview}
          onClose={() => setPreview(null)}
        />
      ) : null}
    </div>
  );
}
