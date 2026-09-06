"""B 站来源客户端: 将用户动态的每张图映射为一张 BooruPost 卡片.

`tags` 参数承载 B 站 UID, `extra` 为筛选串 (``type:`` / ``after:`` /
``before:`` / ``keyword:``)。搜索时先分页拉取该用户全部动态并解析, 按
筛选条件过滤后把所有可展示图片展开为扁平列表, 缓存于进程内并按其序号
分页返回; 下载时按 ``{动态ID}_{图片序号}`` 的 pid 解析原图 URL 并写入磁盘。

Callers:
    - ``services/online_sources/__init__.py:`` (注册)
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from services.online_sources.framework.base import SourceClient
from services.online_sources.bilibili.client import (
    fetch_bili_bytes,
    fetch_dynamics,
    resolve_record,
    video_pubdate,
)
from services.online_sources.bilibili.models import DynamicRecord
from services.online_sources.framework.session import SessionStore
from services.online_sources.framework.utils import find_downloaded

from services.models import BooruPost

# 动态类型 -> 筛选页展示语义
_TYPE_MAP = {
    "DYNAMIC_TYPE_DRAW": "image",
    "DYNAMIC_TYPE_OPUS": "image",
    "DYNAMIC_TYPE_AV": "video",
    "DYNAMIC_TYPE_FORWARD": "forward",
}


def _pub_iso(pub_ts: int | str | None) -> str:
    """把 B 站动态发布时间戳转为本地时区 ISO8601 文本.

    Args:
        pub_ts (int | str | None): 动态发布时间戳 (desktop 接口为 int,
            v1 接口为字符串), 缺失时返回空串。

    Returns:
        str: ISO8601 本地时区时间文本, 无时间戳时为空串。

    Callers:
        - ``services/online_sources/bilibili/source.py:BilibiliSource._expand``
    """
    if not pub_ts:
        return ""
    return datetime.fromtimestamp(int(pub_ts)).astimezone().isoformat()


class BilibiliSource(SourceClient):
    """从 B 站用户动态拉取图片的来源客户端.

    实现 ``base.SourceClient`` 的 ``search`` / ``download``。

    Callers:
        - ``services/online_sources/framework/base.py:register_source``
    """

    name = "bilibili"
    prefix = "bi"
    order_tags: dict[str, str] = {}
    valid_orders: tuple[str, ...] = ()
    session_based = True
    writes_caption = False
    image_hosts = ("hdslb.com",)
    image_referer = "https://www.bilibili.com/"

    def __init__(self) -> None:
        """初始化会话存储. """
        super().__init__()
        self._sessions = SessionStore()

    def parse_query(self, tags: str, extra: str) -> dict[str, str]:
        """解析 B 站查询串: ``tags`` 为 UID, ``extra`` 为 ``type:``/``after:``/``before:``/``keyword:``.

        类型可多选, 以多个 ``type:`` token 表达, 结果以逗号分隔去重保序
        (字符串, 可直接用作分页缓存键)。

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.search``
        """
        out = {
            "uid": str(tags).strip(),
            "type": "",
            "after": "",
            "before": "",
            "keyword": "",
        }
        types: list[str] = []
        for token in (extra or "").split():
            for key in ("type", "after", "before", "keyword"):
                if token.startswith(key + ":"):
                    val = token[len(key) + 1:].strip()
                    if key == "type":
                        if val:
                            types.append(val)
                    else:
                        out[key] = val
                    break
        out["type"] = ",".join(dict.fromkeys(types))
        return out

    def _matches(self, record: DynamicRecord, f: dict[str, str]) -> bool:
        """判断动态是否满足类型 / 关键词 / 时间筛选.

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.search``
        """
        ttype = _TYPE_MAP.get(record.type or "", "other")
        if f["type"] and ttype not in f["type"].split(","):
            return False
        if f["keyword"] and f["keyword"].lower() not in record.text.lower():
            return False
        if (f["after"] or f["before"]):
            if not record.pub_ts:
                return False
            ts = int(record.pub_ts)
            d = datetime.fromtimestamp(ts).date()
            if f["after"] and d < date.fromisoformat(f["after"]):
                return False
            if f["before"] and d > date.fromisoformat(f["before"]):
                return False
        return True

    def _expand(self, record: DynamicRecord) -> list[BooruPost]:
        """将一条动态展开为 BooruPost 列表.

        转发动态取原文的图; 视频动态取封面; 无可用图片时返回空列表。
        帖子 id 前缀用外层 ``record.id``, 避免原动态与转发动态并存时
        展开出相同 id。缩略图 / 样图 URL 由原图 URL 加 B 站 CDN 后缀派生。
        B 站视频封面为平台规范尺寸 1920x1080 (实测确认), 直接作为真实宽高,
        不另做探测请求。

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.search``
        """
        source = record.forwarded if record.forwarded is not None else record
        text = source.text or record.text
        # 视频动态: 携带 bvid 作为帖子 id, 前端据此提供"下载视频"(经 BBDown 写入数据集的 video 目录)
        bvid = (source.video.bvid or "") if source.video else ""
        is_video = bool(bvid)
        if source.images:
            items = [
                (img.url, img.width or 0, img.height or 0) for img in source.images
            ]
        elif source.video and source.video.cover:
            items = [(source.video.cover, 1920, 1080)]
        else:
            return []
        return [
            BooruPost(
                source=self.name,
                id=bvid if is_video else f"{record.id}_{i}",
                width=w,
                height=h,
                posted_at=_pub_iso(source.pub_ts),
                file_url=url,
                preview_url=url + "@480w_480h_1e.webp",
                sample_url=url + "@1280w_1280h_1e.webp",
                tags=text,
                is_video=is_video,
                is_forward=record.forwarded is not None,
            )
            for i, (url, w, h) in enumerate(items)
        ]

    async def search(
        self,
        tags: str,
        page: int,
        order: str,
        extra: str,
    ) -> tuple[list[BooruPost], bool]:
        """游标式来源不支持页码式搜索; 使用 ``search_session``. 实现 ``base.SourceClient.search``.

        Callers:
            - ``services/online_sources/framework/base.py:search_posts`` (仅页码式来源触发)
        """
        raise NotImplementedError("bilibili 为会话式来源, 请使用 search_session")

    async def search_session(
        self,
        tags: str,
        session: str,
        order: str,
        extra: str,
    ) -> tuple[list[BooruPost], bool, str]:
        """按会话续拉下一批动态图片, 返回 (posts, has_more, session_token).

        B 站 feed 是游标分页, 会话持有动态游标; 每次调用从当前游标拉取
        下一批动态并过滤展开, 直到凑出非空批次或上游耗尽。发起新搜索
        (session 为空或查询键变化) 或会话超过一天无翻页时由 ``SessionStore``
        重建会话。

        风控/限流时接口返回空心响应 (无记录且无游标), 与真实末页结构一致、
        无法单次区分; 会话**不锁存**末页判定: 空心页直接返回空结果且不推进
        游标, 由有记忆的调用方 (批量下载) 连续空页确认后才判定抓完, 避免
        瞬时风控导致批量提前停止。

        Callers:
            - ``routes/online_sources.py:search``
        """
        f = self.parse_query(tags, extra)
        uid = int(f["uid"])
        query_key = (uid, f["type"], f["after"], f["before"], f["keyword"])
        sess = await self._sessions.acquire(session, self.name, query_key)
        async with sess.lock:
            st = sess.state
            if not st:
                st.update(offsets=[""])
            page_posts: list[BooruPost] = []
            has_more = False
            retries = 0
            while not page_posts:
                records, next_offset, has_more = await fetch_dynamics(
                    uid, offset=st["offsets"][-1]
                )
                if not records and not next_offset:
                    if retries < 3:
                        # 风控空心响应: 短暂等待后重试, 最多 3 次
                        retries += 1
                        await asyncio.sleep(1.5)
                        continue
                    # 连续 3 次空心: 交由上层判定
                    return [], False, sess.token
                if next_offset and next_offset != st["offsets"][-1]:
                    st["offsets"].append(next_offset)
                page_posts = [
                    post
                    for record in records
                    if self._matches(record, f)
                    for post in self._expand(record)
                ]
        return page_posts, has_more, sess.token

    def split(self, stem: str) -> str:
        """从 stem 拆出本来源 pid, 非本来源格式抛 ValueError.

        pid 为 ``{动态ID}_{序号}`` 文本, 经 ``_split_pid`` 校验.

        Callers:
            - ``services/online_sources/framework/base.py:detect_source``
        """
        rest = stem[len(self.prefix):] if stem.startswith(self.prefix) else ""
        self._split_pid(rest)
        return rest

    def _split_pid(self, pid: str) -> tuple[str, int]:
        """将 ``{动态ID}_{图片序号}`` 拆为 (动态ID, 序号).

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.download``
            - ``services/online_sources/bilibili/source.py:BilibiliSource.fetch_meta``
        """
        idx = pid.rfind("_")
        if idx <= 0:
            raise ValueError(f"非法 id: {pid}")
        return pid[:idx], int(pid[idx + 1:])

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """按 pid 重新解析并写入帖元数据.

        图片帖复用 ``_expand`` 展开逻辑, 以与搜索阶段同源的完整字段 (发布
        时间 / 宽高 / 缩略图派生 URL) 重写 meta; 视频帖 (bvid) 经视频信息
        接口补齐发布时间, 其余字段沿用既有 meta。实现
        ``base.SourceClient.fetch_meta``。

        Callers:
            - ``routes/online_sources.py:_run_refresh``
        """
        meta_path = out_dir / self.meta_subdir / f"{pid}.json"
        if pid.startswith("BV"):
            old = BooruPost.model_validate_json(meta_path.read_text(encoding="utf-8"))
            pubdate = await video_pubdate(pid)
            self._write_meta(out_dir, pid, old.model_copy(update={"posted_at": _pub_iso(pubdate)}))
            return
        dynamic_id, index = self._split_pid(pid)
        record = await resolve_record(dynamic_id)
        self._write_meta(out_dir, pid, self._expand(record)[index])

    async def download(
        self,
        post: BooruPost,
        out_dir: Path,
        *,
        overwrite: bool = False,
        anima_format: bool = True,
        include_meta_tags: bool = False,
        keep_underscores: bool = False,
        emit: Callable[[int, int], None] | None = None,
    ) -> dict:
        """下载帖子到 ``out_dir`` (图片帖) 或 ``video`` 目录 (视频帖).

        图片帖按 ``post.file_url`` 转 PNG 写入磁盘并写旁路标签与 meta; 视频帖
        (``post.is_video``) 直接经 ``_download_video`` 用 BBDown 写入 ``out_dir``
        (即数据集 ``video`` 目录), 并把 BBDown 进度透传给 ``emit``。实现
        ``base.SourceClient.download``。

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.download`` (经 ``media.download_post`` 委托)
            - ``services/online_sources/batch.py:download_one``
        """
        if post.is_video:
            return await self._download_video(post, out_dir, emit=emit)
        pid = post.id
        stem = Path(post.filename).stem
        image_path = out_dir / post.filename
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, post.filename)

        if not overwrite and existing is not None and (tags_path.is_file() or not self.writes_caption):
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
        caption = (post.tags or "").strip()
        if include_meta_tags:
            caption = (caption + (" " + self.name) if caption else self.name).strip()

        data, _ = await fetch_bili_bytes(url)
        await self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
        if self.writes_caption:
            tags_path.write_text(caption + "\n", encoding="utf-8")
        # 保留搜索阶段携带的 posted_at / 宽高等字段, 仅以标签说明刷新 tags
        self._write_meta(out_dir, pid, post.model_copy(update={"tags": caption}))
        return {
            "path": str(image_path),
            "stem": stem,
            "id": pid,
            "tags_path": str(tags_path),
        }

    async def _download_video(
        self,
        post: BooruPost,
        video_dir: Path,
        *,
        emit: Callable[[int, int], None] | None = None,
    ) -> dict:
        """调用 BBDown 下载视频帖子(BV 号)到 video 目录, 返回写入结果.

        B 站 DASH 音视频混流由专用工具 BBDown 完成, 本层仅委托子进程并
        透传进度。BBDown 路径取自环境变量 ``BBDOWN_PATH``, 缺省时在 PATH
        中查找 ``BBDown``。下载以 ``post.id`` (即 BV 号) 为输入, 输出到 ``video_dir``
        (数据集根 ``video``)。

        Args:
            post: 视频帖子(``id`` 即 BV 号).
            video_dir: 输出目录(数据集根 ``video``).

        Returns:
            dict: 含 ``path``/``stem``/``id``, 供 ``media.download_post`` 包装.

        Raises:
            ValueError: 缺少 bvid / 未找到 BBDown 工具 / 未产出文件.

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.download``
        """
        bvid = (post.id or "").strip()
        if not bvid:
            raise ValueError("视频帖子缺少 bvid (id)")
        exe = os.environ.get("BBDOWN_PATH") or shutil.which("BBDown")
        if not exe:
            raise ValueError(
                "未找到 BBDown, 请设置环境变量 BBDOWN_PATH 或在 PATH 中提供 BBDown"
            )
        video_dir.mkdir(parents=True, exist_ok=True)
        before = {f.name for f in video_dir.iterdir() if f.is_file()}
        cmd = [
            exe, bvid,
            "-o", str(video_dir),
            "--skip-cover", "--skip-subtitle", "--skip-ai",
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        if emit:
            emit(1, 100)
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            m = re.search(r"(\d{1,3})\s*%", text)
            pct = int(m.group(1)) if m else None
            if pct is not None and emit:
                emit(pct, 100)
        await process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"BBDown 下载失败, 退出码 {process.returncode}")
        after = {f.name for f in video_dir.iterdir() if f.is_file()}
        created = sorted(after - before)
        if emit:
            emit(100, 100)
        if not created:
            raise ValueError("BBDown 未产出任何文件")
        video_path = video_dir / created[0]
        return {
            "path": str(video_path),
            "stem": video_path.stem,
            "id": post.id,
        }
