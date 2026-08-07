"""Gelbooru 底层客户端: 直连官方 JSON API.

封装 Gelbooru JSON API 的请求与结果解析; 代理从环境变量读取
(经 ``framework.http`` 统一处理)。

Callers:
    - ``gelbooru/source.py`` (搜索 / 下载 / 元数据)
"""

from __future__ import annotations

from pathlib import Path

from services.online_sources.framework import http

_GELBOORU_BASE = "https://gelbooru.com/index.php"


class GelbooruClient:
    """Gelbooru JSON API 客户端.

    Callers:
        - ``gelbooru/source.py:GelbooruSource``
    """

    def __init__(self, proxy: str | None = None) -> None:
        """初始化客户端.

        Args:
            proxy: 上游代理地址, None 表示直连.
        """
        self._proxy = proxy

    async def _api(self, params: dict[str, str | int]) -> dict:
        """调用 Gelbooru JSON API, 返回解析后的响应.

        Args:
            params: 查询参数 (含 page=dapi&s=post&q=index&json=1 等).

        Returns:
            dict: API 响应 JSON.

        Raises:
            http.NetworkError: 网络错误.

        Callers:
            - ``GelbooruClient.get_post`` / ``search`` / ``get_tag_types``
        """
        resp = await http.request(_GELBOORU_BASE, params=params, proxy=self._proxy)
        resp.raise_for_status()
        return resp.json()

    async def get_post(self, pid: int) -> dict:
        """获取单帖元数据.

        Args:
            pid: 帖子 ID.

        Returns:
            dict: 帖子数据; 未找到时返回空字典.

        Raises:
            http.NetworkError: 网络错误.

        Callers:
            - ``GelbooruSource.download`` / ``fetch_meta``
            - ``GelbooruClient.get_image``
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
            http.NetworkError: 网络错误.

        Callers:
            - ``GelbooruSource.search``
        """
        return await self._api({
            "page": "dapi", "s": "post", "q": "index", "json": "1",
            "tags": tags, "pid": str(pid), "limit": "100",
        })

    async def get_image(self, pid: int, path: str) -> None:
        """下载帖子原图到本地文件.

        Args:
            pid: 帖子 ID.
            path: 目标文件路径.

        Raises:
            ValueError: 帖子无 file_url.
            http.NetworkError: 网络错误.

        Callers:
            - ``GelbooruSource.download``
        """
        post = await self.get_post(pid)
        file_url = post.get("file_url")
        if not file_url:
            raise ValueError(f"帖子 {pid} 无 file_url")
        data, _ = await http.get_bytes(
            file_url, referer="https://gelbooru.com/", proxy=self._proxy,
        )
        Path(path).write_bytes(data)

    async def get_tag_types(self, tags: list[str]) -> dict[str, int]:
        """批量查询标签类型.

        Args:
            tags: 标签名列表.

        Returns:
            dict[str, int]: 标签名 -> 类型编号 (0=general, 1=artist, 3=copyright, 4=character, 5=meta).

        Raises:
            http.NetworkError: 网络错误.

        Callers:
            - ``GelbooruSource.download``
        """
        data = await self._api({
            "page": "dapi", "s": "tag", "q": "index", "json": "1",
            "names": " ".join(tags),
        })
        result: dict[str, int] = {}
        for tag in data.get("tag", []):
            if isinstance(tag, dict):
                name = tag.get("name")
                ttype = tag.get("type")
                if name and ttype is not None:
                    result[name] = int(ttype)
        return result