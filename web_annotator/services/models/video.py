"""视频筛选与抽帧模型.

引用: ``services.models`` 包导出；各 routes / services.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VideoInfo(BaseModel):
    """筛选页视频列表项.

    引用:
        ``services.core.video_scanner.scan_videos``；``routes.video.list_videos``.
    """

    path: str  # 相对数据集根, 如 video/foo.mp4
    name: str
    stem: str
    width: int = 0
    height: int = 0
    duration: float = 0.0  # 秒
    size: int = Field(0, ge=0)
    cover_hash: str = ""


class FrameType(BaseModel):
    """单帧编码类型与大小.

    引用:
        ``VideoDetail.types``.
    """

    type: str = "?"  # I / P / B 帧类型
    size: int = Field(0, ge=0)  # 包大小(字节)


class BitstreamTypes(BaseModel):
    """紧凑帧位流: 类型字符串与平行大小数组, 数组下标即帧序号.

    取代逐帧 ``{idx: {type, size}}`` 的冗长字典, 显著压缩 detail 响应体积
    (长视频约 10 万帧时由数 MB 降至数百 KB)。``s`` 记录的是每帧包大小**量化到
    KB**(``字节 // 1024``)后的值, 仅用于柱状图相对高度, 亚 KB 精度对可视化无感,
    却能把数组值压进单字节范围, 配合 gzip 传输体积进一步骤降。

    引用:
        ``VideoDetail.types``；``services.core.video_frame``.
    """

    t: str = ""  # 每帧一字符的编码类型(I/P/B); 无分析位可留空
    s: list[int] = Field(default_factory=list)  # 与 t 平行的包大小(量化到 KB)


class VideoDetail(BaseModel):
    """视频抽帧页详情.

    引用:
        ``routes.video.video_detail``.
    """

    path: str
    name: str
    width: int = 0
    height: int = 0
    duration: float = 0.0
    total: int = Field(0, ge=0)  # 总帧数
    types: BitstreamTypes = Field(default_factory=BitstreamTypes)  # 紧凑帧位流
    saved: list[int] = Field(default_factory=list)  # 已保存帧索引(升序)


class FrameSaveBody(BaseModel):
    """保存 / 删除抽帧请求体. 引用: ``POST /video/frame/save`` / ``/video/frame/delete``. """

    path: str = Field(..., description="相对数据集根的视频路径")
    index: int = Field(..., ge=0, description="帧索引")


class FrameSaveResponse(BaseModel):
    """保存 / 删除抽帧响应. 引用: ``routes.video``. """

    path: str
    index: int
    saved: list[int] = Field(default_factory=list)
    count: int = Field(0, ge=0)