"""任务归档: py7zr 极限压缩, 校验完整性, 清理缓存.

归档流程: 选取预览 → 清理 .thumbnails/ 和 *.npz → 压缩 → 校验 → 删除任务目录.
解压流程: 解压归档到任务根目录.
"""

from __future__ import annotations

import random
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import py7zr
from py7zr.callbacks import ExtractCallback

from services.core.natural import natural_key
from services.core.scanner import scan_images
from services.core.thumbnailer import Thumbnailer
from services.core.work_dir import total_image_count
from services.dataset.layout import PACKED_DIR
from services.models import JobSnapshot
from services.models.archive import ArchivedTask

# LZMA2 极限压缩 (preset 9 = ultra), 固实归档
_FILTERS = [{"id": py7zr.FILTER_LZMA2, "preset": 9}]


class _ExtractReporter(ExtractCallback):
    """解压进度回调: 逐文件计数并发布 JobSnapshot.

    Callers:
        - ``services/state/archiver.py:restore_task``
    """

    def __init__(self, total: int, dataset: str, emit: Callable[[JobSnapshot], None]) -> None:
        self._total = total
        self._dataset = dataset
        self._emit = emit
        self._done = 0

    def report_start_preparation(self) -> None:
        pass

    def report_start(self, processing_file_path: str, processing_bytes: str) -> None:
        pass

    def report_update(self, decompressed_bytes: str) -> None:
        pass

    def report_end(self, processing_file_path: str, wrote_bytes: str) -> None:
        self._done += 1
        self._emit(JobSnapshot(
            status="running", kind="archive", dataset=self._dataset,
            phase="extract", done=self._done, total=self._total,
            pct=round(self._done / self._total * 100, 1) if self._total else None,
            current=processing_file_path,
        ))

    def report_warning(self, message: str) -> None:
        pass

    def report_postprocess(self) -> None:
        pass


def _archive_base(name: str) -> str:
    """生成归档基本名: ``name-YYYYMMDD_HHMMSS``.

    Callers:
        - ``services/state/archiver.py:archive_task``
    """
    return f"{name}-{time.strftime('%Y%m%d_%H%M%S')}"


def _list_files(root: Path) -> list[Path]:
    """递归列出目录下所有文件(不含目录).

    Callers:
        - ``services/state/archiver.py:archive_task``
    """
    return sorted(
        (p for p in root.rglob("*") if p.is_file()),
        key=lambda p: natural_key(p.relative_to(root).as_posix()),
    )


def _should_exclude(p: Path, task_dir: Path) -> bool:
    """判断文件是否应排除: .thumbnails/ 下或 .npz 后缀.

    Args:
        p: 文件绝对路径.
        task_dir: 任务目录绝对路径.

    Returns:
        bool: True 表示排除.

    Callers:
        - ``services/state/archiver.py:archive_task``
    """
    parts = p.relative_to(task_dir).parts
    if ".thumbnails" in parts:
        return True
    return p.suffix in (".npz", ".npy")


def archive_task(
    task_dir: Path,
    name: str,
    task_root: Path,
    thumbnailer: Thumbnailer,
    emit: Callable[[JobSnapshot], None],
) -> ArchivedTask:
    """归档任务: 选取预览 → 清理缓存 → 压缩 → 校验 → 删除目录.

    Args:
        task_dir: 任务目录绝对路径.
        name: 任务名称.
        task_root: 任务根目录(所有任务的父目录).
        thumbnailer: 缩略图管理器(用于选取预览).
        emit: JobManager 进度回调.

    Returns:
        ArchivedTask: 归档元数据.

    Raises:
        RuntimeError: testzip 返回坏文件.

    Callers:
        - ``routes/tasks.py:archive_task_route``
    """
    packed_dir = task_root / PACKED_DIR
    packed_dir.mkdir(parents=True, exist_ok=True)
    base = _archive_base(name)
    archive_path = packed_dir / f"{base}.7z"

    # 1. 选取预览缩略图(清理前, 确保文件存在)
    images = scan_images(task_dir / "main", path_prefix="main")
    picks = images if len(images) <= 4 else random.sample(images, 4)
    preview_hashes: list[str] = []
    preview_dir = packed_dir / f"{base}.previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    for img in picks:
        h = thumbnailer.hash_for(img.path)
        thumb_path = thumbnailer.thumb_dir / f"{h}.avif"
        if thumb_path.is_file():
            shutil.copy2(thumb_path, preview_dir / f"{h}.avif")
            preview_hashes.append(h)

    # 2. 压缩: 排除 .thumbnails/ 和 *.npz, 逐文件 write() 过滤
    #    (py7zr writeall 不支持排除; LZMA2 压缩在 close 时发生, 无回调)
    files = [p for p in _list_files(task_dir) if not _should_exclude(p, task_dir)]
    total = len(files)
    emit(JobSnapshot(
        status="running", kind="archive", dataset=name,
        phase="compress", done=0, total=total, message="压缩中…",
    ))
    try:
        with py7zr.SevenZipFile(archive_path, "w", filters=_FILTERS) as archive:
            for p in files:
                arcname = str(p.relative_to(task_dir.parent))
                archive.write(p, arcname)

        # 3. 校验: testzip 完整解压校验 CRC
        emit(JobSnapshot(
            status="running", kind="archive", dataset=name,
            phase="verify", done=0, total=total, message="校验归档…",
        ))
        with py7zr.SevenZipFile(archive_path, "r") as archive:
            bad = archive.testzip()
            if bad is not None:
                raise RuntimeError(f"校验失败: {bad}")
    except Exception:
        archive_path.unlink(missing_ok=True)
        if preview_dir.is_dir():
            shutil.rmtree(preview_dir)
        raise
    emit(JobSnapshot(
        status="running", kind="archive", dataset=name,
        phase="verify", done=total, total=total,
        pct=100.0, message="校验通过",
    ))

    # 4. 统计图片总数, 删除任务目录(含 .thumbnails/ 和 *.npz, 校验通过后一次性清理)
    emit(JobSnapshot(
        status="running", kind="archive", dataset=name,
        phase="cleanup", message="清理目录…",
    ))
    image_count = total_image_count(task_dir)
    shutil.rmtree(task_dir)

    # 5. 返回归档结果
    meta = ArchivedTask(
        base=base,
        name=name,
        archived_at=int(time.time()),
        preview_hashes=preview_hashes,
        image_count=image_count,
        format="7z",
    )

    emit(JobSnapshot(
        status="done", kind="archive", dataset=name,
        message="归档完成",
    ))
    return meta


def restore_task(
    archive_path: Path,
    task_root: Path,
    emit: Callable[[JobSnapshot], None],
) -> None:
    """解压归档到任务根目录.

    Args:
        archive_path: 归档文件绝对路径.
        task_root: 任务根目录(解压目标).
        emit: JobManager 进度回调.

    Callers:
        - ``routes/tasks.py:restore_task_route``
    """
    emit(JobSnapshot(
        status="running", kind="archive", dataset="",
        phase="extract", done=0, total=0, message="解压中…",
    ))
    with py7zr.SevenZipFile(archive_path, "r") as archive:
        names = archive.namelist()
        total = len(names)
        archive.extractall(path=task_root, callback=_ExtractReporter(total, "", emit))

    emit(JobSnapshot(
        status="done", kind="archive", dataset="",
        message="解压完成",
    ))
