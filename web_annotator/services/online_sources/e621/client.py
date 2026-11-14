"""e621 网络层: 帖子索引、标签补全与图床请求.

e621 要求所有请求携带非空且具描述性的 User-Agent, 并明确拒绝冒充浏览器的
UA 与语言/库默认 UA, 故本模块统一以 ``_USER_AGENT`` 发送请求。认证
(login / api_key) 可选, 直接从 ``os.environ`` 读取, 不跨层传参。

Callers:
    - ``services/online_sources/e621/source.py:`` (搜索 / 下载 / 元数据)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from services.online_sources.framework.http import HttpClient
from services.online_sources.framework.utils import save_png_bytes

_E621_BASE = "https://e621.net"
# e621 要求具描述性的 User-Agent, 禁止冒充浏览器或使用语言/库默认 UA
_USER_AGENT = "ZhaoLora-WebAnnotator/1.0"
_API_HEADERS = {"user-agent": _USER_AGENT, "accept": "application/json"}
_IMAGE_HEADERS = {"user-agent": _USER_AGENT, "accept": "image/*,*/*;q=0.8"}
# 单页条数上限 (e621 硬上限 320)
PAGE_LIMIT = 320


def http_error(resp: Any, what: str) -> RuntimeError:
    """构造带上游说明的 HTTP 异常, 供各调用点直接抛出.

    e621 的 JSON 错误体为 ``{"success": false, "reason": ...}``, 取
    ``reason`` / ``message`` 作为详情; 非 JSON 响应 (如 Cloudflare 质询页)
    标注内容类型, 便于区分认证 / 限流 / 边缘拦截。

    Args:
        resp (Any): ``httpx.Response``。
        what (str): 请求用途, 如 ``"搜索"`` / ``"元数据"`` / ``"标签补全"``。

    Returns:
        RuntimeError: 含状态码与上游详情的异常。

    Callers:
        - ``services/online_sources/e621/client.py:E621Client._api``
        - ``services/online_sources/e621/client.py:E621Client.get_image``
    """
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    detail = ""
    if ctype == "application/json":
        payload = resp.json()
        if isinstance(payload, dict):
            detail = str(payload.get("reason") or payload.get("message") or "").strip()
    if detail:
        return RuntimeError(f"e621 {what} HTTP {resp.status_code}: {detail}")
    return RuntimeError(
        f"e621 {what} HTTP {resp.status_code} (Content-Type: {ctype or '未知'})"
    )


class E621Client:
    """e621 JSON API 客户端.

    Callers:
        - ``services/online_sources/e621/source.py:E621Source``
    """

    async def _api(self, path: str, params: dict[str, str], what: str) -> Any:
        """调用 e621 JSON 接口并返回解析后的响应.

        Args:
            path: 接口路径, 如 ``/posts.json``。
            params: 查询参数。
            what: 请求用途, 用于异常信息。

        Returns:
            Any: 响应 JSON。

        Raises:
            RuntimeError: 上游返回非 200。
            NetworkError: 网络错误。

        Callers:
            - ``services/online_sources/e621/client.py:E621Client.search``
            - ``services/online_sources/e621/client.py:E621Client.get_post``
            - ``services/online_sources/e621/client.py:E621Client.autocomplete``
        """
        query = {**params, **self._auth_params()}
        async with HttpClient(headers=_API_HEADERS) as client:
            resp = await client.get(
                f"{_E621_BASE}{path}", params=query, follow_redirects=True,
            )
        if resp.status_code != 200:
            raise http_error(resp, what)
        return resp.json()

    async def search(self, tags: str, page: int) -> list[dict]:
        """搜索帖子.

        Args:
            tags: 标签查询串 (可含 e621 元标签)。
            page: 页码 (1-based)。

        Returns:
            list[dict]: 帖子原始数据列表。

        Raises:
            RuntimeError: 上游返回非 200 或响应缺少 ``posts`` 列表。
            NetworkError: 网络错误。

        Callers:
            - ``services/online_sources/e621/source.py:E621Source.search``
        """
        data = await self._api(
            "/posts.json",
            {"tags": tags, "page": str(page), "limit": str(PAGE_LIMIT)},
            "搜索",
        )
        posts = data.get("posts") if isinstance(data, dict) else None
        if not isinstance(posts, list):
            raise RuntimeError("e621 搜索响应缺少 posts 列表")
        return [p for p in posts if isinstance(p, dict)]

    async def get_post(self, pid: int) -> dict:
        """获取单帖元数据.

        Args:
            pid: 帖子 ID.

        Returns:
            dict: 帖子原始数据。

        Raises:
            RuntimeError: 上游返回非 200 或响应缺少 ``post`` 对象。
            NetworkError: 网络错误。

        Callers:
            - ``services/online_sources/e621/source.py:E621Source.fetch_meta``
        """
        data = await self._api(f"/posts/{pid}.json", {}, "元数据")
        post = data.get("post") if isinstance(data, dict) else None
        if not isinstance(post, dict):
            raise RuntimeError(f"e621 元数据响应缺少 post: {pid}")
        return post

    async def get_image(self, file_url: str, path: str, *, referer: str) -> None:
        """下载原图字节, 校验后转为 PNG 写入磁盘.

        校验与写入复用 ``utils.save_png_bytes``, 保证写盘内容必为可识别图片;
        HTML / zip / video 等非图片数据在此被拒绝, 不会以图片扩展名写入磁盘。

        Args:
            file_url: 原图 URL (取自帖子 ``file.url``)。
            path: 目标 PNG 文件路径。
            referer: 防盗链 Referer。

        Raises:
            RuntimeError: 上游返回非 200。
            ValueError: 下载内容非图片 (html/zip/video/webm 等)。
            NetworkError: 网络错误。

        Callers:
            - ``services/online_sources/e621/source.py:E621Source.download``
        """
        async with HttpClient(headers=_IMAGE_HEADERS) as client:
            resp = await client.get(
                file_url, headers={**_IMAGE_HEADERS, "referer": referer},
                timeout=60.0, follow_redirects=True,
            )
        if resp.status_code != 200:
            raise http_error(resp, "图片")
        await asyncio.to_thread(save_png_bytes, resp.content, Path(path))

    def _auth_params(self) -> dict[str, str]:
        """从环境变量读取可选的 e621 认证参数.

        未配置 ``E621_LOGIN`` 与 ``E621_API_KEY`` 时返回空字典, 匿名可完成
        公开搜索与元数据读取。

        Returns:
            dict[str, str]: 认证查询参数; 未配置时为空字典。

        Callers:
            - ``services/online_sources/e621/client.py:E621Client._api``
        """
        login = os.environ.get("E621_LOGIN", "").strip()
        key = os.environ.get("E621_API_KEY", "").strip()
        if not (login and key):
            return {}
        return {"login": login, "api_key": key}

    async def autocomplete(self, query: str, limit: int = 10) -> list[dict]:
        """标签自动补全 (``/tags.json`` 前缀匹配, 按使用次数降序).

        Args:
            query: 标签前缀 (不含通配符)。
            limit: 返回条数上限。

        Returns:
            规范化的建议列表: 每项为 ``{"name", "category", "count"}``,
            ``category`` 为 e621 原始分类编号 (0 通用 / 1 画师 / 2 贡献 /
            3 版权 / 4 角色 / 5 物种 / 6 无效 / 7 元 / 8 设定)。

        Raises:
            RuntimeError: 上游返回非 200 或响应不是数组。
            NetworkError: 网络错误。

        Callers:
            - ``services/online_sources/e621/source.py:E621Source.autocomplete``
        """
        pattern = query if query.endswith("*") else f"{query}*"
        data = await self._api(
            "/tags.json",
            {
                "search[name_matches]": pattern,
                "search[order]": "count",
                "limit": str(limit),
            },
            "标签补全",
        )
        if not isinstance(data, list):
            raise RuntimeError("e621 标签补全响应不是数组")
        out: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            out.append({
                "name": str(name),
                "category": int(item.get("category") or 0),
                "count": int(item.get("post_count") or 0),
            })
        return out
