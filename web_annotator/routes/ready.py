"""就绪：导出训练 datasets TOML。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime
from services.ready_toml import build_ready_toml
from services.work_dir import list_work_dirs

router = APIRouter(prefix="/ready", tags=["ready"])


class ReadyBody(BaseModel):
    dirs: list[str] | None = None
    num_repeats: int = Field(default=1, ge=1, le=100)


@router.get("/dirs")
def ready_dirs(request: Request):
    root = get_runtime(request).root
    return {"root": str(root), "dirs": list_work_dirs(root)}


@router.post("/toml")
def ready_toml(request: Request, body: ReadyBody):
    root = get_runtime(request).root
    result = build_ready_toml(root, dirs=body.dirs, num_repeats=body.num_repeats)
    if not result["subsets"]:
        raise HTTPException(400, "没有可导出的图片目录")
    return result
