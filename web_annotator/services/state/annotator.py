"""标注状态与回收站文件移动.

状态文件与 ``.trash`` 位于数据集根(``meta_dir``).
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from services.models import AnnotatorState
from services.core.scanner import IMAGE_EXTS
from services.core.work_dir import resolve_under

STATE_FILENAME = ".annotator_state.json"
TRASH_DIRNAME = ".trash"


class StateManager:
    """管理选中状态, 并将文件在数据目录与回收站之间移动.

    Callers:
        - ``services/dataset/runtime.py:DatasetRegistry.ensure``
    """

    def __init__(self, data_dir: Path, *, meta_dir: Path | None = None) -> None:
        """绑定数据集根目录.

        Args:
            data_dir (Path): 数据集根(相对路径相对此目录).
            meta_dir (Path | None): 状态文件与回收站所在目录; 默认与 ``data_dir`` 相同.

        Returns:
            None

        Callers:
            - ``services/dataset/runtime.py:DatasetRegistry.ensure``
        """
        self.data_dir = data_dir
        meta = meta_dir or data_dir
        self.meta_dir = meta
        self.state_path = meta / STATE_FILENAME
        self.trash_dir = meta / TRASH_DIRNAME

    def _read(self) -> AnnotatorState:
        """读取状态文件.

        Returns:
            AnnotatorState: 当前状态; 文件不存在时为空状态.

        Raises:
            ValidationError: JSON 结构不符合模型.
            json.JSONDecodeError: 文件不是合法 JSON.
            OSError: 读取失败.
        """
        if not self.state_path.exists():
            return AnnotatorState()
        return AnnotatorState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def _write(self, state: AnnotatorState) -> None:
        """将状态写入 JSON 文件.

        Args:
            state (AnnotatorState): 待写入的状态对象.

        Returns:
            None
        """
        self.state_path.write_text(
            state.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def get(self) -> AnnotatorState:
        """返回当前标注状态.

        Returns:
            AnnotatorState: 包含 selected 和 trashed 映射的状态对象.

        Callers:
            - ``routes/annotations.py:get_state``
            - ``routes/annotations.py:select_all``
            - ``routes/annotations.py:apply_delete``
            - ``routes/annotations.py:restore``
            - ``services/state/annotator.py:StateManager.toggle``
            - ``services/state/annotator.py:StateManager.select_all``
            - ``services/state/annotator.py:StateManager.deselect_all``
            - ``services/state/annotator.py:StateManager.apply_delete``
            - ``services/state/annotator.py:StateManager.restore``
        """
        return self._read()

    def toggle(self, path: str) -> bool:
        """切换单张图片选中状态.

        Args:
            path (str): 相对数据集根的路径.

        Returns:
            bool: 切换后是否选中.

        Callers:
            - ``routes/annotations.py:toggle``
        """
        state = self.get()
        if path in state.selected:
            del state.selected[path]
            result = False
        else:
            state.selected[path] = int(time.time())
            result = True
        self._write(state)
        return result

    def select_all(self, paths: list[str]) -> int:
        """将给定路径全部标为选中.

        Args:
            paths (list[str]): 相对路径列表.

        Returns:
            int: 选中表当前条目数.

        Callers:
            - ``routes/annotations.py:select_all``
            - ``routes/annotations.py:select_range``
        """
        state = self.get()
        now = int(time.time())
        for p in paths:
            state.selected.setdefault(p, now)
        self._write(state)
        return len(state.selected)

    def deselect_all(self) -> None:
        """清空选中表.

        Returns:
            None

        Callers:
            - ``routes/annotations.py:deselect_all``
        """
        state = self.get()
        state.selected = {}
        self._write(state)

    def apply_delete(self, paths: list[str]) -> int:
        """将指定图片及其同 stem 旁路文件移入 ``.trash/``.

        Args:
            paths (list[str]): 相对数据集根、且文件当前存在的路径.

        Returns:
            int: 成功移动的图片数量.

        Raises:
            ValueError: 路径越界.
            FileNotFoundError: 源文件不存在.
            OSError: 移动失败.

        Callers:
            - ``routes/annotations.py:apply_delete``
            - ``routes/dedupe.py:apply_delete``
        """
        state = self.get()
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        data_root = self.data_dir.resolve()
        trash_root = self.trash_dir.resolve()

        moved = 0
        for rel_path in paths:
            src = resolve_under(self.data_dir, rel_path)
            rel_path = src.relative_to(data_root).as_posix()
            dst = resolve_under(self.trash_dir, rel_path)
            if not src.is_file():
                raise FileNotFoundError(rel_path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            state.trashed[rel_path] = int(time.time())
            moved += 1
            self._move_sidecar_files(src.parent, rel_path, trash_root)

        state.selected = {}
        self._write(state)
        return moved

    def restore(self, paths: list[str]) -> int:
        """从 ``.trash/`` 恢复指定图片及其同 stem 旁路文件.

        Args:
            paths (list[str]): 回收站状态中的相对路径.

        Returns:
            int: 成功恢复的图片数量.

        Raises:
            ValueError: 路径越界.
            OSError: 移动失败.

        Callers:
            - ``routes/annotations.py:restore``
        """
        state = self.get()
        data_root = self.data_dir.resolve()

        restored = 0
        for rel_path in paths:
            if rel_path not in state.trashed:
                continue
            src = resolve_under(self.trash_dir, rel_path)
            rel_path = src.relative_to(self.trash_dir.resolve()).as_posix()
            dst = resolve_under(self.data_dir, rel_path)
            if not src.is_file():
                del state.trashed[rel_path]
                restored += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            del state.trashed[rel_path]
            restored += 1
            self._move_sidecar_files(src.parent, rel_path, data_root)

        self._write(state)
        return restored

    def _move_sidecar_files(self, parent: Path, rel_path: str, dest_root: Path) -> None:
        """移动与图片同 stem、非图像后缀的旁路文件.

        Args:
            parent (Path): 源文件所在目录.
            rel_path (str): 图片相对路径(用于拼旁路相对路径).
            dest_root (Path): 目标根目录(数据集根或回收站根).

        Returns:
            None
        """
        if not parent.is_dir():
            return
        stem = Path(rel_path).stem
        img_name = Path(rel_path).name
        rel_parent = str(Path(rel_path).parent)
        for sib in list(parent.iterdir()):
            if not sib.is_file() or sib.name == img_name:
                continue
            if sib.stem != stem:
                continue
            if sib.suffix.lower() in IMAGE_EXTS:
                continue
            sib_rel = f"{rel_parent}/{sib.name}" if rel_parent not in ("", ".") else sib.name
            sib_dst = resolve_under(dest_root, sib_rel)
            sib_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sib), str(sib_dst))
