"""标签统计与清洗模型.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/tag_stats.py:`` : 标签统计与检查路由.
    - ``services/tagging/stats.py:`` : 标签频率统计.
    - ``services/tagging/clean.py:`` : 标签清洗.
    - ``services/models/jobs.py:`` : 引用查询结果类型.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dataset import ImageThumbItem


class TagStatRow(BaseModel):
    """标签频率一行."""

    tag: str
    count: int
    pct: float = 0.0
    zh: str = ""


class TagStatsResult(BaseModel):
    """标签频率统计结果."""

    sample_count: int
    failed: int = 0
    skipped: int = Field(0, description="因不满足 include/exclude 筛选条件而跳过的样本数")
    file_count: int = 0
    tag_count: int = 0
    total_occurrences: int = 0
    per_sample: bool = True
    tags: list[TagStatRow] = Field(default_factory=list)
    dirs: list[str] = Field(default_factory=list)
    zh_cache_size: int = 0


class TagCleanSample(BaseModel):
    """标签清洗样本对比."""

    path: str
    name: str = ""
    before: str
    after: str


class TagCleanResult(BaseModel):
    """标签清洗结果."""

    dry_run: bool
    file_count: int
    changed: int
    unchanged: int
    failed: int
    samples: list[TagCleanSample] = Field(default_factory=list)
    drop_exact_count: int = 0
    drop_contains: list[str] = Field(default_factory=list)
    promote: list[str] = Field(default_factory=list)
    prepend: list[str] = Field(default_factory=list)
    inject_pony_score: bool = False
    pony_injected: int = 0
    pony_missing: int = 0
    inject_quality_tag: bool = False
    quality_injected: int = 0
    quality_missing: int = 0
    inject_year_tag: bool = False
    year_injected: int = 0
    year_missing: int = 0
    matched_tags: dict[str, list[str]] = Field(default_factory=dict, description="模糊匹配模式 → 命中的 tag_key 列表(去重)")
    dirs: list[str] = Field(default_factory=list)
    root: str = ""


# 标签检查位置选项: any / top / bottom / before-sep / after-sep / bottom-before
TAG_CHECK_POSITIONS = frozenset({"any", "top", "bottom", "before-sep", "after-sep", "bottom-before"})

# 标签插入位置选项: top / bottom / before-sep / after-sep
TAG_INSERT_POSITIONS = frozenset({"top", "bottom", "before-sep", "after-sep"})


class TagCheckItem(ImageThumbItem):
    """单张图片的标签检查结果."""

    dir: str
    exists: bool
    position_ok: bool
    caption_preview: str = ""


class TagCheckResponse(BaseModel):
    """标签检查批量响应."""

    tag: str
    position: str
    total: int
    ok_count: int
    bad_count: int
    items: list[TagCheckItem] = Field(default_factory=list)


class TagCheckBody(BaseModel):
    """标签检查请求."""

    dirs: list[str] | None = Field(default=None, description="子目录名, 空/省略=全选")
    tag: str = Field(default="", description="标签, 为空时返回全部图片(不检查)")
    position: str = Field("any", description="any/top/bottom/before-sep/after-sep")


class TagEditOneBody(BaseModel):
    """单文件标签编辑请求."""

    path: str = Field(..., description="图片相对数据集根的路径")
    tag: str
    position: str = Field(..., description="插入位置: top/bottom/before-sep/after-sep")
    check_position: str = Field(..., description="编辑后检查所用位置: any/top/bottom/before-sep/after-sep/bottom-before")


class EmbeddingStatusResponse(BaseModel):
    """向量嵌入模型状态响应."""

    ok: bool
    message: str = ""


class EmbeddingQueryBody(BaseModel):
    """向量查询请求."""

    query: str = Field(..., description="查询文本")
    dirs: list[str] | None = Field(default=None, description="子目录名, 空/省略=全选")
    top_k: int = Field(0, description="返回前 k 条；0 表示全部")


class EmbeddingQueryItem(BaseModel):
    """向量查询结果项."""

    path: str
    stem: str
    similarity: float


class EmbeddingQueryResult(BaseModel):
    """向量查询长任务结束结果."""

    items: list[EmbeddingQueryItem] = Field(default_factory=list)


class EmbeddingHistoryItem(BaseModel):
    """一次历史语义排序查询记录."""

    query: str = Field(..., description="查询文本")
    time: float = Field(0.0, description="执行时间戳（epoch 秒）")


class EmbeddingHistoryResponse(BaseModel):
    """语义排序查询历史."""

    queries: list[EmbeddingHistoryItem] = Field(default_factory=list)


class EmbeddingBatchTagBody(BaseModel):
    """批量标记请求."""

    paths: list[str] = Field(..., description="按相似度阈值筛选出的图片相对路径列表")
    tag: str = Field(..., description="要添加或删除的标签")
    action: str = Field("add", description="add(添加) / delete(删除)")
    position: str = Field("top", description="添加时的位置")
    fuzzy: bool = Field(False, description="删除时是否模糊匹配")


class EmbeddingBatchTagResponse(BaseModel):
    """批量标记响应."""

    ok: bool
    matched_count: int = 0
    message: str = ""


class ConceptQueryBody(BaseModel):
    """概念分割查询请求."""

    query: str = Field(..., description="概念提示词")
    dirs: list[str] | None = Field(default=None, description="子目录名, 空/省略=全选")


class ConceptQueryItem(BaseModel):
    """概念分割查询结果项."""

    path: str
    ratio: float = Field(0.0, description="掩码占比 [0,1]")
    mask_data: str = Field("", description="半透明叠加 PNG 的 base64 data URL, 未命中时为空")


class ConceptQueryResult(BaseModel):
    """概念分割查询长任务结束结果."""

    items: list[ConceptQueryItem] = Field(default_factory=list)

