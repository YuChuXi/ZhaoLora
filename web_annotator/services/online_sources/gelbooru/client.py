"""Gelbooru 底层客户端: 直连官方 JSON API.

封装 Gelbooru JSON API 的请求与结果解析; 代理从 ``HTTP_PROXY`` / ``HTTPS_PROXY``
环境变量自动读取 (``httpx`` 原生支持)。

Callers:
    - ``services/online_sources/gelbooru/source.py:`` (搜索 / 下载 / 元数据)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from services.online_sources.framework.http import HttpClient, NetworkError
from services.online_sources.framework.utils import save_png_bytes

_GELBOORU_BASE = "https://gelbooru.com/index.php"


class GelbooruClient:
    """Gelbooru JSON API 客户端.

    Callers:
        - ``services/online_sources/gelbooru/source.py:GelbooruSource``
    """

    async def _api(self, params: dict[str, str | int]) -> dict:
        """调用 Gelbooru JSON API, 自动注入认证参数, 返回解析后的响应.

        Args:
            params: 查询参数 (含 page=dapi&s=post&q=index&json=1 等).

        Returns:
            dict: API 响应 JSON.

        Raises:
            NetworkError: 网络错误.

        Callers:
            - ``services/online_sources/gelbooru/client.py:GelbooruClient.get_post``
            - ``services/online_sources/gelbooru/client.py:search``
            - ``services/online_sources/gelbooru/client.py:get_tag_types``
            - ``services/online_sources/gelbooru/client.py:autocomplete``
        """
        params.update(self._auth_params())
        async with HttpClient() as client:
            resp = await client.get(_GELBOORU_BASE, params=params, follow_redirects=True)
            resp.raise_for_status()
            return resp.json()

    async def get_post(self, pid: int) -> dict:
        """获取单帖元数据.

        Args:
            pid: 帖子 ID.

        Returns:
            dict: 帖子数据; 未找到时返回空字典.

        Raises:
            NetworkError: 网络错误.

        Callers:
            - ``services/online_sources/gelbooru/source.py:GelbooruSource.fetch_meta``
        """
        data = await self._api({
            "page": "dapi", "s": "post", "q": "index", "json": "1",
            "id": str(pid),
        })
        post = data.get("post")
        if isinstance(post, list):
            return post[0] if post else {}
        return post or {}

    async def search(self, tags: str, pid: int) -> dict:
        """搜索帖子.

        Args:
            tags: 标签查询串.
            pid: 页码 (0-based).

        Returns:
            dict: API 响应 JSON.

        Raises:
            NetworkError: 网络错误.

        Callers:
            - ``services/online_sources/gelbooru/source.py:GelbooruSource.search``
        """
        return await self._api({
            "page": "dapi", "s": "post", "q": "index", "json": "1",
            "tags": tags, "pid": str(pid), "limit": "100",
        })

    async def get_image(self, file_url: str, path: str, *, referer: str = "https://gelbooru.com/") -> None:
        """下载原图字节, 校验后转为 PNG 写入磁盘.

        校验与写入逻辑与 ``SourceClient._save_png`` 一致 (共用
        ``utils.save_png_bytes``), 保证写盘内容必为可识别图片; 下载到的
        HTML/zip/video 等非图片数据在此被拒绝, 不会以图片扩展名写入磁盘。

        Args:
            file_url: 原图 URL (取自帖子 file_url).
            path: 目标 PNG 文件路径.
            referer: 防盗链 Referer.

        Raises:
            ValueError: 下载内容非图片 (html/zip/video/webm 等).
            NetworkError: 网络错误.

        Callers:
            - ``services/online_sources/gelbooru/source.py:GelbooruSource.download``
        """
        async with HttpClient() as client:
            data, _ = await client.get_bytes(file_url, referer=referer)
        await asyncio.to_thread(save_png_bytes, data, Path(path))

    def _auth_params(self) -> dict[str, str]:
        """从环境变量读取 Gelbooru API 认证参数.

        Gelbooru 的 post / tag 索引查询均需 api_key + user_id, 否则返回 401。
        凭据直接经 ``os.environ`` 读取 (GELBOORU_API_KEY / GELBOORU_USER_ID),
        不跨层传参; 未配置时返回空字典。

        Returns:
            dict[str, str]: 认证查询参数; 未配置时为空字典.

        Callers:
            - ``services/online_sources/gelbooru/client.py:GelbooruClient._api``
        """
        out: dict[str, str] = {}
        key = os.environ.get("GELBOORU_API_KEY", "").strip()
        uid = os.environ.get("GELBOORU_USER_ID", "").strip()
        if key and uid:
            out["api_key"] = key
            out["user_id"] = uid
        return out

    async def get_tag_types(self, tags: list[str]) -> dict[str, int]:
        """批量查询标签类型.

        Args:
            tags: 标签名列表.

        Returns:
            dict[str, int]: 标签名 -> 类型编号 (0=general, 1=artist, 3=copyright, 4=character, 5=meta).

        Raises:
            NetworkError: 网络错误.

        Callers:
            - ``services/online_sources/gelbooru/source.py:GelbooruSource.download``
        """
        params: dict[str, str | int] = {
            "page": "dapi", "s": "tag", "q": "index", "json": "1",
            "names": " ".join(tags),
        }
        data = await self._api(params)
        result: dict[str, int] = {}
        for tag in data.get("tag", []):
            if isinstance(tag, dict):
                name = tag.get("name")
                ttype = tag.get("type")
                if name and ttype is not None:
                    result[name] = int(ttype)
        return result

    async def autocomplete(self, query: str, limit: int = 10) -> list[dict]:
        """标签自动补全 (tag 索引按前缀匹配, 按使用次数排序).

        Args:
            query: 标签前缀.
            limit: 返回条数上限.

        Returns:
            规范化的建议列表: 每项为 ``{"name", "category", "count"}``。

        Raises:
            NetworkError: 网络错误.

        Callers:
            - ``services/online_sources/gelbooru/source.py:GelbooruSource.autocomplete``
        """
        params: dict[str, str | int] = {
            "page": "dapi", "s": "tag", "q": "index", "json": "1",
            "name_pattern": query, "limit": str(limit), "order": "count",
        }
        data = await self._api(params)
        raw = data.get("tag")
        tags = [raw] if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        out: list[dict] = []
        for t in tags:
            if not isinstance(t, dict):
                continue
            name = t.get("name")
            if not name:
                continue
            out.append({
                "name": str(name),
                "category": int(t.get("type") or 0),
                "count": int(t.get("count") or 0),
            })
        return out
