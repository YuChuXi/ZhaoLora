"""标签统计与清洗模型。

引用: ``services.models`` 包导出；各 routes / services。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TagStatRow(BaseModel):
    """标签频率一行。

    引用:
        ``tag_stats.count_tags``。
    """

    tag: str
    count: int
    pct: float = 0.0
    zh: str = ""


class TagStatsResult(BaseModel):
    """标签频率统计结果。

    引用:
        ``tag_stats.count_tags``；``routes.tag_stats``。
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
    """标签清洗样本对比。

    引用:
        ``tag_clean.clean_txt_dirs``。
    """

    path: str
    name: str = ""
    before: str
    after: str


class TagCleanResult(BaseModel):
    """标签清洗结果。

    引用:
        ``tag_clean.clean_txt_dirs``；``routes.tag_stats``。
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
    dirs: list[str] = Field(default_factory=list)
    root: str = ""

