#!/usr/bin/env python3
"""下载 pixiv 近一年周排行前 50 到指定数据集.

分两阶段执行: 先建立周时间点索引并抓取两种榜单 (综合 / R18),
跨周按作品去重; 再按 时间 → 作品 → 分P 的广度优先顺序下载。
分P 任务按页序号分层排列, 先下所有作品的首图, 再下所有作品的
第二图, 依此类推, 使各作品的分P 均匀推进。
图片转码与 ``.pixiv_meta`` 元数据沿用 ``web_annotator`` 的 pixiv
来源实现, 产物与界面下载一致 (``p{N}_{i}.png`` 与
``.pixiv_meta/{N}_{i}.json``), 已存在的分P 自动跳过。

用法 (从仓库根目录):
  .venv/bin/python download_pixiv_weekly_ranking.py
  .venv/bin/python download_pixiv_weekly_ranking.py --dataset regN --weeks 52 --conn 2
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web_annotator"
DATASET_ROOT = ROOT / "datasets"
WORK_DIRNAME = "main"

RANKING_URL = "https://www.pixiv.net/ranking.php"
MODES = ("weekly", "weekly_r18")
# 仅下载插画与漫画, 动图 (illust_type=2) 原图为 zip 包
IMAGE_TYPES = frozenset({"0", "1"})
INDEX_CONCURRENCY = 3

sys.path.insert(0, str(WEB_DIR))

from dotenv import load_dotenv

load_dotenv(WEB_DIR / ".env")

from services.models.booru import BooruPost
from services.online_sources.framework.http import NetworkError
from services.online_sources.framework.media import download_post
from services.online_sources.pixiv.source import PixivSource


@dataclass(frozen=True)
class RankingWork:
    """跨周去重后的榜单作品, 作为下载阶段的队列单位.

    Attributes:
        illust_id (str): pixiv 作品 ID.
        page_count (int): 作品分P 数.
        width (int): 封面宽度, 单页作品即图片宽度.
        height (int): 封面高度, 单页作品即图片高度.
        posted_at (str): 投稿时间, UTC ISO8601 文本.
        tags (tuple[str, ...]): 作品标签.
        preview_url (str): 榜单给出的封面缩略图地址.
    """

    illust_id: str
    page_count: int
    width: int
    height: int
    posted_at: str
    tags: tuple[str, ...]
    preview_url: str


@dataclass
class Stats:
    """下载阶段的累计计数.

    Attributes:
        ok (int): 新写入磁盘的分P 数.
        skipped (int): 已存在而跳过的分P 数.
        failed (int): 下载失败的分P 数.
        pages (int): 已处理的分P 任务数, 用于进度输出.
    """

    ok: int = 0
    skipped: int = 0
    failed: int = 0
    pages: int = 0


def _prepare_env() -> None:
    """校验凭据配置, 把相对 cookie 路径解析为绝对路径.

    配置文件中的 ``PIXIV_COOKIES`` 相对 ``web_annotator/`` 目录书写,
    脚本从仓库根目录运行, 需要先行解析。

    Raises:
        RuntimeError: 未配置 ``PIXIV_COOKIES`` 或 cookie 文件不存在.

    Callers:
        - ``download_pixiv_weekly_ranking.py:main``
    """
    raw = os.environ.get("PIXIV_COOKIES", "").strip()
    if not raw:
        raise RuntimeError("web_annotator/.env 未配置 PIXIV_COOKIES")
    path = Path(raw)
    if not path.is_absolute():
        path = (WEB_DIR / path).resolve()
        os.environ["PIXIV_COOKIES"] = str(path)
    if not path.is_file():
        raise RuntimeError(f"pixiv cookie 文件不存在: {path}")


def _ranking_url(mode: str, date: str | None = None) -> str:
    """拼接排行接口地址, 固定取第 1 页的前 50 名.

    Args:
        mode (str): 排行模式, ``weekly`` 或 ``weekly_r18``.
        date (str | None): 排行日期 YYYYMMDD; 省略时取最新一期.

    Returns:
        str: 排行接口 URL.

    Callers:
        - ``download_pixiv_weekly_ranking.py:_fetch_week``
        - ``download_pixiv_weekly_ranking.py:_build_index``
    """
    url = f"{RANKING_URL}?mode={mode}&content=all&format=json&p=1"
    return f"{url}&date={date}" if date else url


async def _fetch_week(source: PixivSource, mode: str, date: str) -> list[RankingWork]:
    """抓取单周单个榜单的前 50 名作品, 保持名次顺序.

    Args:
        source (PixivSource): 复用其 AJAX 请求与 cookie 处理.
        mode (str): 排行模式.
        date (str): 排行日期 YYYYMMDD.

    Returns:
        list[RankingWork]: 榜单作品列表.

    Callers:
        - ``download_pixiv_weekly_ranking.py:_build_index``
    """
    data = await source._request(_ranking_url(mode, date))
    works: list[RankingWork] = []
    for item in data["contents"]:
        if item["illust_type"] not in IMAGE_TYPES or item.get("is_masked"):
            continue
        works.append(RankingWork(
            illust_id=str(item["illust_id"]),
            page_count=int(item["illust_page_count"]),
            width=int(item["width"]),
            height=int(item["height"]),
            posted_at=datetime.fromtimestamp(
                int(item["illust_upload_timestamp"]), tz=timezone.utc,
            ).isoformat(),
            tags=tuple(item["tags"]),
            preview_url=item["url"],
        ))
    return works


async def _build_index(
    source: PixivSource, weeks: int,
) -> tuple[str, int, list[RankingWork]]:
    """建立周时间点索引, 抓取各周榜单并按作品去重.

    以接口返回的最新周日期为基准, 每 7 天向前取一个时间点; 榜单按
    时间升序、模式顺序、名次顺序拼接后去重, 保持首次出现的顺序。

    Args:
        source (PixivSource): pixiv 来源客户端.
        weeks (int): 时间点数量.

    Returns:
        tuple[str, int, list[RankingWork]]: (最新周日期, 去重前条目数,
        去重后的作品列表).

    Callers:
        - ``download_pixiv_weekly_ranking.py:_run``
    """
    latest = (await source._request(_ranking_url(MODES[0])))["date"]
    latest_day = datetime.strptime(latest, "%Y%m%d").replace(tzinfo=timezone.utc)
    dates = [
        (latest_day - timedelta(days=7 * offset)).strftime("%Y%m%d")
        for offset in reversed(range(weeks))
    ]
    semaphore = asyncio.Semaphore(INDEX_CONCURRENCY)

    async def fetch(mode: str, date: str) -> list[RankingWork]:
        """限流抓取单个时间点的单个榜单.

        Args:
            mode (str): 排行模式.
            date (str): 排行日期 YYYYMMDD.

        Returns:
            list[RankingWork]: 该榜作品列表.

        Callers:
            - ``download_pixiv_weekly_ranking.py:_build_index``
        """
        async with semaphore:
            return await _fetch_week(source, mode, date)

    batches = await asyncio.gather(
        *(fetch(mode, date) for date in dates for mode in MODES)
    )
    total = sum(len(batch) for batch in batches)
    seen: set[str] = set()
    works: list[RankingWork] = []
    for batch in batches:
        for work in batch:
            if work.illust_id in seen:
                continue
            seen.add(work.illust_id)
            works.append(work)
    return latest, total, works


def _page_tasks(works: list[RankingWork]) -> list[tuple[RankingWork, int]]:
    """按分P 层展开下载任务, 使分P 之间均匀推进.

    第 0 层为所有作品的首图, 第 1 层为所有作品的第二图, 依次类推;
    每层内部保持时间去重后的作品顺序.

    Args:
        works (list[RankingWork]): 去重后的作品列表.

    Returns:
        list[tuple[RankingWork, int]]: 按层排列的 (作品, 分P 序号) 任务.

    Callers:
        - ``download_pixiv_weekly_ranking.py:_run``
    """
    deepest = max((work.page_count for work in works), default=0)
    return [
        (work, num)
        for num in range(deepest)
        for work in works
        if num < work.page_count
    ]


class WorkPosts:
    """按作品惰性展开分P 并缓存, 供分P 级队列复用.

    同一作品的所有分P 任务共享一次 ``PixivSource._expand`` 结果,
    展开用作品级锁串行, 避免重复请求分P 接口.

    Callers:
        - ``download_pixiv_weekly_ranking.py:_worker``
    """

    def __init__(self, source: PixivSource) -> None:
        """初始化来源引用与缓存容器.

        Args:
            source (PixivSource): pixiv 来源客户端.

        Callers:
            - ``download_pixiv_weekly_ranking.py:_run``
        """
        self._source = source
        self._posts: dict[str, list[BooruPost]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, work: RankingWork) -> list[BooruPost]:
        """返回作品的全部分P 帖子, 首次调用时展开并缓存.

        Args:
            work (RankingWork): 榜单作品.

        Returns:
            list[BooruPost]: 按分P 顺序排列的帖子.

        Raises:
            NetworkError: 分P 接口请求失败.
            RuntimeError: 分P 接口返回错误.

        Callers:
            - ``download_pixiv_weekly_ranking.py:_worker``
        """
        lock = self._locks.setdefault(work.illust_id, asyncio.Lock())
        async with lock:
            posts = self._posts.get(work.illust_id)
            if posts is None:
                posts = await self._source._expand(work.illust_id, {
                    "pageCount": work.page_count,
                    "width": work.width,
                    "height": work.height,
                    "url": work.preview_url,
                    "createDate": work.posted_at,
                    "tags": list(work.tags),
                })
                self._posts[work.illust_id] = posts
            return posts


async def _download_page(
    source: PixivSource, post: BooruPost, out_dir: Path,
) -> tuple[int, int, int]:
    """下载单个分P, 返回 (新增, 跳过, 失败) 计数.

    图片已写入而元数据缺失的分P (任务中断的残留) 经 ``fetch_meta``
    补齐后计入跳过.

    Args:
        source (PixivSource): pixiv 来源客户端.
        post (BooruPost): 已展开的分P 帖子.
        out_dir (Path): 数据集工作目录.

    Returns:
        tuple[int, int, int]: (新增, 跳过, 失败) 计数, 三者之和为 1.

    Callers:
        - ``download_pixiv_weekly_ranking.py:_worker``
    """
    post.stem = source.file_stem(post.id)
    post.filename = f"{post.stem}.png"
    meta_path = out_dir / source.meta_subdir / f"{post.id}.json"
    if (out_dir / post.filename).is_file() and not meta_path.is_file():
        try:
            await source.fetch_meta(out_dir, post.id)
        except (NetworkError, RuntimeError, ValueError) as exc:
            print(f"    失败 {post.id}: 元数据补齐失败: {exc}")
            return 0, 0, 1
        return 0, 1, 0
    try:
        result = await download_post(source.name, post, out_dir)
    except (NetworkError, OSError, RuntimeError, ValueError) as exc:
        print(f"    失败 {post.id}: {exc}")
        return 0, 0, 1
    return (0, 1, 0) if result.skipped else (1, 0, 0)


async def _worker(
    source: PixivSource,
    queue: asyncio.Queue[tuple[int, RankingWork, int]],
    out_dir: Path,
    total: int,
    stats: Stats,
    posts_cache: WorkPosts,
) -> None:
    """从队列取单个分P 任务并下载.

    Args:
        source (PixivSource): pixiv 来源客户端.
        queue (asyncio.Queue[tuple[int, RankingWork, int]]): 按层排好的
            (任务序号, 作品, 分P 序号) 队列.
        out_dir (Path): 数据集工作目录.
        total (int): 分P 任务总数, 用于进度输出.
        stats (Stats): 共享累计计数.
        posts_cache (WorkPosts): 按作品缓存的分P 帖子.

    Returns:
        None: 无返回值; 队列取空后退出.

    Callers:
        - ``download_pixiv_weekly_ranking.py:_run``
    """
    while True:
        try:
            order, work, num = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            posts = await posts_cache.get(work)
        except (NetworkError, RuntimeError) as exc:
            stats.pages += 1
            stats.failed += 1
            print(f"[{order}/{total}] {work.illust_id}_{num} · 失败: {exc}")
            continue
        if num >= len(posts):
            stats.pages += 1
            stats.failed += 1
            print(f"[{order}/{total}] {work.illust_id}_{num} · 失败: 分P 结果缺失")
            continue
        ok, skipped, failed = await _download_page(source, posts[num], out_dir)
        stats.pages += 1
        stats.ok += ok
        stats.skipped += skipped
        stats.failed += failed
        print(
            f"[{order}/{total}] {posts[num].id} "
            f"· 新增 {ok} · 跳过 {skipped} · 失败 {failed}"
        )


async def _run(dataset: str, weeks: int, conn: int, limit: int) -> int:
    """执行索引与下载两阶段, 返回失败分P 数.

    Args:
        dataset (str): ``datasets/`` 下的任务名.
        weeks (int): 周时间点数量.
        conn (int): 并发下载的分P 任务数.
        limit (int): 仅下载前 N 个作品, 0 表示不限.

    Returns:
        int: 失败的分P 数.

    Callers:
        - ``download_pixiv_weekly_ranking.py:main``
    """
    source = PixivSource()
    latest, total, works = await _build_index(source, weeks)
    print(f"[索引] 最新周 {latest}, {weeks} 个时间点 × {len(MODES)} 种榜单")
    print(f"[索引] 榜单条目 {total} 个, 去重后作品 {len(works)} 个")
    if limit:
        works = works[:limit]
        print(f"[索引] 按 --limit 截取前 {len(works)} 个作品")

    tasks = _page_tasks(works)
    out_dir = DATASET_ROOT / dataset / WORK_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[下载] 输出目录 {out_dir}, 并发 {conn}, 分P 任务 {len(tasks)} 个")

    queue: asyncio.Queue[tuple[int, RankingWork, int]] = asyncio.Queue()
    for order, (work, num) in enumerate(tasks, start=1):
        queue.put_nowait((order, work, num))
    stats = Stats()
    posts_cache = WorkPosts(source)
    await asyncio.gather(*(
        _worker(source, queue, out_dir, len(tasks), stats, posts_cache)
        for _ in range(conn)
    ))
    print(
        f"[完成] 作品 {len(works)} 个 · 分P {stats.pages} 个 · "
        f"新增 {stats.ok} · 跳过 {stats.skipped} · 失败 {stats.failed}"
    )
    return stats.failed


def _parse_args() -> argparse.Namespace:
    """解析命令行参数.

    Returns:
        argparse.Namespace: 含 dataset / weeks / conn / limit.

    Callers:
        - ``download_pixiv_weekly_ranking.py:main``
    """
    parser = argparse.ArgumentParser(description="下载 pixiv 近一年周排行前 50")
    parser.add_argument("--dataset", default="regN", help="datasets/ 下的任务名")
    parser.add_argument("--weeks", type=int, default=52, help="周时间点数量")
    parser.add_argument("--conn", type=int, default=2, help="并发下载的作品数")
    parser.add_argument("--limit", type=int, default=0, help="仅下载前 N 个作品, 0 不限")
    return parser.parse_args()


def main() -> None:
    """脚本入口: 校验环境并执行索引与下载.

    Raises:
        SystemExit: 存在下载失败的分P 时以状态码 1 退出.

    Callers:
        - 命令行直接执行
    """
    args = _parse_args()
    if args.weeks < 1 or args.conn < 1 or args.limit < 0:
        raise SystemExit("--weeks / --conn 必须为正数, --limit 不得为负数")
    _prepare_env()
    failed = asyncio.run(_run(args.dataset, args.weeks, args.conn, args.limit))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
