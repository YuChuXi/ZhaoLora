"""Booru 搜索与下载模型. 

引用: ``services.models`` 包导出；各 routes / services. 
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, computed_field, model_validator

from services.models.jobs import JobSnapshot


class BooruPost(BaseModel):
    """图站帖子, 同时接纳 Gelbooru/Danbooru API 原始字段并统一规范化. 

    引用: ``sources.*.normalize`` / ``search_posts`` / ``routes.booru``. 
    """

    model_config = {"extra": "allow"}

    source: str = ""
    id: str = ""
    width: int = 0
    height: int = 0
    score: int = 0
    tags: str = ""
    file_url: str = ""
    preview_url: str = ""
    sample_url: str = ""
    large_file_url: str = ""
    rating: str = ""
    file_ext: str = ""
    # API 特有字段(extra="allow" 保证写入 meta 时不丢失)
    md5: str = ""
    media_asset: dict[str, Any] | None = None
    is_banned: bool = False
    is_deleted: bool = False
    tag_string_meta: str = ""
    tag_string_general: str = ""
    tag_string_character: str = ""
    tag_string_copyright: str = ""
    tag_string_artist: str = ""
    # 运行时附加
    downloaded: bool = False
    stem: str = ""

    @computed_field
    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @model_validator(mode="before")
    @classmethod
    def _normalize_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for src, dst in [
            ("image_width", "width"), ("image_height", "height"),
            ("tag_string", "tags"), ("preview_file_url", "preview_url"),
        ]:
            if src in data and dst not in data:
                data[dst] = data[src]
        # id 可能是 int(API 原始)→ str
        if "id" in data and not isinstance(data["id"], str):
            data["id"] = str(data["id"])
        return data


class BooruSearchResult(BaseModel):
    """Booru 搜索响应. 

    引用:
        ``routes.booru.booru_search``. 
    """

    source: str
    tags: str
    extra: str = ""
    order: str = ""
    query: str
    dir: str
    page: int
    count: int
    has_more: bool
    posts: list[BooruPost]


class BooruSourcesResponse(BaseModel):
    """可用图站与代理配置. 

    引用:
        ``routes.booru.booru_sources``. 
    """

    sources: list[str]
    proxy: str = ""


class BooruDownloadResult(BaseModel):
    """Booru 下载结果. 

    引用:
        ``booru_client.download_post``；``routes.booru``. 
    """

    path: str
    stem: str
    id: str
    source: str
    tags_path: str = ""
    skipped: bool = False
    dir: str = ""
    rel_path: str = ""
    thumbnail_hash: str = ""


class BooruUndownloadResult(BaseModel):
    """Booru 移除本地下载结果. 

    引用:
        ``booru_client.remove_download``；``routes.booru``. 
    """

    id: str
    source: str
    removed: list[str] = Field(default_factory=list)
    count: int = 0
    dir: str = ""


class BooruBatchStartBody(BaseModel):
    """批量下载启动请求. 引用: ``POST /booru/batch/start``. """

    source: str
    tags: str = ""
    extra: str = ""
    order: str = ""
    dir: str = Field("main", description="工作子目录")
    page: int = Field(1, ge=1, le=10000, description="起始页(向后翻)")
    limit: int = Field(50, ge=1, le=5000)
    conn: int = Field(2, ge=1, le=8)
    rate_rps: float = Field(0, ge=0, le=60, description="次/秒, 0=不限")
    anima_format: bool = Field(True, description="Anima 格式: [性别][角色][系列][画师][通用标签]")
    include_meta_tags: bool = Field(False, description="下载元数据标签(rating、分辨率等)")
    keep_underscores: bool = Field(False, description="保留下划线, 不转为空格")


class BooruBatchParamsBody(BaseModel):
    """批量下载运行中改参. 引用: ``POST /booru/batch/params``. """

    limit: int | None = Field(None, ge=1, le=5000)
    conn: int | None = Field(None, ge=1, le=8)
    rate_rps: float | None = Field(None, ge=0, le=60)


class BooruBatchActiveResponse(BaseModel):
    """当前数据集上未结束的批量任务. 引用: ``GET /booru/batch/active``. """

    ok: bool = True
    job_id: str | None = None
    status: JobSnapshot | None = None

