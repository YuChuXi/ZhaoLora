"""
任务清单: 唯一来源为任务根下的 .annotator_tasks.json.

Callers:
    - ``server.py:main``
    - ``server.py:bind_dataset_middleware``
    - ``server.py:dataset_thumb``
    - ``routes/tasks.py`` 各接口
    - ``routes/meta.py:list_datasets``
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from services.models import TaskEntry, TaskInfo, TaskListFile

TASKS_FILE = ".annotator_tasks.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-()\[\]]{0,63}$")


def validate_task_name(name: str) -> str:
    """校验并规范化任务目录名.

    Args:
        name (str): 待校验的任务目录名原文.

    Returns:
        str: 规范化后的任务名.

    Raises:
        ValueError: 名称非法.

    Callers:
        - ``routes/tasks.py:list_candidates``
        - ``services/state/tasks.py:TaskStore.add``
    """
    name = (name or "").strip()
    if not name or not _NAME_RE.match(name):
        raise ValueError("任务名须为字母、数字、下划线、连字符或括号, 且不以符号开头")
    if name in (".", ".."):
        raise ValueError("非法任务名")
    return name


class TaskStore:
    """读写任务清单.

    Callers:
        - ``server.py:main``
        - ``routes/tasks.py`` 各接口
        - ``routes/meta.py:list_datasets``
    """

    def __init__(self, root: Path) -> None:
        """绑定任务根目录.

        Args:
            root (Path): 任务根目录, 清单文件位于其下的 ``.annotator_tasks.json``.

        Returns:
            None

        Callers:
            - ``server.py:main``
        """
        self.root = root.resolve()
        self.path = self.root / TASKS_FILE

    def _read(self) -> TaskListFile:
        """读取并校验任务清单文件.

        Returns:
            TaskListFile: 清单对象; 文件不存在时返回空清单.

        Raises:
            json.JSONDecodeError: 文件不是合法 JSON.
            ValidationError: JSON 结构不符合模型.
            OSError: 读取失败.
        """
        if not self.path.is_file():
            return TaskListFile()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return TaskListFile.model_validate(data)

    def _write(self, data: TaskListFile) -> None:
        """原子写入任务清单.

        Args:
            data (TaskListFile): 待写入的清单对象.

        Returns:
            None
        """
        self.root.mkdir(parents=True, exist_ok=True)
        text = data.model_dump_json(indent=2) + "\n"
        self.path.write_text(text, encoding="utf-8")

    def list_entries(self) -> list[TaskInfo]:
        """返回清单条目列表, 按 sort_order 升序排列.

        Returns:
            list[TaskInfo]: 排序后的任务信息列表.

        Callers:
            - ``server.py:main``
            - ``routes/tasks.py:list_tasks``
            - ``routes/tasks.py:list_candidates``
            - ``routes/tasks.py:archive_task_route``
            - ``services/state/tasks.py:TaskStore.active_names``
        """
        tasks = self._read().tasks
        out: list[TaskInfo] = []
        for name, meta in sorted(tasks.items(), key=lambda kv: (kv[1].sort_order, kv[0].lower())):
            out.append(TaskInfo(
                name=name,
                completed=meta.completed,
                note=meta.note,
                trigger_word=meta.trigger_word,
                created=meta.created,
                sort_order=meta.sort_order,
            ))
        return out

    def contains(self, name: str) -> bool:
        """检查任务是否在清单中.

        Args:
            name (str): 任务名.

        Returns:
            bool: 存在则为 True.

        Callers:
            - ``server.py:bind_dataset_middleware``
            - ``server.py:dataset_thumb``
        """
        return name in self._read().tasks

    def active_names(self) -> list[str]:
        """未完成任务名列表.

        Returns:
            list[str]: 未完成任务的名称列表.

        Callers:
            - ``routes/meta.py:list_datasets``
        """
        return [e.name for e in self.list_entries() if not e.completed]

    def add(self, name: str) -> TaskInfo:
        """登记新任务；必要时自动创建 main/ 与 mask/ 目录.

        Args:
            name (str): 待登记的任务名, 须通过 ``validate_task_name``.

        Returns:
            TaskInfo: 新创建的任务信息.

        Raises:
            ValueError: 名称非法或任务已存在.

        Callers:
            - ``routes/tasks.py:add_task``
            - ``routes/tasks.py:restore_task_route``
        """
        name = validate_task_name(name)
        data = self._read()
        if name in data.tasks:
            raise ValueError(f"任务已存在: {name}")
        task_root = self.root / name
        main = task_root / "main"
        mask = task_root / "mask"
        if not main.is_dir():
            main.mkdir(parents=True, exist_ok=True)
        if not mask.is_dir():
            mask.mkdir(parents=True, exist_ok=True)
        max_order = max((e.sort_order for e in data.tasks.values()), default=-1)
        data.tasks[name] = TaskEntry(completed=False, created=int(time.time()), sort_order=max_order + 1)
        self._write(data)
        return TaskInfo(name=name, completed=False, root=str(task_root), note="", trigger_word="", sort_order=max_order + 1)

    def set_completed(self, name: str, completed: bool) -> TaskInfo:
        """标记任务完成或重新打开.

        Args:
            name (str): 任务名.
            completed (bool): True 标记完成, False 重新打开.

        Returns:
            TaskInfo: 更新后的任务信息.

        Raises:
            KeyError: 任务名不在清单中.

        Callers:
            - ``routes/tasks.py:complete_task``
            - ``routes/tasks.py:reopen_task``
            - ``routes/tasks.py:restore_task_route``
        """
        data = self._read()
        if name not in data.tasks:
            raise KeyError(name)
        entry = data.tasks[name].model_copy(update={"completed": bool(completed)})
        data.tasks[name] = entry
        self._write(data)
        return TaskInfo(name=name, completed=bool(completed), note=entry.note, trigger_word=entry.trigger_word)

    def remove(self, name: str) -> None:
        """从清单中移除任务(不删除目录).

        Args:
            name (str): 任务名.

        Returns:
            None

        Raises:
            KeyError: 任务名不在清单中.

        Callers:
            - ``routes/tasks.py:archive_task_route``
        """
        data = self._read()
        if name not in data.tasks:
            raise KeyError(name)
        del data.tasks[name]
        self._write(data)

    def set_note(self, name: str, note: str) -> TaskInfo:
        """修改任务备注.

        Args:
            name (str): 任务名.
            note (str): 备注原文, 写入前去空白.

        Returns:
            TaskInfo: 更新后的任务信息.

        Raises:
            KeyError: 任务名不在清单中.

        Callers:
            - ``routes/tasks.py:set_note``
        """
        data = self._read()
        if name not in data.tasks:
            raise KeyError(name)
        entry = data.tasks[name].model_copy(update={"note": note.strip()})
        data.tasks[name] = entry
        self._write(data)
        return TaskInfo(name=name, completed=entry.completed, note=entry.note, trigger_word=entry.trigger_word)

    def set_trigger_word(self, name: str, trigger_word: str) -> TaskInfo:
        """修改任务触发词(用于就绪 TOML 的 subset class_tokens).

        Args:
            name (str): 任务名.
            trigger_word (str): 触发词原文, 写入前去空白.

        Returns:
            TaskInfo: 更新后的任务信息.

        Raises:
            KeyError: 任务名不在清单中.

        Callers:
            - ``routes/tasks.py:set_trigger_word``
        """
        data = self._read()
        if name not in data.tasks:
            raise KeyError(name)
        entry = data.tasks[name].model_copy(update={"trigger_word": trigger_word.strip()})
        data.tasks[name] = entry
        self._write(data)
        return TaskInfo(name=name, completed=entry.completed, note=entry.note, trigger_word=entry.trigger_word)

    def reorder(self, ordered_names: list[str]) -> None:
        """按给定名称列表重排所有任务的 sort_order.

        未出现在列表中的任务保持原有相对顺序并追加到末尾.

        Args:
            ordered_names (list[str]): 期望的任务名顺序.

        Returns:
            None

        Raises:
            KeyError: 存在未知的任务名.

        Callers:
            - ``routes/tasks.py:reorder_tasks``
        """
        data = self._read()
        existing = set(data.tasks.keys())
        given = set(ordered_names)
        unknown = given - existing
        if unknown:
            raise KeyError(f"未知任务: {', '.join(sorted(unknown))}")
        missing = [n for n, e in sorted(data.tasks.items(), key=lambda kv: (kv[1].sort_order, kv[0].lower()))
                   if n not in given]
        full_order = ordered_names + missing
        for idx, name in enumerate(full_order):
            data.tasks[name] = data.tasks[name].model_copy(update={"sort_order": idx})
        self._write(data)
