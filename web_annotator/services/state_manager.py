"""
状态管理器 —— 读写 .annotator_state.json，管理选中/回收站状态。

隔离设计：
  - 状态与回收站放在数据集根（meta_dir），不污染 main/
  - 删除操作将文件移入 .trash/（物理隔离）
  - 状态为 {selected: {path: timestamp}, trashed: {path: timestamp}}
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

STATE_FILENAME = ".annotator_state.json"
TRASH_DIRNAME = ".trash"


class StateManager:
    """管理标注状态和回收站操作。"""

    def __init__(self, data_dir: Path, *, meta_dir: Path | None = None) -> None:
        self.data_dir = data_dir
        meta = meta_dir or data_dir
        self.meta_dir = meta
        self.state_path = meta / STATE_FILENAME
        self.trash_dir = meta / TRASH_DIRNAME

    # ---- state file I/O ----

    def _read(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _write(self, state: dict) -> None:
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self) -> dict:
        """获取完整状态，确保必要字段存在。"""
        s = self._read()
        s.setdefault("selected", {})
        s.setdefault("trashed", {})
        return s

    # ---- selection ----

    def toggle(self, path: str) -> bool:
        """切换单张图片选中状态，返回新状态。"""
        state = self.get()
        sel = state["selected"]
        if path in sel:
            del sel[path]
            result = False
        else:
            sel[path] = int(time.time())
            result = True
        self._write(state)
        return result

    def select_all(self, paths: list[str]) -> int:
        """全选给定路径列表。"""
        state = self.get()
        sel = state["selected"]
        now = int(time.time())
        for p in paths:
            sel.setdefault(p, now)
        self._write(state)
        return len(sel)

    def deselect_all(self) -> None:
        """取消全选。"""
        state = self.get()
        state["selected"] = {}
        self._write(state)

    # ---- trash operations ----

    def apply_delete(self, paths: list[str]) -> int:
        """
        将选中的图片（及关联文件）移入 .trash/。
        返回成功移动的数量。
        """
        state = self.get()
        trashed = state["trashed"]
        self.trash_dir.mkdir(parents=True, exist_ok=True)

        moved = 0
        for rel_path in paths:
            src = self.data_dir / rel_path
            dst = self.trash_dir / rel_path

            if not src.exists():
                # 已被外部删除，仅更新状态
                trashed[rel_path] = int(time.time())
                moved += 1
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(dst))
                trashed[rel_path] = int(time.time())
                moved += 1
            except OSError:
                continue

            # 移动关联文件
            stem = Path(rel_path).stem
            for sib in list(self.data_dir.iterdir()):
                if sib.is_file() and sib.stem == stem and sib.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
                    sib_dst = self.trash_dir / sib.name
                    try:
                        shutil.move(str(sib), str(sib_dst))
                    except OSError:
                        pass

        # 清空选中
        state["selected"] = {}
        self._write(state)
        return moved

    def restore(self, paths: list[str]) -> int:
        """
        从 .trash/ 恢复指定图片（及关联文件）到数据目录。
        返回成功恢复的数量。
        """
        state = self.get()
        trashed = state["trashed"]

        restored = 0
        for rel_path in paths:
            if rel_path not in trashed:
                continue

            src = self.trash_dir / rel_path
            dst = self.data_dir / rel_path

            if not src.exists():
                del trashed[rel_path]
                restored += 1
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(dst))
                del trashed[rel_path]
                restored += 1
            except OSError:
                continue

            # 恢复关联文件
            stem = Path(rel_path).stem
            for sib in list(self.trash_dir.iterdir()):
                if sib.is_file() and sib.stem == stem:
                    sib_dst = self.data_dir / sib.name
                    try:
                        shutil.move(str(sib), str(sib_dst))
                    except OSError:
                        pass

        self._write(state)
        return restored
