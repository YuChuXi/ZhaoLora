import re
import httpx
import json
import random
from urllib.parse import quote
from .Danbooru import Danbooru

class Gelbooru(Danbooru):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        
    async def getJson(self, args: str) -> dict:
        api_key = "***REMOVED***"
        user_id = "***REMOVED***"
        url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&{args}&api_key={api_key}&user_id={user_id}"
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
        """下载原图并返回该次请求使用的完整 post 元数据（与 id= 接口一致）。"""
        post = await self.getPost(ID)
        imageUrl = post["file_url"]
        # 图片请求必须带 Referer，否则 Gelbooru 返回 403 HTML
        headers = {**self.headers, "referer": self._IMAGE_REFERER}
        async with httpx.AsyncClient(proxy=self.proxy, timeout=self.timeout) as client:
            resp = await client.get(imageUrl, headers=headers)
        data = resp.content
        if not self._is_image_content(data):
            raise RuntimeError(
                f"Image {ID} returned non-image data (e.g. 403 HTML), size={len(data)}"
            )
        with open(path, "wb") as f:
            f.write(data)
        return post

    async def getImageHD(self, ID: int, path: str) -> dict:
        return await self.getImage(ID, path)

    async def randomPostID(self, tags: str="") -> str:
        postList = await self.getPosts(tags)
        if not postList:
            return ""
        return random.choice(postList)["id"]
