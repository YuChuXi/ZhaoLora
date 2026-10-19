"""数据集级前端设置存取.

各业务页的输入参数按数据集持久化于 ``.annotator_settings.json``,
详见 ``services/dataset/settings.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from routes.deps import get_runtime
from services.dataset.settings import load_settings, update_settings

router = APIRouter(tags=["settings"])


@router.get("/settings")
async def read_settings(request: Request) -> dict:
    """返回当前数据集的设置全量.

    Returns:
        dict: ``{"settings": {键: 任意 JSON 值}}``; 无任何保存记录时为空映射.

    Callers:
        - ``static/js/settings.js:ready`` : 页面启动时加载.
    """
    rt = get_runtime(request)
    return {"settings": await asyncio.to_thread(load_settings, rt.root)}


@router.put("/settings")
async def write_settings(request: Request, patch: dict[str, Any]) -> dict:
    """按键合并写入设置, 返回合并后的全量.

    Args:
        request: 当前 HTTP 请求, 经中间件绑定数据集运行时.
        patch (dict[str, Any]): 键到任意 JSON 值的映射, 同键整体替换.

    Returns:
        dict: ``{"settings": 合并后的全量}``.

    Raises:
        HTTPException: 设置键非法(500 转发自 ValueError)或未绑定数据集.

    Callers:
        - ``static/js/settings.js:set`` : 各页面保存输入参数.
    """
    rt = get_runtime(request)
    async with rt.settings_lock:
        data = await asyncio.to_thread(update_settings, rt.root, patch)
    return {"settings": data}
