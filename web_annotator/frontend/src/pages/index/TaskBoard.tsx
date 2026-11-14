import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { api } from '@/lib/api';
import type { BatchSnapshot } from '@/lib/types';
import {
  addTask,
  archiveTask,
  errorMessage,
  findActiveArchiveJob,
  listCandidates,
  listTasks,
  reorderTasks,
  restoreTask,
  setTaskCompleted,
  setTaskNote,
  setTaskTrigger,
  type JobSnapshot,
  type SortField,
  type SortOrder,
  type TaskItem,
} from './homeApi';
import { importLegacySettings, legacyKeys } from './legacySettings';
import { TaskCard, type TaskProgress } from './TaskCard';

/** 排序按钮定义。 */
const SORT_OPTIONS: Array<{ field: SortField; label: string; order: SortOrder }> = [
  { field: 'sort_order', label: '手动', order: 'asc' },
  { field: 'name', label: '名称', order: 'asc' },
  { field: 'image_count', label: '图像数', order: 'desc' },
  { field: 'created', label: '创建时间', order: 'desc' },
];

/**
 * 按前缀从候选目录中筛选补全项。
 *
 * Args:
 *   candidates (string[]): 全部候选目录名。
 *   query (string): 输入框当前文本。
 *
 * Returns:
 *   string[]: 最多 12 个候选；空查询返回前 12 个。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:TaskBoard`
 */
function filterCandidates(candidates: string[], query: string): string[] {
  const needle = query.toLowerCase();
  if (!needle) return candidates.slice(0, 12);
  return candidates
    .filter((name) => name.toLowerCase().startsWith(needle))
    .slice(0, 12);
}

/**
 * 生成排序按钮文案，激活的非手动排序附带方向箭头。
 *
 * Args:
 *   option (object): 排序项定义。
 *   sortBy (SortField): 当前排序字段。
 *   sortOrder (SortOrder): 当前排序方向。
 *
 * Returns:
 *   string: 按钮显示文本。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx:TaskBoard`
 */
function sortButtonLabel(
  option: { field: SortField; label: string },
  sortBy: SortField,
  sortOrder: SortOrder,
): string {
  if (option.field !== sortBy || option.field === 'sort_order') return option.label;
  return `${option.label} ${sortOrder === 'asc' ? '↑' : '↓'}`;
}

/**
 * 任务列表区：任务卡片、添加与补全、排序、拖拽重排、归档恢复、旧设置导入。
 *
 * Args:
 *   props (object): 含全局提示函数 `notify`。
 *
 * Returns:
 *   JSX.Element: 任务列表视图。
 *
 * Callers:
 *   - `pages/index/IndexPage.tsx:IndexPage`
 */
export function TaskBoard({ notify }: { notify: (message: string) => void }) {
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [candidates, setCandidates] = useState<string[]>([]);
  const [sortBy, setSortBy] = useState<SortField>('sort_order');
  const [sortOrder, setSortOrder] = useState<SortOrder>('asc');
  const [name, setName] = useState('');
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [suggestIndex, setSuggestIndex] = useState(-1);
  const [draggingName, setDraggingName] = useState<string | null>(null);
  const [dragOverName, setDragOverName] = useState<string | null>(null);
  const [progress, setProgress] = useState<Record<string, TaskProgress>>({});
  const [loaded, setLoaded] = useState(false);
  const [hasLegacy, setHasLegacy] = useState(false);
  const [legacyBusy, setLegacyBusy] = useState(false);

  const inputRef = useRef<HTMLInputElement | null>(null);
  const blurTimer = useRef<number>();
  const loadSeq = useRef(0);
  const tasksRef = useRef<TaskItem[]>([]);
  const closers = useRef<Set<() => void>>(new Set());
  const reconnected = useRef(false);

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);

  const activeTasks = tasks.filter((task) => !task.archived && !task.completed);
  const completedTasks = tasks.filter((task) => !task.archived && task.completed);
  const archivedTasks = tasks.filter((task) => task.archived);
  const suggestions = filterCandidates(candidates, name.trim());

  useEffect(() => {
    const tracked = closers.current;
    return () => {
      for (const close of tracked) close();
      tracked.clear();
      window.clearTimeout(blurTimer.current);
    };
  }, []);

  /**
   * 按当前排序参数重新拉取任务列表；乱序响应以请求序号丢弃。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 加载完成。
   *
   * Callers:
   *   - 排序变化副作用。
   *   - 添加、完成、重开、归档与恢复后的刷新。
   */
  const reloadTasks = useCallback(async () => {
    const seq = loadSeq.current + 1;
    loadSeq.current = seq;
    try {
      const data = await listTasks(sortBy, sortOrder);
      if (seq !== loadSeq.current) return;
      setTasks(data.tasks ?? []);
      setLoaded(true);
    } catch (error) {
      notify(errorMessage(error));
    }
  }, [notify, sortBy, sortOrder]);

  useEffect(() => {
    void reloadTasks();
  }, [reloadTasks]);

  /**
   * 刷新未登记候选目录，供添加任务输入框补全。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 加载完成。
   *
   * Callers:
   *   - 页面启动副作用。
   *   - 添加任务成功后。
   */
  const loadCandidates = useCallback(async () => {
    try {
      const data = await listCandidates();
      setCandidates(data.candidates ?? []);
    } catch (error) {
      notify(errorMessage(error));
    }
  }, [notify]);

  useEffect(() => {
    setHasLegacy(legacyKeys().length > 0);
    void loadCandidates();
  }, [loadCandidates]);

  /**
   * 订阅长任务 SSE 并登记关闭句柄，组件卸载时统一释放。
   *
   * Args:
   *   jobId (string): 长任务标识。
   *   onEvent (function): 快照回调。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/index/TaskBoard.tsx` 的归档、恢复与重连流程。
   */
  const trackJob = useCallback((jobId: string, onEvent: (snapshot: JobSnapshot) => void) => {
    const close = api.watchJob(jobId, (event: BatchSnapshot) => {
      onEvent(event as JobSnapshot);
    });
    closers.current.add(close);
  }, []);

  /**
   * 合并更新指定任务的进度叠层。
   *
   * Args:
   *   target (string): 任务名。
   *   patch (Partial<TaskProgress>): 待更新字段。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/index/TaskBoard.tsx` 的归档、恢复与重连流程。
   */
  const updateProgress = (target: string, patch: Partial<TaskProgress>) => {
    setProgress((current) =>
      current[target]
        ? { ...current, [target]: { ...current[target], ...patch } }
        : current,
    );
  };

  /**
   * 移除指定任务的进度叠层。
   *
   * Args:
   *   target (string): 任务名。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - 归档、恢复结束或失败时。
   */
  const clearProgress = (target: string) => {
    setProgress((current) => {
      if (!current[target]) return current;
      const next = { ...current };
      delete next[target];
      return next;
    });
  };

  /**
   * 提交任务归档并展示压缩进度，完成后刷新列表。
   *
   * Args:
   *   target (string): 任务名。
   *
   * Returns:
   *   Promise<void>: 归档流程完成。
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的「归档」按钮。
   */
  const handleArchive = async (target: string) => {
    setProgress((current) => ({
      ...current,
      [target]: { pct: null, message: '准备归档…' },
    }));
    try {
      const data = await archiveTask(target);
      trackJob(data.job_id, (snapshot) => {
        updateProgress(target, {
          ...(typeof snapshot.pct === 'number' ? { pct: snapshot.pct } : {}),
          message: snapshot.message || snapshot.phase || '',
        });
        if (snapshot.status === 'done') {
          notify(`已归档: ${target}`);
          clearProgress(target);
          void reloadTasks();
        } else if (snapshot.status === 'error') {
          notify(`归档失败: ${snapshot.message || ''}`);
          clearProgress(target);
        }
      });
    } catch (error) {
      notify(errorMessage(error));
      clearProgress(target);
    }
  };

  /**
   * 提交归档解压并展示恢复进度，完成后刷新列表。
   *
   * Args:
   *   task (TaskItem): 已归档任务。
   *
   * Returns:
   *   Promise<void>: 恢复流程完成。
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的「恢复」按钮。
   */
  const handleRestore = async (task: TaskItem) => {
    setProgress((current) => ({
      ...current,
      [task.name]: { pct: null, message: '准备恢复…' },
    }));
    try {
      const data = await restoreTask(task.archive_base);
      trackJob(data.job_id, (snapshot) => {
        updateProgress(task.name, { message: snapshot.message || snapshot.phase || '' });
        if (snapshot.status === 'done') {
          notify('恢复完成');
          clearProgress(task.name);
          void reloadTasks();
        } else if (snapshot.status === 'error') {
          notify(`恢复失败: ${snapshot.message || ''}`);
          clearProgress(task.name);
        }
      });
    } catch (error) {
      notify(errorMessage(error));
      clearProgress(task.name);
    }
  };

  /**
   * 页面加载后重连未结束的归档任务，在对应卡片上恢复进度显示。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 重连完成。
   *
   * Callers:
   *   - 任务列表首次加载完成后的副作用。
   */
  const reconnectArchive = useCallback(async () => {
    const snapshot = await findActiveArchiveJob();
    if (!snapshot?.job_id) return;
    const list = tasksRef.current;
    const target =
      list.find((task) => task.name === snapshot.dataset) ??
      list.find((task) => task.archived);
    if (!target) return;
    setProgress((current) => ({
      ...current,
      [target.name]: {
        pct: typeof snapshot.pct === 'number' ? snapshot.pct : null,
        message: snapshot.message || snapshot.phase || '恢复连接…',
      },
    }));
    trackJob(snapshot.job_id, (event) => {
      updateProgress(target.name, {
        ...(typeof event.pct === 'number' ? { pct: event.pct } : {}),
        message: event.message || event.phase || '',
      });
      if (event.status === 'done') {
        notify('归档完成');
        clearProgress(target.name);
        void reloadTasks();
      } else if (event.status === 'error') {
        notify(`归档失败: ${event.message || ''}`);
        clearProgress(target.name);
      }
    });
  }, [notify, reloadTasks, trackJob]);

  useEffect(() => {
    if (!loaded || reconnected.current) return;
    reconnected.current = true;
    reconnectArchive().catch((error: unknown) => notify(errorMessage(error)));
  }, [loaded, notify, reconnectArchive]);

  /**
   * 切换任务完成状态并刷新列表。
   *
   * Args:
   *   target (string): 任务名。
   *   completed (boolean): True 标记完成，False 重新打开。
   *
   * Returns:
   *   Promise<void>: 状态更新完成。
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的「完成」与「重新打开」按钮。
   */
  const changeCompleted = async (target: string, completed: boolean) => {
    try {
      await setTaskCompleted(target, completed);
      await reloadTasks();
    } catch (error) {
      notify(errorMessage(error));
    }
  };

  /**
   * 保存任务备注，成功后以服务端返回值更新卡片。
   *
   * Args:
   *   target (string): 任务名。
   *   note (string): 新备注。
   *
   * Returns:
   *   Promise<void>: 保存完成；失败时向调用方抛出。
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的备注内联编辑。
   */
  const saveNote = async (target: string, note: string) => {
    const data = await setTaskNote(target, note);
    setTasks((current) =>
      current.map((task) => (task.name === target ? { ...task, note: data.note } : task)),
    );
  };

  /**
   * 保存任务触发词，成功后以服务端返回值更新卡片。
   *
   * Args:
   *   target (string): 任务名。
   *   trigger (string): 新触发词。
   *
   * Returns:
   *   Promise<void>: 保存完成；失败时向调用方抛出。
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的触发词内联编辑。
   */
  const saveTrigger = async (target: string, trigger: string) => {
    const data = await setTaskTrigger(target, trigger);
    setTasks((current) =>
      current.map((task) =>
        task.name === target ? { ...task, trigger_word: data.trigger_word } : task,
      ),
    );
  };

  /**
   * 写入剪贴板并提示结果。
   *
   * Args:
   *   text (string): 待复制文本。
   *   success (string): 成功提示文案。
   *
   * Returns:
   *   Promise<void>: 复制流程完成。
   *
   * Callers:
   *   - `pages/index/TaskBoard.tsx:copyLink`
   *   - `pages/index/TaskBoard.tsx:copyTrigger`
   */
  const copyText = async (text: string, success: string) => {
    try {
      await navigator.clipboard.writeText(text);
      notify(success);
    } catch (error) {
      notify(errorMessage(error));
    }
  };

  /**
   * 复制任务处理页绝对链接。
   *
   * Args:
   *   target (string): 任务名。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的「复制链接」按钮。
   */
  const copyLink = (target: string) => {
    const href = `/d/${encodeURIComponent(target)}/filter`;
    void copyText(`${window.location.origin}${href}`, '已复制');
  };

  /**
   * 复制触发词；为空时提示无可复制内容。
   *
   * Args:
   *   trigger (string): 触发词文本。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的「复制」按钮。
   */
  const copyTrigger = (trigger: string) => {
    if (!trigger) {
      notify('触发词为空');
      return;
    }
    void copyText(trigger, '已复制触发词');
  };

  /**
   * 提交添加任务表单，成功后清空输入并刷新任务与候选。
   *
   * Args:
   *   event (FormEvent<HTMLFormElement>): 表单提交事件。
   *
   * Returns:
   *   Promise<void>: 添加流程完成。
   *
   * Callers:
   *   - 添加任务表单的 onSubmit。
   */
  const submitTask = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSuggestOpen(false);
    const value = name.trim();
    try {
      await addTask(value);
      setName('');
      notify(`已添加 ${value}`);
      await Promise.all([reloadTasks(), loadCandidates()]);
    } catch (error) {
      notify(errorMessage(error));
    }
  };

  /**
   * 切换排序；再次点击当前字段时反转方向，手动排序不反转。
   *
   * Args:
   *   field (SortField): 目标排序字段。
   *   defaultOrder (SortOrder): 首次选中的默认方向。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - 排序按钮的 onClick。
   */
  const changeSort = (field: SortField, defaultOrder: SortOrder) => {
    if (sortBy === field) {
      if (field === 'sort_order') return;
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc');
      return;
    }
    setSortBy(field);
    setSortOrder(defaultOrder);
  };

  /**
   * 选中补全项并回填输入框。
   *
   * Args:
   *   value (string): 候选目录名。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - 补全列表项的 mousedown。
   *   - 输入框的 Enter 处理。
   */
  const pickSuggestion = (value: string) => {
    setName(value);
    setSuggestOpen(false);
    inputRef.current?.focus();
  };

  /**
   * 处理输入框键盘事件：上下选择补全项，Enter 确认，Esc 关闭。
   *
   * Args:
   *   event (ReactKeyboardEvent<HTMLInputElement>): 键盘事件。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - 任务名输入框的 onKeyDown。
   */
  const handleInputKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (!suggestOpen || !suggestions.length) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setSuggestIndex((suggestIndex + 1) % suggestions.length);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setSuggestIndex(suggestIndex <= 0 ? suggestions.length - 1 : suggestIndex - 1);
    } else if (event.key === 'Enter' && suggestIndex >= 0) {
      const picked = suggestions[suggestIndex];
      if (picked) {
        event.preventDefault();
        pickSuggestion(picked);
      }
    } else if (event.key === 'Escape') {
      setSuggestOpen(false);
    }
  };

  /**
   * 关闭补全下拉，延迟执行以保留列表项 mousedown。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - 任务名输入框的 onBlur。
   */
  const closeSuggestLater = () => {
    window.clearTimeout(blurTimer.current);
    blurTimer.current = window.setTimeout(() => setSuggestOpen(false), 120);
  };

  /**
   * 开始拖拽排序，记录源任务并写入拖拽数据。
   *
   * Args:
   *   target (string): 源任务名。
   *   event (DragEvent<HTMLDivElement>): 拖拽开始事件。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的 onDragStart。
   */
  const handleDragStart = (target: string, event: DragEvent<HTMLDivElement>) => {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', target);
    setDraggingName(target);
  };

  /**
   * 标记拖拽经过的任务卡片。
   *
   * Args:
   *   target (string): 经过的任务名。
   *   event (DragEvent<HTMLDivElement>): 拖拽经过事件。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的 onDragOver。
   */
  const handleDragOver = (target: string, event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    if (target === draggingName) return;
    setDragOverName(target);
  };

  /**
   * 清除拖拽经过标记。
   *
   * Args:
   *   target (string): 离开的任务名。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的 onDragLeave。
   */
  const handleDragLeave = (target: string) => {
    setDragOverName((current) => (current === target ? null : current));
  };

  /**
   * 结束拖拽，清除源与目标标记。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的 onDragEnd。
   */
  const handleDragEnd = () => {
    setDraggingName(null);
    setDragOverName(null);
  };

  /**
   * 放下卡片后按新顺序提交手动排序并刷新列表。
   *
   * Args:
   *   target (string): 目标任务名。
   *   event (DragEvent<HTMLDivElement>): 放下事件。
   *
   * Returns:
   *   Promise<void>: 重排流程完成。
   *
   * Callers:
   *   - `pages/index/TaskCard.tsx:TaskCard` 的 onDrop。
   */
  const handleDrop = async (target: string, event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragOverName(null);
    const source = draggingName;
    if (!source || source === target) return;
    const ordered = activeTasks.map((task) => task.name);
    const sourceIndex = ordered.indexOf(source);
    const targetIndex = ordered.indexOf(target);
    if (sourceIndex < 0 || targetIndex < 0) return;
    ordered.splice(sourceIndex, 1);
    ordered.splice(targetIndex, 0, source);
    try {
      await reorderTasks(ordered);
      await reloadTasks();
    } catch (error) {
      notify(errorMessage(error));
    }
  };

  /**
   * 导入 localStorage 中的旧设置，成功后隐藏入口。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 导入流程完成。
   *
   * Callers:
   *   - 「导入旧设置」按钮的 onClick。
   */
  const importLegacy = async () => {
    setLegacyBusy(true);
    try {
      const imported = await importLegacySettings();
      setHasLegacy(false);
      notify(imported > 0 ? `已导入 ${imported} 个任务的旧设置` : '旧设置已导入');
    } catch (error) {
      notify(`导入失败: ${errorMessage(error)}`);
      setLegacyBusy(false);
    }
  };

  return (
    <>
      <h1>任务</h1>
      <p className="home-lead">
        手动添加任务后进入处理。未登记的子目录不会出现；输入框可补全尚未登记的目录名。
      </p>

      <form className="home-task-add" onSubmit={submitTask} autoComplete="off">
        <div className="home-task-input-wrap">
          <input
            ref={inputRef}
            className="home-task-input"
            type="text"
            placeholder="任务目录名"
            required
            pattern={'(?!\\.)(?:[^\\\\/:*?"<>|\\x00-\\x1f\\x7f]){1,255}'}
            title={'不得以点开头, 不得包含 \\ / : * ? " < > | 等特殊符号'}
            aria-autocomplete="list"
            aria-controls="home-task-suggest"
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setSuggestIndex(-1);
              setSuggestOpen(true);
            }}
            onFocus={() => setSuggestOpen(true)}
            onBlur={closeSuggestLater}
            onKeyDown={handleInputKeyDown}
          />
          <div
            id="home-task-suggest"
            className={suggestOpen && suggestions.length ? 'home-task-suggest open' : 'home-task-suggest'}
            role="listbox"
          >
            {suggestions.map((item, index) => (
              <button
                key={item}
                type="button"
                className={index === suggestIndex ? 'active' : undefined}
                onMouseDown={(event) => {
                  event.preventDefault();
                  pickSuggestion(item);
                }}
              >
                {item}
              </button>
            ))}
          </div>
        </div>
        <button type="submit" className="home-btn green">
          添加任务
        </button>
        <button
          type="button"
          className="home-btn"
          hidden={!hasLegacy}
          disabled={legacyBusy}
          onClick={() => void importLegacy()}
        >
          导入旧设置
        </button>
      </form>

      <div className="home-sort-bar">
        <span className="home-sort-label">排序：</span>
        <div className="home-sort-group">
          {SORT_OPTIONS.map((option) => (
            <button
              key={option.field}
              type="button"
              className={option.field === sortBy ? 'home-sort-btn active' : 'home-sort-btn'}
              onClick={() => changeSort(option.field, option.order)}
            >
              {sortButtonLabel(option, sortBy, sortOrder)}
            </button>
          ))}
        </div>
      </div>

      <div className="home-task-grid">
        {activeTasks.map((task) => (
          <TaskCard
            key={task.name}
            task={task}
            completed={false}
            sortable={sortBy === 'sort_order'}
            progress={progress[task.name] ?? null}
            dragging={draggingName === task.name}
            dragOver={dragOverName === task.name}
            notify={notify}
            onComplete={(target) => void changeCompleted(target, true)}
            onReopen={(target) => void changeCompleted(target, false)}
            onArchive={(target) => void handleArchive(target)}
            onRestore={(item) => void handleRestore(item)}
            onSaveNote={saveNote}
            onSaveTrigger={saveTrigger}
            onCopyLink={copyLink}
            onCopyTrigger={copyTrigger}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={(target, event) => void handleDrop(target, event)}
          />
        ))}
      </div>
      <p className="home-empty" hidden={activeTasks.length > 0}>
        尚未添加任务
      </p>

      <details className="home-completed-block" open hidden={completedTasks.length === 0}>
        <summary>已完成（{completedTasks.length}）</summary>
        <div className="home-task-grid">
          {completedTasks.map((task) => (
            <TaskCard
              key={task.name}
              task={task}
              completed
              sortable={false}
              progress={progress[task.name] ?? null}
              dragging={false}
              dragOver={false}
              notify={notify}
              onComplete={(target) => void changeCompleted(target, true)}
              onReopen={(target) => void changeCompleted(target, false)}
              onArchive={(target) => void handleArchive(target)}
              onRestore={(item) => void handleRestore(item)}
              onSaveNote={saveNote}
              onSaveTrigger={saveTrigger}
              onCopyLink={copyLink}
              onCopyTrigger={copyTrigger}
              onDragStart={handleDragStart}
              onDragEnd={handleDragEnd}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={(target, event) => void handleDrop(target, event)}
            />
          ))}
        </div>
      </details>

      <details className="home-completed-block" open hidden={archivedTasks.length === 0}>
        <summary>已归档（{archivedTasks.length}）</summary>
        <div className="home-task-grid">
          {archivedTasks.map((task) => (
            <TaskCard
              key={task.name}
              task={task}
              completed
              sortable={false}
              progress={progress[task.name] ?? null}
              dragging={false}
              dragOver={false}
              notify={notify}
              onComplete={(target) => void changeCompleted(target, true)}
              onReopen={(target) => void changeCompleted(target, false)}
              onArchive={(target) => void handleArchive(target)}
              onRestore={(item) => void handleRestore(item)}
              onSaveNote={saveNote}
              onSaveTrigger={saveTrigger}
              onCopyLink={copyLink}
              onCopyTrigger={copyTrigger}
              onDragStart={handleDragStart}
              onDragEnd={handleDragEnd}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={(target, event) => void handleDrop(target, event)}
            />
          ))}
        </div>
      </details>
    </>
  );
}
