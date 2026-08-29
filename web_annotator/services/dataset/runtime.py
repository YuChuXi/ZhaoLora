"""单个任务数据集在运行时的服务句柄. 

Callers:
    - ``server.py:`` 按需构建
    - ``routes/deps.py:`` 按请求路径解析
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from services.dataset.layout import DatasetLayout, resolve_dataset_layout
from services.core.natural import natural_key
from services.models import JobSnapshot
from services.state.annotator import StateManager
from services.core.thumbnailer import Thumbnailer
from services.analysis.sort_cache import CACHE_FILENAME, SemanticSortCache

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
    sort_cache: SemanticSortCache
    color_index: ColorIndex | None = None

    @property
    def root(self) -> Path:
        """任务根目录. 

        Callers:
            - ``routes/deps.py:get_runtime`` : 各路由经此访问 root.
        """
        return self.layout.root

    @property
    def main_dir(self) -> Path:
        """main 图像目录. 

        Callers:
            - ``routes/deps.py:get_runtime`` : 各路由经此访问 main_dir.
        """
        return self.layout.main_dir


@dataclass
class DatasetRegistry:
    """按需缓存任务运行时. 

    Callers:
        - ``server.py:main`` (初始化挂载到 ``app.state.datasets``)
        - ``server.py:dataset_thumb`` 等路由经 ``ensure`` 按需加载
        - ``routes/tasks.py:`` 预览等经 ``ensure`` 访问
    """

    task_root: Path
    thumbnail_size: int = 1280
    job_manager: JobManager | None = None
    by_name: dict[str, DatasetRuntime] = field(default_factory=dict)

    def get(self, name: str) -> DatasetRuntime | None:
        """按名取已加载运行时. 

        Callers:
            - ``services/dataset/runtime.py:DatasetRegistry``
        """
        return self.by_name.get(name)

    def names(self) -> list[str]:
        """已加载任务名列表. 
        """
        return sorted(self.by_name.keys(), key=natural_key)

    async def ensure(self, name: str) -> DatasetRuntime:
        """若尚未加载则解析 ``{task_root}/{name}`` 并缓存, 同时提交缩略图填充任务.

        声明为协程: 内部经 ``_submit_thumb_fill`` → ``JobManager.submit`` 提交
        后台任务, 后者要求事件循环上下文.

        Callers:
            - ``server.py:bind_dataset_middleware``
            - ``server.py:dataset_thumb``
            - ``routes/scoring.py:results``
            - ``routes/images.py:similar_images``
            - ``routes/tasks.py:list_tasks`` / `add_task`
            - ``routes/online_sources.py:download``
        """
        existing = self.by_name.get(name)
        if existing is not None:
            return existing
        layout = resolve_dataset_layout(self.task_root / name)
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
            sort_cache=SemanticSortCache(layout.root / CACHE_FILENAME),
        )
        self.by_name[name] = rt
        if self.job_manager is not None:
            await _submit_thumb_fill(self.job_manager, name, thumbnailer)
        return rt


async def _submit_thumb_fill(jobs: JobManager, dataset: str, thumb: Thumbnailer) -> None:
    """通过 JobManager 提交后台缩略图填充任务.

    Callers:
        - ``services/dataset/runtime.py:DatasetRegistry.ensure``
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

    await jobs.submit(
        kind="thumbnail",
        group=f"thumb_{dataset}",
        dataset=dataset,
        run=run,
    )
