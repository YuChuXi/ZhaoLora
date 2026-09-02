"""米画师来源适配器: 按画师 UID 拉取精选作品 / 上架橱窗 / 价目表.

``tags`` 参数承载画师 UID, ``extra`` 承载板块标识 (``mode:artworks``=精选
作品, ``mode:stalls``=上架橱窗, ``mode:draw_types``=价目表)。精选作品走
``/api/v1/users/{uid}/artworks`` 原生分页; 橱窗商品来自 ``/api/v1/users/{uid}``
响应的 ``displayable_stalls``; 价目表走 ``/api/v1/users/{uid}/draw_types``,
其示例图逐条展开为卡片。

pid 按条目类型编码: ``a{作品ID}`` / ``s{橱窗商品ID}`` / ``t{价目项ID}_{序号}``,
stem 为 ``{prefix}{pid}`` (prefix=``m``)。

Callers:
    - ``services/online_sources/mihuashi/__init__.py:`` (导出)
"""

from __future__ import annotations

from pathlib import Path

from services.models import BooruPost
from services.online_sources.framework.base import ProgressFn, SourceClient
from services.online_sources.framework.http import HttpClient
from services.online_sources.framework.utils import find_downloaded
from services.online_sources.mihuashi.client import api_get

# 板块标识 (与前端 _BARS.mihuashi 的 mode 选项一致)
_MODE_ARTWORKS = "artworks"
_MODE_STALLS = "stalls"
_MODE_DRAW_TYPES = "draw_types"

# 精选作品每页条数 (与站点网页一致)
_ARTWORKS_PER = 12

# pid 类型前缀: a=作品, s=橱窗商品, t=价目表示例
_PID_KINDS = ("a", "s", "t")


def _tag_names(item: dict) -> str:
    """把米画师标签数组压平为空格分隔的标签文本.

    Args:
        item (dict): 含 ``tags`` 字段的条目数据。

    Returns:
        str: 标签名文本, 无标签时为空串。

    Callers:
        - ``services/online_sources/mihuashi/source.py:MihuashiSource._artwork_post``
        - ``services/online_sources/mihuashi/source.py:MihuashiSource.fetch_meta``
    """
    return " ".join(str(t.get("name", "")).strip()
                    for t in item.get("tags") or [] if str(t.get("name", "")).strip())


class MihuashiSource(SourceClient):
    """从米画师画师主页三个板块拉取图片的来源客户端.

    Callers:
        - ``services/online_sources/framework/base.py:register_source``
    """

    name = "mihuashi"
    prefix = "m"
    order_tags: dict[str, str] = {}
    valid_orders: tuple[str, ...] = ()
    writes_caption = False
    image_hosts = ("mihuashi.com",)

    def parse_query(self, tags: str, extra: str) -> dict[str, str]:
        """解析米画师查询串: ``tags`` 为画师 UID, ``extra`` 里 ``mode:`` 指定板块.

        Args:
            tags: 画师 UID。
            extra: ``mode:artworks`` / ``mode:stalls`` / ``mode:draw_types``,
                缺省按精选作品。

        Returns:
            dict[str, str]: 含 ``mode`` 与 ``uid`` 两键。

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.build_search_query``
            - ``static/js/booru_filter.js:`` : 前端来源选择页面.
        """
        mode = _MODE_ARTWORKS
        for token in (extra or "").split():
            if token.startswith("mode:"):
                val = token[len("mode:"):].strip()
                if val in (_MODE_ARTWORKS, _MODE_STALLS, _MODE_DRAW_TYPES):
                    mode = val
        return {"mode": mode, "uid": str(tags).strip()}

    # ---- pid 编码 ----

    def _validate_pid(self, pid: str) -> str:
        """校验 pid 格式并返回类型前缀.

        Args:
            pid (str): 卡片 id, 形如 ``a37866939`` / ``s1395058`` / ``t66186_0``。

        Returns:
            str: 类型前缀 (``a`` / ``s`` / ``t``)。

        Raises:
            ValueError: pid 格式非法。

        Callers:
            - ``services/online_sources/mihuashi/source.py:MihuashiSource.download``
            - ``services/online_sources/mihuashi/source.py:MihuashiSource.fetch_meta``
        """
        if not pid or pid[0] not in _PID_KINDS:
            raise ValueError(f"非法 id: {pid}")
        kind = pid[0]
        rest = pid[1:]
        if kind == "t":
            head, sep, tail = rest.rpartition("_")
            if not sep or not head.isdigit() or not tail.isdigit():
                raise ValueError(f"非法 id: {pid}")
        elif not rest.isdigit():
            raise ValueError(f"非法 id: {pid}")
        return kind

    def split(self, stem: str) -> str:
        """从 stem 拆出本来源 pid, 非本来源格式抛 ValueError.

        Args:
            stem (str): 文件名 stem, 以本来源 ``prefix`` 开头。

        Returns:
            str: pid。

        Callers:
            - ``services/online_sources/framework/base.py:detect_source``
        """
        pid = stem[len(self.prefix):]
        self._validate_pid(pid)
        return pid

    # ---- 三板块条目 -> BooruPost ----

    def _artwork_post(self, item: dict) -> BooruPost:
        """把精选作品条目转为卡片.

        Args:
            item (dict): artworks 列表条目。

        Returns:
            BooruPost: 卡片 (pid=``a{作品ID}``, 标签为技能/品类标签名)。

        Callers:
            - ``services/online_sources/mihuashi/source.py:MihuashiSource._serve_artworks``
            - ``services/online_sources/mihuashi/source.py:MihuashiSource.fetch_meta``
        """
        aid = item["id"]
        return BooruPost(
            source=self.name,
            id=f"a{aid}",
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
            score=int(item.get("likes_count") or 0),
            tags=_tag_names(item),
            posted_at=item.get("created_at") or "",
            file_url=item.get("url") or "",
            preview_url=item.get("url") or "",
            sample_url=item.get("url") or "",
        )

    def _stall_posts(self, stalls: list[dict]) -> list[BooruPost]:
        """把上架橱窗商品列表转为卡片列表.

        Args:
            stalls (list[dict]): ``displayable_stalls`` 数组。

        Returns:
            list[BooruPost]: 卡片 (pid=``s{商品ID}``, 标签为商品名与价格)。

        Callers:
            - ``services/online_sources/mihuashi/source.py:MihuashiSource._serve_stalls``
        """
        posts: list[BooruPost] = []
        for st in stalls:
            cover = st.get("cover_url") or ""
            if not cover:
                continue
            posts.append(BooruPost(
                source=self.name,
                id=f"s{st['id']}",
                tags=f"{st.get('name', '')} ¥{st.get('price', '')}".strip(),
                posted_at=st.get("onsale_at") or "",
                file_url=cover,
                preview_url=cover,
                sample_url=cover,
            ))
        return posts

    def _draw_type_posts(self, draw_types: list[dict]) -> list[BooruPost]:
        """把价目表各项的示例图逐条展开为卡片列表.

        Args:
            draw_types (list[dict]): draw_types 数组, 每项含 ``examples``。

        Returns:
            list[BooruPost]: 卡片 (pid=``t{价目项ID}_{序号}``, 标签为价目项
                标题与价格区间)。

        Callers:
            - ``services/online_sources/mihuashi/source.py:MihuashiSource._serve_draw_types``
        """
        posts: list[BooruPost] = []
        for dt in draw_types:
            label = f"{dt.get('title', '')} ¥{dt.get('min_price', '')}-{dt.get('max_price', '')}".strip()
            for num, ex in enumerate(dt.get("examples") or []):
                url = ex.get("url") or ""
                if not url:
                    continue
                posts.append(BooruPost(
                    source=self.name,
                    id=f"t{dt['id']}_{num}",
                    width=int(float(ex["width"])) if ex.get("width") else 0,
                    height=int(float(ex["height"])) if ex.get("height") else 0,
                    tags=label,
                    file_url=url,
                    preview_url=url,
                    sample_url=url,
                ))
        return posts

    # ---- 搜索 ----

    async def _serve_artworks(self, uid: str, page: int) -> tuple[list[BooruPost], bool]:
        """拉取画师精选作品第 ``page`` 页 (API 原生分页).

        Args:
            uid: 画师 UID。
            page: 前端页码 (1-based)。

        Returns:
            tuple[list[BooruPost], bool]: (卡片列表, 是否还有下一页)。

        Callers:
            - ``services/online_sources/mihuashi/source.py:MihuashiSource.search``
        """
        data = await api_get(
            f"/api/v1/users/{uid}/artworks?page={page}&per={_ARTWORKS_PER}&return_type=page",
        )
        posts = [self._artwork_post(item) for item in data.get("artworks") or []]
        total_pages = int(data.get("total_pages") or 0)
        return posts, page < total_pages

    async def _serve_stalls(self, uid: str) -> tuple[list[BooruPost], bool]:
        """拉取画师上架橱窗商品 (单次全量, 无分页).

        Args:
            uid: 画师 UID。

        Returns:
            tuple[list[BooruPost], bool]: (卡片列表, has_more 恒为 False)。

        Callers:
            - ``services/online_sources/mihuashi/source.py:MihuashiSource.search``
        """
        data = await api_get(f"/api/v1/users/{uid}")
        return self._stall_posts(data.get("user", {}).get("displayable_stalls") or []), False

    async def _serve_draw_types(self, uid: str) -> tuple[list[BooruPost], bool]:
        """拉取画师价目表并把示例图展开为卡片 (单次全量, 无分页).

        Args:
            uid: 画师 UID。

        Returns:
            tuple[list[BooruPost], bool]: (卡片列表, has_more 恒为 False)。

        Callers:
            - ``services/online_sources/mihuashi/source.py:MihuashiSource.search``
        """
        data = await api_get(f"/api/v1/users/{uid}/draw_types?only_display=true")
        return self._draw_type_posts(data.get("draw_types") or []), False

    async def search(
        self,
        tags: str,
        page: int,
        order: str,
        extra: str,
    ) -> tuple[list[BooruPost], bool]:
        """按画师 UID 与板块搜索, 返回 (posts, has_more)。

        精选作品按 API 原生分页逐页拉取; 橱窗与价目表单次全量返回,
        仅第 1 页有内容。

        Args:
            tags: 画师 UID。
            page: 前端页码 (1-based)。
            order: 排序 (米画师不支持, 忽略)。
            extra: ``mode:`` 板块标识。

        Returns:
            tuple[list[BooruPost], bool]: (卡片列表, 是否还有下一页)。

        Callers:
            - ``routes/online_sources.py:search``
            - ``services/online_sources/batch.py:run_batch``
        """
        f = self.parse_query(tags, extra)
        if not f["uid"]:
            return [], False
        if f["mode"] == _MODE_STALLS:
            return await self._serve_stalls(f["uid"])
        if f["mode"] == _MODE_DRAW_TYPES:
            return await self._serve_draw_types(f["uid"])
        return await self._serve_artworks(f["uid"], page)

    # ---- 下载 / 元数据 ----

    async def download(
        self,
        post: BooruPost,
        out_dir: Path,
        *,
        overwrite: bool = False,
        anima_format: bool = True,
        include_meta_tags: bool = False,
        keep_underscores: bool = False,
        emit: ProgressFn | None = None,
    ) -> dict:
        """下载条目图片为 PNG, 并写 meta (米画师不产出旁路标签).

        媒体 URL 直接取自 search 阶段已持有的 ``post.file_url`` (图床无防盗链,
        列表 URL 即原图)。三种条目类型的下载链路一致。实现
        ``base.SourceClient.download``。

        Args:
            post: 帖子。
            out_dir: 输出目录 (筛选子目录)。
            overwrite: 是否覆盖已下载文件。
            anima_format: 未使用 (透传签名统一)。
            include_meta_tags: 未使用 (透传签名统一)。
            keep_underscores: 未使用 (透传签名统一)。
            emit: 下载进度回调, 图片来源忽略 (透传签名统一)。

        Returns:
            dict: 含 ``path``/``stem``/``id``/``tags_path`` 的写入结果。

        Raises:
            ValueError: pid 非法或帖子无媒体 URL。

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.download``
        """
        pid = post.id
        self._validate_pid(pid)

        stem = Path(post.filename).stem
        image_path = out_dir / post.filename
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, post.filename)
        if not overwrite and existing is not None:
            self._write_meta(out_dir, pid, post)  # 补写 meta, 修复历史下载遗留的缺失
            return {
                "skipped": True,
                "path": str(existing),
                "stem": stem,
                "id": pid,
                "tags_path": str(tags_path),
            }

        url = (post.file_url or "").strip()
        if not url:
            raise ValueError(f"pid {pid} 无媒体 URL")
        async with HttpClient() as client:
            data, _ = await client.get_bytes(url, timeout=120.0)

        await self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
        self._write_meta(out_dir, pid, post)
        return {
            "path": str(image_path),
            "stem": stem,
            "id": pid,
            "tags_path": str(tags_path),
        }

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """按 pid 拉取条目元数据并写入磁盘.

        仅精选作品存在单条详情接口 (``/api/v1/artworks/{id}``); 橱窗商品与
        价目表示例无单条接口, 刷新时跳过并记录错误。

        Args:
            out_dir (Path): 输出目录。
            pid (str): 卡片 id。

        Raises:
            ValueError: pid 非法、非精选作品类型或作品详情缺失。

        Callers:
            - ``routes/online_sources.py:_run_refresh``
        """
        kind = self._validate_pid(pid)
        if kind != "a":
            raise ValueError(f"pid {pid} 非精选作品, 无单条详情接口, 跳过刷新")
        data = await api_get(f"/api/v1/artworks/{pid[1:]}")
        artwork = data.get("artwork")
        if not artwork:
            raise ValueError(f"作品 {pid[1:]} 详情缺失")
        self._write_meta(out_dir, pid, self._artwork_post(artwork))
