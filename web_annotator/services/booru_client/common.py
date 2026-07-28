"""Booru 来源客户端基类与注册表。

引用：``sources`` / ``routes.booru``。
"""

from __future__ import annotations

import io
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from services.scanner import IMAGE_EXTS
from services.booru_client.utils import (
    _GENDER_TAGS,
    sniff_image_kind,
    _tags_to_caption,
    find_downloaded,
)

if TYPE_CHECKING:
    from services.models import BooruPost


_SOURCES: dict[str, SourceClient] = {}


class SourceClient(ABC):
    """
    来源客户端基类。子类实现 ``fetch_meta`` / ``search`` / ``download``。
    """

    name: str
    prefix: str
    order_tags: dict[str, str] = {}
    valid_orders: tuple[str, ...] = ()

    @abstractmethod
    async def fetch_meta(self, out_dir: Path, pid: str, proxy: str | None) -> None:
        """拉取帖子元数据并写入磁盘。

        引用：``routes.booru._run_refresh``。
        """
    @abstractmethod
    async def search(self, tags: str, page: int, limit: int, order: str, extra: str,
                     proxy: str | None, **auth) -> tuple[list[BooruPost], bool]:
        """搜索帖子，返回 (posts, has_more)。

        实现类应将 API 原始帖转为 ``BooruPost`` 后返回。
        引用：``routes.booru.booru_search`` / ``common.search_posts``。
        """
    @abstractmethod
    async def download(self, pid: str, out_dir: Path, proxy: str | None, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False) -> dict:
        """下载帖子原图并写旁路标签与 meta。

        引用：``booru_batch.download_one``。
        """

    def preview_url(self, post: Mapping[str, Any]) -> str:
        """从帖数据提取缩略图 URL。

        引用：``search_posts`` 结果构造。
        """
        return str(post.get("preview_url") or post.get("file_url") or "")

    def sample_url(self, post: Mapping[str, Any]) -> str:
        """从帖数据提取大图 URL。

        引用：前端预览框。
        """
        return str(post.get("sample_url") or self.preview_url(post))

    def is_media_url(self, url: str) -> bool:
        """判断 URL 是否为本来源图床。

        引用：``routes.booru`` 缩略图代理白名单。
        """
        return True

    @property
    def meta_subdir(self) -> str:
        """元数据子目录名，如 .gelbooru_meta。

        引用：``common.SourceClient._write_meta``。
        """
        return f".{self.name}_meta"

    def file_stem(self, pid: str) -> str:
        """文件名 stem，如 g12345。

        引用：``media.download_post`` / ``utils.find_downloaded``。
        """
        return f"{self.prefix}{pid}"

    def build_search_query(self, tags: str, *, order: str = "", extra: str = "") -> str:
        """拼标签 + 额外元标签 + 排序。

        引用：``routes.booru.booru_search`` / ``sources.search``。
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
        """过滤额外元标签；子类可覆盖。

        引用：``build_search_query``。
        """
        return extra.strip()

    def _write_meta(self, out_dir: Path, pid: str, post: Mapping[str, Any]) -> None:
        meta_dir = out_dir / self.meta_subdir
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / f"{pid}.json").write_text(
            json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _save_png(self, data: bytes, dest: Path) -> None:
        kind = sniff_image_kind(data)
        if kind in ("html", "zip", "video", "webm"):
            raise ValueError(f"不支持该文件类型（{kind}）")
        if kind == "png":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return
        img = Image.open(io.BytesIO(data))
        img.load()
        out = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        out.save(dest, format="PNG", optimize=True)

    def _clean_old_files(self, out_dir: Path, pid: str, keep: Path) -> None:
        from services.booru_client.utils import stem_candidates
        for old in stem_candidates(self.name, pid, file_stem_fn=self.file_stem):
            for ext in IMAGE_EXTS:
                p = out_dir / f"{old}{ext}"
                if p.is_file() and p.resolve() != keep.resolve():
                    p.unlink()

    def _build_caption(self, post: Mapping[str, Any], *, anima_format: bool = False,
                       include_meta_tags: bool = False, keep_underscores: bool = False) -> str:
        def _norm(t: str) -> str:
            return t if keep_underscores else t.replace("_", " ")
        if not anima_format and include_meta_tags:
            tags = str(post.get("tag_string") or post.get("tags") or "")
            return _norm(_tags_to_caption(tags))
        meta_set: set[str] = set()
        if not include_meta_tags:
            for t in str(post.get("tag_string_meta") or "").split():
                t = t.strip().lower()
                if t: meta_set.add(t)
        if not anima_format:
            raw = str(post.get("tag_string") or post.get("tags") or "")
            return ", ".join(_norm(t) for t in raw.replace(",", " ").split()
                             if t.strip() and t.lower() not in meta_set)

        def _split(raw: str) -> list[str]:
            return [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
        general = _split(str(post.get("tag_string_general") or post.get("tag_string") or post.get("tags") or ""))
        gender, rest = [], []
        for t in general:
            if t.lower() in _GENDER_TAGS: gender.append(t)
            elif t.lower() not in meta_set: rest.append(t)
        used = {g.lower() for g in gender}
        captions = [_norm(g) for g in gender]
        for group in [_split(str(post.get("tag_string_character") or "")),
                      _split(str(post.get("tag_string_copyright") or ""))]:
            for t in group:
                low = t.lower()
                if low not in used and low not in meta_set:
                    used.add(low); captions.append(_norm(t))
        for t in _split(str(post.get("tag_string_artist") or "")):
            low = t.lower()
            if low not in used and low not in meta_set:
                used.add(low); captions.append(f"@{_norm(t)}")
        tail = [_norm(t) for t in rest if t.lower() not in used]
        return ", ".join(captions) + " ||| " + ", ".join(tail)


# 元数据子目录映射，供外部（如 media.regenerate_captions）使用。

META_SUBDIR: dict[str, str] = {
    "gelbooru": ".gelbooru_meta",
    "danbooru": ".danbooru_meta",
}


def build_caption_from_meta(post: Mapping[str, Any], *, anima_format: bool = False,
                            include_meta_tags: bool = False,
                            keep_underscores: bool = False) -> str:
    """从元数据构建标签文本（独立函数版）。

    与 ``SourceClient._build_caption`` 逻辑一致但无需实例。

    引用：``media.regenerate_captions``。
    """
    def _norm(t: str) -> str:
        return t if keep_underscores else t.replace("_", " ")
    if not anima_format and include_meta_tags:
        tags = str(post.get("tag_string") or post.get("tags") or "")
        return _norm(_tags_to_caption(tags))
    meta_set: set[str] = set()
    if not include_meta_tags:
        for t in str(post.get("tag_string_meta") or "").split():
            t = t.strip().lower()
            if t: meta_set.add(t)
    if not anima_format:
        raw = str(post.get("tag_string") or post.get("tags") or "")
        return ", ".join(_norm(t) for t in raw.replace(",", " ").split()
                         if t.strip() and t.lower() not in meta_set)

    def _split(raw: str) -> list[str]:
        return [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
    general = _split(str(post.get("tag_string_general") or post.get("tag_string") or post.get("tags") or ""))
    gender, rest = [], []
    for t in general:
        if t.lower() in _GENDER_TAGS: gender.append(t)
        elif t.lower() not in meta_set: rest.append(t)
    used = {g.lower() for g in gender}
    captions = [_norm(g) for g in gender]
    for group in [_split(str(post.get("tag_string_character") or "")),
                  _split(str(post.get("tag_string_copyright") or ""))]:
        for t in group:
            low = t.lower()
            if low not in used and low not in meta_set:
                used.add(low); captions.append(_norm(t))
    for t in _split(str(post.get("tag_string_artist") or "")):
        low = t.lower()
        if low not in used and low not in meta_set:
            used.add(low); captions.append(f"@{_norm(t)}")
    tail = [_norm(t) for t in rest if t.lower() not in used]
    return ", ".join(captions) + " ||| " + ", ".join(tail)


def register_source(client: SourceClient) -> None:
    """注册来源客户端。

    引用：``sources`` 各来源模块初始化。
    """
    _SOURCES[client.name] = client

def get_source(name: str) -> SourceClient:
    """获取来源客户端。

    Raises:
        ValueError: 未知来源名。

    引用：``routes.booru`` 所有端点；``utils.find_downloaded``。
    """
    src = _SOURCES.get(name)
    if src is None:
        raise ValueError(f"未知来源: {name}")
    return src

def list_source_names() -> list[str]:
    """列出已注册来源名。

    引用：``routes.booru.booru_sources``。
    """
    return sorted(_SOURCES.keys())

def detect_source(stem: str) -> tuple[str, str] | None:
    """从文件名 stem 检测来源与 ID。

    Returns:
        ``(source, post_id)`` 或 None。

    引用：``routes.booru._run_refresh``。
    """
    for src in _SOURCES.values():
        if stem.startswith(src.prefix) and stem[len(src.prefix):].isdigit():
            return src.name, stem[len(src.prefix):]
    return None

def file_stem(source: str, post_id: str) -> str:
    """规范文件名，委托给 SourceClient。

    引用：``media.download_post`` / ``media.is_downloaded``。
    """
    return get_source(source).file_stem(post_id)

def build_search_query(source: str, tags: str, *, order: str = "", extra: str = "") -> str:
    """拼搜索查询，委托给 SourceClient。

    引用：``routes.booru.booru_search``。
    """
    return get_source(source).build_search_query(tags, order=order, extra=extra)

async def search_posts(
    source: str, tags: str, *, page: int = 1, limit: int = 40, order: str = "",
    extra: str = "", proxy: str | None = None,
    danbooru_login: str | None = None, danbooru_api_key: str | None = None,
) -> tuple[list[BooruPost], bool]:
    """搜索帖子，委托给 SourceClient；返回值中每帖为 ``BooruPost`` 模型。

    引用：``booru_batch._run_async``。
    """
    src = get_source(source)
    return await src.search(
        tags, page=page, limit=limit, order=order, extra=extra, proxy=proxy,
        danbooru_login=danbooru_login, danbooru_api_key=danbooru_api_key,
    )
