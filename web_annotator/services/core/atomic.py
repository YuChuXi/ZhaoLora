"""并发安全写入原语.

所有持久化写入统一经由本模块: ``atomic_write_*`` 先写入同目录唯一临时文件,
再以 ``os.replace`` 原子替换目标文件, 目标文件要么保持旧内容、要么是完整新内容,
不会出现写了一半或并发交错拼接的损坏文件; ``locked`` 装饰器把有状态的
读-改-写方法用实例锁串行化, 消除并发请求间的丢失更新.

Callers:
    - ``services/state/annotator.py:``
    - ``services/state/tasks.py:``
    - ``services/state/mask_templates.py:``
    - ``services/analysis/sort_cache.py:``
    - ``services/analysis/wd14.py:``
    - ``services/tagging/clean.py:``
    - ``services/tagging/stats.py:``
    - ``services/core/video_frame.py:``
    - ``services/core/video_scanner.py:``
    - ``services/online_sources/framework/base.py:``
    - ``services/online_sources/framework/media.py:``
    - ``services/online_sources/danbooru/source.py:``
    - ``services/online_sources/gelbooru/source.py:``
    - ``routes/tag_stats.py:``
    - ``routes/ready.py:``
    - ``routes/crop.py:``
"""

from __future__ import annotations

import functools
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

_T = TypeVar("_T")


def locked(fn: Callable[..., _T]) -> Callable[..., _T]:
    """把实例方法用 ``self._lock`` 串行化.

    适用对象: 持有 ``_lock`` 属性且方法体为"读-改-写"临界区的类
    (如状态与清单存储类), 消除并发请求交错导致的丢失更新.

    Args:
        fn (Callable[..., _T]): 需串行化的实例方法.

    Returns:
        Callable[..., _T]: 包装后的方法; 行为与原方法一致, 仅执行被实例锁串行化.

    Callers:
        - ``services/state/annotator.py:StateManager``
        - ``services/state/tasks.py:TaskStore``
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


def atomic_write_text(path: Path, text: str) -> None:
    """把文本写入目标文件 (临时文件 + 原子替换).

    Args:
        path (Path): 目标文件路径, 所在目录必须已存在.
        text (str): 待写入的完整文本.

    Raises:
        OSError: 写入或替换失败; 失败时目标文件保持原内容, 半截临时文件被清理.

    Callers:
        - ``services/state/annotator.py:StateManager._write``
        - ``services/state/tasks.py:TaskStore._write``
        - ``services/state/mask_templates.py:``
        - ``services/analysis/sort_cache.py:``
        - ``services/analysis/wd14.py:``
        - ``services/tagging/clean.py:``
        - ``services/tagging/stats.py:``
        - ``services/core/video_frame.py:VideoFrameStore._write_bitstream``
        - ``services/online_sources/framework/base.py:SourceClient._write_meta``
        - ``services/online_sources/framework/media.py:``
        - ``services/online_sources/danbooru/source.py:``
        - ``services/online_sources/gelbooru/source.py:``
        - ``routes/tag_stats.py:``
        - ``routes/ready.py:``
        - ``routes/crop.py:_record_saved``
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """把二进制数据写入目标文件 (临时文件 + 原子替换).

    Args:
        path (Path): 目标文件路径, 所在目录必须已存在.
        data (bytes): 待写入的完整二进制内容.

    Raises:
        OSError: 写入或替换失败; 失败时目标文件保持原内容, 半截临时文件被清理.

    Callers:
        - ``services/core/video_frame.py:VideoFrameStore.save_frame``
        - ``services/core/video_scanner.py:``
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
