"""Booru 来源客户端基类与注册表. 

Callers:
    - ``services/online_sources/danbooru/__init__.py:``
    - ``services/online_sources/gelbooru/__init__.py:``
    - ``services/online_sources/x/__init__.py:``
    - ``services/online_sources/pixiv/__init__.py:``
    - ``services/online_sources/bilibili/__init__.py:``
    - ``routes/__init__.py:``
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from urllib.parse import urlparse

from services.core.scanner import IMAGE_EXTS
from services.online_sources.framework.http import HttpClient
from services.online_sources.framework.utils import (
    _GENDER_TAGS,
    save_png_bytes,
    _tags_to_caption,
)
from services.models.booru import BooruPost


_SOURCES: dict[str, SourceClient] = {}

# 下载进度回调: (当前进度, 总量), 数值为整数(如 20, 100). 仅视频帖有意义.
ProgressFn = Callable[[int, int], None]


class SourceClient(ABC):
    """
    来源客户端基类. 子类实现 ``fetch_meta`` / ``search`` / ``download``. 
    """

    name: str
    prefix: str
    order_tags: dict[str, str] = {}
    valid_orders: tuple[str, ...] = ()
    # 游标式来源用会话续拉 (Bilibili/X), 无页码概念; 经由 search_session
    session_based: bool = False
    # 是否在下载时生成旁路 .txt 标注; booru 来源开启, 非 booru 来源关闭
    writes_caption: bool = True

    @abstractmethod
    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """拉取帖子元数据并写入磁盘. 

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.fetch_meta``

        Callers:
            - ``routes/online_sources.py:_run_refresh``
        """
    @abstractmethod
    async def search(self, tags: str, page: int, order: str, extra: str,
                     ) -> tuple[list[BooruPost], bool]:
        """搜索帖子, 返回 (posts, has_more). 

        每页条数由来源自行决定 (上游 API 语义), 调用方只传页码. 

        实现类应将 API 原始帖转为 ``BooruPost`` 后返回. 

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.search``

        Callers:
            - ``routes/online_sources.py:search``
            - ``services/online_sources/framework/base.py:search_posts``
        """
    async def search_session(
        self,
        tags: str,
        session: str,
        order: str,
        extra: str,
    ) -> tuple[list[BooruPost], bool, str]:
        """按会话续拉下一批结果, 返回 (posts, has_more, session_token).

        游标式来源 (Bilibili/X) 覆盖此方法; 页码式来源不应调用, 默认抛错。

        Returns:
            ``(posts, has_more, session_token)``.

        Raises:
            NotImplementedError: 非会话式来源调用时.

        Callers:
            - ``routes/online_sources.py:search``
        """
        raise NotImplementedError("非会话式来源不支持 search_session")
    @abstractmethod
    async def download(self, post: BooruPost, out_dir: Path, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False,
                       emit: ProgressFn | None = None) -> dict:
        """下载帖子并写旁路标签与 meta.

        入参直接吃 search 阶段已返回的 ``BooruPost`` (含原图 URL 与标签),
        各实现据此下载, 不再凭 id 重新请求帖文元数据。视频帖 (``post.is_video``)
        同样经此方法写入 ``out_dir`` (即数据集 ``video`` 目录)。``emit`` 为
        下载进度回调 ``(当前进度, 总量)``, 仅视频帖 (如 B 站 BBDown) 有意义,
        图片来源可忽略。

        Args:
            post: 帖子.
            out_dir: 输出目录(图片帖为筛选子目录, 视频帖为数据集 ``video``).
            overwrite: 是否覆盖已下载文件.
            anima_format: 按 Anima 格式输出标签.
            include_meta_tags: 是否保留元数据标签.
            keep_underscores: 是否保留下划线.
            emit: 下载进度回调, 形如 ``(pct, total)``.

        Returns:
            dict: 含 ``path``/``stem``/``id`` 等写入结果的字典.

        Callers:
            - ``services/online_sources/batch.py:download_one``
            - ``services/online_sources/framework/media.py:download_post``
        """

    async def autocomplete(self, query: str, limit: int = 10) -> list[dict]:
        """标签自动补全. 默认不支持, 返回空列表.

        Args:
            query: 标签前缀.
            limit: 返回条数上限.

        Returns:
            规范化的建议列表: 每项为 ``{"name", "category", "count"}``.

        Callers:
            - ``routes/online_sources.py:tag_suggest``
        """
        return []

    def parse_query(self, tags: str, extra: str) -> dict[str, str]:
        """把来源原始查询串解析为结构化条件.

        默认将 ``tags``/``extra`` 原样返回；特殊参数语义的来源 (如 Bilibili
        把 ``tags`` 作 UID、``extra`` 作筛选串) 覆盖此方法自行解析。

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.search``
        """
        return {"tags": tags, "extra": extra}

    def preview_url(self, post: BooruPost) -> str:
        """从已规范化的帖模型提取缩略图 URL. 

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.sample_url``
        """
        return post.preview_url or post.file_url

    def sample_url(self, post: BooruPost) -> str:
        """从已规范化的帖模型提取大图 URL. 
        """
        return post.sample_url or self.preview_url(post)

    def is_media_url(self, url: str) -> bool:
        """判断 URL 是否为本来源图床. 
        """
        return True

    # ---- 图片代理: 子类用以下属性描述图床差异, 继承统一的 `proxy_image` ----

    image_hosts: tuple[str, ...] = ()
    """允许代理的图床 host (精确或子域后缀), 空元组表示不限定. 
    """

    image_referer: str = ""
    """防盗链 Referer, 空串表示无需防盗链. 
    """

    image_timeout: float = 30.0
    """图片代理默认超时(秒). 
    """

    def image_timeout_for(self, kind: str) -> float:
        """按 kind 返回代理超时, 默认沿用 ``image_timeout``. 

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.proxy_image``
        """
        return self.image_timeout

    async def proxy_image(self, url: str, *, kind: str = "") -> tuple[str, AsyncIterator[bytes]]:
        """流式代理本来源图床图片.

        校验 URL 属于 ``image_hosts`` 白名单, 并带 ``image_referer`` 防盗链头,
        返回 ``(content_type, 字节迭代器)`` 供 `routes.online_sources` 直接包成
        ``StreamingResponse``。

        Raises:
            ValueError: URL 非法或不属于本来源图床.
            NetworkError: 网络或上游错误.

        Callers:
            - ``routes/online_sources.py:proxy_image``
        """
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("非法地址")
        if self.image_hosts:
            host = u.hostname.lower()
            if not any(host == h or host.endswith("." + h) for h in self.image_hosts):
                raise ValueError("非本站图床")
        async with HttpClient() as client:
            return await client.stream_response(
                url,
                referer=self.image_referer or None,
                timeout=self.image_timeout_for(kind),
            )

    @property
    def meta_subdir(self) -> str:
        """元数据子目录名, 如 .gelbooru_meta. 

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.meta_subdir``

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient._write_meta``
        """
        return f".{self.name}_meta"

    def file_stem(self, pid: str) -> str:
        """文件名 stem, 如 g12345. 

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.file_stem``

        Callers:
            - ``services/online_sources/framework/media.py:download_post``
            - ``services/online_sources/framework/utils.py:find_downloaded``
        """
        return f"{self.prefix}{pid}"

    def build_search_query(self, tags: str, *, order: str = "", extra: str = "") -> str:
        """拼接标签、额外元标签与排序. 

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.build_search_query``

        Callers:
            - ``routes/online_sources.py:search``
            - ``services/online_sources/gelbooru/source.py:GelbooruSource.search``
        """
        parts = [tags.strip()] if tags.strip() else []
        filtered = self.filter_extra(extra)
        if filtered:
            parts.append(filtered)
        ord_tag = self.order_tags.get(order, "")
        if ord_tag:
            parts.append(ord_tag)
        return " ".join(parts).strip()

    def filter_extra(self, extra: str) -> str:
        """过滤额外元标签；子类可覆盖. 

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.build_search_query``
        """
        return extra.strip()

    def _write_meta(self, out_dir: Path, pid: str, post: BooruPost) -> None:
        meta_dir = out_dir / self.meta_subdir
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / f"{pid}.json").write_text(
            post.model_dump_json(indent=2) + "\n", encoding="utf-8")

    async def _save_png(self, data: bytes, dest: Path) -> None:
        """将下载字节转为 PNG 写入磁盘.

        图片解码/重编码是 CPU 密集的同步操作, 移入线程池执行以避免阻塞事件
        循环; 转码核心逻辑见 ``_save_png_sync``。

        Args:
            data: 原始字节。
            dest: 目标 PNG 路径。

        Raises:
            ValueError: 文件类型不支持 (html/zip/video/webm)。

        Callers:
            - ``services/online_sources/danbooru/source.py:DanbooruSource.download``
            - ``services/online_sources/gelbooru/source.py:GelbooruSource.download``
            - ``services/online_sources/pixiv/source.py:PixivSource.download``
            - ``services/online_sources/bilibili/source.py:BilibiliSource.download``
            - ``services/online_sources/framework/gallerydl.py:download``
        """
        await asyncio.to_thread(self._save_png_sync, data, dest)

    def _save_png_sync(self, data: bytes, dest: Path) -> None:
        """同步执行 PNG 字节转码写入 (经 ``asyncio.to_thread`` 调用).

        校验与转码核心逻辑集中在 ``utils.save_png_bytes``。

        Args:
            data: 原始字节。
            dest: 目标 PNG 路径。

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient._save_png``
        """
        save_png_bytes(data, dest)

    def _clean_old_files(self, out_dir: Path, pid: str, keep: Path) -> None:
        from services.online_sources.framework.utils import stem_candidates
        for old in stem_candidates(pid, file_stem_fn=self.file_stem):
            for ext in IMAGE_EXTS:
                p = out_dir / f"{old}{ext}"
                if p.is_file() and p.resolve() != keep.resolve():
                    p.unlink()

    def _build_caption(self, post: BooruPost, *, anima_format: bool = False,
                       include_meta_tags: bool = False, keep_underscores: bool = False) -> str:
        def _norm(t: str) -> str:
            return t if keep_underscores else t.replace("_", " ")
        if not anima_format and include_meta_tags:
            return _norm(_tags_to_caption(post.tags))
        meta_set: set[str] = set()
        if not include_meta_tags:
            for t in post.tag_string_meta.split():
                t = t.strip().lower()
                if t: meta_set.add(t)
        if not anima_format:
            return ", ".join(_norm(t) for t in post.tags.replace(",", " ").split()
                             if t.strip() and t.lower() not in meta_set)

        def _split(raw: str) -> list[str]:
            return [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
        general = _split(post.tag_string_general or post.tags)
        gender, rest = [], []
        for t in general:
            if t.lower() in _GENDER_TAGS: gender.append(t)
            elif t.lower() not in meta_set: rest.append(t)
        used = {g.lower() for g in gender}
        captions = [_norm(g) for g in gender]
        for group in [_split(post.tag_string_character),
                      _split(post.tag_string_copyright)]:
            for t in group:
                low = t.lower()
                if low not in used and low not in meta_set:
                    used.add(low); captions.append(_norm(t))
        for t in _split(post.tag_string_artist):
            low = t.lower()
            if low not in used and low not in meta_set:
                used.add(low); captions.append(f"@{_norm(t)}")
        tail = [_norm(t) for t in rest if t.lower() not in used]
        return ", ".join(captions) + " ||| " + ", ".join(tail)


# 元数据子目录映射, 供外部(如 media.regenerate_captions)使用. 

META_SUBDIR: dict[str, str] = {
    "gelbooru": ".gelbooru_meta",
    "danbooru": ".danbooru_meta",
}


def build_caption_from_meta(post: BooruPost, *, anima_format: bool = False,
                            include_meta_tags: bool = False,
                            keep_underscores: bool = False) -> str:
    """从元数据构建标签文本(独立函数版). 

    与 ``SourceClient._build_caption`` 逻辑一致但无需实例. 

    Callers:
        - ``services/online_sources/framework/media.py:regenerate_captions``
    """
    def _norm(t: str) -> str:
        return t if keep_underscores else t.replace("_", " ")
    if not anima_format and include_meta_tags:
        return _norm(_tags_to_caption(post.tags))
    meta_set: set[str] = set()
    if not include_meta_tags:
        for t in post.tag_string_meta.split():
            t = t.strip().lower()
            if t: meta_set.add(t)
    if not anima_format:
        return ", ".join(_norm(t) for t in post.tags.replace(",", " ").split()
                         if t.strip() and t.lower() not in meta_set)

    def _split(raw: str) -> list[str]:
        return [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
    general = _split(post.tag_string_general or post.tags)
    gender, rest = [], []
    for t in general:
        if t.lower() in _GENDER_TAGS: gender.append(t)
        elif t.lower() not in meta_set: rest.append(t)
    used = {g.lower() for g in gender}
    captions = [_norm(g) for g in gender]
    for group in [_split(post.tag_string_character),
                  _split(post.tag_string_copyright)]:
        for t in group:
            low = t.lower()
            if low not in used and low not in meta_set:
                used.add(low); captions.append(_norm(t))
    for t in _split(post.tag_string_artist):
        low = t.lower()
        if low not in used and low not in meta_set:
            used.add(low); captions.append(f"@{_norm(t)}")
    tail = [_norm(t) for t in rest if t.lower() not in used]
    return ", ".join(captions) + " ||| " + ", ".join(tail)


def register_source(client: SourceClient) -> None:
    """注册来源客户端. 

    Callers:
        - ``services/online_sources/__init__.py:`` 模块初始化
    """
    _SOURCES[client.name] = client

def get_source(name: str) -> SourceClient:
    """获取来源客户端. 

    Raises:
        ValueError: 未知来源名. 

    Callers:
        - ``routes/online_sources.py:`` 各端点
        - ``services/online_sources/framework/media.py:download_post``
        - ``services/online_sources/framework/media.py:remove_download``
        - ``services/online_sources/batch.py:``
    """
    src = _SOURCES.get(name)
    if src is None:
        raise ValueError(f"未知来源: {name}")
    return src

def list_source_names() -> list[str]:
    """列出已注册来源名. 

    Callers:
        - ``routes/online_sources.py:list_sources``
    """
    return sorted(_SOURCES.keys())

def detect_source(stem: str) -> tuple[str, str] | None:
    """从文件名 stem 检测来源与 ID. 

    Returns:
        ``(source, post_id)`` 或 None. 

    Callers:
        - ``routes/online_sources.py:_run_refresh``
    """
    for src in _SOURCES.values():
        if stem.startswith(src.prefix) and stem[len(src.prefix):].isdigit():
            return src.name, stem[len(src.prefix):]
    return None

def file_stem(source: str, post_id: str) -> str:
    """规范文件名, 委托给 SourceClient. 

    Callers:
        - ``services/online_sources/framework/media.py:download_post``
        - ``services/online_sources/framework/media.py:regenerate_captions``
    """
    return get_source(source).file_stem(post_id)

def build_search_query(source: str, tags: str, *, order: str = "", extra: str = "") -> str:
    """拼接搜索查询, 委托给 SourceClient. 

    Callers:
        - ``routes/online_sources.py:search``
    """
    return get_source(source).build_search_query(tags, order=order, extra=extra)

async def search_posts(
    source: str, tags: str, *, page: int = 1, order: str = "",
    extra: str = "",
) -> tuple[list[BooruPost], bool]:
    """搜索帖子, 委托给 SourceClient；返回值中每帖为 ``BooruPost`` 模型. 

    Callers:
        - ``services/online_sources/batch.py:_run_async``
    """
    src = get_source(source)
    posts, has_more = await src.search(
        tags, page=page, order=order, extra=extra,
    )
    for post in posts:
        post.stem = src.file_stem(post.id)
        post.filename = f"{post.stem}.png"
    return posts, has_more


async def search_session_posts(
    source: str, tags: str, session: str, *, order: str = "",
    extra: str = "",
) -> tuple[list[BooruPost], bool, str]:
    """按会话续拉搜索结果, 并补全每帖 ``stem``/``filename``.

    与会话式来源的 ``search_session`` 相比, 本函数额外补全文件名 (与
    ``search_posts`` 对页码式来源的补全一致), 供搜索路由与批量下载统一复用,
    避免各调用方重复补全。仅限 ``session_based`` 来源调用。

    Returns:
        ``(posts, has_more, session_token)``.

    Callers:
        - ``routes/online_sources.py:search``
        - ``services/online_sources/batch.py:_run_async``
    """
    src = get_source(source)
    posts, has_more, token = await src.search_session(
        tags, session, order, extra,
    )
    for post in posts:
        post.stem = src.file_stem(post.id)
        post.filename = f"{post.stem}.png"
    return posts, has_more, token
