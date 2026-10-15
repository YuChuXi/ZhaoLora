"""
请求级数据集解析. 

Callers:
    - ``routes/__init__.py:`` : 各路由模块经此获取数据集服务.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from services.dataset.runtime import DatasetRegistry, DatasetRuntime
from services.core.jobs import JobManager
from services.core.work_dir import resolve_under


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


def get_runtime(request: Request) -> DatasetRuntime:
    """返回当前请求已绑定的数据集运行时. 

    由 server 中间件根据 ``/api/d/{dataset}/...`` 或 ``/d/{dataset}/...``
    写入 ``request.state.runtime``. 

    Callers:
        - ``routes/alpha_paint.py:_services``
        - ``routes/alpha_paint.py:list_paint_images``
        - ``routes/alpha_paint.py:serve_mask_preview``
        - ``routes/annotations.py:_root``
        - ``routes/annotations.py:_state``
        - ``routes/annotations.py:apply_delete``
        - ``routes/annotations.py:restore``
        - ``routes/birefnet.py:mask_generate``
        - ``routes/crop.py:_layout``
        - ``routes/crop.py:_main_dir``
        - ``routes/crop.py:list_crop_images``
        - ``routes/dedupe.py:apply_delete``
        - ``routes/dedupe.py:dirs``
        - ``routes/dedupe.py:scan_duplicates``
        - ``routes/images.py:_color_index``
        - ``routes/images.py:_services``
        - ``routes/images.py:list_dirs``
        - ``routes/online_sources.py:_submit_video_download``
        - ``routes/online_sources.py:batch_params``
        - ``routes/online_sources.py:batch_start``
        - ``routes/online_sources.py:download``
        - ``routes/online_sources.py:refresh_metadata``
        - ``routes/online_sources.py:regenerate_captions``
        - ``routes/online_sources.py:search``
        - ``routes/online_sources.py:undownload``
        - ``routes/ready.py:dirs``
        - ``routes/ready.py:export``
        - ``routes/ready.py:toml``
        - ``routes/scoring.py:dirs``
        - ``routes/scoring.py:results``
        - ``routes/scoring.py:start``
        - ``routes/tag_stats.py:_root``
        - ``routes/tag_stats.py:batch_edit``
        - ``routes/tag_stats.py:check``
        - ``routes/tag_stats.py:clean``
        - ``routes/tag_stats.py:concept_query``
        - ``routes/tag_stats.py:delete_one``
        - ``routes/tag_stats.py:edit_one``
        - ``routes/tag_stats.py:embedding_batch_tag``
        - ``routes/tag_stats.py:embedding_history``
        - ``routes/tag_stats.py:embedding_query``
        - ``routes/tagging.py:dirs``
        - ``routes/tagging.py:start``
        - ``routes/video.py:_services``
        - ``routes/video.py:_stores``
        - ``routes/video.py:delete_frame``
        - ``routes/video.py:save_frame``
        - ``routes/video.py:start_analysis``
        - ``routes/video.py:video_cover``
        - ``routes/video.py:video_detail``
        - ``routes/video.py:video_frame``
        - ``routes/video.py:video_segment``
    """
    rt = getattr(request.state, "runtime", None)
    if rt is None:
        raise HTTPException(500, "请求未绑定数据集")
    return rt


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
