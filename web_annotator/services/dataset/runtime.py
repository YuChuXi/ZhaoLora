"""单个任务数据集在运行时的服务句柄. 

Callers:
    - `server.py` 按需构建
    - `routes/deps.py` 按请求路径解析
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from services.dataset.layout import DatasetLayout, resolve_dataset_layout
from services.models import JobSnapshot
from services.state.annotator import StateManager
from services.core.thumbnailer import Thumbnailer

if TYPE_CHECKING:
    from services.core.jobs import JobManager

if TYPE_CHECKING:
    from services.analysis.similarity import ColorIndex


@dataclass
class DatasetRuntime:
    """一个已登记任务目录的运行时对象. """

    name: str
    layout: DatasetLayout
    thumbnailer: Thumbnailer
    state_manager: StateManager
    mask_dir: Path
    color_index: ColorIndex | None = None

    @property
    def root(self) -> Path:
        """任务根目录. 

        Callers:
            - 各路由 `get_runtime`
        """
        return self.layout.root

    @property
    def main_dir(self) -> Path:
        """main 图像目录. 

        Callers:
            - 各路由
        """
        return self.layout.main_dir


@dataclass
class DatasetRegistry:
    """按需缓存任务运行时. 

    Callers:
        - `server.py`
        - 中间件
        - `routes/tasks.py` 预览
    """

    task_root: Path
    thumbnail_size: int = 2560
    job_manager: JobManager | None = None
    by_name: dict[str, DatasetRuntime] = field(default_factory=dict)
    _migrate: Callable[[Path, Path], None] | None = None

    def get(self, name: str) -> DatasetRuntime | None:
        """按名取已加载运行时. 

        Callers:
            - `DatasetRegistry`
        """
        return self.by_name.get(name)

    def names(self) -> list[str]:
        """已加载任务名列表. 

        Callers:
            - 管理接口
        """
        return sorted(self.by_name.keys())

    def ensure(self, name: str) -> DatasetRuntime:
        """若尚未加载则解析 ``{task_root}/{name}`` 并缓存, 同时提交缩略图填充任务. 

        Callers:
            - 中间件 `bind_dataset_middleware`
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
        if self.job_manager is not None:
            _submit_thumb_fill(self.job_manager, name, thumbnailer)
        return rt


def _submit_thumb_fill(jobs: JobManager, dataset: str, thumb: Thumbnailer) -> None:
    """通过 JobManager 提交后台缩略图填充任务. 

    Callers:
        - `DatasetRegistry.ensure`
    """
    from services.models.jobs import JobSnapshot as Snap

    def run(emit: Callable[[Snap], None]) -> None:
        total = thumb.count_images()
        emit(Snap(
            status="running", kind="thumbnail", dataset=dataset,
            job_id="", done=0, total=total,
        ))
        ok = thumb.fill_missing()
        emit(Snap(
            status="done", kind="thumbnail", dataset=dataset,
            job_id="", done=ok, total=total,
        ))

    jobs.submit(
        kind="thumbnail",
        group=f"thumb_{dataset}",
        dataset=dataset,
        run=run,
    )
