"""就绪：导出训练 datasets TOML，并返回目录与蒙版统计。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime
from services.models import ReadyDirsResponse, ReadyTomlResult
from services.ready_toml import build_ready_toml, main_mask_stats
from services.work_dir import list_work_dirs

router = APIRouter(prefix="/ready", tags=["ready"])


class ReadyBody(BaseModel):
    """就绪 TOML 生成请求。引用：``POST /ready/toml``。"""

    dirs: list[str] | None = None
    num_repeats: int = Field(default=1, ge=1, le=100)


@router.get("/dirs")
def ready_dirs(request: Request) -> ReadyDirsResponse:
    """
    可导出工作目录与 main 蒙版统计。

    蒙版状态仅「无」或「完整」。引用：ready.js。
    """
    root = get_runtime(request).root
    mask = main_mask_stats(root)
    return ReadyDirsResponse(
        root=str(root),
        dirs=list_work_dirs(root),
        mask_status=mask.mask_status,
        mask_complete=mask.mask_complete,
        mask_none=mask.mask_none,
        main_count=mask.main_count,
    )


@router.post("/toml")
def ready_toml(request: Request, body: ReadyBody) -> ReadyTomlResult:
    """
    生成 TOML 预览文本。引用：ready.js。
    """
    root = get_runtime(request).root
    result = build_ready_toml(root, dirs=body.dirs, num_repeats=body.num_repeats)
    if not result.subsets:
        raise HTTPException(400, "没有可导出的图片目录")
    return result
