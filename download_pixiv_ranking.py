#!/usr/bin/env python3
import argparse
import calendar
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from pixiv_search_download import (
    download_file,
    ensure_dir,
    get_illust_meta,
    load_cookies_netscape,
    make_session,
    require_cookie,
)


def parse_yyyymmdd(s: str) -> date:
    if len(s) != 8 or not s.isdecimal():
        raise ValueError(f"日期格式应为 YYYYMMDD，收到：{s}")
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def shift_month(d: date, months: int) -> date:
    m = d.month - months
    y = d.year + m // 12
    m = m % 12
    if m <= 0:
        m += 12
        y -= 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def default_ranking_end(mode: str, today: date | None = None) -> date:
    today = today or date.today()
    if mode == "monthly":
        return shift_month(today, 1)
    if mode == "weekly":
        return today - timedelta(days=today.weekday() + 7)
    return today - timedelta(days=1)


def generate_ranking_dates(mode: str, end: date, periods: int) -> list[str]:
    dates: list[str] = []
    if mode == "monthly":
        for i in range(periods):
            dates.append(shift_month(end, i).strftime("%Y%m%d"))
    elif mode == "weekly":
        cur = end - timedelta(days=end.weekday())
        for _ in range(periods):
            dates.append(cur.strftime("%Y%m%d"))
            cur -= timedelta(days=7)
    elif mode == "daily":
        for i in range(periods):
            dates.append((end - timedelta(days=i)).strftime("%Y%m%d"))
    else:
        dates.append(end.strftime("%Y%m%d"))
    return dates


def fetch_ranking(
    session: requests.Session,
    mode: str,
    content: str,
    page: int,
    rank_date: str | None = None,
) -> list[dict] | None:
    url = (
        f"https://www.pixiv.net/ranking.php"
        f"?mode={mode}&content={content}&format=json&p={page}"
    )
    if rank_date:
        url += f"&date={rank_date}"
    r = session.get(url, timeout=30)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        print(f"排行榜请求失败：{r.status_code} {url}", file=sys.stderr)
        sys.exit(3)
    data = r.json()
    return data.get("contents") or []


def collect_ranking_items(
    session: requests.Session,
    mode: str,
    content: str,
    dates: list[str],
    limit: int,
    page_sleep: float,
    per_period: int = 0,
) -> list[dict]:
    seen: set[int] = set()
    items: list[dict] = []
    for rank_date in dates:
        before = len(items)
        period_added = 0
        page = 1
        while len(items) < limit:
            if per_period > 0 and period_added >= per_period:
                break
            batch = fetch_ranking(session, mode, content, page, rank_date)
            if batch is None or not batch:
                break
            for it in batch:
                iid = int(it["illust_id"])
                if iid in seen:
                    continue
                seen.add(iid)
                items.append(it)
                period_added += 1
                if per_period > 0 and period_added >= per_period:
                    break
                if len(items) >= limit:
                    break
            page += 1
            time.sleep(page_sleep)
        print(f"date={rank_date} 新增 {len(items) - before}，累计 {len(items)} 个作品", flush=True)
        if per_period <= 0 and len(items) >= limit:
            break
    return items


def ext_from_url(url: str) -> str:
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\?|$)", url, re.I)
    return (m.group(1).lower() if m else "jpg").replace("jpeg", "jpg")


def main():
    ap = argparse.ArgumentParser(description="下载 Pixiv 排行榜作品原图")
    ap.add_argument("--cookies", required=True, help="Netscape cookies.txt 路径")
    ap.add_argument("--out", default="datasets/reg", help="输出目录")
    ap.add_argument("--mode", default="monthly", help="排行榜模式：daily/weekly/monthly/rookie")
    ap.add_argument("--content", default="illust", help="内容类型：illust/manga")
    ap.add_argument("--limit", type=int, default=150, help="最多下载作品数")
    ap.add_argument("--date", default="", help="单个排行榜日期 YYYYMMDD，不指定则按最近一年多个时间点抓取")
    ap.add_argument("--end-date", default="", help="多时间点范围的结束日期 YYYYMMDD，monthly 默认上个月")
    ap.add_argument("--periods", type=int, default=12, help="回溯时间点数量（monthly=月，weekly=周，daily=天）")
    ap.add_argument(
        "--per-period",
        type=int,
        default=-1,
        help="每个时间点最多新增作品数；-1 表示多时间点时自动按 limit/periods 均分，0 表示不限（凑够 limit 即停）",
    )
    ap.add_argument("--proxy", default="", help="HTTP 代理，例如 http://127.0.0.1:7890")
    ap.add_argument("--sleep", type=float, default=0.2, help="每张图下载后暂停秒数")
    args = ap.parse_args()

    cookies = load_cookies_netscape(args.cookies)
    require_cookie(cookies, "PHPSESSID")
    session = make_session(cookies)
    if args.proxy:
        session.proxies = {"http": args.proxy, "https": args.proxy}

    out_dir = Path(args.out)
    ensure_dir(out_dir)

    if args.date:
        dates = [args.date]
        per_period = 0
    else:
        end = parse_yyyymmdd(args.end_date) if args.end_date else default_ranking_end(args.mode)
        dates = generate_ranking_dates(args.mode, end, args.periods)
        if args.per_period < 0:
            per_period = max(1, (args.limit + len(dates) - 1) // len(dates))
        else:
            per_period = args.per_period

    print(f"将抓取 {len(dates)} 个时间点：{', '.join(dates)}", flush=True)
    if len(dates) > 1 and per_period > 0:
        print(f"每个时间点最多新增 {per_period} 个作品（共上限 {args.limit}）", flush=True)
    items = collect_ranking_items(
        session, args.mode, args.content, dates, args.limit, page_sleep=0.2, per_period=per_period
    )
    downloaded = 0
    skipped = 0

    for it in items:
        illust_id = int(it["illust_id"])
        meta = get_illust_meta(session, illust_id)
        if int(meta.get("illustType") or 0) == 2:
            print(f"跳过 {illust_id}（动图）", flush=True)
            skipped += 1
            continue

        page_count = int(meta.get("pageCount") or 1)
        urls = meta.get("urls") or {}
        original = urls.get("original")
        if not original:
            print(f"跳过 {illust_id}（无原图链接）", flush=True)
            skipped += 1
            continue

        referer = f"https://www.pixiv.net/artworks/{illust_id}"
        for p in range(page_count):
            url = original
            if page_count > 1:
                url = re.sub(r"_p0(\.[a-zA-Z0-9]+)$", rf"_p{p}\1", url)
            ext = ext_from_url(url)
            dst = out_dir / f"{illust_id}_p{p}.{ext}"
            before = dst.exists() and dst.stat().st_size > 0
            download_file(session, url, referer, dst, args.sleep)
            if before or (dst.exists() and dst.stat().st_size > 0):
                downloaded += 1
                print(f"{'跳过' if before else '保存'} {dst.name}", flush=True)

    print(f"完成：作品 {len(items)}，图片 {downloaded}，跳过 {skipped}", flush=True)


if __name__ == "__main__":
    main()
