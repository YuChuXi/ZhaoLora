"""Gelbooru API 客户端（下载 / 标签类型查询 / 随机检索）。

API 凭据 (api_key / user_id) 的设置优先级从高到低：
1. 构造函数参数：``Gelbooru(api_key=..., user_id=...)``
2. 环境变量：``GELBOORU_API_KEY`` / ``GELBOORU_USER_ID``
3. 模块默认常量 ``_DEFAULT_API_KEY`` / ``_DEFAULT_USER_ID``

Callers:
    - ``download_dataset.py`` / ``gelbooru_interactive_download.py``
    - ``gelbooru_tag_cooccurrence.py`` / ``DanbooruApi/__init__.py``
"""

import re
import asyncio
import httpx
import json
import os
import random
from urllib.parse import quote
from .Danbooru import Danbooru


# Gelbooru API 凭据默认值（未通过环境变量或构造参数指定时使用）
_DEFAULT_API_KEY = "***REMOVED***"
_DEFAULT_USER_ID = "***REMOVED***"


class Gelbooru(Danbooru):

    # 站点内容确认（含受限标签检索）
    _COOKIES = {"fringeBenefits": "yup"}

    def __init__(self, *args, api_key: str | None = None, user_id: str | None = None, **kwargs) -> None:
        """初始化 Gelbooru 客户端并解析 API 凭据。

        凭据优先级：构造参数 > 环境变量 ``GELBOORU_API_KEY`` / ``GELBOORU_USER_ID``
        > 模块默认常量。

        Args:
            api_key (str | None): Gelbooru API 密钥，不传时依次取环境变量与默认常量。
            user_id (str | None): Gelbooru 用户 ID，不传时依次取环境变量与默认常量。

        Callers:
            - ``DanbooruApi/__init__.py`` / ``download_dataset.py``
            - ``gelbooru_interactive_download.py`` / ``gelbooru_tag_cooccurrence.py``
        """
        super().__init__(*args, **kwargs)
        self.api_key = api_key or os.environ.get("GELBOORU_API_KEY") or _DEFAULT_API_KEY
        self.user_id = user_id or os.environ.get("GELBOORU_USER_ID") or _DEFAULT_USER_ID

    async def get(self, url: str) -> httpx.Response:
        """发起 Gelbooru HTTP GET，并附带 fringeBenefits cookie。

        引用:
            ``getJson`` / ``Danbooru.get`` 的 Gelbooru 覆盖。
        """
        print(url)
        async with httpx.AsyncClient(
            proxy=self.proxy,
            cookies=self._COOKIES,
            timeout=self.timeout,
        ) as client:
            return await client.get(url=url, headers=self.headers)

    async def getJson(self, args: str) -> dict:
        url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&{args}&api_key={self.api_key}&user_id={self.user_id}"
        req: httpx.Response = await self.get(url)
        if not req.content:
            return {}
        try:
            return json.loads(req.content)
        except json.JSONDecodeError:
            # This can happen on pages with no results, as the API returns an empty body.
            print(req.content)
            return {}

    async def getPost(self, ID: int) -> dict:
        r = await self.getJson(f"id={ID}")
        raw = r.get("post")
        if raw is None:
            raise KeyError(ID)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, list):
            if len(raw) == 0:
                raise KeyError(ID)
            return raw[0]
        raise TypeError(raw)

    async def getTagTypes(self, names: list[str]) -> dict[str, int]:
        """批量查询标签类型（0=general, 1=artist, 3=copyright, 4=character, 5=meta）。

        引用：``media.download_post`` / ``media.regenerate_captions``。
        """
        if not names:
            return {}
        enc = quote(" ".join(names), safe="")
        url = f"https://gelbooru.com/index.php?page=dapi&s=tag&q=index&json=1&names={enc}&api_key={self.api_key}&user_id={self.user_id}"
        req = await self.get(url)
        if not req.content:
            return {}
        try:
            data = json.loads(req.content)
        except json.JSONDecodeError:
            return {}
        result: dict[str, int] = {}
        raw = data.get("tag")
        tags = []
        if isinstance(raw, list):
            tags = raw
        elif isinstance(raw, dict):
            tags = [raw]
        for t in tags:
            name = str(t.get("name") or "")
            typ = int(t.get("type") or 0)
            if name:
                result[name] = typ
        return result

    async def getPosts(self, tags: str, pid: int = 0) -> list:
        # metatags 含 :、>=、空格等，必须编码，否则查询无效或截断
        enc = quote(tags, safe="")
        json_response = await self.getJson(f"tags={enc}&pid={pid}")
        raw = json_response.get("post")
        if raw is None:
            return []
        if isinstance(raw, dict):
            return [raw]
        return list(raw)

    # Gelbooru 图床要求带 Referer，否则返回 403 的 HTML 页面
    _IMAGE_REFERER = "https://gelbooru.com/"

    def _is_image_content(self, data: bytes) -> bool:
        """通过文件头判断是否为真实图片，避免把 403/HTML 当图片保存。"""
        if not data or len(data) < 12:
            return False
        # PNG: 89 50 4E 47 0D 0A 1A 0A
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return True
        # JPEG: FF D8 FF
        if data[:3] == b"\xff\xd8\xff":
            return True
        # GIF: GIF87a / GIF89a
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return True
        # WebP: RIFF....WEBP
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return True
        return False

    async def getImage(self, ID: int, path: str) -> dict:
        """下载原图；遇 HTTP 429 按 Retry-After / 指数退避重试。

        引用: ``web_annotator.services.online_sources.framework.media.download_post``。
        """
        post = await self.getPost(ID)
        imageUrl = post["file_url"]
        headers = {**self.headers, "referer": self._IMAGE_REFERER}
        delay = 2.0
        last_err = f"Image {ID} download failed"
        async with httpx.AsyncClient(
            proxy=self.proxy,
            timeout=self.timeout,
            cookies=self._COOKIES,
        ) as client:
            for attempt in range(10):
                resp = await client.get(imageUrl, headers=headers)
                if resp.status_code == 429:
                    ra = (resp.headers.get("retry-after") or "").strip()
                    wait = float(ra) if ra.isdigit() else delay
                    delay = min(delay * 2.0, 120.0)
                    last_err = f"Image {ID} HTTP 429 (attempt {attempt + 1})"
                    await asyncio.sleep(wait)
                    continue
                data = resp.content
                if not self._is_image_content(data):
                    raise RuntimeError(
                        f"Image {ID} returned non-image data "
                        f"(HTTP {resp.status_code}), size={len(data)}"
                    )
                with open(path, "wb") as f:
                    f.write(data)
                return post
        raise RuntimeError(last_err)

    async def getImageHD(self, ID: int, path: str) -> dict:
        return await self.getImage(ID, path)

    async def randomPostID(self, tags: str="") -> str:
        postList = await self.getPosts(tags)
        if not postList:
            return ""
        return random.choice(postList)["id"]
