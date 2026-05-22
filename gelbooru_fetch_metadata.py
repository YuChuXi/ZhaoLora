#!/usr/bin/env python3
"""
批量拉取 Gelbooru 帖子元数据（不下载图片）。

默认目标：拉取 10000 条「非 NSFW（rating:general）且 score>=10」的帖子元数据，
写入 JSONL（每行一个 JSON 对象）。

示例：
  python gelbooru_fetch_metadata.py
  python gelbooru_fetch_metadata.py --max-posts 2000 --query "rating:general score:>=10 1girl"
  python gelbooru_fetch_metadata.py --no-proxy --output output/gelbooru_meta.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import urllib.parse
import urllib.request
import urllib.error

_DEFAULT_PROXY = "http://127.0.0.1:7890"
# 复用仓库里 DanbooruApi/Gelbooru.py 的默认 key/user_id（可用参数覆盖）
_DEFAULT_API_KEY = "***REMOVED***"
_DEFAULT_USER_ID = "***REMOVED***"


def _build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def _http_get_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    timeout: int,
    headers: dict[str, str],
) -> dict[str, Any]:
    req = urllib.request.Request(url=url, headers=headers, method="GET")
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read()
    if not data:
        return {}
    return json.loads(data.decode("utf-8", errors="replace"))


def _fetch_posts_page(
    *,
    opener: urllib.request.OpenerDirector,
    query: str,
    pid: int,
    api_key: str,
    user_id: str,
    timeout: int,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    enc = urllib.parse.quote(query, safe="")
    url = (
        "https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1"
        f"&tags={enc}&pid={pid}&api_key={urllib.parse.quote(api_key, safe='')}"
        f"&user_id={urllib.parse.quote(user_id, safe='')}"
    )
    raw = _http_get_json(opener, url, timeout=timeout, headers=headers).get("post")
    return _normalize_posts(raw)


def _normalize_posts(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, dict):
        return [raw]
    return list(raw)


def _rating_is_general(raw: Any) -> bool:
    r = str(raw or "").strip().lower()
    return r in {"general", "g", "safe", "s"}


def _as_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except Exception:
        return default


def _post_to_record(post: dict[str, Any]) -> dict[str, Any]:
    tags_raw = str(post.get("tags", "")).strip()
    record: dict[str, Any] = {
        "id": _as_int(post.get("id")),
        "created_at": post.get("created_at"),
        "score": _as_int(post.get("score")),
        "rating": post.get("rating"),
        "md5": post.get("md5"),
        "file_url": post.get("file_url"),
        "sample_url": post.get("sample_url"),
        "preview_url": post.get("preview_url"),
        "source": post.get("source"),
        "width": _as_int(post.get("width")),
        "height": _as_int(post.get("height")),
        "sample_width": _as_int(post.get("sample_width")),
        "sample_height": _as_int(post.get("sample_height")),
        "file_ext": post.get("file_ext"),
        "tags": tags_raw,
        "tags_list": [t for t in tags_raw.split() if t],
    }
    return record


def _open_csv(path: Path, fieldnames: list[str]) -> tuple[csv.DictWriter, Any]:
    f = open(path, "w", encoding="utf-8", newline="")
    w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    return w, f


def _records_to_csv_rows(records: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for r in records:
        row = dict(r)
        if isinstance(row.get("tags_list"), list):
            row["tags_list"] = " ".join(row["tags_list"])
        return_row = row
        yield return_row


def fetch_metadata(
    *,
    opener: urllib.request.OpenerDirector,
    query: str,
    max_posts: int,
    page_delay_sec: float,
    min_score: int,
    only_general: bool,
    api_key: str,
    user_id: str,
    timeout: int,
    headers: dict[str, str],
    max_retries_per_page: int,
    retry_sleep_sec: float,
    max_consecutive_failures: int,
    output_jsonl: Path,
    output_csv: Path | None,
) -> int:
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    if output_csv is not None:
        output_csv.parent.mkdir(parents=True, exist_ok=True)

    seen_ids: set[int] = set()
    got = 0
    pid = 0
    consecutive_failures = 0

    csv_writer: csv.DictWriter | None = None
    csv_f = None
    if output_csv is not None:
        csv_fields = [
            "id",
            "created_at",
            "score",
            "rating",
            "md5",
            "file_url",
            "sample_url",
            "preview_url",
            "source",
            "width",
            "height",
            "sample_width",
            "sample_height",
            "file_ext",
            "tags",
            "tags_list",
        ]
        csv_writer, csv_f = _open_csv(output_csv, csv_fields)

    try:
        with open(output_jsonl, "w", encoding="utf-8") as jf:
            while got < max_posts:
                batch: list[dict[str, Any]] = []
                ok = False
                for attempt in range(1, max_retries_per_page + 1):
                    try:
                        batch = _fetch_posts_page(
                            opener=opener,
                            query=query,
                            pid=pid,
                            api_key=api_key,
                            user_id=user_id,
                            timeout=timeout,
                            headers=headers,
                        )
                        ok = True
                        break
                    except (
                        urllib.error.URLError,
                        urllib.error.HTTPError,
                        TimeoutError,
                        json.JSONDecodeError,
                    ) as e:
                        print(
                            f"第 {pid} 页请求失败（attempt {attempt}/{max_retries_per_page}）：{e}",
                            file=sys.stderr,
                        )
                        if attempt < max_retries_per_page and retry_sleep_sec > 0:
                            time.sleep(retry_sleep_sec)

                if not ok:
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        print(
                            f"连续失败 {consecutive_failures} 次，停止。",
                            file=sys.stderr,
                        )
                        break
                    # 放慢一点再试下一页（也可能是代理/网络短时波动）
                    if retry_sleep_sec > 0:
                        time.sleep(retry_sleep_sec)
                    pid += 1
                    continue

                consecutive_failures = 0
                if not batch:
                    break

                accepted: list[dict[str, Any]] = []
                for post in batch:
                    post_id = _as_int(post.get("id"), default=-1)
                    if post_id <= 0 or post_id in seen_ids:
                        continue
                    seen_ids.add(post_id)

                    if only_general and not _rating_is_general(post.get("rating")):
                        continue
                    if _as_int(post.get("score")) < min_score:
                        continue

                    accepted.append(_post_to_record(post))

                for rec in accepted:
                    jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                jf.flush()

                if csv_writer is not None:
                    for row in _records_to_csv_rows(accepted):
                        csv_writer.writerow(row)
                    assert csv_f is not None
                    csv_f.flush()

                got += len(accepted)
                if pid % 10 == 0:
                    print(f"进度：pid={pid}，已写入 {got}/{max_posts} 条（去重见到 {len(seen_ids)} 个 id）")
                pid += 1

                if len(batch) < 100:
                    break
                if got >= max_posts:
                    break
                if page_delay_sec > 0:
                    time.sleep(page_delay_sec)
    finally:
        if csv_f is not None:
            csv_f.close()

    print(f"完成：写入 {got} 条，去重后共见到 {len(seen_ids)} 个 id。输出：{output_jsonl}")
    if output_csv is not None:
        print(f"CSV：{output_csv}")
    if got < max_posts:
        print(
            f"注意：实际写入 {got} < 目标 {max_posts}，可能是 query 结果不足或被限流/网络问题。",
            file=sys.stderr,
        )
        return 2
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--query",
        type=str,
        default="rating:general score:>=10",
        help='Gelbooru 搜索串（默认 "rating:general score:>=10"）',
    )
    p.add_argument(
        "--max-posts",
        type=int,
        default=10000,
        help="最多写入多少条满足条件的记录（默认 10000）",
    )
    p.add_argument(
        "--min-score",
        type=int,
        default=20,
        help="最低 score（二次过滤，默认 10）",
    )
    p.add_argument(
        "--only-general",
        action="store_true",
        help="二次过滤 rating=general（默认开启于默认 query 逻辑，显式传也可）",
    )
    p.add_argument(
        "--page-delay",
        type=float,
        default=0.35,
        help="每页请求间隔秒数，降低限流风险（默认 0.35）",
    )
    p.add_argument(
        "--proxy",
        type=str,
        default=None,
        metavar="URL",
        help=f"覆盖默认代理；不设则用默认 {_DEFAULT_PROXY}",
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
        "--retries",
        type=int,
        default=3,
        help="每一页失败时最多重试次数（默认 3）",
    )
    p.add_argument(
        "--retry-sleep",
        type=float,
        default=1.2,
        help="失败重试间隔秒数（默认 1.2）",
    )
    p.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=5,
        help="连续失败达到该次数就停止（默认 5）",
    )
    p.add_argument(
        "--api-key",
        type=str,
        default=_DEFAULT_API_KEY,
        help="Gelbooru API key（默认使用仓库内置值）",
    )
    p.add_argument(
        "--user-id",
        type=str,
        default=_DEFAULT_USER_ID,
        help="Gelbooru user_id（默认使用仓库内置值）",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path("datasets/gelbooru_score10_general_meta.jsonl"),
        help="输出 JSONL 路径",
    )
    p.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="可选：同时输出 CSV 路径",
    )
    args = p.parse_args()

    if args.max_posts < 1:
        p.error("--max-posts 须 >= 1")
    if args.timeout < 5:
        p.error("--timeout 过小")
    if args.page_delay < 0:
        p.error("--page-delay 不能为负")
    if args.retries < 1:
        p.error("--retries 须 >= 1")
    if args.retry_sleep < 0:
        p.error("--retry-sleep 不能为负")
    if args.max_consecutive_failures < 1:
        p.error("--max-consecutive-failures 须 >= 1")

    proxy = None
    if args.no_proxy:
        proxy = None
    elif args.proxy is not None:
        proxy = args.proxy
    else:
        proxy = _DEFAULT_PROXY
    opener = _build_opener(proxy)

    q = str(args.query).strip()
    if not q:
        p.error("--query 不能为空")

    only_general = bool(args.only_general) or ("rating:general" in q.replace(" ", ""))

    headers = {
        "accept": "application/json,text/plain,*/*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "user-agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
    }

    raise SystemExit(
        fetch_metadata(
            opener=opener,
            query=q,
            max_posts=int(args.max_posts),
            page_delay_sec=float(args.page_delay),
            min_score=int(args.min_score),
            only_general=only_general,
            api_key=str(args.api_key),
            user_id=str(args.user_id),
            timeout=max(10, int(args.timeout)),
            headers=headers,
            max_retries_per_page=int(args.retries),
            retry_sleep_sec=float(args.retry_sleep),
            max_consecutive_failures=int(args.max_consecutive_failures),
            output_jsonl=args.output.resolve(),
            output_csv=(args.output_csv.resolve() if args.output_csv else None),
        )
    )


if __name__ == "__main__":
    main()

