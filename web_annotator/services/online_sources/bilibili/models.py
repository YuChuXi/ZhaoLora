"""B 站动态数据模型.

与 ``bilibili_test/fetch_dynamics.py`` 采用同一套结构化模型, 供在线来源
(``services/online_sources/bilibili/source.py``) 解析动态并将每张图映射为一张卡片.

Callers:
    - ``services.online_sources.bilibili.client``
"""

from __future__ import annotations

from pydantic import BaseModel


class Author(BaseModel):
    """动态/图文作者信息.

    Args:
        mid (int | None): 作者 UID。
        name (str | None): 作者昵称。
        pub_ts (int | str | None): 发布时间戳 (desktop 接口为 int, v1 接口为字符串)。
    """

    mid: int | None
    name: str | None
    pub_ts: int | str | None


class ImageInfo(BaseModel):
    """图片原图信息.

    Args:
        url (str): 原图 URL (无 ``@`` 缩略图后缀)。
        width (int | None): 图片宽度。
        height (int | None): 图片高度。
    """

    url: str
    width: int | None
    height: int | None


class TopicInfo(BaseModel):
    """话题信息.

    Args:
        id (int | str | None): 话题 ID。
        name (str): 话题名称。
        jump_url (str | None): 话题跳转链接。
    """

    id: int | str | None
    name: str
    jump_url: str | None


class VideoInfo(BaseModel):
    """视频稿件信息.

    Args:
        bvid (str | None): 视频 BV 号。
        title (str | None): 视频标题。
        cover (str | None): 视频封面 URL。
        duration_text (str | None): 视频时长文本。
        play (str | None): 播放量。
    """

    bvid: str | None
    title: str | None
    cover: str | None
    duration_text: str | None
    play: str | None


class StatInfo(BaseModel):
    """动态统计信息.

    Args:
        like (int | None): 点赞数。
        comment (int | None): 评论数。
        forward (int | None): 转发数。
    """

    like: int | None
    comment: int | None
    forward: int | None


class DynamicRecord(BaseModel):
    """一条动态或图文 (opus) 记录, 转发动态递归挂载于 ``forwarded``。

    Args:
        id (str): 动态 ID。
        type (str | None): 动态类型 (如 ``DYNAMIC_TYPE_DRAW``)。
        author (Author | None): 作者信息。
        pub_ts (int | str | None): 发布时间戳。
        text (str): 正文文本。
        topic (TopicInfo | None): 话题。
        images (list[ImageInfo]): 图片原图列表。
        video (VideoInfo | None): 视频稿件信息, 非视频动态为 None。
        stat (StatInfo | None): 点赞/评论/转发统计。
        forwarded (DynamicRecord | None): 转发动态的原始记录。
    """

    id: str
    type: str | None
    author: Author | None
    pub_ts: int | str | None
    text: str
    topic: TopicInfo | None
    images: list[ImageInfo]
    video: VideoInfo | None
    stat: StatInfo | None
    forwarded: DynamicRecord | None = None