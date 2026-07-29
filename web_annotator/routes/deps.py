"""
请求级数据集解析. 

引用: 各 routes/* 通过 get_runtime 读取当前数据集服务. 
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from services.dataset.runtime import DatasetRegistry, DatasetRuntime
from services.core.jobs import JobManager
from services.core.work_dir import resolve_under


def registry(request: Request) -> DatasetRegistry:
    """返回进程内数据集注册表. 

    引用:
        中间件与任务相关路由. 
    """
    return request.app.state.datasets


def jobs(request: Request) -> JobManager:
    """返回全局长任务管理器. 

    引用:
        ``routes.meta`` 与各长任务路由. 
    """
    return request.app.state.jobs


def get_runtime(request: Request) -> DatasetRuntime:
    """返回当前请求已绑定的数据集运行时. 

    由 server 中间件根据 ``/api/d/{dataset}/...`` 或 ``/d/{dataset}/...``
    写入 ``request.state.runtime``. 

    引用:
        需访问数据集文件的全部路由. 
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

    引用:
        ``routes.alpha_paint`` / ``crop`` / ``annotations`` / ``images``. 
    """
    try:
        return resolve_under(root, rel)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
