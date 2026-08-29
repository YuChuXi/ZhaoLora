"""就绪: 导出训练 datasets TOML, 并返回目录与蒙版统计. """

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime
from services.models import ReadyDirsResponse, ReadyTomlResult, TrainExportResult
from services.ready import build_ready_toml, build_train_config, subdir_mask_stats
from services.core.work_dir import list_work_dirs

logger = logging.getLogger(__name__)

# web_annotator 应用根目录(server.py 所在目录), 用于把相对的 LORA_CONFIG_ROOT 锚定到项目内
_APP_DIR = Path(__file__).resolve().parents[1]

router = APIRouter(prefix="/ready", tags=["ready"])


class ReadyBody(BaseModel):
    """就绪 TOML 生成请求. 引用: ``POST /ready/toml`` / ``POST /ready/export``. """

    dirs: list[str] | None = None
    target_images: int = Field(default=512, ge=1, description="目标图片数, 据此计算各子集 num_repeats 使重复后图片数 >= 目标折算份额")
    subset_resolutions: dict[str, list[int]] = Field(
        default_factory=dict,
        description="各子集的分辨率列表(目录名→列表); 未指定或为空的子集生成单块无 resolution",
    )
    use_reg: bool = Field(default=True, description="是否在导出的 dataset.toml 中附带固定正则化数据段")


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
        subset_resolutions=body.subset_resolutions,
    )
    if not result.subsets:
        raise HTTPException(400, "没有可导出的图片目录")
    logger.info("数据集 %s 生成就绪 TOML: %d 个子集, 目标图片数 %d, 子集分辨率 %s",
                root.name, len(result.subsets), body.target_images,
                body.subset_resolutions or "默认")
    return result


@router.post("/export", response_model=TrainExportResult)
def export(
    request: Request, body: ReadyBody, overwrite: bool = False
) -> TrainExportResult:
    """按代码内置的 anima 固定配置导出整套训练配置到 ``{LORA_CONFIG_ROOT}/{数据集名}/``.

    LORA_CONFIG_ROOT 由 .env 配置; 相对路径按 web_annotator/ 目录解析.
    数据集 main 段由就绪页参数生成, 固定训练段(含回归数据/模型路径/超参)由代码内置;
    prompt.txt 写入该数据集触发词(note 页设置, 未设置则省略)与出现次数最多的 10 个标签.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (ReadyBody): 就绪页导出参数(dirs / target_images / subset_resolutions / use_reg).
        overwrite (bool): 目标目录已存在可视化配置时, 需置 True 才覆盖写入, 否则返回 409.

    Returns:
        TrainExportResult: 实际写入的配置文件文件名列表(不返回绝对路径).

    Raises:
        HTTPException: 无可导出的图片目录、未配置 LORA_CONFIG_ROOT、目标目录已存在配置且未确认覆盖时抛错.

    Callers:
        - ``static/js/ready.js:exportConfig``
    """
    root = get_runtime(request).root
    preview = build_ready_toml(
        root, dirs=body.dirs, target_images=body.target_images,
        subset_resolutions=body.subset_resolutions,
    )
    if not preview.subsets:
        raise HTTPException(400, "没有可导出的图片目录")
    store = request.app.state.task_store
    trigger = next(
        (t.trigger_word for t in store.list_entries() if t.name == root.name), ""
    )
    config_root = os.environ.get("LORA_CONFIG_ROOT", "").strip()
    if not config_root:
        raise HTTPException(400, "未配置 LORA_CONFIG_ROOT(见 .env.example)")
    base = Path(config_root).expanduser()
    base = base if base.is_absolute() else (_APP_DIR / base).resolve()
    out_dir = base / root.name
    if not overwrite and out_dir.exists() and any(out_dir.iterdir()):
        raise HTTPException(
            409, f"目标目录已存在配置({out_dir}), 确认覆盖请重试"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    files = build_train_config(
        root,
        out_dir=out_dir,
        dirs=body.dirs,
        target_images=body.target_images,
        subset_resolutions=body.subset_resolutions,
        use_reg=body.use_reg,
        trigger_word=trigger,
    )
    written: list[str] = []
    for name, content in files.items():
        p = out_dir / name
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
        written.append(name)
    logger.info("数据集 %s 导出训练配置到 %s: 写入 %d 个文件 (%s)",
                root.name, out_dir, len(written), ", ".join(written))
    return TrainExportResult(files=written)
