#!/usr/bin/env python3
"""拉取 B 站用户的完整动态列表、详情与图文 (opus), 输出结构化 JSON。

基于专业库 ``bilibili-api`` (https://github.com/Nemo2011/bilibili-api), 免登录
抓取两部分内容:

1. 动态列表: ``User.get_dynamics_new`` (``web-dynamic/v1/feed/space``), 分页
   拉取全部空间动态 (含视频/转发/图文), 转发原文在条目顶层 ``orig`` 字段。
2. 图文 (opus): ``User.get_opus`` (``web-dynamic/v1/opus/feed/space``) 分页
   拉取 ``space.bilibili.com/<uid>/upload/opus`` 页面的全部图文, 再对每条
   用桌面版详情接口 (``web-dynamic/desktop/v1/detail``) 获取完整详情
   (作者/发布时间/文本) 与全部原图。库的 ``Opus.get_info`` 所依赖的 v1
   detail 接口在未登录会话下会被 412 风控, 详情因此改走桌面版接口。

该库自动处理 buvid3/buvid4/bili_ticket 会话、WBI 签名与 dm 设备指纹参数,
规避了手写 requests 调用被 -352/412 风控的问题。

输出:
- ``dynamics.json``: 动态列表, 每条含 id/类型/作者/发布时间/文本/话题/统计/
  图片原图; 转发动态递归解析转发链 (最大深度 8 层)。
- ``opus.json``: 图文列表, 每条含 id/作者/发布时间/文本/话题/统计/全部原图。

用法:
    .venv/bin/python fetch_dynamics.py 178788300 -o out/

Args:
    host_mid (str): B 站用户 UID, 位置参数。

Returns:
    int: 0 表示成功, 非 0 表示失败。

Callers:
    - 命令行直接调用。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests
from pydantic import BaseModel

from bilibili_api import user
from bilibili_api.exceptions import NetworkException

# 转发链递归解析的最大深度, 防止异常深链造成栈溢出
MAX_FORWARD_DEPTH = 8

# feed 接口偶发返回空响应 (同一 offset 请求结果不稳定),
# 空页需重试确认, 连续多次为空才判定列表已抓完
MAX_EMPTY_RETRIES = 3

# 需要过滤的条目类型: 直播推荐卡片并非用户主动发布的动态
FILTER_TYPES = {"DYNAMIC_TYPE_LIVE_RCMD"}


class Author(BaseModel):
    """动态/图文作者信息。

    Args:
        mid (int | None): 作者 UID。
        name (str | None): 作者昵称。
        pub_ts (int | str | None): 发布时间戳 (desktop 接口为 int,
            v1 接口为字符串)。
    """

    mid: int | None
    name: str | None
    pub_ts: int | str | None


class ImageInfo(BaseModel):
    """图片原图信息。

    Args:
        url (str): 原图 URL (无 ``@`` 缩略图后缀)。
        width (int | None): 图片宽度。
        height (int | None): 图片高度。
    """

    url: str
    width: int | None
    height: int | None


class TopicInfo(BaseModel):
    """话题信息。

    Args:
        id (int | str | None): 话题 ID。
        name (str): 话题名称。
        jump_url (str | None): 话题跳转链接。
    """

    id: int | str | None
    name: str
    jump_url: str | None


class VideoInfo(BaseModel):
    """视频稿件信息。

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
    """动态统计信息。

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

# 桌面版详情接口: 免登录稳定可用; 库的 ``Dynamic.get_info`` 所用的 v1 detail
# 接口在未登录会话下会被 412 风控拦截, 故单条详情改走此接口
_DESKTOP_DETAIL_API = "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/detail"
_HOME_URL = "https://www.bilibili.com/"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
_desktop_session_cache: requests.Session | None = None


def _desktop_session() -> requests.Session:
    """获取带访客会话的请求会话, 进程内复用。

    桌面版接口同样要求先访问首页获得 ``buvid3`` Cookie 才会放行。

    Returns:
        requests.Session: 已建立访客会话的请求对象。
    """
    global _desktop_session_cache
    if _desktop_session_cache is None:
        session = requests.Session()
        session.headers.update(
            {
                "user-agent": _UA,
                "referer": "https://space.bilibili.com/",
                "accept": "application/json, text/plain, */*",
            }
        )
        resp = session.get(_HOME_URL, timeout=30)
        resp.raise_for_status()
        _desktop_session_cache = session
    return _desktop_session_cache


def _desktop_detail(
    dynamic_id: str, *, retries: int = 3, base_sleep: float = 3.0
) -> dict | None:
    """免登录获取单条动态完整详情 (桌面版接口, 带退避重试)。

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
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = _desktop_session().get(
                _DESKTOP_DETAIL_API,
                params={"id": dynamic_id, "timezone_offset": str(-480)},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            code = data.get("code")
            if code == -1024:
                # 不支持的动态类型, 无可用详情, 由调用方跳过
                return None
            if code != 0:
                raise RuntimeError(
                    f"code={code} message={data.get('message')}"
                )
            return (data.get("data") or {}).get("item") or {}
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                wait = base_sleep * (2 ** (attempt - 1))
                print(f"详情 {dynamic_id} 请求失败 ({exc}), {wait:.0f} 秒后重试...")
                time.sleep(wait)
    raise last_exc or RuntimeError(f"详情 {dynamic_id} 请求失败")


async def _retry_coro(
    factory: Any, *, what: str, retries: int = 3, base_sleep: float = 3.0
) -> dict:
    """对间歇性风控异常退避重试接口调用。

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
    last_exc: NetworkException | None = None
    for attempt in range(1, retries + 1):
        try:
            return await factory()
        except NetworkException as exc:
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
    """将协议相对 URL (``//``) 补全为 https。

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
    """从动态条目中按模块名取模块, 兼容 dict 与 list 两种 modules 格式。

    v1 格式 ``modules`` 为 dict, key 即模块名 (如 ``module_author``);
    desktop 格式 ``modules`` 为 list, 每项形如
    ``{"module_type": "MODULE_TYPE_AUTHOR", "module_author": {...}}``。
    注意 desktop 条目的每个元素都带全部 ``module_*`` 键 (空值), 因此
    list 分支必须按键值非空取模块, 否则会误命中空元素。

    Args:
        item (dict): 动态条目。
        key (str): 模块名, 如 ``module_dynamic``。

    Returns:
        dict | None: 模块内容; 不存在时返回 None。
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
    """提取作者信息 (mid / name / 发布时间), 兼容 v1 与 desktop 两种布局。

    v1 格式 ``module_author`` 顶层直接带 mid/name/pub_ts;
    desktop 格式作者信息在 ``module_author.user`` 下。

    Args:
        module_author (dict | None): module_author 模块内容。

    Returns:
        Author | None: 作者信息; 无内容时返回 None。
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
    """提取动态正文文本, 兼容多种载体。

    - opus 图文: ``major.opus.summary.text`` (或标题)。
    - 普通/转发动态: ``desc.text``。

    Args:
        module_dynamic (dict | None): module_dynamic 模块内容。

    Returns:
        str: 规范化空白后的正文; 无文本时返回空字符串。
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
    """提取图片原图列表 (URL 无 ``@`` 缩略图后缀)。

    v1 格式图片在 ``major.opus.pics[].url``; desktop 格式在
    ``dyn_draw.items[].src``。

    Args:
        module_dynamic (dict | None): module_dynamic 模块内容。

    Returns:
        list[ImageInfo]: 原图信息列表。
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
    """提取视频动态的稿件信息。

    v1 格式在 ``major.archive``; desktop 格式在 ``dyn_archive``。

    Args:
        module_dynamic (dict | None): module_dynamic 模块内容。

    Returns:
        VideoInfo | None: 视频稿件信息; 非视频动态返回 None。
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
    """提取动态话题信息 (v1 格式, 话题在 module_dynamic.topic)。

    Args:
        module_dynamic (dict | None): module_dynamic 模块内容。

    Returns:
        TopicInfo | None: 话题信息; 无话题时返回 None。
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
    """提取点赞 / 评论 / 转发计数。

    Args:
        module_stat (dict | None): module_stat 模块内容。

    Returns:
        StatInfo | None: 统计信息; 无内容时返回 None。
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
    """定位转发动态的原始条目。

    v1 格式原文在条目顶层 ``orig``; desktop 格式在
    ``module_dynamic.dyn_forward.item``。

    Args:
        item (dict): 转发动态条目。

    Returns:
        dict | None: 原始动态条目; 非转发或无原文时返回 None。
    """
    orig = item.get("orig")
    if isinstance(orig, dict):
        return orig
    module_dynamic = _find_module(item, "module_dynamic") or {}
    forward = module_dynamic.get("dyn_forward") or {}
    inner = forward.get("item")
    return inner if isinstance(inner, dict) else None


def _parse_item(item: dict[str, Any], depth: int = 0) -> DynamicRecord | None:
    """将一条动态条目解析为统一模型, 转发动态递归展开。

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


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    Returns:
        argparse.Namespace: 参数对象。
    """
    parser = argparse.ArgumentParser(
        description="拉取 B 站用户完整动态列表及详情 (基于 bilibili-api)"
    )
    parser.add_argument("host_mid", type=str, help="B 站用户 UID")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        required=True,
        help="输出目录, 写入 dynamics.json",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=0,
        help="最多拉取的动态条数, 0 表示全部, 默认 0",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="翻页间隔秒数, 默认 0.5",
    )
    return parser.parse_args()


async def fetch_all(uid: int, limit: int, sleep_seconds: float) -> list[DynamicRecord]:
    """分页拉取全部动态并解析, 过滤直播推荐卡片。

    Args:
        uid (int): B 站用户 UID。
        limit (int): 最大条数, 0 表示不限制。
        sleep_seconds (float): 翻页间隔。

    Returns:
        list[DynamicRecord]: 解析后的动态记录, 按发布时间倒序 (接口顺序)。
    """
    u = user.User(uid=uid)
    records: list[DynamicRecord] = []
    offset = ""
    page = 0
    empty_retries = 0
    while True:
        page += 1
        data = await _retry_coro(
            lambda: u.get_dynamics_new(offset),
            what=f"动态第 {page} 页",
        )
        items = data.get("items") or []
        if not items:
            # 接口偶发空响应, 重试同一 offset 确认是否真正抓完
            empty_retries += 1
            if empty_retries >= MAX_EMPTY_RETRIES:
                print(f"第 {page} 页连续 {empty_retries} 次为空, 判定列表抓取完毕")
                break
            print(f"第 {page} 页返回空, 重试 {empty_retries}/{MAX_EMPTY_RETRIES}...")
            await asyncio.sleep(sleep_seconds * empty_retries)
            continue
        empty_retries = 0
        print(
            f"第 {page} 页: {len(items)} 条, has_more={data.get('has_more')}, "
            f"累计 {len(records)} 条"
        )
        for item in items:
            if item.get("type") in FILTER_TYPES:
                continue
            record = _parse_item(item)
            if record is not None:
                records.append(record)
                if limit > 0 and len(records) >= limit:
                    return records[:limit]
        if limit > 0 and len(records) >= limit:
            return records[:limit]
        next_offset = data.get("offset") or ""
        if not data.get("has_more") or not next_offset:
            if data.get("has_more") and not next_offset:
                print("接口未返回 offset, 停止翻页", file=sys.stderr)
            break
        if next_offset == offset:
            print("检测到 offset 未推进, 停止翻页", file=sys.stderr)
            break
        offset = next_offset
        await asyncio.sleep(sleep_seconds)
    return records


async def fetch_all_opus(
    uid: int, limit: int, sleep_seconds: float
) -> list[DynamicRecord]:
    """分页拉取用户全部图文 (opus) 及每条的全部原图。

    先通过 ``User.get_opus`` 分页取得图文列表 (封面图索引), 再对每条调用
    ``_desktop_detail`` (桌面版详情接口, 免登录稳定可用) 获取完整详情与
    全部原图。列表翻页经 ``_retry_coro`` 对间歇性风控退避重试; 列表空页按
    ``MAX_EMPTY_RETRIES`` 连续重试后判定抓完。

    Args:
        uid (int): B 站用户 UID。
        limit (int): 最大条数, 0 表示不限制。
        sleep_seconds (float): 请求间隔。

    Returns:
        list[DynamicRecord]: 解析后的图文记录, 按发布时间倒序 (接口顺序)。
    """
    u = user.User(uid=uid)
    records: list[DynamicRecord] = []
    offset = ""
    page = 0
    empty_retries = 0
    while True:
        page += 1
        data = await _retry_coro(
            lambda: u.get_opus(offset=offset),
            what=f"opus 列表第 {page} 页",
        )
        items = data.get("items") or []
        if not items:
            empty_retries += 1
            if empty_retries >= MAX_EMPTY_RETRIES:
                print(f"opus 第 {page} 页连续 {empty_retries} 次为空, 列表抓取完毕")
                break
            print(
                f"opus 第 {page} 页返回空, 重试 {empty_retries}/{MAX_EMPTY_RETRIES}..."
            )
            await asyncio.sleep(sleep_seconds * empty_retries)
            continue
        empty_retries = 0
        for item in items:
            opus_id = item.get("opus_id")
            if not opus_id:
                continue
            detail = await asyncio.to_thread(_desktop_detail, opus_id)
            if detail is None:
                print(f"[skip] opus {opus_id}: 桌面版接口不支持的动态类型, 跳过")
                continue
            record = _parse_item(detail)
            if record is not None:
                records.append(record)
                print(
                    f"[{len(records)}] opus {opus_id}: 图 {len(record.images)} 张"
                )
                if limit > 0 and len(records) >= limit:
                    return records[:limit]
            await asyncio.sleep(sleep_seconds)
        if limit > 0 and len(records) >= limit:
            return records[:limit]
        next_offset = data.get("offset") or ""
        if not data.get("has_more") or not next_offset:
            break
        if next_offset == offset:
            print("检测到 opus offset 未推进, 停止翻页", file=sys.stderr)
            break
        offset = next_offset
        await asyncio.sleep(sleep_seconds)
    return records


def main() -> int:
    """脚本入口: 抓取动态与图文, 输出 JSON。"""
    args = parse_args()
    out_dir = args.output_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    uid = int(args.host_mid)
    if args.limit > 0:
        print(f"仅取最新 {args.limit} 条")

    async def _run() -> None:
        # 1) 动态列表
        print("=== 拉取动态列表 ===")
        records = await fetch_all(uid, args.limit, args.sleep)
        print(f"共解析 {len(records)} 条动态")
        output = out_dir / "dynamics.json"
        output.write_text(
            json.dumps(
                [rec.model_dump(mode="json") for rec in records],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"已写入 {output}")
        counter = Counter(rec.type for rec in records)
        print("类型分布:", dict(counter))
        with_forward = sum(1 for rec in records if rec.forwarded)
        print(f"含图片: {sum(1 for rec in records if rec.images)} 条, "
              f"含转发: {with_forward} 条")

        # 2) 图文 (opus)
        print("\n=== 拉取图文 (opus) 列表 ===")
        opus_records = await fetch_all_opus(uid, args.limit, args.sleep)
        print(f"共解析 {len(opus_records)} 条图文")
        output = out_dir / "opus.json"
        output.write_text(
            json.dumps(
                [rec.model_dump(mode="json") for rec in opus_records],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"已写入 {output}")
        total_images = sum(len(rec.images) for rec in opus_records)
        print(f"图文总数: {len(opus_records)} 条, 原图总数: {total_images} 张")

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
