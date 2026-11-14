import { useCallback, useEffect, useRef, useState } from 'react';
import { apiPath, request } from '@/lib/api';
import { useApp } from '@/app/AppContext';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/controls';
import { Toolbar, ToolbarDivider } from '@/components/ui/toolbar';
import { useStatus } from '@/components/ui/status';

/** 工作子目录摘要。 */
interface WorkDirInfo {
  name: string;
  label: string;
  image_count: number;
}

/** 单目录蒙版与标注统计。 */
interface MaskStats {
  name: string;
  mask_status: '完整' | '无';
  main_count: number;
  text_count: number;
  caption_count: number;
  mask_complete: number;
  mask_none: number;
  ready: boolean;
}

/** 就绪页目录与正则化候选响应。 */
interface ReadyDirsData {
  root: string;
  dirs: WorkDirInfo[];
  mask_stats: MaskStats[];
  reg_dirs: WorkDirInfo[];
}

/** 就绪 TOML 中的一个子集。 */
interface ReadySubset {
  name: string;
  image_dir: string;
  image_count: number;
  conditioning_data_dir?: string | null;
}

/** 就绪页生成结果。 */
interface ReadyTomlResult {
  files: Record<string, string>;
  subsets: ReadySubset[];
  subset_repeats: Record<string, number[]>;
  reg_subsets: ReadySubset[];
}

/** 就绪页导出结果。 */
interface TrainExportResult {
  files: string[];
}

/** 就绪页生成请求体。 */
interface ReadyBody {
  dirs: string[];
  target_images: number;
  train_steps: number;
  subset_resolutions: Record<string, number[]>;
  use_reg: boolean;
  reg_dirs: string[];
  reg_resolutions: Record<string, number[]>;
  mode: 'style' | 'character';
  train_format: string;
  variant: string;
}

/** 就绪页表单状态。 */
interface ReadyForm {
  format: string;
  variant: string;
  mode: 'style' | 'character';
  target: string;
  steps: string;
  useReg: boolean;
  dirs: WorkDirInfo[];
  maskStats: MaskStats[];
  regDirs: WorkDirInfo[];
  enabled: Record<string, boolean>;
  resolutions: Record<string, string>;
  regEnabled: Record<string, boolean>;
  regResolutions: Record<string, string>;
  fileNames: string[];
  files: Record<string, string>;
  active: string | null;
  summary: string;
}

/** 设置中恢复的顶栏与生成参数。 */
interface SavedReady {
  format: string;
  variant: string;
  mode: 'style' | 'character';
  target: string;
  steps: string;
  useReg: boolean;
}

/** 配置格式/变体选项。 */
interface FormatOption {
  format: string;
  variant: string;
  label: string;
  title: string;
}

/** 顶栏可选的配置格式与变体。 */
const FORMATS: FormatOption[] = [
  {
    format: 'sd-scripts',
    variant: 'anima',
    label: 'sd-scripts(anima)',
    title: 'sd-scripts(Anima) 训练配置, 文件集合取自 assets/configs/sd-scripts/anima/',
  },
  {
    format: 'diffusion-pipe',
    variant: 'krea2',
    label: 'diffusion-pipe(krea2)',
    title: 'diffusion-pipe(krea2) 训练配置, 文件集合取自 assets/configs/diffusion-pipe/krea2/',
  },
  {
    format: 'diffusion-pipe',
    variant: 'moss',
    label: 'diffusion-pipe(moss)',
    title: 'diffusion-pipe(moss) 训练配置, 文件集合取自 assets/configs/diffusion-pipe/moss/',
  },
];

/** 训练模式选项。 */
const MODES: Array<{ mode: 'style' | 'character'; label: string; title: string }> = [
  { mode: 'style', label: '画风', title: '画风模式' },
  { mode: 'character', label: '角色', title: '角色模式' },
];

/**
 * 提取错误对象的可读文本。
 *
 * Args:
 *   error (unknown): 捕获到的异常。
 *
 * Returns:
 *   string: 异常消息或字符串化结果。
 *
 * Callers:
 *   - `ReadyPage` 的生成、导出与目录加载流程。
 */
function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 从设置对象恢复顶栏与生成参数，格式/变体非法时回退到首个选项。
 *
 * Args:
 *   raw (unknown): `ready` 设置值。
 *
 * Returns:
 *   SavedReady: 规整后的参数。
 *
 * Callers:
 *   - `buildInitialForm`
 */
function readReadySettings(raw: unknown): SavedReady {
  const saved = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const matched =
    FORMATS.find(
      (option) => option.format === saved.format && option.variant === saved.variant,
    ) ?? FORMATS[0];
  const targetValue = Number(saved.target_images);
  const stepsValue = Number(saved.train_steps);
  return {
    format: matched.format,
    variant: matched.variant,
    mode: saved.mode === 'character' ? 'character' : 'style',
    target: targetValue > 0 ? String(targetValue) : '512',
    steps: stepsValue > 0 ? String(stepsValue) : '500',
    useReg: saved.use_reg !== false,
  };
}

/**
 * 构造就绪页初始表单状态。
 *
 * Args:
 *   raw (unknown): `ready` 设置值。
 *
 * Returns:
 *   ReadyForm: 初始表单状态。
 *
 * Callers:
 *   - `ReadyPage` 初始化表单。
 */
function buildInitialForm(raw: unknown): ReadyForm {
  const saved = readReadySettings(raw);
  return {
    format: saved.format,
    variant: saved.variant,
    mode: saved.mode,
    target: saved.target,
    steps: saved.steps,
    useReg: saved.useReg,
    dirs: [],
    maskStats: [],
    regDirs: [],
    enabled: {},
    resolutions: {},
    regEnabled: {},
    regResolutions: {},
    fileNames: [],
    files: {},
    active: null,
    summary: '-',
  };
}

/**
 * 解析单个目录的分辨率输入。
 *
 * Args:
 *   text (string): 逗号分隔的分辨率文本，留空表示不指定。
 *   name (string): 目录名，用于提示。
 *   silent (boolean): 是否静默失败。
 *   notify (function): 全局提示函数。
 *
 * Returns:
 *   number[] | null: 分辨率列表；存在非法值时返回 null。
 *
 * Callers:
 *   - `collectResolutions`
 */
function parseResolutions(
  text: string,
  name: string,
  silent: boolean,
  notify: (message: string) => void,
): number[] | null {
  if (!text) return [];
  const list: number[] = [];
  for (const part of text.split(/\s*,\s*/)) {
    const value = Number.parseInt(part, 10);
    if (!(value > 0)) {
      if (!silent) notify(`目录 ${name} 无效分辨率: ${part}`);
      return null;
    }
    list.push(value);
  }
  return list;
}

/**
 * 收集一组目录的分辨率输入。
 *
 * Args:
 *   names (string[]): 当前展示的目录名。
 *   values (Record<string, string>): 目录名到输入文本的映射。
 *   silent (boolean): 是否静默失败。
 *   notify (function): 全局提示函数。
 *
 * Returns:
 *   Record<string, number[]> | null: 目录名到分辨率列表的映射；存在非法值时返回 null。
 *
 * Callers:
 *   - `collectBody`
 */
function collectResolutions(
  names: string[],
  values: Record<string, string>,
  silent: boolean,
  notify: (message: string) => void,
): Record<string, number[]> | null {
  const out: Record<string, number[]> = {};
  for (const name of names) {
    const list = parseResolutions((values[name] ?? '').trim(), name, silent, notify);
    if (!list) return null;
    out[name] = list;
  }
  return out;
}

/**
 * 读取表单参数构造生成/导出请求体。
 *
 * Args:
 *   form (ReadyForm): 当前表单状态。
 *   silent (boolean): 是否静默失败。
 *   notify (function): 全局提示函数。
 *
 * Returns:
 *   ReadyBody | null: 请求体；参数非法时返回 null。
 *
 * Callers:
 *   - `ReadyPage.generate`
 *   - `ReadyPage.doExport`
 */
function collectBody(
  form: ReadyForm,
  silent: boolean,
  notify: (message: string) => void,
): ReadyBody | null {
  const target = Number.parseInt(form.target, 10);
  if (!(target >= 1)) {
    if (!silent) notify('目标图片数无效');
    return null;
  }
  const steps = Number.parseInt(form.steps, 10);
  if (!(steps >= 1)) {
    if (!silent) notify('训练步数无效');
    return null;
  }
  const subsetResolutions = collectResolutions(
    form.dirs.map((dir) => dir.name),
    form.resolutions,
    silent,
    notify,
  );
  if (!subsetResolutions) return null;
  const regResolutions = collectResolutions(
    form.regDirs.map((dir) => dir.name),
    form.regResolutions,
    silent,
    notify,
  );
  if (!regResolutions) return null;
  return {
    dirs: form.dirs.filter((dir) => form.enabled[dir.name]).map((dir) => dir.name),
    target_images: target,
    train_steps: steps,
    subset_resolutions: subsetResolutions,
    use_reg: form.useReg,
    reg_dirs: form.regDirs.filter((dir) => form.regEnabled[dir.name]).map((dir) => dir.name),
    reg_resolutions: regResolutions,
    mode: form.mode,
    train_format: form.format,
    variant: form.variant,
  };
}

/**
 * 按生成结果构造工具栏统计摘要。
 *
 * Args:
 *   data (ReadyTomlResult): 生成结果。
 *
 * Returns:
 *   string: 子集数、图片数与重复次数摘要。
 *
 * Callers:
 *   - `ReadyPage.generate`
 */
function formatSummary(data: ReadyTomlResult): string {
  const subsets = data.subsets || [];
  const repeats = data.subset_repeats || {};
  const totalImages = subsets.reduce((sum, subset) => sum + (subset.image_count || 0), 0);
  const repeated = subsets.reduce((sum, subset) => {
    const list = repeats[subset.name] || [];
    return sum + (subset.image_count || 0) * list.reduce((left, right) => left + right, 0);
  }, 0);
  let repStr: string;
  if (!subsets.length) {
    repStr = '0';
  } else {
    const first = (repeats[subsets[0].name] || []).join('+');
    const allSame = subsets.every(
      (subset) => (repeats[subset.name] || []).join('+') === first,
    );
    if (allSame) {
      repStr = first.includes('+') ? `(${first})` : first;
    } else {
      repStr = subsets
        .map((subset) => {
          const list = (repeats[subset.name] || []).join('+');
          return `${subset.name}×${list.includes('+') ? `(${list})` : list}`;
        })
        .join(', ');
    }
  }
  const regImages = (data.reg_subsets || []).reduce(
    (sum, subset) => sum + (subset.image_count || 0),
    0,
  );
  return (
    `${subsets.length} 个 subset · ${totalImages} 张 × ${repStr} = ${repeated} 张` +
    (regImages ? ` · reg ${regImages} 张` : '')
  );
}

/**
 * 就绪页：配置格式/模式切换、目录侧栏、训练配置生成、复制与导出。
 *
 * Args:
 *   无。
 *
 * Returns:
 *   JSX.Element: 就绪页视图。
 *
 * Callers:
 *   - 前端页面注册表按 page 名 `ready` 挂载。
 */
export function ReadyPage() {
  const app = useApp();
  const { dirs, notify, saveSettings } = app;
  const dirsKey = dirs.join(',');
  const [form, setFormState] = useState<ReadyForm>(() => buildInitialForm(app.settings.ready));
  const formRef = useRef(form);
  const started = useRef(false);
  const autoTimer = useRef<number>();

  /**
   * 以补丁方式更新表单，并同步更新生成流程读取的引用。
   *
   * Args:
   *   patch (Partial<ReadyForm>): 待合并字段。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 的顶栏、侧栏与生成/复制/导出流程。
   */
  const setForm = useCallback((patch: Partial<ReadyForm>) => {
    const next = { ...formRef.current, ...patch };
    formRef.current = next;
    setFormState(next);
  }, []);

  /**
   * 加载工作目录、蒙版统计与正则化候选，并为新目录补默认启用状态与分辨率。
   *
   * Args:
   *   silent (boolean): 失败时是否静默。
   *
   * Returns:
   *   Promise<boolean>: 是否加载成功。
   *
   * Callers:
   *   - `ReadyPage` 的启动/目录变化副作用。
   */
  const loadDirs = useCallback(
    async (silent: boolean): Promise<boolean> => {
      try {
        const data = await request<ReadyDirsData>(apiPath('/ready/dirs'));
        const current = formRef.current;
        const enabled = { ...current.enabled };
        const resolutions = { ...current.resolutions };
        for (const dir of data.dirs) {
          if (enabled[dir.name] === undefined) enabled[dir.name] = true;
          if (resolutions[dir.name] === undefined) resolutions[dir.name] = '1536,1024,512';
        }
        const regEnabled = { ...current.regEnabled };
        const regResolutions = { ...current.regResolutions };
        for (const dir of data.reg_dirs) {
          if (regEnabled[dir.name] === undefined) regEnabled[dir.name] = false;
          if (regResolutions[dir.name] === undefined) regResolutions[dir.name] = '1536';
        }
        setForm({
          dirs: data.dirs,
          maskStats: data.mask_stats ?? [],
          regDirs: data.reg_dirs ?? [],
          enabled,
          resolutions,
          regEnabled,
          regResolutions,
        });
        return true;
      } catch (error) {
        if (!silent) notify(messageOf(error));
        return false;
      }
    },
    [notify, setForm],
  );

  /**
   * 生成当前格式的训练配置预览并更新标签页与统计摘要。
   *
   * Args:
   *   silent (boolean): 是否静默失败与成功提示。
   *
   * Returns:
   *   Promise<void>: 生成完成。
   *
   * Callers:
   *   - `ReadyPage` 的启动副作用
   *   - `ReadyPage.changeFormat`
   *   - `ReadyPage.changeMode`
   *   - `ReadyPage.autoGenerate`
   *   - `ReadyPage` 工具栏「生成」
   */
  const generate = useCallback(
    async (silent = false) => {
      const snapshot = formRef.current;
      const body = collectBody(snapshot, silent, notify);
      if (!body) return;
      const formatKey = `${snapshot.format}/${snapshot.variant}`;
      try {
        const data = await request<ReadyTomlResult>(apiPath('/ready/toml'), {
          method: 'POST',
          body: JSON.stringify(body),
        });
        const current = formRef.current;
        if (`${current.format}/${current.variant}` !== formatKey) return;
        const files = data.files ?? {};
        const names = Object.keys(files);
        const namesChanged = names.join('\n') !== current.fileNames.join('\n');
        const active = namesChanged
          ? (names[0] ?? null)
          : current.active && names.includes(current.active)
            ? current.active
            : (names[0] ?? null);
        setForm({
          fileNames: names,
          files: { ...files },
          active,
          summary: formatSummary(data),
        });
        if (!silent) notify('已生成');
      } catch (error) {
        setForm({ summary: messageOf(error) });
        if (!silent) notify(messageOf(error));
      }
    },
    [notify, setForm],
  );

  useEffect(() => {
    const silent = started.current;
    let cancelled = false;
    (async () => {
      const ok = await loadDirs(silent);
      if (cancelled || !ok) return;
      await generate(silent);
      started.current = true;
    })();
    return () => {
      cancelled = true;
    };
  }, [dirsKey, generate, loadDirs]);

  /**
   * 输入变化后延迟 300ms 静默重新生成配置。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 的目标图片数、训练步数、正则化开关与分辨率输入。
   */
  const autoGenerate = useCallback(() => {
    window.clearTimeout(autoTimer.current);
    autoTimer.current = window.setTimeout(() => {
      void generate(true);
    }, 300);
  }, [generate]);

  useEffect(() => () => window.clearTimeout(autoTimer.current), []);

  /**
   * 将顶栏与生成参数写入设置（`ready` 键）。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 的格式、模式、目标图片数、训练步数与正则化开关变更。
   */
  const persistReady = useCallback(() => {
    const current = formRef.current;
    saveSettings({
      ready: {
        format: current.format,
        variant: current.variant,
        mode: current.mode,
        target_images: current.target,
        train_steps: current.steps,
        use_reg: current.useReg,
      },
    });
  }, [saveSettings]);

  /**
   * 切换配置格式与变体，清空文件区并重新生成。
   *
   * Args:
   *   option (FormatOption): 目标格式与变体。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 顶栏格式切换段。
   */
  const changeFormat = (option: FormatOption) => {
    setForm({
      format: option.format,
      variant: option.variant,
      fileNames: [],
      files: {},
      active: null,
    });
    persistReady();
    void generate(true);
  };

  /**
   * 切换训练模式并重新生成。
   *
   * Args:
   *   mode ('style' | 'character'): 目标模式。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 顶栏模式切换段。
   */
  const changeMode = (mode: 'style' | 'character') => {
    if (mode === formRef.current.mode) return;
    setForm({ mode });
    persistReady();
    void generate(true);
  };

  /**
   * 更新目标图片数输入框文本。
   *
   * Args:
   *   value (string): 输入框文本。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 目标图片数输入框。
   */
  const changeTarget = (value: string) => {
    setForm({ target: value });
  };

  /**
   * 在目标图片数输入框失焦时写入设置并触发静默生成。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 目标图片数输入框失焦。
   */
  const commitTarget = () => {
    persistReady();
    autoGenerate();
  };

  /**
   * 更新训练步数输入框文本。
   *
   * Args:
   *   value (string): 输入框文本。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 训练步数输入框。
   */
  const changeSteps = (value: string) => {
    setForm({ steps: value });
  };

  /**
   * 在训练步数输入框失焦时写入设置并触发静默生成。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 训练步数输入框失焦。
   */
  const commitSteps = () => {
    persistReady();
    autoGenerate();
  };

  /**
   * 切换正则化开关，持久化并静默重新生成。
   *
   * Args:
   *   value (boolean): 是否启用。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 顶栏「正则化」勾选框。
   */
  const changeUseReg = (value: boolean) => {
    setForm({ useReg: value });
    persistReady();
    autoGenerate();
  };

  /**
   * 切换工作目录的导出启用状态并立即重新生成。
   *
   * Args:
   *   name (string): 目录名。
   *   value (boolean): 是否启用。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 侧栏工作目录勾选框。
   */
  const toggleMainDir = (name: string, value: boolean) => {
    setForm({ enabled: { ...formRef.current.enabled, [name]: value } });
    void generate(true);
  };

  /**
   * 更新工作目录的分辨率输入并触发静默生成。
   *
   * Args:
   *   name (string): 目录名。
   *   value (string): 输入框文本。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 侧栏工作目录分辨率输入框。
   */
  const changeMainResolution = (name: string, value: string) => {
    setForm({ resolutions: { ...formRef.current.resolutions, [name]: value } });
    autoGenerate();
  };

  /**
   * 切换正则化目录的导出启用状态并立即重新生成。
   *
   * Args:
   *   name (string): 目录名。
   *   value (boolean): 是否启用。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 侧栏正则化目录勾选框。
   */
  const toggleRegDir = (name: string, value: boolean) => {
    setForm({ regEnabled: { ...formRef.current.regEnabled, [name]: value } });
    void generate(true);
  };

  /**
   * 更新正则化目录的分辨率输入并触发静默生成。
   *
   * Args:
   *   name (string): 目录名。
   *   value (string): 输入框文本。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ReadyPage` 侧栏正则化目录分辨率输入框。
   */
  const changeRegResolution = (name: string, value: string) => {
    setForm({ regResolutions: { ...formRef.current.regResolutions, [name]: value } });
    autoGenerate();
  };

  /**
   * 复制当前标签页的配置文本；剪贴板接口不可用时改用 execCommand。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 复制流程完成。
   *
   * Callers:
   *   - `ReadyPage` 工具栏「复制」。
   */
  const copyActive = async () => {
    const current = formRef.current;
    const text = current.active ? (current.files[current.active] ?? '') : '';
    if (!text) {
      notify('无可复制内容');
      return;
    }
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        notify(`已复制 ${current.active}`);
        return;
      } catch {
        // 非安全上下文下剪贴板接口不可用，转入 execCommand 回退。
      }
    }
    const box = document.querySelector<HTMLTextAreaElement>(
      `textarea[data-file="${CSS.escape(current.active ?? '')}"]`,
    );
    if (box) {
      box.focus();
      box.select();
      box.setSelectionRange(0, box.value.length);
      if (document.execCommand('copy')) {
        notify(`已复制 ${current.active}`);
        return;
      }
    }
    notify('剪贴板不可用，已选中文本，请按 Ctrl+C');
  };

  /**
   * 按当前参数与编辑后的文本导出整套训练配置。
   *
   * Args:
   *   overwrite (boolean): 目标目录已存在配置时是否覆盖写入。
   *
   * Returns:
   *   Promise<void>: 导出完成。
   *
   * Callers:
   *   - `ReadyPage.exportConfig`
   */
  const doExport = async (overwrite: boolean) => {
    const current = formRef.current;
    const body = collectBody(current, false, notify);
    if (!body) return;
    const files: Record<string, string> = {};
    for (const name of current.fileNames) {
      const text = current.files[name] ?? '';
      if (!text.trim()) {
        notify(`${name} 内容为空, 请先生成`);
        return;
      }
      files[name] = text;
    }
    const url = apiPath('/ready/export') + (overwrite ? '?overwrite=1' : '');
    const data = await request<TrainExportResult>(url, {
      method: 'POST',
      body: JSON.stringify({ ...body, files }),
    });
    notify(`已导出: ${data.files.join(', ')}`);
  };

  /**
   * 导出整套训练配置；目标目录已存在配置时先确认再覆盖。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 导出完成。
   *
   * Callers:
   *   - `ReadyPage` 工具栏「导出配置」。
   */
  const exportConfig = async () => {
    try {
      await doExport(false);
    } catch (error) {
      if (Number.parseInt(messageOf(error), 10) === 409) {
        if (window.confirm('目标目录已存在配置, 是否覆盖写入?')) {
          await doExport(true);
          return;
        }
      }
      throw error;
    }
  };

  const maskByName: Record<string, MaskStats> = {};
  for (const item of form.maskStats) maskByName[item.name] = item;

  useStatus([], form.summary);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar>
        <span
          className="flex h-7 items-stretch overflow-hidden rounded border border-border bg-[#0a1a30]"
          title="配置导出格式与变体: sd-scripts 写入 SD_SCRIPTS_CONFIG_ROOT, diffusion-pipe 写入 DIFFUSION_PIPE_CONFIG_ROOT, 切换后重新生成"
        >
          {FORMATS.map((option) => {
            const active = option.format === form.format && option.variant === form.variant;
            return (
              <button
                key={`${option.format}/${option.variant}`}
                type="button"
                className={
                  active
                    ? 'bg-accent px-2 text-xs text-foreground'
                    : 'px-2 text-xs text-muted-foreground hover:text-foreground'
                }
                title={option.title}
                onClick={() => changeFormat(option)}
              >
                {option.label}
              </button>
            );
          })}
        </span>
        <ToolbarDivider />
        <span
          className="flex h-7 items-stretch overflow-hidden rounded border border-border bg-[#0a1a30]"
          title="训练模式: 画风 / 角色, 各模式对应的超参由所选格式的模板决定, 切换后重新生成"
        >
          {MODES.map((option) => (
            <button
              key={option.mode}
              type="button"
              className={
                form.mode === option.mode
                  ? 'bg-accent px-2 text-xs text-foreground'
                  : 'px-2 text-xs text-muted-foreground hover:text-foreground'
              }
              title={option.title}
              onClick={() => changeMode(option.mode)}
            >
              {option.label}
            </button>
          ))}
        </span>
        <ToolbarDivider />
        <Button variant="primary" onClick={() => void generate(false)}>
          生成
        </Button>
        <Button onClick={() => void copyActive()}>复制</Button>
        <Button onClick={() => void exportConfig().catch((error) => notify(messageOf(error)))}>
          导出配置
        </Button>
        <ToolbarDivider />
        <label
          className="flex items-center gap-1.5 text-xs text-muted-foreground"
          title="目标图片数: 系统据此计算 num_repeats = ceil(目标 / 实际图片数), 使重复后图片数 >= 目标"
        >
          目标图片数
          <Input
            className="w-[72px]"
            type="number"
            min={1}
            step={1}
            value={form.target}
            onChange={(event) => changeTarget(event.target.value)}
            onBlur={commitTarget}
          />
        </label>
        <ToolbarDivider />
        <label
          className="flex items-center gap-1.5 text-xs text-muted-foreground"
          title="训练步数: 预热与保存间隔由所选格式的模板按步数比例推导(默认各 0.1x)"
        >
          训练步数
          <Input
            className="w-[72px]"
            type="number"
            min={1}
            step={1}
            value={form.steps}
            onChange={(event) => changeSteps(event.target.value)}
            onBlur={commitSteps}
          />
        </label>
        <ToolbarDivider />
        <label
          className="flex items-center gap-1.5 text-xs text-muted-foreground"
          title="启用时 dataset.toml 附带所选 regN 正则化数据块: reg 图以类提示词(不含触发词)参与损失, 即先验保持; diffusion-pipe 下等价于先验保持权重 1.0(无独立 reg loss 权重旋钮)"
        >
          <Checkbox checked={form.useReg} onCheckedChange={changeUseReg} />
          正则化
        </label>
      </Toolbar>

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[280px] shrink-0 flex-col overflow-hidden border-r border-border bg-[#16213e]">
          <h3 className="px-3.5 pb-1.5 pt-3 text-[13px] font-semibold text-muted-foreground">
            工作目录 <span className="text-[11px] opacity-70">img | text | json | mask</span>
          </h3>
          <div className="flex-1 overflow-y-auto px-3.5 pb-3">
            {form.dirs.length === 0 ? (
              <div className="px-0.5 py-2.5 text-center text-xs text-muted-foreground">
                无含图目录
              </div>
            ) : (
              form.dirs.map((dir) => {
                const stats = maskByName[dir.name];
                return (
                  <div key={dir.name} className="border-b border-border py-2 first:pt-0.5">
                    <div className="flex items-center gap-1.5">
                      <Checkbox
                        checked={!!form.enabled[dir.name]}
                        onCheckedChange={(value) => toggleMainDir(dir.name, value)}
                      />
                      <span className="min-w-0 flex-1 truncate" title={dir.name}>
                        {dir.name}
                      </span>
                      <span
                        className="whitespace-nowrap text-[11px] text-muted-foreground"
                        title="图片 | 标注(.txt) | 描述(captions.json) | 蒙版 (后三项仅统计有对应图片的)"
                      >
                        {stats ? (
                          <>
                            <span
                              className={
                                stats.ready
                                  ? 'mr-1 inline-block h-2 w-2 rounded-full bg-[#6bcf8e] align-[1px]'
                                  : 'mr-1 inline-block h-2 w-2 rounded-full bg-[#e9c46a] align-[1px]'
                              }
                              title={
                                stats.ready
                                  ? '就绪: 标注齐全, 蒙版齐全或全无'
                                  : '未就绪: 标注未齐全或蒙版部分存在'
                              }
                            />
                            {`${dir.image_count ?? 0} | ${stats.text_count || 0} | ${
                              stats.caption_count || 0
                            } | ${stats.mask_complete}`}
                          </>
                        ) : (
                          `${dir.image_count ?? 0} | 0 | 0 | 0`
                        )}
                      </span>
                    </div>
                    <Input
                      className="mt-1.5 w-full"
                      type="text"
                      title="逗号分隔的多个分辨率, 每个分辨率按子集是否携带蒙版目录拆分为蒙版块与普通块; 留空则该子集生成单块无 resolution"
                      value={form.resolutions[dir.name] ?? ''}
                      onChange={(event) => changeMainResolution(dir.name, event.target.value)}
                    />
                  </div>
                );
              })
            )}
          </div>
          <div
            className={
              form.useReg
                ? 'max-h-[45%] shrink-0 overflow-y-auto border-t border-border'
                : 'hidden'
            }
          >
            <h3 className="px-3.5 pb-1.5 pt-3 text-[13px] font-semibold text-muted-foreground">
              正则化数据 <span className="text-[11px] opacity-70">img</span>
            </h3>
            <div className="px-3.5 pb-3">
              {form.regDirs.length === 0 ? (
                <div className="px-0.5 py-2.5 text-center text-xs text-muted-foreground">
                  未配置 REG_ROOT 或无含图目录
                </div>
              ) : (
                form.regDirs.map((dir) => (
                  <div key={dir.name} className="border-b border-border py-2 first:pt-0.5">
                    <div className="flex items-center gap-1.5">
                      <Checkbox
                        checked={!!form.regEnabled[dir.name]}
                        onCheckedChange={(value) => toggleRegDir(dir.name, value)}
                      />
                      <span className="min-w-0 flex-1 truncate" title={dir.name}>
                        {dir.name}
                      </span>
                      <span className="whitespace-nowrap text-[11px] text-muted-foreground">
                        {dir.image_count || 0}
                      </span>
                    </div>
                    <Input
                      className="mt-1.5 w-full"
                      type="text"
                      title="该正则化目录的分辨率, 逗号分隔; 留空则该目录不带 resolution"
                      value={form.regResolutions[dir.name] ?? ''}
                      onChange={(event) => changeRegResolution(dir.name, event.target.value)}
                    />
                  </div>
                ))
              )}
            </div>
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex gap-1 px-3.5 pt-2.5">
            {form.fileNames.map((name) => {
              const active = name === form.active;
              return (
                <button
                  key={name}
                  type="button"
                  className={
                    active
                      ? 'rounded-t-md border border-b-0 border-border bg-[#0a1a30] px-3.5 py-1 font-mono text-xs text-foreground'
                      : 'rounded-t-md border border-border bg-transparent px-3.5 py-1 font-mono text-xs text-muted-foreground hover:text-foreground'
                  }
                  onClick={() => setForm({ active: name })}
                >
                  {name}
                </button>
              );
            })}
          </div>
          <div className="flex min-h-0 flex-1 flex-col">
            {form.fileNames.map((name) => (
              <textarea
                key={name}
                data-file={name}
                className={
                  (name === form.active ? '' : 'hidden') +
                  ' mx-3.5 mb-3.5 min-h-0 flex-1 resize-none rounded-b-lg rounded-tr-lg border border-border bg-[#0a1a30] px-3.5 py-3 font-mono text-[13px] leading-relaxed text-[#c8d4e0] outline-none focus:border-[#4a6a9a]'
                }
                spellCheck={false}
                placeholder="生成后显示"
                value={form.files[name] ?? ''}
                onChange={(event) =>
                  setForm({
                    files: { ...formRef.current.files, [name]: event.target.value },
                  })
                }
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
