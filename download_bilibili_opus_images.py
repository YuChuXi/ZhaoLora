#!/usr/bin/env python3
"""下载 B 站用户全部图文动态 (opus) 的高清原图。
无 WBI / 无 Cookie / 无依赖 (仅 requests)。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


API_OPUS_LIST = "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/feed/space"
API_DETAIL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"

HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "referer": "https://space.bilibili.com/",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="下载 B 站用户全部图文动态高清原图")
    p.add_argument("host_mid", type=str, help="B 站用户 UID")
    p.add_argument("-o", "--output-dir", type=Path, required=True)
    p.add_argument("--sleep", type=float, default=0.15)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--overwrite", action="store_true")
    return p.parse_args()


# ---- 列表 ----

def fetch_opus_list(host_mid: str, timeout: float) -> list[dict]:
    items = []
    page = 0
    offset = ""
    while True:
        page += 1
        r = requests.get(API_OPUS_LIST, params={
            "host_mid": host_mid, "page": str(page), "offset": offset,
            "type": "all", "web_location": "333.1387",
        }, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"code={d.get('code')}, msg={d.get('message')}")
        payload = d["data"]
        page_items = payload.get("items") or []
        has_more = bool(payload.get("has_more"))
        new_offset = payload.get("offset") or ""
        items.extend(page_items)
        print(f"第 {page} 页: {len(page_items)} 条, has_more={has_more}, 累计 {len(items)} 条")
        if not has_more or not new_offset:
            break
        offset = new_offset
        time.sleep(0.15)
    return items


# ---- 详情 ----

def fetch_opus_detail(opus_id: str, timeout: float) -> list[tuple[str, int | None, int | None]]:
    """返回 [(url, width, height), ...]"""
    r = requests.get(API_DETAIL, params={"id": opus_id, "timezone_offset": "-480"},
                     headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"code={d.get('code')}")
    item = d.get("data", {}).get("item") or {}
    modules = item.get("modules", {})
    if not isinstance(modules, dict):
        return []
    dyn = modules.get("module_dynamic", {})
    if not isinstance(dyn, dict):
        return []
    major = dyn.get("major", {})
    if not isinstance(major, dict):
        return []
    pics = []
    opus = major.get("opus")
    if isinstance(opus, dict):
        for pic in (opus.get("pics") or []):
            u = pic.get("url")
            if u:
                pics.append((u, pic.get("width"), pic.get("height")))
    draw = major.get("draw")
    if isinstance(draw, dict):
        for pic in (draw.get("items") or []):
            u = pic.get("src")
            if u:
                pics.append((u, pic.get("width"), pic.get("height")))
    return pics


# ---- 下载辅助 ----

def norm(url: str) -> str:
    return "https:" + url if url.startswith("//") else url


def suffix(url: str, resp=None):
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return ext
    if resp:
        ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        return mimetypes.guess_extension(ct) or ".jpg"
    return ".jpg"


def alt_urls(url: str) -> list[str]:
    urls = [url]
    if url.startswith("http://"):
        urls.append("https://" + url[len("http://"):])
    elif url.startswith("https://"):
        urls.append("http://" + url[len("https://"):])
    return urls


# ---- 主流程 ----

def main() -> int:
    args = parse_args()
    out = args.output_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    # 阶段 1: 列表
    print("=== 拉取 opus 列表 ===")
    items = fetch_opus_list(args.host_mid, args.timeout)
    print(f"共 {len(items)} 条\n")

    # 阶段 2: 提取图片
    print("=== detail 获取全部图片 ===")
    records = []
    failures = 0
    total_pics = 0
    for idx, it in enumerate(items, start=1):
        oid = it.get("opus_id", "")
        content = (it.get("content") or "").strip()
        cover = it.get("cover", {})
        cover_url = norm(cover.get("url", "")) if isinstance(cover, dict) else ""
        stat = it.get("stat", {})

        try:
            pics = fetch_opus_detail(oid, args.timeout)
        except Exception as e:
            failures += 1
            if cover_url:
                pics = [(cover_url, cover.get("width"), cover.get("height"))]
                print(f"[{idx}/{len(items)}] 回退封面: {oid} ({e})", file=sys.stderr)
            else:
                print(f"[{idx}/{len(items)}] 失败: {oid} ({e})", file=sys.stderr)
                continue

        for pi, (url, w, h) in enumerate(pics, start=1):
            total_pics += 1
            records.append(dict(opus_id=oid, content=content,
                               image_url=norm(url), image_width=w, image_height=h,
                               image_index=pi, total_images=len(pics), stat=stat))

        if idx % 10 == 0 or idx == len(items):
            print(f"[{idx}/{len(items)}] 图 {total_pics}, fail {failures}")
        time.sleep(args.sleep)

    print(f"\n{len(items)} 条 → {len(records)} 张\n")

    # 保存
    (out / "bilibili_opus_records.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    # 阶段 3: 下载
    print("=== 下载图片 ===")
    url_map = {}
    downloaded = existed = failed = 0

    for idx, rec in enumerate(records, start=1):
        url = rec["image_url"]
        if url in url_map:
            rec["saved_as"] = url_map[url]
            rec["download_status"] = "deduplicated"
            continue
        try:
            dest = None
            fname = ""
            last_err = None
            for candidate in alt_urls(url):
                try:
                    resp = requests.get(candidate, stream=True, timeout=args.timeout, headers=HEADERS)
                    resp.raise_for_status()
                    ext = suffix(candidate, resp)
                    fname = f'{rec["opus_id"]}_{rec.get("image_index",1):02d}{ext}'
                    dest = out / fname
                    if dest.exists() and not args.overwrite:
                        status = "exists"; existed += 1
                    else:
                        tmp = dest.with_suffix(dest.suffix + ".part")
                        with tmp.open("wb") as f:
                            for chunk in resp.iter_content(131072):
                                if chunk: f.write(chunk)
                        tmp.replace(dest)
                        status = "downloaded"; downloaded += 1
                    rec["image_url_used"] = candidate
                    break
                except Exception as e:
                    last_err = e
            if not dest:
                raise last_err or RuntimeError("下载失败")
            url_map[url] = fname
            rec["saved_as"] = fname
            rec["download_status"] = status
            print(f"[{idx}/{len(records)}] {status}: {fname}")
        except Exception as e:
            failed += 1
            rec["download_status"] = f"error: {e}"
            print(f"[{idx}/{len(records)}] FAIL: {e}", file=sys.stderr)
        time.sleep(args.sleep)

    (out / "bilibili_opus_records.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = dict(total_opus=len(items), total_images=len(records),
                   unique_urls=len(url_map), downloaded=downloaded,
                   already_exists=existed, failed=failed,
                   generated_at=int(time.time()))
    (out / "bilibili_opus_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
