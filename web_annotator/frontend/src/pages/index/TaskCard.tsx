import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from 'react';
import { errorMessage, type TaskItem } from './homeApi';

/** 任务卡片上的长任务进度。 */
export interface TaskProgress {
  pct: number | null;
  message: string;
}

/** 内联编辑组件属性。 */
interface InlineEditProps {
  value: string;
  placeholder: string;
  className: string;
  editable: boolean;
  activateOn: 'click' | 'dblclick';
  onSave: (next: string) => Promise<void>;
  notify: (message: string) => void;
}

/** 任务卡片属性。 */
interface TaskCardProps {
  task: TaskItem;
  completed: boolean;
  sortable: boolean;
  progress: TaskProgress | null;
  dragging: boolean;
  dragOver: boolean;
  notify: (message: string) => void;
  onComplete: (name: string) => void;
  onReopen: (name: string) => void;
  onArchive: (name: string) => void;
  onRestore: (task: TaskItem) => void;
  onSaveNote: (name: string, note: string) => Promise<void>;
  onSaveTrigger: (name: string, trigger: string) => Promise<void>;
  onCopyLink: (name: string) => void;
  onCopyTrigger: (trigger: string) => void;
  onDragStart: (name: string, event: DragEvent<HTMLDivElement>) => void;
  onDragEnd: () => void;
  onDragOver: (name: string, event: DragEvent<HTMLDivElement>) => void;
  onDragLeave: (name: string) => void;
  onDrop: (name: string, event: DragEvent<HTMLDivElement>) => void;
}

/**
 * 内联编辑长文本：展示态切换为 textarea，失焦或回车保存，Esc 取消。
 *
 * 备注与触发词共用；textarea 高度随内容自动扩展，保存失败时提示并退出编辑。
 *
 * Args:
 *   props (InlineEditProps): 当前值、占位文案、展示类名、触发方式与保存回调。
 *
 * Returns:
 *   JSX.Element: 展示态文本或编辑态 textarea。
 *
 * Callers:
 *   - `pages/index/TaskCard.tsx:TaskCard` 的备注与触发词。
 */
function InlineEdit({
  value,
  placeholder,
  className,
  editable,
  activateOn,
  onSave,
  notify,
}: InlineEditProps) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(value);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const cancelRef = useRef(false);

  useLayoutEffect(() => {
    if (!editing) return;
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${textarea.scrollHeight}px`;
  }, [editing, text]);

  useEffect(() => {
    if (!editing) return;
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  }, [editing]);

  /**
   * 进入编辑态并以当前值初始化文本框。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - 展示态文本的点击或双击处理。
   */
  const begin = () => {
    setText(value);
    cancelRef.current = false;
    setEditing(true);
  };

  /**
   * 退出编辑态；Esc 取消时丢弃本次输入。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - textarea 失焦、回车与 Esc 处理。
   */
  const commit = () => {
    if (cancelRef.current) {
      cancelRef.current = false;
      setEditing(false);
      return;
    }
    const next = text.trim();
    if (next === value) {
      setEditing(false);
      return;
    }
    onSave(next)
      .then(() => setEditing(false))
      .catch((error: unknown) => {
        notify(errorMessage(error));
        setEditing(false);
      });
  };

  /**
   * 处理编辑态键盘：回车保存，Shift+回车换行，Esc 取消。
   *
   * Args:
   *   event (ReactKeyboardEvent<HTMLTextAreaElement>): 键盘事件。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - 编辑态 textarea 的 onKeyDown。
   */
  const handleKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.blur();
    } else if (event.key === 'Escape') {
      cancelRef.current = true;
      event.currentTarget.blur();
    }
  };

  if (editable && editing) {
    return (
      <textarea
        ref={textareaRef}
        className="home-inline-input"
        value={text}
        onChange={(event) => setText(event.target.value)}
        onBlur={commit}
        onKeyDown={handleKeyDown}
      />
    );
  }

  const displayClass = `${className}${value ? '' : ' empty'}`;
  const display = value || placeholder;
  if (!editable) return <div className={displayClass}>{display}</div>;
  if (activateOn === 'dblclick') {
    return (
      <div className={displayClass} onDoubleClick={begin}>
        {display}
      </div>
    );
  }
  return (
    <div className={displayClass} onClick={begin}>
      {display}
    </div>
  );
}

/**
 * 生成任务卡片的元信息文本。
 *
 * Args:
 *   task (TaskItem): 任务数据。
 *
 * Returns:
 *   string: 图像数与归档时间或美学报告标记。
 *
 * Callers:
 *   - `pages/index/TaskCard.tsx:TaskCard`
 */
function metaText(task: TaskItem): string {
  if (task.archived) {
    return `图像 ${task.image_count || 0} · 归档 ${new Date((task.archived_at || 0) * 1000).toLocaleString()}`;
  }
  return `图像 ${task.image_count || 0}${task.report_ready ? ' · 有美学报告' : ''}`;
}

/**
 * 构造任务缩略图地址。
 *
 * Args:
 *   task (TaskItem): 任务数据。
 *   hash (string): 缩略图哈希。
 *
 * Returns:
 *   string: 归档预览或实时缩略图 URL。
 *
 * Callers:
 *   - `pages/index/TaskCard.tsx:TaskCard`
 */
function previewUrl(task: TaskItem, hash: string): string {
  if (task.archived) {
    const base = task.archive_base || task.name;
    return `/api/archived/${encodeURIComponent(base)}/preview/${hash}.avif`;
  }
  return `/d/${encodeURIComponent(task.name)}/thumb/${hash}.avif`;
}

/**
 * 渲染一张任务卡片：预览图、名称、统计、备注、触发词与操作按钮。
 *
 * Args:
 *   props (TaskCardProps): 任务数据、拖拽状态、进度与各操作回调。
 *
 * Returns:
 *   JSX.Element: 任务卡片。
 *
 * Callers:
 *   - `pages/index/TaskBoard.tsx` 的三组任务网格。
 */
export function TaskCard({
  task,
  completed,
  sortable,
  progress,
  dragging,
  dragOver,
  notify,
  onComplete,
  onReopen,
  onArchive,
  onRestore,
  onSaveNote,
  onSaveTrigger,
  onCopyLink,
  onCopyTrigger,
  onDragStart,
  onDragEnd,
  onDragOver,
  onDragLeave,
  onDrop,
}: TaskCardProps) {
  const href = `/d/${encodeURIComponent(task.name)}/filter`;
  const hashes = task.preview_hashes ?? [];
  const canEnter = !task.archived && !completed;

  const className = [
    'home-task-card',
    sortable ? 'sortable' : '',
    dragging ? 'dragging' : '',
    dragOver ? 'drag-over' : '',
    progress ? 'archiving' : '',
  ]
    .filter(Boolean)
    .join(' ');

  /**
   * 按下时按位置决定卡片是否可拖：仅卡片本体可拖排序，
   * 内部元素按下则临时禁拖，保留浏览器默认的选中与图片拖拽行为。
   *
   * Args:
   *   event (ReactMouseEvent<HTMLDivElement>): 鼠标按下事件。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - 卡片根节点的 onMouseDown。
   */
  const armDrag = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!sortable) return;
    event.currentTarget.draggable = event.target === event.currentTarget;
  };

  return (
    <div
      className={className}
      data-task-name={task.name}
      draggable={sortable}
      onMouseDown={armDrag}
      onDragStart={sortable ? (event) => onDragStart(task.name, event) : undefined}
      onDragEnd={sortable ? onDragEnd : undefined}
      onDragOver={sortable ? (event) => onDragOver(task.name, event) : undefined}
      onDragLeave={sortable ? () => onDragLeave(task.name) : undefined}
      onDrop={sortable ? (event) => onDrop(task.name, event) : undefined}
    >
      <div
        className={canEnter ? 'home-task-previews clickable' : 'home-task-previews'}
        title={canEnter ? '进入处理' : undefined}
        onClick={canEnter ? () => { window.location.href = href; } : undefined}
      >
        {[0, 1, 2, 3].map((index) =>
          hashes[index] ? (
            <img key={index} src={previewUrl(task, hashes[index])} alt="" />
          ) : (
            <div key={index} className="home-empty-slot" />
          ),
        )}
      </div>

      <div className="home-task-body">
        <h3>{task.name}</h3>
        <div className="home-task-meta">{metaText(task)}</div>

        <InlineEdit
          value={task.note}
          placeholder={task.archived ? '无' : '双击添加备注…'}
          className="home-task-note"
          editable={!task.archived}
          activateOn="dblclick"
          onSave={(next) => onSaveNote(task.name, next)}
          notify={notify}
        />

        <div className="home-task-trigger">
          <span className="home-task-trigger-label">触发词</span>
          <InlineEdit
            value={task.trigger_word}
            placeholder={task.archived ? '无' : '点击设置触发词…'}
            className="home-task-trigger-text"
            editable={!task.archived}
            activateOn="click"
            onSave={async (next) => {
              await onSaveTrigger(task.name, next);
              notify('触发词已更新');
            }}
            notify={notify}
          />
          <button
            type="button"
            className="home-task-trigger-copy"
            onClick={() => onCopyTrigger(task.trigger_word)}
          >
            复制
          </button>
        </div>

        <div className="home-task-actions">
          {task.archived ? (
            <button type="button" className="home-btn" onClick={() => onRestore(task)}>
              恢复
            </button>
          ) : (
            <>
              {completed ? null : (
                <a className="home-btn green" href={href}>
                  进入处理
                </a>
              )}
              <button type="button" className="home-btn" onClick={() => onCopyLink(task.name)}>
                复制链接
              </button>
              {completed ? (
                <>
                  <button type="button" className="home-btn" onClick={() => onArchive(task.name)}>
                    归档
                  </button>
                  <button type="button" className="home-btn" onClick={() => onReopen(task.name)}>
                    重新打开
                  </button>
                </>
              ) : (
                <button type="button" className="home-btn" onClick={() => onComplete(task.name)}>
                  完成
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {progress ? (
        <>
          <div className="home-job-bar">
            <div style={{ width: `${progress.pct ?? 0}%` }} />
          </div>
          <div className="home-job-msg">{progress.message}</div>
        </>
      ) : null}
    </div>
  );
}
