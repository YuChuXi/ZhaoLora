"""就绪: 生成训练配置文本, 并返回目录与蒙版统计. """

from __future__ import annotations

import logging
from pathlib import Path

import tomlkit
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from tomlkit.exceptions import ParseError

from routes.deps import get_runtime
from services.models import LoraMode, ReadyDirsResponse, ReadyTomlResult, TrainExportResult
from services.ready import (
    TRAIN_FORMAT_TEMPLATES,
    build_ready,
    export_dir,
    exportable_dirs,
    subdir_mask_stats,
)
from services.core.work_dir import list_work_dirs
from services.core.atomic import atomic_write_text

logger = logging.getLogger(__name__)

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
    mode: LoraMode = Field("style", description="训练模式: 画风 style / 角色 character(各模式对应的超参由所选格式的模板决定)")
    train_format: str = Field(description="配置导出格式, 取值为 assets/configs/ 下的模板目录名(文件集合由该目录扫描得到)")
    variant: str = Field(description="配置变体, 取值为格式模板目录下的子目录名(与该格式共同决定文件集合)")

    @model_validator(mode="after")
    def _check_format_variant(self) -> "ReadyBody":
        """校验 (train_format, variant) 组合恰为某个已收录的格式与变体.

        可用格式即 ``assets/configs/`` 下的顶层模板目录, 可用变体即该格式目录下含 ``*.j2``
        的子目录, 使两者完全由模板目录结构决定.

        Returns:
            ReadyBody: 校验通过的原模型实例.

        Raises:
            ValueError: 格式名无对应模板目录, 或该格式下不存在该变体子目录.

        Callers:
            - ``routes/ready.py:toml`` / ``export`` (经请求体校验触发)
        """
        if self.train_format not in TRAIN_FORMAT_TEMPLATES:
            raise ValueError(
                f"未知配置格式 {self.train_format!r}; 可用: "
                f"{', '.join(sorted(TRAIN_FORMAT_TEMPLATES))}"
            )
        variants = TRAIN_FORMAT_TEMPLATES[self.train_format]
        if self.variant not in variants:
            raise ValueError(
                f"格式 {self.train_format!r} 下未知配置变体 {self.variant!r}; 可用: "
                f"{', '.join(sorted(variants))}"
            )
        return self


class ReadyExportBody(ReadyBody):
    """就绪页导出请求: 在 ReadyBody 基础上要求携带编辑后的整套配置文本.

    ``files`` 键必须恰为 ``TRAIN_FORMAT_TEMPLATES[train_format][variant]`` 的键集合; 导出逐字段以编辑后的
    文本为准, 原样写入 ``{格式对应配置根}/{数据集名}-{变体}/``.
    引用: ``POST /ready/export``.
    """

    files: dict[str, str] = Field(description="编辑后的配置文本, 键为当前格式的配置文件名集合")

    @model_validator(mode="after")
    def _check_file_names(self) -> "ReadyExportBody":
        """校验 files 键集合恰为当前格式与变体的配置文件名集合, 防止静默漏写或多写.

        Returns:
            ReadyExportBody: 校验通过的原模型实例.

        Raises:
            ValueError: files 键缺失或存在未知键.
        """
        expected = TRAIN_FORMAT_TEMPLATES[self.train_format][self.variant]
        if set(self.files) != set(expected):
            raise ValueError(f"files 键必须恰为 {sorted(expected)}")
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

    配置文件文本与子集/蒙版统计由 ``build_ready`` 一次生成: 渲染时即按 ``export_dir``
    写出引用同目录文件的绝对路径, 故预览文本与导出写入的文本完全一致.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (ReadyBody): 就绪页生成参数(dirs / target_images / subset_resolutions / use_reg / mode / train_format / variant).

    Returns:
        ReadyTomlResult: 子集/蒙版统计与配置文件预览文本(files).

    Raises:
        HTTPException: 无可导出的图片目录, 或缺少对应格式的位置类环境变量时抛出.

    Callers:
        - ``static/js/ready.js:generate``
    """
    root = get_runtime(request).root
    try:
        result = build_ready(
            root,
            mode=body.mode,
            train_format=body.train_format,
            variant=body.variant,
            dirs=body.dirs,
            target_images=body.target_images,
            subset_resolutions=body.subset_resolutions,
            use_reg=body.use_reg,
            trigger_word=_trigger_word(request, root),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not result.subsets:
        raise HTTPException(400, "没有可导出的图片目录")
    logger.info("数据集 %s 生成就绪预览(%s / %s 模式): %d 个子集, 目标图片数 %d, 子集分辨率 %s",
                root.name, body.train_format, body.mode, len(result.subsets), body.target_images,
                body.subset_resolutions or "默认")
    return result


@router.post("/export", response_model=TrainExportResult)
def export(
    request: Request, body: ReadyExportBody, overwrite: bool = False
) -> TrainExportResult:
    """按编辑后的配置文本导出整套训练配置到 ``{格式对应配置根}/{数据集名}-{变体}/``.

    导出目录与渲染训练配置时所用的一致(``export_dir``), 配置文本内的路径因此无需改写;
    ``files`` 的文本即最终写入内容, 逐字段以用户编辑后为准, 写入前仅校验 .toml 的语法.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (ReadyExportBody): 就绪页导出参数(dirs / target_images / subset_resolutions / use_reg / mode / train_format / variant / files).
        overwrite (bool): 目标目录已存在可视化配置时, 需置 True 才覆盖写入, 否则返回 409.

    Returns:
        TrainExportResult: 实际写入的配置文件文件名列表(不返回绝对路径).

    Raises:
        HTTPException: 无可导出的图片目录、编辑后的 TOML 语法错误、未配置格式对应的
            配置根环境变量、数据集当前不可用于导出、目标目录已存在配置且未确认覆盖时抛错.

    Callers:
        - ``static/js/ready.js:doExport``
    """
    root = get_runtime(request).root
    file_names = TRAIN_FORMAT_TEMPLATES[body.train_format][body.variant]
    if not exportable_dirs(root, body.dirs):
        raise HTTPException(400, "没有可导出的图片目录")
    for name in file_names:
        if not name.endswith(".toml"):
            continue
        try:
            tomlkit.parse(body.files[name])
        except ParseError as e:
            raise HTTPException(400, f"编辑后的 {name} 不是合法 TOML: {e}") from e

    try:
        out_dir = export_dir(root.name, body.train_format, body.variant)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not overwrite and out_dir.exists() and any(out_dir.iterdir()):
        raise HTTPException(
            409, f"目标目录已存在配置({out_dir}), 确认覆盖请重试"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for name in file_names:
        atomic_write_text(out_dir / name, body.files[name])
        written.append(name)
    logger.info("数据集 %s 导出训练配置到 %s (format=%s, mode=%s): 写入 %d 个文件 (%s)",
                root.name, out_dir, body.train_format, body.mode, len(written), ", ".join(written))
    return TrainExportResult(files=written)
