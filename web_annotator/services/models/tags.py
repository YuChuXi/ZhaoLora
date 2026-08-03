"""标签统计与清洗模型. 

引用: ``services.models`` 包导出；各 routes / services. 
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TagStatRow(BaseModel):
    """标签频率一行. 

    引用:
        ``tag_stats.count_tags``. 
    """

    tag: str
    count: int
    pct: float = 0.0
    zh: str = ""


class TagStatsResult(BaseModel):
    """标签频率统计结果. 

    引用:
        ``tag_stats.count_tags``；``routes.tag_stats``. 
    """

    sample_count: int
    failed: int = 0
    file_count: int = 0
    tag_count: int = 0
    total_occurrences: int = 0
    per_sample: bool = True
    tags: list[TagStatRow] = Field(default_factory=list)
    dirs: list[str] = Field(default_factory=list)
    root: str = ""
    zh_cache_size: int = 0


class TagCleanSample(BaseModel):
    """标签清洗样本对比.

    引用:
        ``services.tagging.clean.clean_txt_dirs``.
    """

    path: str
    name: str = ""
    before: str
    after: str


class TagCleanResult(BaseModel):
    """标签清洗结果.

    引用:
        ``services.tagging.clean.clean_txt_dirs``；``routes.tag_stats``.
    """

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
    matched_tags: dict[str, list[str]] = Field(default_factory=dict, description="模糊匹配模式 → 命中的 tag_key 列表(去重)")
    dirs: list[str] = Field(default_factory=list)
    root: str = ""


# 标签检查位置选项: any / top / bottom / before-sep / after-sep / bottom-before
TAG_CHECK_POSITIONS = frozenset({"any", "top", "bottom", "before-sep", "after-sep", "bottom-before"})

# 标签插入位置选项: top / bottom / before-sep / after-sep / random / random-before / random-after
TAG_INSERT_POSITIONS = frozenset({"top", "bottom", "before-sep", "after-sep", "random", "random-before", "random-after"})


class TagCheckItem(BaseModel):
    """单张图片的标签检查结果.

    引用:
        ``routes.tag_stats.tag_check`` / ``tag_edit_one``.
    """

    path: str
    stem: str
    width: int = 0
    height: int = 0
    thumbnail_hash: str
    dir: str
    exists: bool
    position_ok: bool
    caption_preview: str = ""


class TagCheckResponse(BaseModel):
    """标签检查批量响应.

    引用:
        ``routes.tag_stats.tag_check``.
    """

    tag: str
    position: str
    total: int
    ok_count: int
    bad_count: int
    items: list[TagCheckItem] = Field(default_factory=list)


class TagCheckBody(BaseModel):
    """标签检查请求. 引用: ``POST /tags/check``."""

    dirs: list[str] | None = Field(default=None, description="子目录名, 空/省略=全选")
    tag: str = Field(default="", description="标签, 为空时返回全部图片(不检查)")
    position: str = Field("any", description="any/top/bottom/before-sep/after-sep")


class TagEditOneBody(BaseModel):
    """单文件标签编辑请求. 引用: ``POST /tags/edit-one``."""

    path: str = Field(..., description="图片相对数据集根的路径")
    tag: str
    position: str = Field("any", description="any/top/bottom/before-sep/after-sep")

