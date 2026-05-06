#!/usr/bin/env python3
"""
通过 Gelbooru API（与 download_dataset 相同客户端）拉取帖子，统计「满足某搜索条件时，
其它 tag 与之一同出现的频率」——语义对齐 dataset_tag_frequency 的「按样本（帖子）计次」：

- 默认：每个帖子里同一 tag 只计 1 次；某伴随 tag 的次数 = 有多少帖包含它（且满足 query）
- 百分比 = 次数 / 有效帖子数

查询串写法与站内一致（空格 AND，- 排除，rating: 等元标签）。

从统计中排除的标签：
  默认排除 query 里所有「普通 tag」（不含冒号、不以 - 开头的片段），避免主条件占满排行。
  可用 --keep-query-tags-in-stats 关闭；或用 --exclude-tag 追加要排除的展示项。

示例:
  %(prog)s "toriumi_arisu" --max-posts 500
  %(prog)s "rossi_(arknights) 1girl" --top 30 --lower
  %(prog)s "hapymaher" --no-proxy --max-posts 200
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from DanbooruApi.Gelbooru import Gelbooru  # noqa: E402


def split_tags_from_post_field(raw: str) -> list[str]:
    """Gelbooru post['tags'] 为空格分隔的 tag 串。"""
    if not raw:
        return []
    return [x.strip() for x in str(raw).split() if x.strip()]


def normalize_tag(
    tag: str, *, lower: bool, underscores: bool
) -> str:
    t = tag.strip()
    if underscores:
        t = " ".join(t.replace("_", " ").split())
    if lower:
        t = t.lower()
    return t


def tag_key(tag: str, *, lower: bool, underscores: bool) -> str:
    return normalize_tag(tag, lower=lower, underscores=underscores)


def query_parts_for_exclusion(query: str) -> list[str]:
    """从搜索串抽出应排除统计的「普通 tag」片段（去掉元标签）。"""
    out: list[str] = []
    for part in query.split():
        p = part.strip()
        if not p or ":" in p or p.startswith("-"):
            continue
        out.append(p)
    return out


class QuietGelbooru(Gelbooru):
    """避免基类 get() 对每个 URL print，刷屏。"""

    async def get(self, url: str) -> httpx.Response:
        async with httpx.AsyncClient(proxy=self.proxy) as client:
            return await client.get(
                url=url, timeout=self.timeout, headers=self.headers
            )


def _normalize_posts(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, dict):
        return [raw]
    return list(raw)


async def fetch_posts(
    api: Gelbooru,
    query: str,
    *,
    max_posts: int,
    page_delay_sec: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pid = 0
    while len(out) < max_posts:
        batch = _normalize_posts(await api.getPosts(query, pid=pid))
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        pid += 1
        if len(out) >= max_posts:
            break
        if page_delay_sec > 0:
            await asyncio.sleep(page_delay_sec)
    return out[:max_posts]


async def run_async(args: argparse.Namespace) -> int:
    api_kw: dict = {"timeout": max(10, args.timeout)}
    if args.no_proxy:
        api_kw["proxy"] = None
    elif args.proxy is not None:
        api_kw["proxy"] = args.proxy
    api = QuietGelbooru(**api_kw)

    query = args.query.strip()
    if not query:
        print("错误：query 为空", file=sys.stderr)
        return 1

    exclude_keys: set[str] = set()
    if not args.keep_query_tags_in_stats:
        for p in query_parts_for_exclusion(query):
            exclude_keys.add(
                tag_key(p, lower=args.lower, underscores=args.underscores_to_space)
            )
    for item in args.exclude_tag:
        for part in item.split(","):
            s = part.strip()
            if s:
                exclude_keys.add(
                    tag_key(s, lower=args.lower, underscores=args.underscores_to_space)
                )

    print(f"查询: {query!r}  |  最多拉取 {args.max_posts} 帖  |  proxy={api.proxy!r}")
    posts = await fetch_posts(
        api,
        query,
        max_posts=args.max_posts,
        page_delay_sec=args.page_delay,
    )
    n = len(posts)
    if not n:
        print("未拉到任何帖子（检查网络、代理、query 或账号权限）。")
        return 1

    counter: Counter[str] = Counter()
    display_spelling: dict[str, str] = {}
    per_sample = not args.per_occurrence

    for post in posts:
        tags_raw = split_tags_from_post_field(str(post.get("tags", "")))
        normed = [
            normalize_tag(
                t,
                lower=args.lower,
                underscores=args.underscores_to_space,
            )
            for t in tags_raw
        ]
        normed = [t for t in normed if t]
        if per_sample:
            # 按 key 去重，保留首次见到的展示拼写
            seen_k: set[str] = set()
            row: list[tuple[str, str]] = []
            for orig, z in zip(tags_raw, normed):
                k = tag_key(orig, lower=args.lower, underscores=args.underscores_to_space)
                if k in seen_k:
                    continue
                seen_k.add(k)
                row.append((k, z))
        else:
            row = [
                (
                    tag_key(
                        o,
                        lower=args.lower,
                        underscores=args.underscores_to_space,
                    ),
                    z,
                )
                for o, z in zip(tags_raw, normed)
            ]

        for k, disp in row:
            if k in exclude_keys:
                continue
            counter[k] += 1
            display_spelling.setdefault(k, disp)

    rows = [(display_spelling[k], c) for k, c in counter.items() if c >= args.min_count]
    rows.sort(key=lambda x: (-x[1], x[0].lower()))

    denom = n if per_sample else sum(counter.values())
    mode = "按帖子（样本）" if per_sample else "按 tag 出现次数"
    print(
        f"有效帖子: {n}  |  统计模式: {mode}  |  "
        f"排除统计用的 key 数: {len(exclude_keys)}  |  "
        f"不同伴随 tag 数（过 min 阈值）: {len(rows)}"
    )
    if exclude_keys and not args.keep_query_tags_in_stats:
        print(f"（已从排行中排除与 query 相同的普通 tag 片段）")

    if not rows:
        return 0

    limit = args.top if args.top > 0 else len(rows)
    pct_label = "占帖子" if per_sample else "占 tag 总次"
    print(f"\n前 {min(limit, len(rows))} 个伴随 tag（按次数降序；% = {pct_label}）:\n")
    w = max(len(str(r[1])) for r in rows[:limit])
    for disp, c in rows[:limit]:
        pct = 100.0 * c / denom if denom else 0.0
        print(f"{c:>{w}}  ({pct:5.2f}%)  {disp}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "query",
        type=str,
        help="Gelbooru 搜索串，与网页一致，如 toriumi_arisu 或 \"1girl rossi_(arknights)\"",
    )
    p.add_argument(
        "--max-posts",
        type=int,
        default=800,
        metavar="N",
        help="最多分析 N 帖（分页拉取，每页最多 100）",
    )
    p.add_argument(
        "--page-delay",
        type=float,
        default=0.35,
        metavar="SEC",
        help="分页请求间隔，略降限流风险",
    )
    p.add_argument(
        "--proxy",
        type=str,
        default=None,
        metavar="URL",
        help="覆盖默认代理；不设则用 DanbooruApi.Danbooru.__init__ 的默认 proxy",
    )
    p.add_argument(
        "--no-proxy",
        action="store_true",
        help="直连（proxy=None）",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="单次 HTTP 超时秒数",
    )
    p.add_argument(
        "--per-occurrence",
        action="store_true",
        help="不按帖子去重：同一帖里重复 tag 会计多次",
    )
    p.add_argument(
        "--lower",
        action="store_true",
        help="统计时小写合并",
    )
    p.add_argument(
        "--underscores-to-space",
        action="store_true",
        help="下划线改空格再合并计数",
    )
    p.add_argument(
        "--keep-query-tags-in-stats",
        action="store_true",
        help="不要把 query 里的普通 tag 从排行中排除",
    )
    p.add_argument(
        "--exclude-tag",
        action="append",
        default=[],
        metavar="T",
        help="从伴随统计中排除（可多次，逗号分隔）",
    )
    p.add_argument(
        "--top",
        type=int,
        default=50,
        metavar="N",
        help="终端打印前 N 条，0 为全部",
    )
    p.add_argument(
        "--min-count",
        type=int,
        default=1,
        metavar="K",
        help="只显示出现次数 >= K",
    )
    args = p.parse_args()
    if args.max_posts < 1:
        p.error("--max-posts 须 >= 1")
    raise SystemExit(asyncio.run(run_async(args)))


if __name__ == "__main__":
    main()
