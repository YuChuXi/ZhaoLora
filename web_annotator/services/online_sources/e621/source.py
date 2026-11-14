"""e621 来源适配器: 实现 ``SourceClient`` 的搜索 / 下载 / 元数据.

e621 的帖子按 ``file`` / ``preview`` / ``sample`` / ``score`` / ``tags``
分组返回, 与 Danbooru 的扁平字段不同, 由 ``_to_model_data`` 统一映射为
``BooruPost`` 字段。

Callers:
    - ``services/online_sources/e621/__init__.py:`` (导出)
"""

from __future__ import annotations

from pathlib import Path

from services.core.atomic import atomic_write_text
from services.online_sources.e621.client import PAGE_LIMIT, E621Client
from services.online_sources.framework.base import ProgressFn, SourceClient, build_caption
from services.online_sources.framework.media import download_raw_file, post_is_video
from services.online_sources.framework.utils import find_downloaded
from services.models import BooruPost

# e621 tags 分组 -> BooruPost 扁平标签字段. species 与 general 同为描述性
# 标签, 合并进 general; invalid / lore / contributor 不进入 caption
# (非画面信息或已废弃标签), 但仍原样保留在 meta JSON 中。
_TAG_FIELDS: dict[str, str] = {
    "artist": "tag_string_artist",
    "copyright": "tag_string_copyright",
    "character": "tag_string_character",
    "meta": "tag_string_meta",
}


def _to_model_data(raw: dict) -> dict:
    """将 e621 API 原始响应映射为 ``BooruPost`` 统一字段名.

    Args:
        raw: e621 API 原始帖子字典 (含 ``file`` / ``preview`` / ``sample`` /
            ``score`` / ``tags`` / ``flags`` 分组, 原样保留进 meta)。

    Returns:
        dict: 扁平化后的统一字段; ``created_at`` 仅消费为 ``posted_at``,
            原始分组保留以供 meta 完整记录。

    Callers:
        - ``services/online_sources/e621/source.py:E621Source.fetch_meta``
        - ``services/online_sources/e621/source.py:E621Source.search``
    """
    file = raw["file"]
    tags = raw["tags"]
    general = [*(tags.get("general") or []), *(tags.get("species") or [])]
    sample = raw.get("sample") or {}
    file_url = file.get("url") or ""
    sample_url = (sample.get("url") or "") if sample.get("has") else ""

    out = {**raw}
    out.update(
        id=str(raw["id"]),
        width=file["width"],
        height=file["height"],
        file_ext=file.get("ext") or "",
        file_url=file_url,
        md5=file.get("md5") or "",
        preview_url=(raw.get("preview") or {}).get("url") or "",
        sample_url=sample_url or file_url,
        score=int((raw.get("score") or {}).get("total") or 0),
        rating=raw.get("rating") or "",
        posted_at=raw["created_at"],
        is_deleted=bool((raw.get("flags") or {}).get("deleted")),
        tag_string_general=" ".join(general),
    )
    for group, field in _TAG_FIELDS.items():
        out[field] = " ".join(tags.get(group) or [])
    out["tags"] = " ".join(
        [
            *general,
            *(tags.get("character") or []),
            *(tags.get("copyright") or []),
            *(tags.get("artist") or []),
            *(tags.get("meta") or []),
        ]
    )
    return out


class E621Source(SourceClient):
    """e621 图站来源客户端 (页码式).

    Callers:
        - ``services/online_sources/framework/base.py:register_source``
    """

    name = "e621"
    prefix = "e"
    # e621 的 order:id 为「旧→新」, 新帖在前需显式使用 order:id_desc
    order_tags = {"score": "order:score", "id": "order:id_desc", "id_asc": "order:id_asc"}
    valid_orders = ("score", "id", "id_asc")
    image_hosts = ("e621.net",)
    image_referer = "https://e621.net/"

    @staticmethod
    def normalize(post: BooruPost) -> BooruPost | None:
        """e621 原始帖补充 ``source`` 与视频标记, 跳过已删除或无原图的帖子.

        Callers:
            - ``services/online_sources/e621/source.py:E621Source.search``
        """
        if post.is_deleted or not post.file_url:
            return None
        post.source = "e621"
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
        """拉取 e621 帖元数据并写入磁盘.

        实现 ``base.SourceClient.fetch_meta``.

        Callers:
            - ``routes/online_sources.py:_run_refresh``
        """
        raw = await E621Client().get_post(int(pid))
        self._write_meta(out_dir, pid, BooruPost.model_validate(_to_model_data(raw)))

    async def autocomplete(self, query: str, limit: int = 10) -> list[dict]:
        """标签自动补全.

        实现 ``base.SourceClient.autocomplete``.

        Callers:
            - ``routes/online_sources.py:tag_suggest``
        """
        return await E621Client().autocomplete(query, limit)

    async def search(self, tags: str, page: int, order: str, extra: str) -> tuple[list[BooruPost], bool]:
        """搜索 e621 帖.

        每页 320 条由上游固定, 页大小不由调用方决定. 实现
        ``base.SourceClient.search``.

        Callers:
            - ``routes/online_sources.py:search``
            - ``services/online_sources/batch.py:fill_buffer``
        """
        api = E621Client()
        query = self.build_search_query(tags, order=order, extra=extra)
        posts_raw = await api.search(query, max(1, page))
        out: list[BooruPost] = []
        for raw_post in posts_raw:
            n = self.normalize(BooruPost.model_validate(_to_model_data(raw_post)))
            if n is not None:
                out.append(n)
        return out, len(posts_raw) >= PAGE_LIMIT

    async def download(self, post: BooruPost, out_dir: Path, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False,
                       emit: ProgressFn | None = None) -> dict:
        """下载 e621 原图并写旁路标签.

        直接使用 search 阶段已持有的 ``post.file_url`` 与已分组标签写入,
        帖文元数据一律以入参为准. 视频帖 (``post.is_video``) 经由
        ``download_raw_file`` 下载原始文件. 实现 ``base.SourceClient.download``.

        Args:
            emit: 下载进度回调, 图片来源忽略 (透传签名统一).

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.download`` (经 ``media.download_post`` 委托)
        """
        api = E621Client()
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

        caption = build_caption(
            post, anima_format=anima_format,
            include_meta_tags=include_meta_tags, keep_underscores=keep_underscores,
        )
        atomic_write_text(tags_path, caption + "\n")
        self._write_meta(out_dir, pid, post)
        return {"path": str(image_path), "stem": stem, "id": pid, "tags_path": str(tags_path)}
