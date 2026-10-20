"""
请求级数据集解析. 

Callers:
    - ``routes/__init__.py:`` : 各路由模块经此获取数据集服务.
"""

from __future__ import annotations

from pathlib import Path

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from services.dataset.runtime import Dataset, DatasetRegistry
from services.core.jobs import JobManager
from services.dataset.workdir import resolve_under


def current_dataset(request: Request) -> Dataset:
    """返回当前请求绑定的数据集, 作为路由依赖注入.

    Args:
        request (Request): FastAPI 请求对象.

    Returns:
        Dataset: 当前数据集对象.

    Raises:
        HTTPException: 请求未经数据集路由绑定时返回 500.
    """
    rt = getattr(request.state, "runtime", None)
    if rt is None:
        raise HTTPException(500, "请求未绑定数据集")
    return rt


DatasetDep = Annotated[Dataset, Depends(current_dataset)]


def current_registry(request: Request) -> DatasetRegistry:
    """返回进程内数据集注册表, 作为全站路由的依赖注入.

    Args:
        request (Request): FastAPI 请求对象.

    Returns:
        DatasetRegistry: 数据集注册表.
    """
    return request.app.state.datasets


RegistryDep = Annotated[DatasetRegistry, Depends(current_registry)]


def registry(request: Request) -> DatasetRegistry:
    """返回进程内数据集注册表. 

    Callers:
        - ``routes/tasks.py:list_tasks``
        - ``routes/tasks.py:add_task``
        - ``routes/tasks.py:archive_task_route``
    """
    return request.app.state.datasets


def jobs(request: Request) -> JobManager:
    """返回全局长任务管理器. 

    Callers:
        - ``routes/meta.py:find_active_job``
        - ``routes/meta.py:job_events``
        - ``routes/meta.py:job_cancel``
        - ``routes/scoring.py:start``
        - ``routes/tagging.py:start``
        - ``routes/dedupe.py:scan_duplicates``
        - ``routes/tag_stats.py:clean``
        - ``routes/tag_stats.py:batch_edit``
        - ``routes/tag_stats.py:embedding_query``
        - ``routes/tag_stats.py:concept_query``
        - ``routes/online_sources.py:_submit_video_download``
        - ``routes/online_sources.py:batch_start``
        - ``routes/online_sources.py:batch_params``
        - ``routes/online_sources.py:refresh_metadata``
        - ``routes/birefnet.py:mask_generate``
        - ``routes/tasks.py:archive_task_route``
        - ``routes/tasks.py:restore_task_route``
        - ``routes/video.py:start_analysis``
    """
    return request.app.state.jobs


def parse_dir_names(dirs: str | None = None, dir: str | None = None) -> list[str]:
    """从查询参数 ``dirs=a,b`` 或 ``dir=a`` 解析目录名列表.

    Args:
        dirs (str | None): 逗号分隔的目录名, 优先.
        dir (str | None): 单个目录名, ``dirs`` 为空时使用.

    Returns:
        list[str]: 目录名列表; 均未提供时返回 ``["main"]``.

    Callers:
        - ``routes/images.py:list_images``
        - ``routes/annotations.py:select_all`` / `get_state`
    """
    if dirs and dirs.strip():
        return [x.strip() for x in dirs.split(",") if x.strip()]
    if dir and dir.strip():
        return [dir.strip()]
    return ["main"]


def resolve_path(root: Path, rel: str) -> Path:
    """在 ``root`` 下解析相对路径；失败时返回 HTTP 400. 

    Args:
        root: 允许访问的根目录. 
        rel: 相对路径. 

    Returns:
        规范化绝对路径. 

    Callers:
        - ``routes/alpha_paint.py:get_existing_mask``
        - ``routes/alpha_paint.py:serve_mask_preview``
        - ``routes/alpha_paint.py:serve_original_image``
        - ``routes/alpha_paint.py:upload_mask``
        - ``routes/annotations.py:_rel_under``
        - ``routes/birefnet.py:mask_generate``
        - ``routes/crop.py:save_crops``
        - ``routes/crop.py:serve_original_image``
        - ``routes/images.py:_resolve_image``
        - ``routes/tag_stats.py:delete_one``
        - ``routes/tag_stats.py:edit_one``
        - ``routes/tag_stats.py:embedding_batch_tag``
        - ``routes/video.py:delete_frame``
        - ``routes/video.py:save_frame``
        - ``routes/video.py:start_analysis``
        - ``routes/video.py:video_cover``
        - ``routes/video.py:video_detail``
        - ``routes/video.py:video_frame``
        - ``routes/video.py:video_segment``
    """
    try:
        return resolve_under(root, rel)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
