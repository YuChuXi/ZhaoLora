"""就绪: 导出训练 datasets TOML, 并返回目录与蒙版统计. """

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime
from services.models import ReadyDirsResponse, ReadyTomlResult
from services.ready import build_ready_toml, subdir_mask_stats
from services.core.work_dir import list_work_dirs

router = APIRouter(prefix="/ready", tags=["ready"])


class ReadyBody(BaseModel):
    """就绪 TOML 生成请求. 引用: ``POST /ready/toml``. """

    dirs: list[str] | None = None
    target_images: int = Field(default=512, ge=1, description="目标图片数, 据此计算 num_repeats 使重复后图片数 >= 目标")
    resolutions: list[int] = Field(default_factory=list, description="多分辨率, 如 [512, 1024, 1536]; 为空时生成单块无 resolution")


@router.get("/dirs")
def dirs(request: Request) -> ReadyDirsResponse:
    """
    可导出工作目录与各目录蒙版统计. 

    每目录蒙版状态仅「无」或「完整」. 引用: ready.js. 
    """
    root = get_runtime(request).root
    dirs = list_work_dirs(root)
    mask_stats = [subdir_mask_stats(root, d.name) for d in dirs]
    main_mask = next((m for m in mask_stats if m.name == "main"), None)
    return ReadyDirsResponse(
        root=str(root),
        dirs=dirs,
        mask_stats=mask_stats,
        mask_status=main_mask.mask_status if main_mask else "无",
        mask_complete=main_mask.mask_complete if main_mask else 0,
        mask_none=main_mask.mask_none if main_mask else 0,
        main_count=main_mask.main_count if main_mask else 0,
    )


@router.post("/toml")
def toml(request: Request, body: ReadyBody) -> ReadyTomlResult:
    """
    生成 TOML 预览文本. 引用: ready.js.
    """
    root = get_runtime(request).root
    result = build_ready_toml(
        root,
        dirs=body.dirs,
        target_images=body.target_images,
        resolutions=body.resolutions,
    )
    if not result.subsets:
        raise HTTPException(400, "没有可导出的图片目录")
    return result
