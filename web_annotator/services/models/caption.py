"""自然语言描述标注模型(描述页).

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/caption.py:`` : 描述页路由.
    - ``services/analysis/caption.py:`` : 描述标注服务.
    - ``services/models/jobs.py:`` : 引用任务结果类型.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dataset import ImageThumbItem, WorkDirInfo

# 描述模式: 粗略(仅焦点与要点) / 详细(全实体属性与空间关系), 顺序即 captions.json 中的条目顺序
CAPTION_MODES = ("coarse", "detailed")


class CaptionRule(BaseModel):
    """预设提示词条目: 按 WD14 标签命中触发, 留空则对该数据集全部图片生效.

    ``pattern`` 为逗号分隔的多个标签模式, 每个模式支持 ``*`` 通配; 图片的 WD14
    缓存标签(等价键: 小写、下划线转空格)命中任一模式即注入 ``text``; ``pattern``
    为空串时不再做标签判定, 全部图片都注入 ``text``.
    """

    text: str = Field(..., description="补充给模型的提示词文本")
    pattern: str = Field("", description="逗号分隔的标签匹配模式, 支持 * 通配; 空串=全部图片生效")


class CaptionBody(BaseModel):
    """描述标注启动请求."""

    dirs: list[str] | None = Field(default=None, description="子目录名, 空/省略=全选")
    modes: list[str] = Field(default_factory=lambda: list(CAPTION_MODES), description="生成的描述模式")
    general_threshold: float | None = Field(
        None, ge=0.0, le=1.0, description="通用标签置信度下限; 省略为不注入"
    )
    character_threshold: float | None = Field(
        None, ge=0.0, le=1.0, description="角色标签置信度下限; 省略为不注入"
    )
    rules: list[CaptionRule] = Field(default_factory=list, description="预设提示词条目")
    force: bool = Field(False, description="是否覆盖 captions.json 中已存在的条目")


class CaptionListBody(BaseModel):
    """描述页图片列表请求."""

    dirs: list[str] | None = Field(default=None, description="子目录名, 空/省略=全选")


class CaptionImageItem(ImageThumbItem):
    """描述页单张图片及其 captions.json 现状."""

    dir: str
    captions: list[str] = Field(default_factory=list, description="captions.json 中该图片的描述列表, 空表示未描述")


class CaptionListResponse(BaseModel):
    """描述页图片列表响应."""

    items: list[CaptionImageItem] = Field(default_factory=list)
    total: int = 0
    described: int = 0
    dirs: list[str] = Field(default_factory=list)


class CaptionResult(BaseModel):
    """描述任务结束结果."""

    dirs: list[str] = Field(default_factory=list)
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    items: list[CaptionImageItem] = Field(default_factory=list)


class CaptionRuleTemplateInfo(BaseModel):
    """预设提示词模板: 供描述页一键生成条目."""

    label: str = Field(..., description="界面展示名")
    text: str = Field(..., description="模板源码, 其中的 Jinja 变量即前端的待填空位")


class CaptionDirsResponse(BaseModel):
    """描述页目录与描述服务状态."""

    dirs: list[WorkDirInfo]
    ready: bool = Field(..., description="全部描述服务是否就绪")
    info: str = Field("", description="服务就绪说明或配置错误原因")
    modes: list[str] = Field(default_factory=lambda: list(CAPTION_MODES))
    default_wd14_threshold: float = Field(..., description="WD14 参考标签置信度下限的默认值")
    rule_templates: list[CaptionRuleTemplateInfo] = Field(
        default_factory=list, description="预设提示词模板"
    )
