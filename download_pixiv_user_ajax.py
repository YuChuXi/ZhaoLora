#!/usr/bin/env python3
"""通过 Pixiv 网页 AJAX 下载指定用户公开插画/漫画原图（无需 OAuth）。"""
import argparse
import json
import re
import time
from pathlib import Path

import requests

SESSION = requests.Session()


def ajax(path: str):
    url = f"https://www.pixiv.net{path}"
    url = url + ("&lang=en" if "?" in path else "?lang=en")
    r = SESSION.get(url, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data.get("message") or json.dumps(data, ensure_ascii=False)[:200])
    return data["body"]


def ext_from_url(u: str) -> str:
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\?|$)", u, re.I)
    ext = (m.group(1).lower() if m else "bin").replace("jpeg", "jpg")
    return ext


def download(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return False
    with SESSION.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(262144):
                f.write(chunk)
    return True


def collect_ids(body) -> list[int]:
    s: set[int] = set()
    for key in ("illusts", "manga"):
        block = body.get(key) or {}
        if isinstance(block, dict):
            s.update(int(k) for k in block.keys())
    return sorted(s, reverse=True)


def illust_original_urls(illust_id: int) -> list[str]:
    meta = ajax(f"/ajax/illust/{illust_id}")
    if meta.get("illustType") == 2:
        return []
    pc = int(meta.get("pageCount") or 1)
    if pc <= 1:
        u = (meta.get("urls") or {}).get("original")
        return [u] if u else []
    pages = ajax(f"/ajax/illust/{illust_id}/pages")
    out: list[str] = []
    for p in pages:
        u = (p.get("urls") or {}).get("original")
        if u:
            out.append(u)
    return out


def main():
    parser = argparse.ArgumentParser(description="通过 Pixiv 网页 AJAX 下载指定用户公开插画/漫画原图。")
    parser.add_argument("user_id", help="Pixiv 用户 ID")
    parser.add_argument("-o", "--output", help="输出目录 (默认: datasets/<user_id>)")
    parser.add_argument("-c", "--cookie", help="Cookie 字符串")
    args = parser.parse_args()

    user_id = args.user_id
    if args.output:
        out_dir = Path(args.output)
    else:
        out_dir = Path(__file__).resolve().parent / "datasets" / user_id

    SESSION.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.pixiv.net/",
        }
    )
    if args.cookie:
        SESSION.headers.update({"Cookie": args.cookie})

    profile = ajax(f"/ajax/user/{user_id}/profile/all")
    ids = collect_ids(profile)
    print(f"user {user_id}: {len(ids)} works", flush=True)
    new_files = 0
    for iid in ids:
        try:
            urls = illust_original_urls(iid)
        except Exception as e:
            print(f"error fetching {iid}: {e}", flush=True)
            continue

        if not urls:
            print(f"skip {iid} (ugoira or empty)", flush=True)
            time.sleep(0.12)
            continue
        for idx, url in enumerate(urls):
            ext = ext_from_url(url)
            dest = out_dir / f"{iid}_p{idx}.{ext}"
            if download(url, dest):
                print(f"saved {dest.relative_to(out_dir.parent)}", flush=True)
                new_files += 1
            else:
                print(f"skip existing {dest.name}", flush=True)
        time.sleep(0.15)
    print(f"done, new files: {new_files}", flush=True)


if __name__ == "__main__":
    main()
