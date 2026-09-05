"""
任务清单: 唯一来源为任务根下的 .annotator_tasks.json.

Callers:
    - ``server.py:main``
    - ``server.py:bind_dataset_middleware``
    - ``server.py:dataset_thumb``
    - ``routes/tasks.py:`` 各接口
    - ``routes/meta.py:list_datasets``
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from services.core.natural import natural_key
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
        - ``routes/tasks.py:`` 各接口
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
        self.tasks = self._load()

    def _load(self) -> dict[str, TaskEntry]:
        """从磁盘加载任务清单并建立按任务名的索引.

        Returns:
            dict[str, TaskEntry]: 按任务名索引的任务字典; 文件不存在时返回空字典.

        Raises:
            json.JSONDecodeError: 文件不是合法 JSON.
            ValidationError: JSON 结构不符合模型.
            OSError: 读取失败.

        Callers:
            - ``services/state/tasks.py:TaskStore.__init__``
        """
        if not self.path.is_file():
            return {}
        data = TaskListFile.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
        return {e.name: e for e in data.tasks}

    def _write(self) -> None:
        """把内存中的任务清单原子写入磁盘.

        Returns:
            None

        Callers:
            - ``services/state/tasks.py:TaskStore.add``
            - ``services/state/tasks.py:TaskStore.add_trigger_word``
            - ``services/state/tasks.py:TaskStore.clear_archived``
            - ``services/state/tasks.py:TaskStore.reorder``
            - ``services/state/tasks.py:TaskStore.set_archived``
            - ``services/state/tasks.py:TaskStore.set_completed``
            - ``services/state/tasks.py:TaskStore.set_note``
            - ``services/state/tasks.py:TaskStore.set_trigger_word``
        """
        self.root.mkdir(parents=True, exist_ok=True)
        text = TaskListFile(tasks=list(self.tasks.values())).model_dump_json(indent=2) + "\n"
        self.path.write_text(text, encoding="utf-8")

    def list_entries(self) -> list[TaskInfo]:
        """返回全部任务条目, 按 sort_order 升序排列.

        Returns:
            list[TaskInfo]: 全部任务信息列表.

        Callers:
            - ``routes/meta.py:list_datasets``
            - ``routes/tasks.py:list_tasks``
            - ``routes/tasks.py:list_candidates``
        """
        entries = sorted(self.tasks.values(), key=lambda e: (e.sort_order, natural_key(e.name)))
        return [TaskInfo(**e.model_dump()) for e in entries]

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
        return name in self.tasks

    def add(self, name: str) -> TaskInfo:
        """登记新任务, 默认排在最前; 必要时自动创建 main/ 与 mask/ 目录.

        Args:
            name (str): 待登记的任务名, 须通过 ``validate_task_name``.

        Returns:
            TaskInfo: 新创建的任务信息, 排序位置位于现有任务之前.

        Raises:
            ValueError: 名称非法或任务已存在.

        Callers:
            - ``routes/tasks.py:add_task``
            - ``routes/tasks.py:restore_task_route``
        """
        name = validate_task_name(name)
        if name in self.tasks:
            raise ValueError(f"任务已存在: {name}")
        task_root = self.root / name
        main = task_root / "main"
        mask = task_root / "mask"
        if not main.is_dir():
            main.mkdir(parents=True, exist_ok=True)
        if not mask.is_dir():
            mask.mkdir(parents=True, exist_ok=True)
        min_order = min((e.sort_order for e in self.tasks.values()), default=1)
        self.tasks[name] = TaskEntry(name=name, completed=False, created=int(time.time()), sort_order=min_order - 1)
        self._write()
        return TaskInfo(name=name, completed=False, root=str(task_root), note="", trigger_word="", sort_order=min_order - 1)

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
        if name not in self.tasks:
            raise KeyError(name)
        self.tasks[name] = self.tasks[name].model_copy(update={"completed": bool(completed)})
        self._write()
        updated = self.tasks[name]
        return TaskInfo(name=name, completed=bool(completed), note=updated.note, trigger_word=updated.trigger_word)

    def archived_entries(self) -> list[tuple[str, TaskEntry]]:
        """返回已归档任务条目, 按归档时间降序排列.

        Returns:
            list[tuple[str, TaskEntry]]: 已归档任务的 ``(name, entry)`` 列表.

        Callers:
            - ``routes/tasks.py:list_archived``
            - ``routes/tasks.py:restore_task_route``
        """
        items = [(name, e) for name, e in self.tasks.items() if e.archived]
        items.sort(key=lambda kv: kv[1].archived_at or 0, reverse=True)
        return items

    def set_archived(
        self,
        name: str,
        archived_at: int,
        archive_base: str,
        preview_hashes: list[str],
        image_count: int,
    ) -> TaskInfo:
        """把任务标记为已归档并写入归档详情.

        Args:
            name (str): 任务名.
            archived_at (int): 归档时间戳.
            archive_base (str): 归档基本名(与 ``__packed__/{base}.*`` 对应).
            preview_hashes (list[str]): 归档预览缩略图 hash 列表.
            image_count (int): 归档时图片总数.

        Returns:
            TaskInfo: 更新后的任务信息.

        Raises:
            KeyError: 任务名不在清单中.

        Callers:
            - ``routes/tasks.py:archive_task_route``
        """
        if name not in self.tasks:
            raise KeyError(name)
        updated = self.tasks[name].model_copy(update={
            "archived": True,
            "archived_at": archived_at,
            "archive_base": archive_base,
            "preview_hashes": preview_hashes,
            "image_count": image_count,
        })
        self.tasks[name] = updated
        self._write()
        return TaskInfo(name=name, completed=updated.completed, note=updated.note, trigger_word=updated.trigger_word)

    def clear_archived(self, name: str) -> TaskInfo:
        """把已归档任务恢复为当前任务(清空归档状态与详情).

        Args:
            name (str): 任务名.

        Returns:
            TaskInfo: 更新后的任务信息.

        Raises:
            KeyError: 任务名不在清单中.

        Callers:
            - ``routes/tasks.py:restore_task_route``
        """
        if name not in self.tasks:
            raise KeyError(name)
        updated = self.tasks[name].model_copy(update={
            "archived": False,
            "archived_at": None,
            "archive_base": "",
            "preview_hashes": [],
            "image_count": 0,
        })
        self.tasks[name] = updated
        self._write()
        return TaskInfo(name=name, completed=updated.completed, note=updated.note, trigger_word=updated.trigger_word)

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
        if name not in self.tasks:
            raise KeyError(name)
        updated = self.tasks[name].model_copy(update={"note": note.strip()})
        self.tasks[name] = updated
        self._write()
        return TaskInfo(name=name, completed=updated.completed, note=updated.note, trigger_word=updated.trigger_word)

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
        if name not in self.tasks:
            raise KeyError(name)
        updated = self.tasks[name].model_copy(update={"trigger_word": trigger_word.strip()})
        self.tasks[name] = updated
        self._write()
        return TaskInfo(name=name, completed=updated.completed, note=updated.note, trigger_word=updated.trigger_word)

    def add_trigger_word(self, name: str, word: str) -> TaskInfo:
        """向任务触发词集合添加一个词, 不覆盖已有触发词.

        触发词以逗号分隔存储; 新词插入最前(批量打标的画师词为数据集主体,
        应居 class_tokens 首位), 已存在的词保留原位, 空词为无操作. 整串设置
        (标签页以勾选集合为准的覆盖语义)请使用 ``set_trigger_word``.

        Args:
            name (str): 任务名.
            word (str): 待添加的触发词, 写入前去空白.

        Returns:
            TaskInfo: 任务信息 (触发词为添加后的最新值).

        Raises:
            KeyError: 任务名不在清单中.

        Callers:
            - ``routes/tasks.py:add_trigger_word``
        """
        if name not in self.tasks:
            raise KeyError(name)
        entry = self.tasks[name]
        word = word.strip()
        words = [s for s in (t.strip() for t in entry.trigger_word.split(",")) if s]
        if word and word not in words:
            words.insert(0, word)
            entry = entry.model_copy(update={"trigger_word": ", ".join(words)})
            self.tasks[name] = entry
            self._write()
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
        existing = set(self.tasks)
        given = set(ordered_names)
        unknown = given - existing
        if unknown:
            raise KeyError(f"未知任务: {', '.join(sorted(unknown))}")
        missing = [
            kv[0] for kv in sorted(self.tasks.items(), key=lambda kv: (kv[1].sort_order, natural_key(kv[0])))
            if kv[0] not in given
        ]
        full_order = ordered_names + missing
        for idx, name in enumerate(full_order):
            self.tasks[name] = self.tasks[name].model_copy(update={"sort_order": idx})
        self._write()
