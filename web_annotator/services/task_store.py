"""
任务清单：唯一来源为任务根下的 .annotator_tasks.json。

引用：server.py 启动校验；routes/tasks.py、routes/meta.py；中间件按清单放行。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TASKS_FILE = ".annotator_tasks.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_task_name(name: str) -> str:
    """
    校验任务目录名。

    引用：routes/tasks.py 添加任务。
    """
    name = (name or "").strip()
    if not name or not _NAME_RE.match(name):
        raise ValueError("任务名须为字母、数字、下划线或连字符，且不以符号开头")
    if name in (".", ".."):
        raise ValueError("非法任务名")
    return name


class TaskStore:
    """
    读写任务清单。

    引用：server.py（app.state.task_store）；routes/tasks.py。
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = self.root / TASKS_FILE

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"tasks": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"任务清单格式错误: {self.path}")
        tasks = data.get("tasks")
        if tasks is None:
            tasks = {}
        if not isinstance(tasks, dict):
            raise ValueError(f"任务清单 tasks 须为对象: {self.path}")
        return {"tasks": tasks}

    def _write(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self.path.write_text(text, encoding="utf-8")

    def list_entries(self) -> list[dict]:
        """
        返回清单条目列表。

        每项：name, completed。
        引用：GET /api/tasks、GET /api/datasets。
        """
        tasks = self._read()["tasks"]
        out = []
        for name in sorted(tasks.keys()):
            meta = tasks[name] if isinstance(tasks[name], dict) else {}
            out.append({
                "name": name,
                "completed": bool(meta.get("completed", False)),
            })
        return out

    def contains(self, name: str) -> bool:
        """引用：中间件、缩略图路由。"""
        return name in self._read()["tasks"]

    def active_names(self) -> list[str]:
        """未完成任务名。引用：GET /api/datasets。"""
        return [e["name"] for e in self.list_entries() if not e["completed"]]

    def add(self, name: str) -> dict:
        """
        登记任务；必要时创建 main/ 与 mask/。

        引用：POST /api/tasks。
        """
        name = validate_task_name(name)
        data = self._read()
        if name in data["tasks"]:
            raise ValueError(f"任务已存在: {name}")
        task_root = self.root / name
        main = task_root / "main"
        mask = task_root / "mask"
        if not main.is_dir():
            main.mkdir(parents=True, exist_ok=True)
        if not mask.is_dir():
            mask.mkdir(parents=True, exist_ok=True)
        data["tasks"][name] = {"completed": False}
        self._write(data)
        return {"name": name, "completed": False, "root": str(task_root)}

    def set_completed(self, name: str, completed: bool) -> dict:
        """引用：complete / reopen。"""
        data = self._read()
        if name not in data["tasks"]:
            raise KeyError(name)
        meta = data["tasks"][name]
        if not isinstance(meta, dict):
            meta = {}
        meta["completed"] = bool(completed)
        data["tasks"][name] = meta
        self._write(data)
        return {"name": name, "completed": bool(completed)}
