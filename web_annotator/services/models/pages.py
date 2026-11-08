"""评分 / 打标页目录与启动回执.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/scoring.py:`` : 评分路由.
    - ``routes/tagging.py:`` : 打标路由.
    - ``services/analysis/scoring.py:`` : 评分项定义与评分引擎.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dataset import WorkDirInfo


class ScoreItem(BaseModel):
    """数据集评分项: 由 WD14 标签概率组成的表达式.

    Attributes:
        key: 稳定标识, 用于 API 与前端排序.
        label: 显示名称.
        expr: 评分表达式, 双引号字符串引用同名 WD14 标签概率.
        builtin: 是否为内置评分项(不可删除).

    Callers:
        - ``services/analysis/scoring.py:`` : 读取、创建与删除评分项.
        - ``routes/scoring.py:`` : 返回评分项列表.
    """

    key: str
    label: str
    expr: str
    builtin: bool = False


class ScoreItemBody(BaseModel):
    """新增评分项请求体.

    Callers:
        - ``routes/scoring.py:create_item``
    """

    label: str = Field(..., min_length=1, description="显示名称")
    expr: str = Field(..., min_length=1, description="评分表达式")


class ScoreItemsResponse(BaseModel):
    """评分项列表响应.

    Callers:
        - ``routes/scoring.py:items`` / ``create_item`` / ``delete_item``
    """

    items: list[ScoreItem] = Field(default_factory=list)


class ScoringDirsResponse(BaseModel):
    """统一评分页目录、评分项与缓存状态."""

    dirs: list[WorkDirInfo]
    aesthetic_ready: bool
    aesthetic_count: int = 0
    items: list[ScoreItem] = Field(default_factory=list)
    item_counts: dict[str, int] = Field(default_factory=dict)
    wd14_ready: bool = False
    wd14_message: str = ""
    wd14_count: int = 0
    wd14_total: int = 0


class ScoringStartResponse(BaseModel):
    """统一评分任务提交回执."""

    ok: bool = True
    job_id: str
    kind: str
    dirs: list[str]


class TaggingDirsResponse(BaseModel):
    """打标页目录与模型状态."""

    dirs: list[WorkDirInfo]
    model_ready: bool
    model_path: str
    default_threshold: float


class TaggingStartResponse(BaseModel):
    """打标任务提交回执."""

    ok: bool = True
    job_id: str
    dirs: list[str]
    threshold: float
    disable_orig_tags: bool
    force: bool
