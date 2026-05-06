import argparse
import asyncio
import json
import os
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

import httpx
from tqdm.asyncio import tqdm as tqdm_async

from DanbooruApi.Gelbooru import Gelbooru

DEFAULT_MAX_CONCURRENT = 8
DEFAULT_DOWNLOAD_RETRIES = 5
DEFAULT_RETRY_BASE_DELAY = 1.0
DEFAULT_RETRY_MAX_DELAY = 60.0

T = TypeVar("T")

# 每条帖子一份完整 dapi post JSON，与图片同目录下的隐藏文件夹
GELBOORU_META_SUBDIR = ".gelbooru_meta"

_GELBOORU_CREATED_AT_FMT = "%a %b %d %H:%M:%S %z %Y"


@dataclass
class GelbooruSearchOptions:
    """与 Gelbooru 站内高级搜索一致的筛选，写入 tags 查询串（服务端筛选）。"""

    rating: str | None = None
    exclude_ratings: tuple[str, ...] = ()
    min_score: int | None = None
    max_score: int | None = None
    min_width: int | None = None
    max_width: int | None = None
    min_height: int | None = None
    max_height: int | None = None
    id_min: int | None = None
    id_max: int | None = None
    sort: str | None = None
    uploaded_by: str | None = None
    extra_tag_chunks: tuple[str, ...] = ()

    # 上传日期在 API 返回的 created_at 上做客户端过滤（不增加列表 API 能力，但可精确到日）
    created_after: date | None = None
    created_before: date | None = None


def build_gelbooru_tag_query(base_tags: str, opts: GelbooruSearchOptions) -> str:
    parts: list[str] = []
    base = base_tags.strip()
    if base:
        parts.append(base)
    if opts.rating:
        parts.append(f"rating:{opts.rating}")
    for er in opts.exclude_ratings:
        parts.append(f"-rating:{er}")
    if opts.min_score is not None:
        parts.append(f"score:>={opts.min_score}")
    if opts.max_score is not None:
        parts.append(f"score:<={opts.max_score}")
    if opts.min_width is not None:
        parts.append(f"width:>={opts.min_width}")
    if opts.max_width is not None:
        parts.append(f"width:<={opts.max_width}")
    if opts.min_height is not None:
        parts.append(f"height:>={opts.min_height}")
    if opts.max_height is not None:
        parts.append(f"height:<={opts.max_height}")
    if opts.id_min is not None:
        parts.append(f"id:>={opts.id_min}")
    if opts.id_max is not None:
        parts.append(f"id:<={opts.id_max}")
    if opts.sort:
        s = opts.sort.strip()
        if not s.startswith("sort:"):
            s = f"sort:{s}"
        parts.append(s)
    if opts.uploaded_by:
        u = opts.uploaded_by.strip()
        if not u.startswith("user:"):
            u = f"user:{u}"
        parts.append(u)
    for chunk in opts.extra_tag_chunks:
        c = chunk.strip()
        if c:
            parts.append(c)
    return " ".join(parts)


def _parse_gelbooru_created_at(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), _GELBOORU_CREATED_AT_FMT)
    except ValueError:
        return None


def _post_upload_date_utc(post: dict[str, Any]) -> date | None:
    dt = _parse_gelbooru_created_at(str(post.get("created_at", "")))
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).date()


def _has_client_metadata_filter(opts: GelbooruSearchOptions) -> bool:
    return (
        opts.created_after is not None
        or opts.created_before is not None
        or opts.min_width is not None
        or opts.max_width is not None
        or opts.min_height is not None
        or opts.max_height is not None
        or opts.min_score is not None
        or opts.max_score is not None
        or bool(opts.exclude_ratings)
    )


def filter_posts_client_constraints(posts: list[dict[str, Any]], opts: GelbooruSearchOptions) -> list[dict[str, Any]]:
    """
    用列表 API 返回的 width/height/score/created_at 再筛一层。
    避免仅依赖 tags 串（或旧 run 残留文件）导致结果与分辨率/分数条件不一致。
    """
    if not _has_client_metadata_filter(opts):
        return posts
    out: list[dict[str, Any]] = []
    for p in posts:
        if opts.created_after is not None or opts.created_before is not None:
            d = _post_upload_date_utc(p)
            if d is None:
                continue
            if opts.created_after is not None and d < opts.created_after:
                continue
            if opts.created_before is not None and d > opts.created_before:
                continue
        if (
            opts.min_width is not None
            or opts.max_width is not None
            or opts.min_height is not None
            or opts.max_height is not None
            or opts.min_score is not None
            or opts.max_score is not None
        ):
            try:
                w = int(p["width"])
                h = int(p["height"])
                sc = int(p["score"])
            except (KeyError, TypeError, ValueError):
                continue
            if opts.min_width is not None and w < opts.min_width:
                continue
            if opts.max_width is not None and w > opts.max_width:
                continue
            if opts.min_height is not None and h < opts.min_height:
                continue
            if opts.max_height is not None and h > opts.max_height:
                continue
            if opts.min_score is not None and sc < opts.min_score:
                continue
            if opts.max_score is not None and sc > opts.max_score:
                continue
        if opts.exclude_ratings:
            pr = str(p.get("rating", "")).lower().strip()
            banned = {x.lower() for x in opts.exclude_ratings}
            if pr in banned:
                continue
        out.append(p)
    return out


def _normalize_posts(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, dict):
        return [raw]
    return list(raw)


def _page_all_upload_dates_strictly_before(posts: list[dict[str, Any]], cutoff: date) -> bool:
    """页内每条都能解析且 UTC 日历日都严格早于 cutoff（无解析成功则视为不满足）。"""
    if not posts:
        return False
    for p in posts:
        d = _post_upload_date_utc(p)
        if d is None or d >= cutoff:
            return False
    return True


def _page_all_upload_dates_strictly_after(posts: list[dict[str, Any]], cutoff: date) -> bool:
    """页内每条都能解析且 UTC 日历日都严格晚于 cutoff。"""
    if not posts:
        return False
    for p in posts:
        d = _post_upload_date_utc(p)
        if d is None or d <= cutoff:
            return False
    return True


def _listing_opts_for_upload_date_pagination(opts: GelbooruSearchOptions) -> tuple[GelbooruSearchOptions, str | None]:
    """
    为「按上传日客户端筛选」选固定排序以便提前结束分页：
    - 含 --created-after：sort:id:desc（从新到旧，一旦整页都早于下界则后续更旧，可停）
    - 仅 --created-before：sort:id:asc（从旧到新，一旦整页都晚于上界则后续更新，可停）
    """
    if not _has_any_client_date_criteria(opts):
        return opts, None
    prev = opts.sort.strip() if opts.sort else ""
    if opts.created_after is not None:
        forced = "id:desc"
    else:
        forced = "id:asc"
    return replace(opts, sort=forced), (prev or None)


def _should_stop_date_pagination(
    raw_page: list[dict[str, Any]], opts: GelbooruSearchOptions
) -> bool:
    if opts.created_after is not None:
        return _page_all_upload_dates_strictly_before(raw_page, opts.created_after)
    if opts.created_before is not None:
        return _page_all_upload_dates_strictly_after(raw_page, opts.created_before)
    return False


def search_options_from_args(args: argparse.Namespace) -> GelbooruSearchOptions:
    extra: tuple[str, ...] = tuple(args.extra_tags) if args.extra_tags else ()
    excl = tuple(args.exclude_rating) if args.exclude_rating else ()
    return GelbooruSearchOptions(
        rating=args.rating,
        exclude_ratings=excl,
        min_score=args.min_score,
        max_score=args.max_score,
        min_width=args.min_width,
        max_width=args.max_width,
        min_height=args.min_height,
        max_height=args.max_height,
        id_min=args.id_min,
        id_max=args.id_max,
        sort=args.sort,
        uploaded_by=args.uploaded_by,
        extra_tag_chunks=extra,
        created_after=args.created_after,
        created_before=args.created_before,
    )


def _has_any_server_side_criteria(opts: GelbooruSearchOptions) -> bool:
    return bool(
        opts.rating
        or bool(opts.exclude_ratings)
        or opts.min_score is not None
        or opts.max_score is not None
        or opts.min_width is not None
        or opts.max_width is not None
        or opts.min_height is not None
        or opts.max_height is not None
        or opts.id_min is not None
        or opts.id_max is not None
        or opts.sort
        or opts.uploaded_by
        or opts.extra_tag_chunks
    )


def _has_any_client_date_criteria(opts: GelbooruSearchOptions) -> bool:
    return opts.created_after is not None or opts.created_before is not None


def _log(msg: str, *, use_tqdm_write: bool) -> None:
    if use_tqdm_write:
        tqdm_async.write(msg)
    else:
        print(msg)


def _write_gelbooru_meta_json(output_dir: str, post: dict[str, Any]) -> None:
    meta_dir = os.path.join(output_dir, GELBOORU_META_SUBDIR)
    os.makedirs(meta_dir, exist_ok=True)
    pid = post.get("id", "unknown")
    path = os.path.join(meta_dir, f"{pid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(post, f, ensure_ascii=False, indent=2)


async def async_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    attempts = max(1, max_retries)
    for attempt in range(attempts):
        try:
            return await fn()
        except (httpx.HTTPError, httpx.RequestError, TimeoutError, RuntimeError, OSError) as e:
            if attempt >= attempts - 1:
                raise
            delay = min(base_delay * (2**attempt), max_delay)
            if on_retry:
                on_retry(attempt + 1, e, delay)
            await asyncio.sleep(delay)
    raise RuntimeError("async_retry: unreachable")


async def _process_one_post(
    post,
    output_dir,
    gelbooru_api,
    *,
    max_retries: int,
    retry_base_delay: float,
    retry_max_delay: float,
    use_tqdm_write: bool,
    overwrite: bool = False,
):
    image_id = post["id"]
    try:
        image_url = post["file_url"]
        image_tags = post["tags"]

        file_extension = os.path.splitext(image_url)[1]
        if file_extension not in [".jpg", ".png", ".jpeg"]:
            _log(f"Skipping non-image file: {image_url}", use_tqdm_write=use_tqdm_write)
            return

        image_filename = f"{image_id}{file_extension}"
        tags_filename = f"{image_id}.txt"

        image_path = os.path.join(output_dir, image_filename)
        tags_path = os.path.join(output_dir, tags_filename)

        if not overwrite and os.path.exists(image_path) and os.path.exists(tags_path):
            _write_gelbooru_meta_json(output_dir, post)
            return

        async def _download_and_tags() -> dict[str, Any] | None:
            fresh: dict[str, Any] | None = None
            if overwrite or not os.path.exists(image_path):
                fresh = await gelbooru_api.getImage(image_id, image_path)
            with open(tags_path, "w", encoding="utf-8") as f:
                f.write(image_tags.replace(" ", ", "))
            return fresh

        def _on_retry(n: int, err: BaseException, delay: float) -> None:
            _log(
                f"post {image_id} 失败 (第 {n}/{max_retries} 次): {err!s}，{delay:.1f}s 后重试…",
                use_tqdm_write=use_tqdm_write,
            )

        refreshed = await async_retry(
            _download_and_tags,
            max_retries=max_retries,
            base_delay=retry_base_delay,
            max_delay=retry_max_delay,
            on_retry=_on_retry,
        )
        meta_src = refreshed if refreshed is not None else post
        _write_gelbooru_meta_json(output_dir, meta_src)

    except Exception as e:
        _log(f"Error processing post {post.get('id', 'N/A')}: {e}", use_tqdm_write=use_tqdm_write)


async def download_images(
    posts,
    output_dir,
    gelbooru_api,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    *,
    show_progress: bool = True,
    max_retries: int = DEFAULT_DOWNLOAD_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
    overwrite: bool = False,
):
    if max_concurrent < 1:
        max_concurrent = 1
    sem = asyncio.Semaphore(max_concurrent)
    use_tqdm_write = show_progress

    async def bounded(post):
        async with sem:
            await _process_one_post(
                post,
                output_dir,
                gelbooru_api,
                max_retries=max_retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                use_tqdm_write=use_tqdm_write,
                overwrite=overwrite,
            )

    coros = [bounded(p) for p in posts]
    if show_progress:
        await tqdm_async.gather(*coros, desc="下载", total=len(coros), unit="张")
    else:
        await asyncio.gather(*coros)


async def download_dataset(
    tags: str,
    output_dir,
    gelbooru_api,
    search_opts: GelbooruSearchOptions | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    *,
    show_progress: bool = True,
    max_retries: int = DEFAULT_DOWNLOAD_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
    overwrite: bool = False,
):
    opts = search_opts or GelbooruSearchOptions()
    query = build_gelbooru_tag_query(tags, opts)
    if not query.strip():
        if _has_any_client_date_criteria(opts):
            print(
                "警告: 未提供 tags/服务端筛选，将按全站结果分页；"
                "已启用上传日筛选时会用固定 sort 尽量提前结束分页，但仍可能请求较多页。"
            )
            query = ""
        else:
            print("搜索条件为空：请提供 --tags 和/或筛选参数。")
            return

    list_opts, overridden_sort = _listing_opts_for_upload_date_pagination(opts)
    list_query = build_gelbooru_tag_query(tags, list_opts)
    date_paginate = _has_any_client_date_criteria(opts)
    if date_paginate and overridden_sort:
        forced = list_opts.sort or ""
        print(f"注意: 上传日期筛选下列表抓取固定为 sort:{forced}，已忽略 --sort {overridden_sort!r}。")

    all_posts: list[dict[str, Any]] = []
    page = 0
    while True:
        print(f"Fetching page {page} for query: {list_query!r}")
        raw = await gelbooru_api.getPosts(list_query, pid=page)
        posts = _normalize_posts(raw)
        if not posts:
            break
        if date_paginate:
            all_posts.extend(filter_posts_client_constraints(posts, opts))
            if _should_stop_date_pagination(posts, opts):
                print("列表分页提前结束: 后续页在 id–日期单调假设下不可能再命中日期条件。")
                break
        else:
            all_posts.extend(filter_posts_client_constraints(posts, opts))
        page += 1

    if date_paginate:
        print(
            f"按客户端条件筛选（分页流式，含上传日与 width/height/score）: 共 {len(all_posts)} 条"
        )
    elif _has_client_metadata_filter(opts):
        print(f"按 width/height/score/日期等客户端校验后: 共 {len(all_posts)} 条")

    if not all_posts:
        print(f"No posts found for query: {list_query!r}")
        return

    print(f"Found {len(all_posts)} posts in total. Starting download (concurrency={max_concurrent})...")
    await download_images(
        all_posts,
        output_dir,
        gelbooru_api,
        max_concurrent=max_concurrent,
        show_progress=show_progress,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        retry_max_delay=retry_max_delay,
        overwrite=overwrite,
    )


async def download_regularization_set(
    tags: str,
    output_dir,
    gelbooru_api,
    num_images,
    search_opts: GelbooruSearchOptions | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    *,
    show_progress: bool = True,
    max_retries: int = DEFAULT_DOWNLOAD_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
    overwrite: bool = False,
):
    import random

    opts = search_opts or GelbooruSearchOptions()
    list_opts, overridden_sort = _listing_opts_for_upload_date_pagination(opts)
    list_query = build_gelbooru_tag_query(tags, list_opts)
    date_paginate = _has_any_client_date_criteria(opts)
    if date_paginate and overridden_sort:
        forced = list_opts.sort or ""
        print(f"注意: 上传日期筛选下列表抓取固定为 sort:{forced}，已忽略 --sort {overridden_sort!r}。")

    collected_posts = []
    seen_ids = set()

    max_pages_to_check = (num_images // 100) + 5

    page = 0
    while len(collected_posts) < num_images and page < max_pages_to_check:
        if date_paginate:
            pid = page
            print(f"Fetching page {pid} for regularization (query={list_query!r})...")
            raw = await gelbooru_api.getPosts(list_query, pid=pid)
            posts = _normalize_posts(raw)
            if not posts:
                break
            if _should_stop_date_pagination(posts, opts):
                print("正则化集列表提前结束: 后续页在 id–日期单调假设下不可能再命中日期条件。")
                break
            posts = filter_posts_client_constraints(posts, opts)
        else:
            random_page = random.randint(0, 100)
            print(f"Fetching random page {random_page} for regularization images (query={list_query!r})...")
            raw = await gelbooru_api.getPosts(list_query, pid=random_page)
            posts = _normalize_posts(raw)
            if not posts:
                page += 1
                continue
            posts = filter_posts_client_constraints(posts, opts)

        random.shuffle(posts)
        for post in posts:
            if len(collected_posts) >= num_images:
                break
            post_id = post["id"]
            if post_id not in seen_ids:
                seen_ids.add(post_id)
                collected_posts.append(post)
        page += 1

    print(f"Collected {len(collected_posts)} posts for regularization. Starting download (concurrency={max_concurrent})...")
    await download_images(
        collected_posts,
        output_dir,
        gelbooru_api,
        max_concurrent=max_concurrent,
        show_progress=show_progress,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        retry_max_delay=retry_max_delay,
        overwrite=overwrite,
    )


def _parse_iso_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="从 Gelbooru 下载图片与标签；筛选条件会并入站内 tags 查询（与网页高级搜索一致）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
日期说明:
  Gelbooru 列表 API 对 date: 类 metatag 支持不稳定，故提供两类方式:
  --id-min / --id-max  用帖子 id 区间近似「越新 id 越大」;
  --created-after / --created-before  按 API 的 created_at（转 UTC 日）在本地过滤。
  启用上传日筛选时，列表会固定 sort:id:desc（含 --created-after）或 sort:id:asc（仅 --created-before），
  并在整页帖子都落在日期截止区外时提前结束分页，无需再拉后续页（依赖 id 与上传时间大致同向，极端乱序可能漏帖）。

元数据:
  每张成功处理的图会在输出目录下写入隐藏文件夹 .gelbooru_meta/<post_id>.json，
  内容为 Gelbooru dapi 单帖 JSON（下载时若重新拉图则与 id= 接口一致，仅跳过下载时用列表里的字段）。

示例:
  %(prog)s ./out --tags "1girl" --rating safe --min-score 10
  %(prog)s ./out --tags "character_name" --min-width 1024 --min-height 1024 --sort score:desc
  %(prog)s ./out --tags "1boy" --id-min 8000000 --extra-tags "-ai-generated"
  %(prog)s ./out --tags "architecture" --exclude-rating explicit
""",
    )
    p.add_argument("output_dir", type=str, help="保存图片与 .txt 标签的目录。")
    p.add_argument("--tags", type=str, default="", help="基础 tags（角色/作品等），可与下方筛选组合。")
    p.add_argument("--reg", action="store_true", help="下载正则化数据集。")
    p.add_argument("--reg_tags", type=str, default="", help="正则化集的基础 tags，默认空。")
    p.add_argument("--num_reg", type=int, default=150, help="正则化集目标张数。")
    p.add_argument(
        "-j",
        "--workers",
        type=int,
        default=DEFAULT_MAX_CONCURRENT,
        metavar="N",
        help=f"最大并发下载数（默认 {DEFAULT_MAX_CONCURRENT}）。",
    )
    p.add_argument(
        "--no-progress",
        action="store_true",
        help="关闭下载进度条（仍打印重试与错误信息）。",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_DOWNLOAD_RETRIES,
        metavar="N",
        help=f"单张图下载/API 失败时的最大尝试次数（默认 {DEFAULT_DOWNLOAD_RETRIES}）。",
    )
    p.add_argument(
        "--retry-base-delay",
        type=float,
        default=DEFAULT_RETRY_BASE_DELAY,
        metavar="SEC",
        help=f"重试退避初始间隔秒数（指数退避，默认 {DEFAULT_RETRY_BASE_DELAY}）。",
    )
    p.add_argument(
        "--retry-max-delay",
        type=float,
        default=DEFAULT_RETRY_MAX_DELAY,
        metavar="SEC",
        help=f"重试退避单次最长等待秒数（默认 {DEFAULT_RETRY_MAX_DELAY}）。",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="即使目标目录已有同 id 的图片与标签也重新下载（换分辨率/评分条件后若仍见旧图请用此项）。",
    )

    g = p.add_argument_group("Gelbooru 服务端筛选（写入搜索串）")
    g.add_argument(
        "--rating",
        choices=("general", "sensitive", "questionable", "explicit"),
        default=None,
        help="只保留该分级（rating:R）。排除某分级请用 --exclude-rating，勿写 --rating '-explicit'（负号会被当成新参数）。",
    )
    g.add_argument(
        "--exclude-rating",
        action="append",
        default=None,
        choices=("general", "sensitive", "questionable", "explicit"),
        metavar="R",
        help="可多次。写入 Gelbooru 的 -rating:R。例：--exclude-rating explicit",
    )
    g.add_argument("--min-score", type=int, default=None, metavar="N", help="score:>=N")
    g.add_argument("--max-score", type=int, default=None, metavar="N", help="score:<=N")
    g.add_argument("--min-width", type=int, default=None, metavar="N", help="width:>=N")
    g.add_argument("--max-width", type=int, default=None, metavar="N", help="width:<=N")
    g.add_argument("--min-height", type=int, default=None, metavar="N", help="height:>=N")
    g.add_argument("--max-height", type=int, default=None, metavar="N", help="height:<=N")
    g.add_argument(
        "--id-min",
        type=int,
        default=None,
        metavar="N",
        help="id:>=N（帖子 id 下限，常用来筛较新的图）。",
    )
    g.add_argument("--id-max", type=int, default=None, metavar="N", help="id:<=N")
    g.add_argument(
        "--sort",
        type=str,
        default=None,
        metavar="KEY[:asc|desc]",
        help="排序，如 score:desc、id:desc；若未含 sort: 前缀会自动补上。",
    )
    g.add_argument(
        "--uploaded-by",
        type=str,
        default=None,
        metavar="NAME",
        help="上传者 user:NAME（可写 bob 或完整 user:bob）。",
    )
    g.add_argument(
        "--extra-tags",
        action="append",
        default=None,
        metavar="FRAG",
        help="附加到查询串的一段（可多次）。如 --extra-tags \"pool:2\" --extra-tags \"-tagme\"",
    )

    d = p.add_argument_group("客户端上传日期筛选（按 created_at）")
    d.add_argument(
        "--created-after",
        type=_parse_iso_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="只保留上传日期 >= 该日（UTC）。启用后列表固定 sort:id:desc，并在可能时提前结束分页。",
    )
    d.add_argument(
        "--created-before",
        type=_parse_iso_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="只保留上传日期 <= 该日（UTC）。若未同时指定 --created-after，列表固定 sort:id:asc 并尽量提前结束分页。",
    )
    return p


async def main():
    parser = _build_arg_parser()
    args = parser.parse_args()
    opts = search_options_from_args(args)

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    gelbooru_api = Gelbooru(proxy="http://127.0.0.1:7890")

    dl_kw = dict(
        show_progress=not args.no_progress,
        max_retries=max(1, args.retries),
        retry_base_delay=max(0.0, args.retry_base_delay),
        retry_max_delay=max(0.0, args.retry_max_delay),
        overwrite=args.overwrite,
    )

    if args.reg:
        await download_regularization_set(
            args.reg_tags,
            args.output_dir,
            gelbooru_api,
            args.num_reg,
            search_opts=opts,
            max_concurrent=args.workers,
            **dl_kw,
        )
    else:
        if not args.tags.strip() and not _has_any_server_side_criteria(opts) and not _has_any_client_date_criteria(opts):
            parser.error("请使用 --tags 和/或筛选参数，或使用 --reg。")
        await download_dataset(
            args.tags,
            args.output_dir,
            gelbooru_api,
            search_opts=opts,
            max_concurrent=args.workers,
            **dl_kw,
        )


if __name__ == "__main__":
    asyncio.run(main())
