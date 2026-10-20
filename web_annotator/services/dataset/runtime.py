"""数据集对象与按名注册表.

Callers:
    - ``server.py:`` 按需构建
    - ``routes/deps.py:`` 按请求路径解析
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from services.dataset.workdir import (
    CROP_META_DIRNAME,
    NON_BROWSABLE_DIRS,
    RESERVED_DIR_NAMES,
    TRASH_DIRNAME,
    VIDEO_DIRNAME,
    WorkDir,
    count_images,
)
from services.core.natural import natural_key
from services.state.annotator import StateManager
from services.core.thumbnailer import Thumbnailer
from services.analysis.sort_cache import CACHE_FILENAME, SemanticSortCache

if TYPE_CHECKING:
    from services.core.jobs import JobManager

if TYPE_CHECKING:
    from services.analysis.similarity import ColorIndex


@dataclass
class Dataset:
    """一个已登记任务的数据集对象.

    数据集根下的目录约定与工作目录枚举全部由本对象承担; 路由与服务只持有
    本对象, 不再各自拼路径或传递目录名.

    Attributes:
        name (str): 任务名(即数据集根目录名).
        root (Path): 数据集根目录.
        thumbnailer (Thumbnailer): 缩略图管理器.
        state_manager (StateManager): 标注选中与回收站状态.
        sort_cache (SemanticSortCache): 语义排序缓存.
        color_index (ColorIndex | None): 色调索引, 首次相似图查询时构建.
        settings_lock (asyncio.Lock): 数据集设置合并写入锁.

    Callers:
        - ``services/dataset/runtime.py:DatasetRegistry.ensure``
        - ``routes/deps.py:DatasetDep``
    """

    name: str
    root: Path
    thumbnailer: Thumbnailer
    state_manager: StateManager
    sort_cache: SemanticSortCache
    color_index: ColorIndex | None = None
    settings_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def open(cls, name: str, root: Path, thumbnail_size: int) -> "Dataset":
        """打开数据集, 确保 ``main/`` 与 ``mask/`` 存在.

        Args:
            name (str): 任务名.
            root (Path): 数据集根目录.
            thumbnail_size (int): 缩略图长边尺寸.

        Returns:
            Dataset: 已就绪的数据集对象.

        Raises:
            FileNotFoundError: 数据集根目录不存在.

        Callers:
            - ``services/dataset/runtime.py:DatasetRegistry.ensure``
        """
        root = root.resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"目录不存在: {root}")
        (root / "main").mkdir(parents=True, exist_ok=True)
        (root / "mask").mkdir(parents=True, exist_ok=True)
        return cls(
            name=name,
            root=root,
            thumbnailer=Thumbnailer(root, size=thumbnail_size, meta_dir=root),
            state_manager=StateManager(root, meta_dir=root),
            sort_cache=SemanticSortCache(root / CACHE_FILENAME),
        )

    @property
    def task_root(self) -> Path:
        """任务根目录(全局缺省层所在处).

        Returns:
            Path: 数据集根的父目录, 如 ``datasets``.
        """
        return self.root.parent

    @property
    def main_dir(self) -> Path:
        """main 图像目录.

        Returns:
            Path: ``root/main``.
        """
        return self.root / "main"

    @property
    def mask_dir(self) -> Path:
        """蒙版目录.

        Returns:
            Path: ``root/mask``.
        """
        return self.root / "mask"

    @property
    def video_dir(self) -> Path:
        """视频目录.

        Returns:
            Path: ``root/video``; 可为指向外部存储的符号链接.
        """
        return self.root / VIDEO_DIRNAME

    @property
    def crops_dir(self) -> Path:
        """未命名选区的裁剪输出目录.

        Returns:
            Path: ``root/crops``.
        """
        return self.root / "crops"

    @property
    def crop_meta_dir(self) -> Path:
        """每图裁剪输出记录目录.

        Returns:
            Path: ``root/.crops``.
        """
        return self.root / CROP_META_DIRNAME

    @property
    def trash_dir(self) -> Path:
        """回收站目录.

        Returns:
            Path: ``root/.trash``.
        """
        return self.root / TRASH_DIRNAME

    def work_dirs(self) -> list[WorkDir]:
        """列出含图片的一级工作子目录, ``main`` 置顶.

        Returns:
            list[WorkDir]: 工作目录列表; 根目录自身不作为工作目录列出.

        Callers:
            - ``routes/images.py:list_dirs``
            - ``routes/caption.py:dirs``
            - ``routes/ready.py:dirs``
            - ``routes/dedupe.py:dirs``
            - ``routes/tag_stats.py:dirs``
            - ``routes/tagging.py:dirs``
            - ``routes/scoring.py:dirs``
            - ``services/state/archiver.py:archive_task``
            - ``routes/tasks.py:list_tasks``
        """
        out: list[WorkDir] = []
        for p in sorted(self.root.iterdir(), key=lambda p: natural_key(p.name)):
            if not p.is_dir() or p.name.startswith(".") or p.name in NON_BROWSABLE_DIRS:
                continue
            if p.name == "main" or count_images(p):
                out.append(WorkDir(self, p.name))
        if not any(w.name == "main" for w in out) and self.main_dir.is_dir():
            out.append(WorkDir(self, "main"))
        out.sort(key=lambda w: (w.name != "main", natural_key(w.name)))
        return out

    def work_dir(self, name: str | None) -> WorkDir:
        """解析一级工作子目录名, 空则 ``main``.

        Args:
            name (str | None): 目录名; 空或 ``.`` 视作 ``main``.

        Returns:
            WorkDir: 对应工作目录对象.

        Raises:
            ValueError: 名称非法或目录不存在.

        Callers:
            - ``routes/online_sources.py:search`` / `download` / `undownload` /
              `batch_start` / `regenerate_captions` / `refresh_metadata`
            - ``routes/images.py:similar_images``
            - ``routes/video.py:_services``
        """
        raw = (name or "main").strip().replace("\\", "/").strip("/")
        if not raw or raw == ".":
            raw = "main"
        if "/" in raw or raw.startswith(".") or raw in NON_BROWSABLE_DIRS:
            raise ValueError(f"非法目录: {name}")
        work = WorkDir(self, raw)
        if not work.path.is_dir():
            raise ValueError(f"目录不存在: {raw}")
        return work

    def resolve(self, rel: str) -> Path:
        """把数据集根相对路径解析为绝对路径.

        Args:
            rel (str): 相对数据集根的路径, 如 ``main/foo.png``.

        Returns:
            Path: 规范化绝对路径.

        Raises:
            ValueError: 路径为空、绝对路径或越出数据集根.

        Callers:
            - ``routes/deps.py:resolve_path``
        """
        from services.dataset.workdir import resolve_under

        return resolve_under(self.root, rel)

    def work_of(self, rel: str) -> WorkDir:
        """返回数据集根相对路径所属的工作目录.

        Args:
            rel (str): 相对数据集根的路径, 如 ``main/foo.png``.

        Returns:
            WorkDir: 路径首段对应的工作目录; 路径不含子目录时为数据集根
                ``WorkDir(self, ".")``.
        """
        parts = Path(rel.replace("\\", "/")).parts
        return WorkDir(self, parts[0] if len(parts) > 1 else ".")

    def select(self, names: list[str] | None) -> list[WorkDir]:
        """把选中目录名解析为工作目录对象; 未知名跳过.

        Args:
            names (list[str] | None): 目录名列表.

        Returns:
            list[WorkDir]: 命中的工作目录; 名称为空时返回空列表.

        Callers:
            - ``routes/caption.py:list_images`` / `start`
            - ``routes/tag_stats.py:stats`` / `clean` / `batch_edit` / `check` /
              `embedding_query` / `concept_query`
            - ``routes/tagging.py:start``
            - ``routes/scoring.py:start``
        """
        available = {w.name: w for w in self.work_dirs()}
        out: list[WorkDir] = []
        for name in names or []:
            work = available.get((name or "").strip())
            if work is not None:
                out.append(work)
        return out

    def select_or_main(self, names: list[str] | None) -> list[WorkDir]:
        """按名取工作目录; 名称为空时回落到 ``main``.

        Args:
            names (list[str] | None): 目录名列表.

        Returns:
            list[WorkDir]: 命中的工作目录; 全未命中时为 ``[main]``.

        Callers:
            - ``routes/images.py:list_images``
            - ``routes/annotations.py:select_all`` / `get_state`
            - ``routes/dedupe.py:scan_duplicates`` / `apply_delete`
        """
        return self.select(names) or [self.work_dir("main")]

    def image_count(self) -> int:
        """统计全部工作目录内的图片总数.

        Returns:
            int: 各工作目录图片数之和.

        Callers:
            - ``services/state/archiver.py:archive_task``
            - ``routes/tasks.py:list_tasks``
        """
        return sum(count_images(w.path) for w in self.work_dirs())

    def part_dir(self, name: str) -> Path:
        """部位输出目录: root/{name}/.

        Args:
            name (str): 部位目录名.

        Returns:
            Path: 部位目录绝对路径.

        Callers:
            - ``routes/crop.py:_resolve_out_path``
            - ``routes/crop.py:save_crops``
        """
        return self.root / name

    def list_part_dirs(self) -> list[Path]:
        """列出部位输出目录.

        video 目录是切帧来源的工作目录而非部位输出目录, 不在列.

        Returns:
            list[Path]: 部位目录绝对路径列表(按名称排序).

        Callers:
            - ``routes/crop.py:get_config``
            - ``routes/crop.py:get_crop_state``
        """
        out = []
        for p in sorted(self.root.iterdir(), key=lambda p: natural_key(p.name)):
            if (
                p.is_dir()
                and p.name not in RESERVED_DIR_NAMES
                and p.name != VIDEO_DIRNAME
                and not p.name.startswith(".")
            ):
                out.append(p)
        return out


@dataclass
class DatasetRegistry:
    """按需缓存数据集对象.

    ``ensure`` 为协程且内部跨 ``await``, 用实例 ``asyncio.Lock`` 串行化,
    并发请求同一数据集时不会重复创建数据集对象与重复提交缩略图任务.

    Callers:
        - ``server.py:main`` (初始化挂载到 ``app.state.datasets``)
        - ``server.py:dataset_thumb`` 等路由经 ``ensure`` 按需加载
        - ``routes/tasks.py:`` 预览等经 ``ensure`` 访问
    """

    task_root: Path
    thumbnail_size: int = 1280
    job_manager: JobManager | None = None
    by_name: dict[str, Dataset] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def get(self, name: str) -> Dataset | None:
        """按名取已加载数据集.

        Args:
            name (str): 任务名.

        Returns:
            Dataset | None: 已加载的数据集对象.
        """
        return self.by_name.get(name)

    def names(self) -> list[str]:
        """已加载任务名列表.

        Returns:
            list[str]: 按自然序排列的任务名.
        """
        return sorted(self.by_name.keys(), key=natural_key)

    async def ensure(self, name: str) -> Dataset:
        """若尚未加载则打开 ``{task_root}/{name}`` 并缓存, 同时提交缩略图填充任务.

        声明为协程: 内部经 ``_submit_thumb_fill`` → ``JobManager.submit`` 提交
        后台任务, 后者要求事件循环上下文. 整个"检查-创建-提交"临界区由
        实例 ``asyncio.Lock`` 串行化, 并发调用只创建一次.

        Args:
            name (str): 任务名.

        Returns:
            Dataset: 数据集对象.

        Callers:
            - ``server.py:bind_dataset_middleware``
            - ``server.py:dataset_thumb``
            - ``routes/scoring.py:results``
            - ``routes/images.py:similar_images``
            - ``routes/tasks.py:list_tasks`` / `add_task`
            - ``routes/online_sources.py:download``
        """
        async with self._lock:
            existing = self.by_name.get(name)
            if existing is not None:
                return existing
            dataset = Dataset.open(name, self.task_root / name, self.thumbnail_size)
            self.by_name[name] = dataset
            if self.job_manager is not None:
                await _submit_thumb_fill(self.job_manager, name, dataset.thumbnailer)
            return dataset


async def _submit_thumb_fill(jobs: JobManager, dataset: str, thumb: Thumbnailer) -> None:
    """通过 JobManager 提交后台缩略图填充任务.

    Args:
        jobs (JobManager): 全局长任务管理器.
        dataset (str): 任务名.
        thumb (Thumbnailer): 缩略图管理器.

    Callers:
        - ``services/dataset/runtime.py:DatasetRegistry.ensure``
    """
    from services.models.jobs import JobSnapshot as Snap

    def run(emit: Callable[[Snap], None]) -> None:
        """工作线程: 统计并补齐缺失缩略图.

        Args:
            emit (Callable[[Snap], None]): 进度发射回调.
        """
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
