"""B 站动态拉取、解析与图片代理.

基于专业库 ``bilibili-api`` (https://github.com/Nemo2011/bilibili-api) 免登录
按游标分页拉取用户动态: ``User.get_dynamics_new`` (``web-dynamic/v1/feed/space``),
库自动处理 buvid 会话、WBI 签名与 dm 设备指纹, 规避手写 requests 调用被
-352/412 风控的问题。单条详情 (下载时需要按动态 ID 取图) 经由桌面版接口
``web-dynamic/desktop/v1/detail``, 免登录稳定可用。

本模块是 ``services.online_sources.bilibili`` 子包的在线来源实现, 供筛选页的
``BilibiliSource`` 使用; 图片经 ``fetch_bili_bytes`` 带 B 站 Referer 拉取,
兼容浏览器防盗链代理。

Callers:
    - ``services/online_sources/bilibili/source.py``
    - ``routes/online_sources.py`` (图片代理)
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from bilibili_api import user
from bilibili_api.exceptions import NetworkException

from services.online_sources.bilibili.models import (
    Author,
    DynamicRecord,
    ImageInfo,
    StatInfo,
    TopicInfo,
    VideoInfo,
)
from services.online_sources.framework.http import DEFAULT_UA, HttpClient, NetworkError

# 转发链递归解析的最大深度, 防止异常深链造成栈溢出
MAX_FORWARD_DEPTH = 8

# feed 接口偶发返回空响应 (同一 offset 请求结果不稳定), 连续多次为空才判定抓完
MAX_EMPTY_RETRIES = 3

# 需要过滤的条目类型: 直播推荐卡片并非用户主动发布的动态
FILTER_TYPES = {"DYNAMIC_TYPE_LIVE_RCMD"}

# 桌面版详情接口: 免登录稳定可用; 库的 ``Dynamic.get_info`` 所用的 v1 detail
# 接口在未登录会话下会被 412 风控拦截, 故单条详情改用此接口
_DESKTOP_DETAIL_API = "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/detail"
_BILI_HEADERS = {
    "user-agent": DEFAULT_UA,
    "referer": "https://www.bilibili.com/",
    "accept": "application/json, text/plain, */*",
}

# 动态 ID -> 解析后的记录, 供下载时按 ID 取图, 避免重复请求
_dynamic_cache: dict[str, DynamicRecord] = {}


async def _desktop_detail(
    dynamic_id: str, *, retries: int = 3, base_sleep: float = 3.0
) -> dict | None:
    """免登录获取单条动态完整详情 (桌面版接口, 带退避重试).

    Args:
        dynamic_id (str): 动态 ID (id_str)。
        retries (int): 网络/风控异常退避重试次数, 默认 3。
        base_sleep (float): 首次退避秒数, 之后指数递增, 默认 3.0。

    Returns:
        dict | None: ``data.item`` 动态条目 (``modules`` 为 list 格式);
            该 id 属于桌面版接口不支持的动态类型 (如专栏/笔记, 接口返回
            -1024) 时返回 None。

    Raises:
        RuntimeError: 重试耗尽或接口返回其他非 0 code。

    Callers:
        - ``bilibili/client.py:resolve_image``
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            async with HttpClient() as client:
                resp = await client.get(
                    _DESKTOP_DETAIL_API,
                    headers=_BILI_HEADERS,
                    params={"id": dynamic_id, "timezone_offset": str(-480)},
                    timeout=30.0,
                    follow_redirects=True,
                )
            resp.raise_for_status()
            data = resp.json()
            code = data.get("code")
            if code == -1024:
                return None
            if code != 0:
                raise RuntimeError(f"code={code} message={data.get('message')}")
            return (data.get("data") or {}).get("item") or {}
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                wait = base_sleep * (2 ** (attempt - 1))
                print(f"详情 {dynamic_id} 请求失败 ({exc}), {wait:.0f} 秒后重试...")
                await asyncio.sleep(wait)
    raise last_exc or RuntimeError(f"详情 {dynamic_id} 请求失败")


async def _retry_coro(
    factory: Any, *, what: str, retries: int = 3, base_sleep: float = 3.0
) -> dict:
    """对间歇性风控异常退避重试接口调用.

    未登录会话存在间歇性风控: 请求偶发被 412 拒绝 (库抛 ``NetworkException``),
    等待冷却后自动恢复。此函数仅对网络类异常 (含风控) 按指数退避重试,
    业务错误 (如 ``ResponseCodeException``) 直接抛出; 重试耗尽后抛出最后一次
    异常, 不吞没错误。

    Args:
        factory: 无参异步可调用, 返回接口结果 (dict)。
        what (str): 请求描述, 用于提示信息。
        retries (int): 最大尝试次数 (含首次), 默认 3。
        base_sleep (float): 首次退避秒数, 之后指数递增, 默认 3.0。

    Returns:
        dict: 接口结果。

    Raises:
        NetworkException: 重试耗尽后最后一次网络异常。
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await factory()
        except (NetworkException, NetworkError) as exc:
            last_exc = exc
            if attempt < retries:
                wait = base_sleep * (2 ** (attempt - 1))
                print(
                    f"{what} 请求失败 ({exc}), {wait:.0f} 秒后重试 "
                    f"({attempt}/{retries - 1})..."
                )
                await asyncio.sleep(wait)
    raise last_exc or RuntimeError(f"{what} 重试失败")


def _norm_url(url: str | None) -> str | None:
    """将协议相对 URL (``//``) 补全为 https.

    Args:
        url (str | None): 原始 URL。

    Returns:
        str | None: 规范化后的 URL; 空值原样返回。
    """
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return url


def _find_module(item: dict[str, Any], key: str) -> dict[str, Any] | None:
    """从动态条目中按模块名取模块, 兼容 dict 与 list 两种 modules 格式.

    引用: ``services.online_sources.bilibili.client._parse_item``。
    """
    modules = item.get("modules")
    if isinstance(modules, dict):
        return modules.get(key)
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            value = module.get(key)
            if value:
                return value
    return None


def _extract_author(module_author: dict[str, Any] | None) -> Author | None:
    """提取作者信息 (mid / name / 发布时间), 兼容 v1 与 desktop 两种布局.

    引用: ``services.online_sources.bilibili.client._parse_item``。
    """
    if not isinstance(module_author, dict):
        return None
    author = module_author.get("user") or {}
    mid = module_author.get("mid") or author.get("mid")
    name = module_author.get("name") or author.get("name")
    if mid is None and name is None:
        return None
    return Author(
        mid=mid,
        name=name,
        pub_ts=module_author.get("pub_ts") or module_author.get("pub_time"),
    )


def _extract_text(module_dynamic: dict[str, Any] | None) -> str:
    """提取动态正文文本, 兼容多种载体.

    引用: ``services.online_sources.bilibili.client._parse_item``。
    """
    text = ""
    if not isinstance(module_dynamic, dict):
        return text
    desc = module_dynamic.get("desc") or {}
    text = (desc.get("text") or "").strip()
    if not text:
        major = module_dynamic.get("major") or {}
        opus = major.get("opus") or {}
        text = ((opus.get("summary") or {}).get("text") or "").strip()
        if not text:
            text = (opus.get("title") or "").strip()
    return re.sub(r"\s+", " ", text).strip()


def _extract_images(module_dynamic: dict[str, Any] | None) -> list[ImageInfo]:
    """提取图片原图列表 (URL 无 ``@`` 缩略图后缀).

    引用: ``services.online_sources.bilibili.client._parse_item``。
    """
    if not isinstance(module_dynamic, dict):
        return []
    images: list[ImageInfo] = []
    major = module_dynamic.get("major") or {}
    opus = major.get("opus") or {}
    for pic in opus.get("pics") or []:
        url = _norm_url(pic.get("url"))
        if url:
            images.append(
                ImageInfo(
                    url=url, width=pic.get("width"), height=pic.get("height")
                )
            )
    dyn_draw = module_dynamic.get("dyn_draw") or {}
    for pic in dyn_draw.get("items") or []:
        url = _norm_url(pic.get("src"))
        if url:
            images.append(
                ImageInfo(
                    url=url, width=pic.get("width"), height=pic.get("height")
                )
            )
    return images


def _extract_video(module_dynamic: dict[str, Any] | None) -> VideoInfo | None:
    """提取视频动态的稿件信息.

    引用: ``services.online_sources.bilibili.client._parse_item``。
    """
    if not isinstance(module_dynamic, dict):
        return None
    major = module_dynamic.get("major") or {}
    for source in (major.get("archive"), module_dynamic.get("dyn_archive")):
        if not isinstance(source, dict):
            continue
        return VideoInfo(
            bvid=source.get("bvid"),
            title=source.get("title"),
            cover=_norm_url(source.get("cover")),
            duration_text=source.get("duration_text"),
            play=(source.get("stat") or {}).get("play"),
        )
    return None


def _extract_topic(module_dynamic: dict[str, Any] | None) -> TopicInfo | None:
    """提取动态话题信息 (v1 格式, 话题在 module_dynamic.topic).

    引用: ``services.online_sources.bilibili.client._parse_item``。
    """
    if not isinstance(module_dynamic, dict):
        return None
    topic = module_dynamic.get("topic")
    if not isinstance(topic, dict) or not topic.get("name"):
        return None
    return TopicInfo(
        id=topic.get("id"),
        name=topic["name"],
        jump_url=_norm_url(topic.get("jump_url")),
    )


def _extract_stat(module_stat: dict[str, Any] | None) -> StatInfo | None:
    """提取点赞 / 评论 / 转发计数.

    引用: ``services.online_sources.bilibili.client._parse_item``。
    """
    if not isinstance(module_stat, dict):
        return None
    counts: dict[str, int | None] = {}
    for key in ("like", "comment", "forward"):
        item = module_stat.get(key)
        counts[key] = (item or {}).get("count") if isinstance(item, dict) else None
    if all(value is None for value in counts.values()):
        return None
    return StatInfo(
        like=counts["like"],
        comment=counts["comment"],
        forward=counts["forward"],
    )


def _forwarded_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """定位转发动态的原始条目.

    引用: ``services.online_sources.bilibili.client._parse_item``。
    """
    orig = item.get("orig")
    if isinstance(orig, dict):
        return orig
    module_dynamic = _find_module(item, "module_dynamic") or {}
    forward = module_dynamic.get("dyn_forward") or {}
    inner = forward.get("item")
    return inner if isinstance(inner, dict) else None


def _parse_item(item: dict[str, Any], depth: int = 0) -> DynamicRecord | None:
    """将一条动态条目解析为统一模型, 转发动态递归展开.

    Args:
        item (dict): 原始动态条目 (v1 或 desktop 格式)。
        depth (int): 当前递归深度, 内部使用。

    Returns:
        DynamicRecord | None: 解析结果; 条目无效或超深时返回 None。
    """
    if not isinstance(item, dict) or depth > MAX_FORWARD_DEPTH:
        return None
    dynamic_id = item.get("id_str")
    if not dynamic_id:
        return None

    module_author = _find_module(item, "module_author")
    module_dynamic = _find_module(item, "module_dynamic") or {}
    module_desc = _find_module(item, "module_desc") or {}
    module_topic = _find_module(item, "module_topic")
    module_stat = _find_module(item, "module_stat")

    text = (module_desc.get("text") or "").strip() or _extract_text(module_dynamic)

    # 话题: v1 格式在 module_dynamic.topic, desktop 格式在 module_topic 模块
    topic = _extract_topic(module_dynamic)
    if topic is None and isinstance(module_topic, dict) and module_topic.get("name"):
        topic = TopicInfo(
            id=module_topic.get("id"),
            name=module_topic["name"],
            jump_url=_norm_url(module_topic.get("jump_url")),
        )

    record = DynamicRecord(
        id=dynamic_id,
        type=item.get("type"),
        author=_extract_author(module_author),
        pub_ts=(module_author or {}).get("pub_ts"),
        text=re.sub(r"\s+", " ", text).strip(),
        topic=topic,
        images=_extract_images(module_dynamic),
        video=_extract_video(module_dynamic),
        stat=_extract_stat(module_stat),
    )

    inner = _forwarded_item(item)
    if inner is not None:
        record.forwarded = _parse_item(inner, depth + 1)
    return record


async def fetch_dynamics(
    uid: int, *, offset: str = ""
) -> tuple[list[DynamicRecord], str, bool]:
    """从指定游标拉取一页动态并解析, 过滤直播推荐卡片.

    解析结果会写入进程内 ``_dynamic_cache``, 供后续按 ID 下载取图。
    每页条数由接口决定 (约 20 条), 页大小不由调用方指定。

    Args:
        uid (int): B 站用户 UID。
        offset (str): 翻页游标, 首页传空字符串。

    Returns:
        tuple[list[DynamicRecord], str, bool]: ``(本页动态, 下一页游标, 是否还有更多)``;
        游标为空串表示已到末页。
    """
    u = user.User(uid=uid)
    records: list[DynamicRecord] = []
    data: dict[str, Any] = {}
    # feed 接口偶发返回空 items 且 has_more=False (风控暂歇), 仅凭 items
    # 非空即视为有效页, 空则退避重试同一游标
    for _ in range(MAX_EMPTY_RETRIES):
        data = await _retry_coro(lambda: u.get_dynamics_new(offset), what="动态一页")
        records = []
        for item in data.get("items") or []:
            if item.get("type") in FILTER_TYPES:
                continue
            record = _parse_item(item)
            if record is not None:
                _dynamic_cache[record.id] = record
                records.append(record)
        if records:
            break
        await asyncio.sleep(0.5)
    next_offset = data.get("offset") or ""
    has_more = bool(data.get("has_more")) and bool(next_offset) and next_offset != offset
    return records, next_offset, has_more


async def resolve_image(dynamic_id: str, index: int) -> tuple[str, str]:
    """按动态 ID 与图片序号解析出原图 URL 与正文文本.

    优先使用已缓存的动态记录; 未命中时重新请求桌面版详情并解析, 结果写入缓存。

    Args:
        dynamic_id (str): 动态 ID。
        index (int): 图片序号 (0 起)。

    Returns:
        tuple[str, str]: ``(原图 URL, 正文文本)``。

    Raises:
        ValueError: 动态无详情、无可用图片或序号越界。
    """
    record = _dynamic_cache.get(dynamic_id)
    if record is None:
        detail = await _desktop_detail(dynamic_id)
        if detail is None:
            raise ValueError(f"动态 {dynamic_id} 无可用详情")
        record = _parse_item(detail)
        if record is None:
            raise ValueError(f"动态 {dynamic_id} 解析失败")
        _dynamic_cache[dynamic_id] = record

    source = record.forwarded if record.forwarded is not None else record
    if source.images:
        if not (0 <= index < len(source.images)):
            raise ValueError(f"动态 {dynamic_id} 无第 {index} 张图")
        img = source.images[index]
        return img.url, source.text or record.text
    if source.video and source.video.cover:
        if index != 0:
            raise ValueError(f"动态 {dynamic_id} 无第 {index} 张图")
        return source.video.cover, source.text or record.text
    raise ValueError(f"动态 {dynamic_id} 无可用图片")


async def fetch_bili_bytes(
    url: str, *, retries: int = 3, base_sleep: float = 1.5
) -> tuple[bytes, str]:
    """带 B 站 Referer 拉取图片字节与 Content-Type.

    供缩略图/大图代理与下载共用, 规避图床防盗链。B 站图床对大图传输存在
    间歇性连接中断, 故对网络异常按指数退避有限重试; 重试耗尽后仍会抛出
    最后一次异常, 不吞没错误。

    Args:
        url (str): B 站图床原图 URL。
        retries (int): 网络异常退避重试次数 (含首次), 默认 3。
        base_sleep (float): 首次退避秒数, 之后指数递增, 默认 1.5。

    Returns:
        tuple[bytes, str]: ``(字节内容, Content-Type)``。

    Raises:
        NetworkError: 网络或 HTTP 状态错误 (重试后仍失败)。

    Callers:
        - ``routes.online_sources.booru_bili_image``
    """
    last_exc: NetworkError | None = None
    for attempt in range(1, retries + 1):
        try:
            async with HttpClient() as client:
                return await client.get_bytes(
                    url, headers=_BILI_HEADERS, timeout=120.0,
                )
        except NetworkError as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(base_sleep * (2 ** (attempt - 1)))
    raise last_exc or NetworkError(f"图片拉取失败: {url}")