"""标注状态与回收站文件移动.

状态文件与 ``.trash`` 位于数据集根(``meta_dir``).
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from services.core.atomic import atomic_write_text, locked
from services.dataset.sidecar import sidecar_paths
from services.dataset.workdir import IMAGE_EXTS, TRASH_DIRNAME, resolve_under
from services.models import AnnotatorState

STATE_FILENAME = ".annotator_state.json"


class StateManager:
    """管理选中状态, 并将文件在数据目录与回收站之间移动.

    状态读改写方法经 ``locked`` 装饰器用实例锁串行化, 写入经临时文件
    原子替换, 并发请求不会交错损坏状态文件.

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
        self._lock = threading.Lock()

    def _read(self) -> AnnotatorState:
        """读取状态文件.

        Returns:
            AnnotatorState: 当前状态; 文件不存在时为空状态.

        Raises:
            ValidationError: JSON 结构不符合模型.
            json.JSONDecodeError: 文件不是合法 JSON.
            OSError: 读取失败.
        Callers:
            - ``services/state/annotator.py:StateManager.get``
        """
        if not self.state_path.exists():
            return AnnotatorState()
        return AnnotatorState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def _write(self, state: AnnotatorState) -> None:
        """将状态写入 JSON 文件 (临时文件写入后原子替换, 不会留下写了一半的文件).

        Args:
            state (AnnotatorState): 待写入的状态对象.

        Returns:
            None
        Callers:
            - ``services/state/annotator.py:StateManager.apply_delete``
            - ``services/state/annotator.py:StateManager.deselect_all``
            - ``services/state/annotator.py:StateManager.restore``
            - ``services/state/annotator.py:StateManager.select_all``
            - ``services/state/annotator.py:StateManager.toggle``
        """
        atomic_write_text(self.state_path, state.model_dump_json(indent=2))

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

    @locked
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

    @locked
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

    @locked
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

    @locked
    def apply_delete(self, paths: list[str]) -> int:
        """把指定图片移入回收站 ``.trash/``.

        处理流程:
        1. 每个路径在回收站内保持相同的相对目录结构
           (``main/a.png`` → ``.trash/main/a.png``).
        2. 同 stem 旁路文件 (``.txt`` 标注、``.npz`` 蒙板等非图像文件) 与
           来源元数据 (``.{来源}_meta/{pid}.json``) 一并随之移入,
           保证永久删除时不残留孤儿元数据.
        3. 回收站路径与删除时间写入状态的 ``trashed`` 表, 并清空选中表.

        Args:
            paths (list[str]): 相对数据集根、且文件当前存在的路径.

        Returns:
            int: 成功移入回收站的图片数量.

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

    @locked
    def restore(self, paths: list[str]) -> int:
        """从 ``.trash/`` 恢复指定图片及其同 stem 旁路文件.

        与 ``apply_delete`` 除移动方向外完全对称: 逐个把回收站内同结构
        路径的图片移回数据集原位、旁路文件与来源元数据随行、从 ``trashed``
        表移除记录; 文件已不在回收站的路径仅清理残留记录. 结束后清空
        选中表, 避免恢复后的图片残留选中状态而被再次删除.

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

        state.selected = {}
        self._write(state)
        return restored

    def _move_sidecar_files(self, parent: Path, rel_path: str, dest_root: Path) -> None:
        """移动与图片同 stem 的旁路文件、来源元数据与本地源产物.

        旁路文件指同目录下同 stem、非图像后缀的文件 (``.txt`` 标签、
        ``.npz`` 蒙板嵌入等); 来源元数据指数据源注册表判定的
        ``.{来源}_meta/{pid}.json``; 本地源产物指经 ``sidecar.register_sidecar``
        ``.wd14_meta/<模型指纹>/<stem>.npy`` 等。三者随图片同进同出回收站,
        保证永久删除时不残留孤儿元数据、恢复时数据完整。

        Args:
            parent (Path): 源文件所在目录.
            rel_path (str): 图片相对路径(用于拼旁路相对路径).
            dest_root (Path): 目标根目录(数据集根或回收站根).

        Returns:
            None
        Callers:
            - ``services/state/annotator.py:StateManager.apply_delete``
            - ``services/state/annotator.py:StateManager.restore``
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
        self._move_meta_files(parent, stem, rel_parent, dest_root)

    def _move_meta_files(self, parent: Path, stem: str, rel_parent: str, dest_root: Path) -> None:
        """移动图片的来源元数据与本地源产物.

        Args:
            parent (Path): 源图片所在目录.
            stem (str): 图片文件名 stem, 经来源注册表拆出 pid.
            rel_parent (str): 图片相对父目录 ("" 表示数据集根).
            dest_root (Path): 目标根目录(数据集根或回收站根).

        Returns:
            None

        Callers:
            - ``services/state/annotator.py:StateManager._move_sidecar_files``
        """
        from services.online_sources import detect_source

        for path in sidecar_paths(parent, stem):
            rel = path.relative_to(parent).as_posix()
            meta_rel = f"{rel_parent}/{rel}" if rel_parent not in ("", ".") else rel
            meta_dst = resolve_under(dest_root, meta_rel)
            meta_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(meta_dst))

        result = detect_source(stem)
        if result is None:
            return
        src_name, pid = result
        meta_src = parent / f".{src_name}_meta" / f"{pid}.json"
        if not meta_src.is_file():
            return
        meta_rel = f"{rel_parent}/.{src_name}_meta/{pid}.json" if rel_parent not in ("", ".") else f".{src_name}_meta/{pid}.json"
        meta_dst = resolve_under(dest_root, meta_rel)
        meta_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(meta_src), str(meta_dst))
