"""Gelbooru 底层客户端: 直连官方 JSON API.

封装 Gelbooru JSON API 的请求与结果解析; 代理从 ``HTTP_PROXY`` / ``HTTPS_PROXY``
环境变量自动读取 (``httpx`` 原生支持)。

Callers:
    - ``services/online_sources/gelbooru/source.py:`` (搜索 / 下载 / 元数据)
"""

from __future__ import annotations

import asyncio
import html
import os
from pathlib import Path
from typing import Any

from services.online_sources.framework.http import HttpClient, NetworkError
from services.online_sources.framework.utils import save_png_bytes

_GELBOORU_BASE = "https://gelbooru.com/index.php"
# gelbooru 图床需携带该 cookie 才能正常返回原图
_IMAGE_COOKIE = "fringeBenefits=yup"
# autocomplete2 返回的分类名 → Gelbooru tag 类型数字 (与 Danbooru tag category 一致)
_AUTOCOMPLETE_CATEGORY: dict[str, int] = {
    "general": 0, "tag": 0,
    "artist": 1,
    "copyright": 3,
    "character": 4,
    "meta": 5,
}


def _unescape(data: Any) -> Any:
    """递归反转义 Gelbooru API 响应中的 HTML 实体.

    Gelbooru API 会把 tag 中的特殊字符(如单引号)编码为 HTML 实体
    (如 ``&#039;``), 需在上游还原为字面字符, 避免脏数据进入保存/显示。

    Args:
        data: API 响应 JSON (dict / list / str / 标量)。

    Returns:
        反转义后的同构数据。

    Callers:
        - ``services/online_sources/gelbooru/client.py:GelbooruClient._api``
    """
    if isinstance(data, dict):
        return {k: _unescape(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_unescape(x) for x in data]
    if isinstance(data, str):
        return html.unescape(data)
    return data


class GelbooruClient:
    """Gelbooru JSON API 客户端.

    Callers:
        - ``services/online_sources/gelbooru/source.py:GelbooruSource``
    """

    async def _api(self, params: dict[str, str | int]) -> Any:
        """调用 Gelbooru HTTP 接口, 自动注入认证参数, 返回解析后的响应.

        dapi 查询返回 dict; autocomplete2 返回 list。认证参数仅对需要认证的
        dapi 查询有意义, 对匿名端点(如 autocomplete2)为多余参数但无害。

        Args:
            params: 查询参数 (page=autocomplete2 或 page=dapi&s=...&q=index 等).

        Returns:
            Any: 响应 JSON (dict 或 list).

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
            return _unescape(post[0]) if post else {}
        return _unescape(post) if isinstance(post, dict) else (post or {})

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
        data = await self._api({
            "page": "dapi", "s": "post", "q": "index", "json": "1",
            "tags": tags, "pid": str(pid), "limit": "100",
        })
        post = data.get("post")
        if isinstance(post, list):
            data["post"] = [_unescape(x) for x in post]
        elif isinstance(post, dict):
            data["post"] = _unescape(post)
        return data

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
        async with HttpClient(headers={"cookie": _IMAGE_COOKIE}) as client:
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
                name = html.unescape(str(tag.get("name")))
                ttype = tag.get("type")
                if name and ttype is not None:
                    result[name] = int(ttype)
        return result

    async def autocomplete(self, query: str, limit: int = 10) -> list[dict]:
        """标签自动补全 (官方 autocomplete2 端点, 支持 * 通配符).

        autocomplete2 返回的 tag 名为字面字符(不含 HTML 实体), 无需反转义;
        支持 ``*`` 通配符, 行为与 Gelbooru 前端一致。

        Args:
            query: 标签前缀(可含 * 通配符).
            limit: 返回条数上限.

        Returns:
            规范化的建议列表: 每项为 ``{"name", "category", "count"}``, 按使用次数降序。

        Raises:
            NetworkError: 网络错误.

        Callers:
            - ``services/online_sources/gelbooru/source.py:GelbooruSource.autocomplete``
        """
        params: dict[str, str | int] = {
            "page": "autocomplete2",
            "term": query,
            "type": "tag_query",
            "limit": str(limit),
        }
        data = await self._api(params)
        out: list[dict] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            name = item.get("value")
            if not name:
                continue
            out.append({
                "name": str(name),
                "category": _AUTOCOMPLETE_CATEGORY.get(str(item.get("category")), 0),
                "count": int(item.get("post_count") or 0),
            })
        return out
