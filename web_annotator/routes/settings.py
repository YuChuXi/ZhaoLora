"""前端设置存取: 全局缺省层与数据集覆盖层.

设置分两层持久化于 ``.annotator_settings.json``: 全局缺省层在任务根
``datasets/`` 下, 存放跨任务通用的输入参数习惯; 数据集层在
``datasets/xxx/`` 下, 存放任务特有的参数(目录选择、描述规则、裁剪部位).
读取返回两层按键合并的结果, 数据集键覆盖全局同键; 写入按键合并、同键
整体替换, 详见 ``services/dataset/settings.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from routes.deps import get_runtime
from services.dataset.settings import load_settings, update_settings

router = APIRouter(tags=["settings"])
global_router = APIRouter(tags=["settings"])


@router.get("/settings")
async def read_settings(request: Request) -> dict:
    """返回当前数据集的设置全量(全局缺省层与数据集层按键合并).

    两层在独立线程先后读取后按键合并, 数据集键覆盖全局同键; caption/crop
    仅存于数据集层, 其余键通常仅存于全局层, 合并结果即各键的生效值.

    Returns:
        dict: ``{"settings": {键: 任意 JSON 值}}``; 两层均无记录时为空映射.

    Callers:
        - ``static/js/settings.js:ready`` : 页面启动时加载.
    """
    rt = get_runtime(request)
    reg = request.app.state.datasets

    def merged() -> dict:
        data = load_settings(reg.task_root)
        data.update(load_settings(rt.root))
        return data

    return {"settings": await asyncio.to_thread(merged)}


@router.put("/settings")
async def write_settings(request: Request, patch: dict[str, Any]) -> dict:
    """按键合并写入数据集层设置, 返回数据集层合并后的全量.

    Args:
        request: 当前 HTTP 请求, 经中间件绑定数据集运行时.
        patch (dict[str, Any]): 键到任意 JSON 值的映射, 同键整体替换.

    Returns:
        dict: ``{"settings": 数据集层合并后的全量}``.

    Raises:
        HTTPException: 设置键非法(500 转发自 ValueError)或未绑定数据集.

    Callers:
        - ``static/js/settings.js:set`` : 保存目录选择、描述规则、裁剪部位.
    """
    rt = get_runtime(request)
    async with rt.settings_lock:
        data = await asyncio.to_thread(update_settings, rt.root, patch)
    return {"settings": data}


@global_router.get("/api/settings")
async def read_global_settings(request: Request) -> dict:
    """返回全局缺省层的设置全量.

    Returns:
        dict: ``{"settings": {键: 任意 JSON 值}}``; 无记录时为空映射.

    Callers:
        - ``static/js/index.js:importLegacy`` : 导入旧设置前读取现值合并.
    """
    reg = request.app.state.datasets
    return {"settings": await asyncio.to_thread(load_settings, reg.task_root)}


@global_router.put("/api/settings")
async def write_global_settings(request: Request, patch: dict[str, Any]) -> dict:
    """按键合并写入全局缺省层设置, 返回全局层合并后的全量.

    Args:
        request: 当前 HTTP 请求.
        patch (dict[str, Any]): 键到任意 JSON 值的映射, 同键整体替换.

    Returns:
        dict: ``{"settings": 全局层合并后的全量}``.

    Raises:
        HTTPException: 设置键非法(500 转发自 ValueError).

    Callers:
        - ``static/js/settings.js:set`` : 保存各页输入参数习惯.
        - ``static/js/index.js:importLegacy`` : 导入旧版全局参数.
    """
    reg = request.app.state.datasets
    async with request.app.state.settings_lock:
        data = await asyncio.to_thread(update_settings, reg.task_root, patch)
    return {"settings": data}
