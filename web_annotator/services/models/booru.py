"""Booru 搜索与下载模型.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/online_sources.py:`` : 在线图站路由.
    - ``services/online_sources/framework/base.py:`` : 下载框架.
    - ``services/online_sources/framework/media.py:`` : 下载框架.
    - ``services/online_sources/framework/gallerydl.py:`` : 下载框架.
    - ``services/online_sources/gelbooru/source.py:`` : 各图站适配.
    - ``services/online_sources/danbooru/source.py:`` : 各图站适配.
    - ``services/online_sources/pixiv/source.py:`` : 各图站适配.
    - ``services/online_sources/bilibili/source.py:`` : 各图站适配.
    - ``services/online_sources/x/source.py:`` : 各图站适配.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field, model_validator

from services.models.jobs import JobSnapshot


class BooruPost(BaseModel):
    """图站帖子, 同时接纳 Gelbooru/Danbooru API 原始字段并统一规范化."""

    model_config = {"extra": "allow"}

    source: str = ""
    id: str = ""
    width: int = 0
    height: int = 0
    score: int = 0
    tags: str = ""
    # 帖子发布时间, 统一为 ISO8601 文本 (各来源在归一化器中格式化)
    posted_at: str = ""
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
    filename: str = ""
    # 视频帖标记: 下载时落入数据集 video 目录
    is_video: bool = False

    @computed_field
    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @model_validator(mode="before")
    @classmethod
    def _normalize_fields(cls, data: Any) -> Any:
        """统一规范化各来源的原始字段: 宽高/标签/预览 URL 映射、id 转文本、发布时间归一.

        发布时间 ``posted_at`` 接受三类原始输入并统一为 ISO8601 文本:
        unix 秒整数 (X 的 ``date``、Bilibili 的 ``pub_ts``)、Gelbooru 的
        ``created_at`` (如 ``Tue Feb 6 00:19:15 -0600 2024``)、以及已是
        ISO8601 的字符串 (Danbooru 的 ``created_at``、Pixiv 的 ``createDate``).

        Args:
            data: 待校验的原始字段字典 (或非字典原样返回).

        Returns:
            Any: 规范化后的字段字典.

        Callers:
            - pydantic v2 在每次 ``BooruPost`` 构造/校验时自动调用
        """
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
        # 发布时间归一: booru API 的 created_at 消费进 posted_at, 避免冗余字段
        if "created_at" in data:
            data.setdefault("posted_at", data.pop("created_at"))
        posted = data.get("posted_at")
        if posted is None:
            data["posted_at"] = ""
        else:
            if isinstance(posted, str) and posted.isdigit():
                posted = int(posted)
            if isinstance(posted, (int, float)):
                posted = datetime.fromtimestamp(posted).astimezone().isoformat()
            elif isinstance(posted, str) and posted and posted[4:5] != "-":
                # 非 ISO8601 (YYYY-MM-DD 前缀) 即 Gelbooru 独有的英文日期格式, 归一为 ISO8601
                posted = datetime.strptime(posted, "%a %b %d %H:%M:%S %z %Y").isoformat()
            data["posted_at"] = posted
        return data


class BooruSearchResult(BaseModel):
    """Booru 搜索响应."""

    source: str
    tags: str
    extra: str = ""
    order: str = ""
    query: str
    dir: str
    page: int
    count: int
    has_more: bool
    session: str = ""  # 会话式来源的续拉 token (页码式来源为空)
    posts: list[BooruPost]


class BooruSourcesResponse(BaseModel):
    """可用图站与代理配置."""

    sources: list[str]
    proxy: str = ""


class BooruDownloadResult(BaseModel):
    """Booru 下载结果."""

    path: str
    stem: str
    id: str
    source: str
    filename: str = ""
    tags_path: str = ""
    skipped: bool = False
    dir: str = ""
    rel_path: str = ""
    thumbnail_hash: str = ""


class BooruUndownloadResult(BaseModel):
    """Booru 移除本地下载结果."""

    id: str
    source: str
    removed: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    count: int = 0
    dir: str = ""


class BatchStartBody(BaseModel):
    """批量下载启动请求."""

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


class BatchParamsBody(BaseModel):
    """批量下载运行中改参."""

    limit: int | None = Field(None, ge=1, le=5000)
    conn: int | None = Field(None, ge=1, le=8)
    rate_rps: float | None = Field(None, ge=0, le=60)

