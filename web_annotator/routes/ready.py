"""就绪: 导出训练 datasets TOML, 并返回目录与蒙版统计. """

from __future__ import annotations

import logging
import os
from pathlib import Path

import tomlkit
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from tomlkit.exceptions import ParseError

from routes.deps import get_runtime
from services.models import LoraMode, ReadyDirsResponse, ReadyTomlResult, TrainExportResult, TrainFormat
from services.ready import (
    TRAIN_FORMAT_FILES,
    apply_lora_output_paths,
    build_ready_toml,
    build_train_config,
    subdir_mask_stats,
)
from services.core.work_dir import list_work_dirs
from services.core.atomic import atomic_write_text

logger = logging.getLogger(__name__)

# web_annotator 应用根目录(server.py 所在目录), 用于把相对的配置根环境变量锚定到项目内
_APP_DIR = Path(__file__).resolve().parents[1]

router = APIRouter(prefix="/ready", tags=["ready"])


class ReadyBody(BaseModel):
    """就绪页生成请求. 引用: ``POST /ready/toml``. """

    dirs: list[str] | None = None
    target_images: int = Field(default=512, ge=1, description="目标图片数, 据此计算各子集 num_repeats 使重复后图片数 >= 目标折算份额")
    subset_resolutions: dict[str, list[int]] = Field(
        default_factory=dict,
        description="各子集的分辨率列表(目录名→列表); 未指定或为空的子集生成单块无 resolution",
    )
    use_reg: bool = Field(default=True, description="是否在导出的 dataset.toml 中附带固定正则化数据段")
    mode: LoraMode = Field("style", description="训练模式: style=画风(network rank 16) / character=角色(network rank 32)")
    train_format: TrainFormat = Field("sd-scripts", description="配置导出格式: sd-scripts 三件套 / diffusion-pipe 两件套")


class ReadyExportBody(ReadyBody):
    """就绪页导出请求: 在 ReadyBody 基础上要求携带编辑后的整套配置文本.

    ``files`` 键必须恰为 ``TRAIN_FORMAT_FILES[train_format]``; 导出以编辑后的文本为准,
    仅 sd-scripts 格式下 lora.toml 的 output_name / sample_prompts / dataset_config
    三项路径由系统按真实导出目录覆写.
    引用: ``POST /ready/export``.
    """

    files: dict[str, str] = Field(description="编辑后的配置文本, 键为当前格式的配置文件名集合")

    @model_validator(mode="after")
    def _check_file_names(self) -> "ReadyExportBody":
        """校验 files 键集合恰为当前格式的配置文件名集合, 防止静默漏写或多写.

        Returns:
            ReadyExportBody: 校验通过的原模型实例.

        Raises:
            ValueError: files 键缺失或存在未知键.
        """
        if set(self.files) != set(TRAIN_FORMAT_FILES[self.train_format]):
            raise ValueError(f"files 键必须恰为 {list(TRAIN_FORMAT_FILES[self.train_format])}")
        return self


def _trigger_word(request: Request, root: Path) -> str:
    """查询数据集触发词(note 页任务卡设置).

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        root (Path): 数据集根目录, 以目录名匹配任务清单.

    Returns:
        str: 触发词; 未设置时为空串.

    Callers:
        - ``routes/ready.py:toml``
        - ``routes/ready.py:export``
    """
    store = request.app.state.task_store
    return next(
        (t.trigger_word for t in store.list_entries() if t.name == root.name), ""
    )


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


@router.post("/toml", response_model=ReadyTomlResult)
def toml(request: Request, body: ReadyBody) -> ReadyTomlResult:
    """生成训练配置的预览文本(文件集合随 train_format 变化).

    dataset.toml 与子集/蒙版统计由 ``build_ready_toml`` 渲染; 其余配置文件由
    ``build_train_config`` 按格式生成, 触发词与导出同源, 保证预览即所得.
    sd-scripts 预览的 lora.toml 三项路径字段以数据集根目录占位, 导出时按真实目录覆写.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (ReadyBody): 就绪页生成参数(dirs / target_images / subset_resolutions / use_reg / mode / train_format).

    Returns:
        ReadyTomlResult: 子集/蒙版统计与配置文件预览文本(files).

    Raises:
        HTTPException: 无可导出的图片目录时抛出.

    Callers:
        - ``static/js/ready.js:generate``
    """
    root = get_runtime(request).root
    try:
        result = build_ready_toml(
            root,
            dirs=body.dirs,
            target_images=body.target_images,
            subset_resolutions=body.subset_resolutions,
            use_reg=body.use_reg,
        )
        if not result.subsets:
            raise HTTPException(400, "没有可导出的图片目录")
        result.files, dp_repeats = build_train_config(
            root,
            mode=body.mode,
            train_format=body.train_format,
            dirs=body.dirs,
            target_images=body.target_images,
            subset_resolutions=body.subset_resolutions,
            use_reg=body.use_reg,
            trigger_word=_trigger_word(request, root),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    result.format = body.train_format
    if body.train_format == "diffusion-pipe":
        # diffusion-pipe 的 dataset.toml 以同 path 多块表达逐分辨率 num_repeats,
        # 与 sd-scripts 的块级分配同源, 覆盖 build_ready_toml 按其默认分辨率算出的值
        result.subset_repeats = dp_repeats
    logger.info("数据集 %s 生成就绪预览(%s / %s 模式): %d 个子集, 目标图片数 %d, 子集分辨率 %s",
                root.name, body.train_format, body.mode, len(result.subsets), body.target_images,
                body.subset_resolutions or "默认")
    return result


@router.post("/export", response_model=TrainExportResult)
def export(
    request: Request, body: ReadyExportBody, overwrite: bool = False
) -> TrainExportResult:
    """按编辑后的配置文本导出整套训练配置到 ``{格式对应配置根}/{数据集名}/``.

    导出根目录按格式取环境变量(sd-scripts → SD_SCRIPTS_CONFIG_ROOT,
    diffusion-pipe → DIFFUSION_PIPE_CONFIG_ROOT). files 的文本即最终写入内容
    (以编辑后为准), 仅 sd-scripts 格式下 lora.toml 的 output_name / sample_prompts /
    dataset_config 三项路径由系统按真实导出目录覆写; 全部 .toml 文件在写入前校验
    TOML 语法.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (ReadyExportBody): 就绪页导出参数(dirs / target_images / subset_resolutions / use_reg / mode / train_format / files).
        overwrite (bool): 目标目录已存在可视化配置时, 需置 True 才覆盖写入, 否则返回 409.

    Returns:
        TrainExportResult: 实际写入的配置文件文件名列表(不返回绝对路径).

    Raises:
        HTTPException: 无可导出的图片目录、编辑后的 TOML 语法错误、未配置格式对应的
            配置根环境变量、已开启正则化但未配置 REG_DATASETS、目标目录已存在配置且
            未确认覆盖时抛错.

    Callers:
        - ``static/js/ready.js:doExport``
    """
    root = get_runtime(request).root
    file_names = TRAIN_FORMAT_FILES[body.train_format]
    try:
        check = build_ready_toml(
            root, dirs=body.dirs, target_images=body.target_images,
            subset_resolutions=body.subset_resolutions,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not check.subsets:
        raise HTTPException(400, "没有可导出的图片目录")
    docs = {}
    for name in file_names:
        if not name.endswith(".toml"):
            continue
        try:
            docs[name] = tomlkit.parse(body.files[name])
        except ParseError as e:
            raise HTTPException(400, f"编辑后的 {name} 不是合法 TOML: {e}") from e

    env_key = "SD_SCRIPTS_CONFIG_ROOT" if body.train_format == "sd-scripts" else "DIFFUSION_PIPE_CONFIG_ROOT"
    config_root = os.environ.get(env_key, "").strip()
    if not config_root:
        raise HTTPException(400, f"未配置 {env_key}(见 .env.example)")
    base = Path(config_root).expanduser()
    base = base if base.is_absolute() else (_APP_DIR / base).resolve()
    out_dir = base / root.name
    if not overwrite and out_dir.exists() and any(out_dir.iterdir()):
        raise HTTPException(
            409, f"目标目录已存在配置({out_dir}), 确认覆盖请重试"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    contents = dict(body.files)
    if body.train_format == "sd-scripts":
        apply_lora_output_paths(docs["lora.toml"], root_name=root.name, out_dir=out_dir)
        contents["lora.toml"] = tomlkit.dumps(docs["lora.toml"])

    written: list[str] = []
    for name in file_names:
        atomic_write_text(out_dir / name, contents[name])
        written.append(name)
    logger.info("数据集 %s 导出训练配置到 %s (format=%s, mode=%s): 写入 %d 个文件 (%s)",
                root.name, out_dir, body.train_format, body.mode, len(written), ", ".join(written))
    return TrainExportResult(files=written)
