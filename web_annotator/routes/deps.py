"""
请求级数据集解析。

引用：各 routes/* 通过 get_runtime 读取当前数据集服务。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from services.dataset_runtime import DatasetRegistry, DatasetRuntime
from services.jobs import JobManager


def registry(request: Request) -> DatasetRegistry:
    """返回进程内数据集注册表。引用：routes/datasets.py、中间件。"""
    return request.app.state.datasets


def jobs(request: Request) -> JobManager:
    """返回全局长任务管理器。引用：routes/jobs.py 与各长任务路由。"""
    return request.app.state.jobs


def get_runtime(request: Request) -> DatasetRuntime:
    """
    返回当前请求已绑定的数据集运行时。

    由 server 中间件根据 ``/api/d/{dataset}/...`` 或 ``/d/{dataset}/...`` 写入 request.state。
    引用：routes 下所有需访问数据集文件的接口。
    """
    rt = getattr(request.state, "runtime", None)
    if rt is None:
        raise HTTPException(500, "请求未绑定数据集")
    return rt
