#!/usr/bin/env python3
import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests


NETSCAPE_FIELDS = 7


def _parse_netscape_cookie_line(line: str):
    # domain \t include_subdomains \t path \t secure \t expires \t name \t value
    parts = line.rstrip("\n").split("\t")
    if len(parts) != NETSCAPE_FIELDS:
        return None
    domain, include_subdomains, path, secure, expires, name, value = parts
    if not name:
        return None
    return {
        "domain": domain.lstrip("#HttpOnly_"),
        "path": path,
        "secure": secure.upper() == "TRUE",
        "name": name,
        "value": value,
    }


def load_cookies_netscape(path: str) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip("\n")
            if not line:
                continue
            if line.startswith("#") and (not line.startswith("#HttpOnly_")):
                continue
            parsed = _parse_netscape_cookie_line(line)
            if not parsed:
                continue
            jar.set(
                parsed["name"],
                parsed["value"],
                domain=parsed["domain"],
                path=parsed["path"],
                secure=parsed["secure"],
            )
    return jar


def require_cookie(jar: requests.cookies.RequestsCookieJar, name: str):
    if jar.get(name) is None:
        print(f"缺少 Cookie `{name}`，请确认你导出的 cookies.txt 包含该字段喵~", file=sys.stderr)
        sys.exit(2)


def make_session(cookie_jar: requests.cookies.RequestsCookieJar) -> requests.Session:
    s = requests.Session()
    s.cookies = cookie_jar
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.pixiv.net/",
            "Origin": "https://www.pixiv.net",
        }
    )
    return s


@dataclass(frozen=True)
class Illust:
    illust_id: int
    title: str
    page_count: int
    thumb_url: str


def pixiv_ajax_get(session: requests.Session, url: str) -> dict:
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        print(f"请求失败：{r.status_code} {url}喵~", file=sys.stderr)
        sys.exit(3)
    data = r.json()
    if not isinstance(data, dict) or not data.get("error") is False:
        print(f"接口返回异常：{url}喵~", file=sys.stderr)
        print(str(data)[:2000], file=sys.stderr)
        sys.exit(3)
    return data


def search_artworks(session: requests.Session, query: str, page: int, s_mode: str, order: str, ai_type: int | None = None) -> list[Illust]:
    # Pixiv Web Ajax endpoint for searching artworks by keyword/tag.
    # ai_type: 1=AI only, 2=exclude AI, None=all (let Pixiv server filter)
    q_path = quote(query, safe="")
    q_param = quote(query, safe="")
    url = (
        f"https://www.pixiv.net/ajax/search/artworks/{q_path}"
        f"?word={q_param}&order={quote(order, safe='')}&mode=all&p={page}"
        f"&s_mode={quote(s_mode, safe='')}&type=all&lang=zh"
    )
    if ai_type is not None:
        url += f"&aiType={ai_type}"
    data = pixiv_ajax_get(session, url)
    body = data.get("body") or {}
    illust_manga = body.get("illustManga") or {}
    items = illust_manga.get("data") or []
    out: list[Illust] = []
    for it in items:
        illust_id = it.get("id")
        if not illust_id:
            continue
        out.append(Illust(
            illust_id=int(illust_id),
            title=str(it.get("title") or ""),
            page_count=int(it.get("pageCount") or 1),
            thumb_url=str(it.get("url") or ""),
        ))
    return out


def get_illust_meta(session: requests.Session, illust_id: int) -> dict:
    url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
    return pixiv_ajax_get(session, url).get("body") or {}


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def safe_filename(s: str) -> str:
    s2 = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s2 = re.sub(r"\s+", " ", s2).strip()
    return s2[:180] if len(s2) > 180 else s2


def download_file(session: requests.Session, url: str, referer: str, dst: Path, sleep_s: float) -> bool:
    if dst.exists() and dst.stat().st_size > 0:
        return True
    headers = {"Referer": referer}
    r = session.get(url, headers=headers, stream=True, timeout=60)
    if r.status_code != 200:
        print(f"  ✗ HTTP {r.status_code} {url}", file=sys.stderr)
        return False
    tmp = dst.with_suffix(dst.suffix + ".part")
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            f.write(chunk)
    os.replace(tmp, dst)
    if sleep_s > 0:
        time.sleep(sleep_s)
    return True


def main():
    ap = argparse.ArgumentParser(description="按 Pixiv 关键词搜索下载作品（可过滤非AI）喵~")
    ap.add_argument("--cookies", required=True, help="Netscape cookies.txt 路径喵~")
    ap.add_argument("--query", required=True, help="搜索关键词，例如：云路翼喵~")
    ap.add_argument("--out", default="pixiv_search_out", help="输出目录喵~")
    ap.add_argument("--s-mode", default="tag_tc", choices=["tag", "tag_tc", "s_tag", "s_tag_full", "s_tc", "s_tc_full", "all"], help="搜索模式（一般用 tag_tc）喵~")
    ap.add_argument("--order", default="date_d", choices=["date_d", "date", "popular_d", "popular_male_d", "popular_female_d"], help="排序喵~")
    ap.add_argument("--hide-ai", action="store_true", help="隐藏AI作品喵~")
    ap.add_argument("--start-page", type=int, default=1, help="起始搜索页码喵~")
    ap.add_argument("--max-pages", type=int, default=0, help="搜索页数，0 表示不限喵~")
    ap.add_argument("--per-illust", type=int, default=0, help="每个作品最多下载页数，0 表示不限喵~")
    ap.add_argument("--pages", default="", help="指定下载的页码（逗号分隔，如 0,1,3），默认全部喵~")
    ap.add_argument("--sleep", type=float, default=0.2, help="每张图下载后暂停秒数，避免太快喵~")
    args = ap.parse_args()

    cookies = load_cookies_netscape(args.cookies)
    require_cookie(cookies, "PHPSESSID")
    session = make_session(cookies)

    out_dir = Path(args.out)
    ensure_dir(out_dir)

    ai_type_param = 1 if args.hide_ai else None
    ai_label = "隐藏AI" if args.hide_ai else "全部"

    print(f"开始搜索「{args.query}」({ai_label})，每页60件，输出到 {out_dir}")
    sys.stdout.flush()

    seen: set[int] = set()
    page = args.start_page
    total_downloaded = 0
    total_items = 0
    end_page = args.start_page + args.max_pages - 1 if args.max_pages > 0 else 0

    while True:
        if end_page and page > end_page:
            break
        print(f"正在获取第 {page} 页...", end=" ")
        sys.stdout.flush()
        items = search_artworks(session, args.query, page, args.s_mode, args.order, ai_type_param)
        print(f"{len(items)} 件")
        sys.stdout.flush()
        if not items:
            break
        total_items += len(items)

        for it in items:
            if it.illust_id in seen:
                continue
            seen.add(it.illust_id)

            title = safe_filename(it.title)
            page_count = it.page_count
            if args.per_illust > 0:
                page_count = min(page_count, args.per_illust)

            # 确定要下载的页码
            if args.pages:
                wanted = [int(x.strip()) for x in args.pages.split(",") if x.strip()]
                wanted = [p for p in wanted if p < page_count]
            else:
                wanted = list(range(page_count))

            # 已有足够文件则跳过，避免重复调用元数据API
            if sum(1 for _ in out_dir.glob(f"{it.illust_id}_*")) >= len(wanted):
                total_downloaded += len(wanted)
                continue

            meta = get_illust_meta(session, it.illust_id)
            urls = meta.get("urls") or {}
            original = urls.get("original")
            if not original:
                continue

            referer = f"https://www.pixiv.net/artworks/{it.illust_id}"
            for p in wanted:
                url = original
                if page_count > 1:
                    url = re.sub(r"_p0(\.[a-zA-Z0-9]+)$", rf"_p{p}\1", url)
                ext = Path(url).suffix
                name = f"{it.illust_id}"
                if title:
                    name += f"_{title}"
                if page_count > 1:
                    name += f"_p{p}"
                dst = out_dir / f"{name}{ext}"
                if dst.exists() and dst.stat().st_size > 0:
                    print(f"  [{total_downloaded + 1}] {name}{ext} (跳过)")
                elif download_file(session, url, referer, dst, args.sleep):
                    print(f"  [{total_downloaded + 1}] {name}{ext} ✓")
                total_downloaded += 1
                sys.stdout.flush()

        print(
            f"── 页 {page} 完成：作品 {len(seen)}，图片 {total_downloaded} ──"
        )
        page += 1
        time.sleep(0.3)

    print(
        f"完成：抓取作品 {len(seen)}（含重复去重），搜索项 {total_items}，下载图片 {total_downloaded}喵~"
    )


if __name__ == "__main__":
    main()

