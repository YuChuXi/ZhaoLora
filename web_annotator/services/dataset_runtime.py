"""
单个任务数据集在运行时的服务句柄。

引用：server.py 按需构建；routes/deps.py 按请求路径解析。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from services.dataset_layout import DatasetLayout, resolve_dataset_layout
from services.state_manager import StateManager
from services.thumbnailer import Thumbnailer

if TYPE_CHECKING:
    from services.color_similarity import ColorIndex


@dataclass
class DatasetRuntime:
    """一个已登记任务目录的运行时对象。"""

    name: str
    layout: DatasetLayout
    thumbnailer: Thumbnailer
    state_manager: StateManager
    mask_dir: Path
    color_index: ColorIndex | None = None

    @property
    def root(self) -> Path:
        """``root``。引用：``DatasetRuntime``。"""
        return self.layout.root

    @property
    def main_dir(self) -> Path:
        """``main_dir``。引用：``DatasetRuntime``。"""
        return self.layout.main_dir


@dataclass
class DatasetRegistry:
    """
    按需缓存任务运行时。

    引用：server.py、中间件、routes/tasks.py 预览。
    """

    task_root: Path
    thumbnail_size: int = 2560
    by_name: dict[str, DatasetRuntime] = field(default_factory=dict)
    _migrate: Callable[[Path, Path], None] | None = None

    def get(self, name: str) -> DatasetRuntime | None:
        """``get``。引用：``DatasetRegistry``。"""
        return self.by_name.get(name)

    def names(self) -> list[str]:
        """``names``。引用：``DatasetRegistry``。"""
        return sorted(self.by_name.keys())

    def ensure(self, name: str) -> DatasetRuntime:
        """
        若尚未加载则解析 `{task_root}/{name}` 并缓存。

        引用：中间件绑定；任务预览生成缩略图。
        """
        existing = self.by_name.get(name)
        if existing is not None:
            return existing
        layout = resolve_dataset_layout(self.task_root / name)
        if self._migrate is not None:
            self._migrate(layout.main_dir, layout.root)
        thumbnailer = Thumbnailer(
            layout.root, size=self.thumbnail_size, meta_dir=layout.root
        )
        state_manager = StateManager(layout.root, meta_dir=layout.root)
        mask_dir = layout.mask_dir
        mask_dir.mkdir(parents=True, exist_ok=True)
        rt = DatasetRuntime(
            name=name,
            layout=layout,
            thumbnailer=thumbnailer,
            state_manager=state_manager,
            mask_dir=mask_dir,
        )
        self.by_name[name] = rt
        return rt
