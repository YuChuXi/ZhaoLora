"""Gelbooru 来源适配器: 实现 ``SourceClient`` 的搜索 / 下载 / 元数据.

Callers:
    - ``services/online_sources/gelbooru/__init__.py:`` (导出)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from services.core.atomic import atomic_write_text
from services.online_sources.framework.base import ProgressFn, SourceClient, build_caption
from services.online_sources.framework.media import download_raw_file, post_is_video
from services.online_sources.framework.utils import find_downloaded
from services.online_sources.gelbooru.client import GelbooruClient
from services.models import BooruPost


def _to_model_data(raw: dict) -> dict:
    """将 Gelbooru API 原始响应映射为 ``BooruPost`` 统一字段名.

    Args:
        raw: Gelbooru API 原始帖子字典 (含大量额外字段, 原样保留进 meta;
            ``id`` 与 ``created_at`` 为 API 必有字段).

    Returns:
        dict: id 转文本、``created_at`` 消费为 ISO8601 ``posted_at`` 后的数据.

    Callers:
        - ``services/online_sources/gelbooru/source.py:GelbooruSource.fetch_meta``
        - ``services/online_sources/gelbooru/source.py:GelbooruSource.search``
    """
    out = {**raw}
    out["id"] = str(out["id"])
    out["posted_at"] = datetime.strptime(out.pop("created_at"), "%a %b %d %H:%M:%S %z %Y").isoformat()
    return out


class GelbooruSource(SourceClient):
    """Gelbooru 图站来源客户端 (页码式).

    Callers:
        - ``services/online_sources/framework/base.py:register_source``
    """

    name = "gelbooru"
    prefix = "g"
    order_tags = {"score": "sort:score:desc", "id": "sort:id:desc", "id_asc": "sort:id:asc"}
    valid_orders = ("score", "id", "id_asc")
    image_referer = "https://gelbooru.com/"
    # 缩略图更快, 大图放宽超时
    _IMAGE_TIMEOUTS = {"thumb": 30.0, "sample": 45.0}

    def image_timeout_for(self, kind: str) -> float:
        """按图片类型返回代理超时: 缩略图快、样图大图放宽.

        覆盖父类 ``base.SourceClient``.

        Args:
            kind: 图片类型 (``thumb``/``sample``/空).

        Returns:
            float: 超时秒数.

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.proxy_image``
        """
        return self._IMAGE_TIMEOUTS.get(kind, self.image_timeout)

    def filter_extra(self, extra: str) -> str:
        """Gelbooru 忽略 date: 元标签, 并合并多评级为 OR.

        Gelbooru 的多评级用 ``(rating:a) OR (rating:b)`` 逐项括号大写
        连接(与 danbooru 的小写 ``or`` 连写不同)。覆盖父类
        ``base.SourceClient``。

        Callers:
            - ``services/online_sources/framework/base.py:build_search_query``
        """
        tokens = [x for x in extra.split() if not x.lower().startswith("date:")]
        return " ".join(tokens)

    @staticmethod
    def normalize(post: BooruPost) -> BooruPost | None:
        """Gelbooru 原始帖补充 ``source``.

        覆盖父类 ``base.SourceClient``. 预览/样图 URL 回退由 ``BooruPost``
        模型校验器统一处理.

        Callers:
            - ``services/online_sources/gelbooru/source.py:GelbooruSource.search``
        """
        if not post.file_url:
            return None
        post.source = "gelbooru"
        post.is_video = post_is_video(post)
        return post

    def split(self, stem: str) -> str:
        """从 stem 拆出本来源 pid, 非本来源格式抛 ValueError.

        pid 为前缀后纯数字文本.

        Callers:
            - ``services/online_sources/framework/base.py:detect_source``
        """
        rest = stem[len(self.prefix):] if stem.startswith(self.prefix) else ""
        if not rest.isdigit():
            raise ValueError(f"非法 stem: {stem}")
        return rest

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """拉取 Gelbooru 帖元数据并写入磁盘.

        实现 ``base.SourceClient.fetch_meta``.

        Callers:
            - ``routes/online_sources.py:_run_refresh``
        """
        api = GelbooruClient()
        raw = await api.get_post(int(pid))
        self._write_meta(out_dir, pid, BooruPost.model_validate(_to_model_data(raw)))

    async def autocomplete(self, query: str, limit: int = 10) -> list[dict]:
        """标签自动补全.

        实现 ``base.SourceClient.autocomplete``。

        Callers:
            - ``routes/online_sources.py:tag_suggest``
        """
        api = GelbooruClient()
        return await api.autocomplete(query, limit)

    async def search(self, tags: str, page: int, order: str, extra: str) -> tuple[list[BooruPost], bool]:
        """搜索 Gelbooru 帖.

        每页 100 条由上游固定, 页大小不由调用方决定。实现
        ``base.SourceClient.search``。

        Callers:
            - ``services/online_sources/framework/base.py:search_posts`` / `routes/online_sources.py:search`
        """
        from services.models import BooruPost

        api = GelbooruClient()
        query = self.build_search_query(tags, order=order, extra=extra)
        pid = max(0, page - 1)
        raw_resp = await api.search(query, pid)
        attrs = raw_resp.get("@attributes") if isinstance(raw_resp, dict) else None
        raw = raw_resp.get("post") if isinstance(raw_resp, dict) else None
        posts_raw = [] if raw is None else ([raw] if isinstance(raw, dict) else list(raw))
        g_limit = 100
        offset = pid * g_limit
        if isinstance(attrs, dict):
            g_limit = int(attrs.get("limit") or g_limit)
            offset = int(attrs.get("offset") or offset)
            total = attrs.get("count")
            has_more = offset + len(posts_raw) < int(total) if total is not None else len(posts_raw) >= g_limit
        else:
            has_more = len(posts_raw) >= g_limit
        out: list[BooruPost] = []
        for raw_post in posts_raw:
            if not isinstance(raw_post, dict):
                continue
            n = self.normalize(BooruPost.model_validate(_to_model_data(raw_post)))
            if n is not None:
                out.append(n)
        return out, has_more

    async def download(self, post: BooruPost, out_dir: Path, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False,
                       emit: ProgressFn | None = None) -> dict:
        """下载 Gelbooru 原图并写旁路标签.

        直接使用 search 阶段已持有的 ``post.file_url`` 与 ``post.tags`` 写入,
        帖文元数据一律以入参为准; 仅 anima 格式需要额外查询标签类型。
        视频帖 (``post.is_video``) 经由 ``download_raw_file`` 下载原始文件。
        实现 ``base.SourceClient.download``。

        Args:
            emit: 下载进度回调, 图片来源忽略 (透传签名统一).

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.download`` (经 ``media.download_post`` 委托)
        """
        api = GelbooruClient()
        pid = post.id
        stem = Path(post.filename).stem
        image_path = out_dir / post.filename
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, post.filename)

        if post.is_video:
            return await download_raw_file(post, out_dir)

        if not overwrite and existing is not None and tags_path.is_file():
            return {"skipped": True, "path": str(existing), "stem": stem, "id": pid, "tags_path": str(tags_path)}

        if not post.file_url:
            raise ValueError("无 file_url")

        if overwrite or not image_path.is_file():
            await api.get_image(post.file_url, str(image_path), referer=self.image_referer)
            self._clean_old_files(out_dir, pid, image_path)

        api_post = post
        if anima_format:
            raw = await _categorize_gelbooru_tags(api_post.model_dump(), api)
            api_post = BooruPost.model_validate(raw)
        caption = build_caption(api_post, anima_format=anima_format, include_meta_tags=include_meta_tags, keep_underscores=keep_underscores)
        atomic_write_text(tags_path, caption + "\n")
        self._write_meta(out_dir, pid, api_post)
        return {"path": str(image_path), "stem": stem, "id": pid, "tags_path": str(tags_path)}


# 标签类型缓存, 进程内复用
_tag_types_cache: dict[str, int] = {}

# Gelbooru 标签类型 -> Danbooru 兼容字段; 编号 2 为历史遗留空缺。
_TAG_TYPE_FIELDS: dict[int, str] = {
    0: "tag_string_general",
    1: "tag_string_artist",
    3: "tag_string_copyright",
    4: "tag_string_character",
    5: "tag_string_meta",
}

# 类型 6 = bad_tag (Gelbooru 声明不使用且永不使用的标签, 其他 booru 的
# 标签被 alias 到此, 见官方 wiki bad_tag 词条): 不属于任何类别, 不写入
# 分类字段 (对训练数据是噪声), 原始 tags 字段不受影响。
_BAD_TAG_TYPE = 6


async def _categorize_gelbooru_tags(post: dict, api: GelbooruClient) -> dict:
    """查询 Gelbooru 标签类型并写入 Danbooru 兼容字段, 结果缓存在内存.

    类型编号必须全部落在 ``_TAG_TYPE_FIELDS`` (bad_tag 6 除外): API 对
    帖子上的标签必然返回类型行, 查询后缺类型的标签以 KeyError 暴露;
    遇到映射表外的未知类型编号同样抛错, 等待查明语义后扩充映射, 不
    静默归类污染 caption。

    Args:
        post: Gelbooru API 返回的帖子数据.
        api: GelbooruClient 实例.

    Returns:
        含 tag_string_* 字段的新 dict.

    Raises:
        KeyError: 标签类型查询结果缺失或出现未知类型编号。

    Callers:
        - ``services/online_sources/gelbooru/source.py:GelbooruSource.download``
    """
    all_names = str(post.get("tags") or "").split()
    if not all_names:
        return dict(post)
    missing = [n for n in all_names if n not in _tag_types_cache]
    if missing:
        types = await api.get_tag_types(missing)
        _tag_types_cache.update(types)
    buckets: dict[str, list[str]] = {}
    for t in all_names:
        ttype = _tag_types_cache[t]
        if ttype == _BAD_TAG_TYPE:
            continue
        buckets.setdefault(_TAG_TYPE_FIELDS[ttype], []).append(t)
    post = dict(post)
    for field in _TAG_TYPE_FIELDS.values():
        post[field] = " ".join(buckets.get(field, []))
    return post
