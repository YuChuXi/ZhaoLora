#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import json
import mimetypes
import re
import sys
import time
from pathlib import Path
from typing import Any
from http.cookies import SimpleCookie
from urllib.parse import urlparse

import requests


API_URL = "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/feed/space"
DEFAULT_TIMEZONE_OFFSET = -480
DEFAULT_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "referer": "https://space.bilibili.com/",
    "accept": "application/json, text/plain, */*",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="下载 B 站用户动态及转发动态中的原图。")
    parser.add_argument("host_mid", type=str, help="B 站用户 UID")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        required=True,
        help="输出目录，例如 datasets/kuriko",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.2,
        help="每次请求/下载后的休眠秒数，默认 0.2",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP 超时时间（秒），默认 30",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="若目标文件已存在则重新下载",
    )
    parser.add_argument(
        "--cookie",
        type=str,
        default=os.environ.get("BILIBILI_COOKIE", ""),
        help="B 站 Cookie 字符串；也可通过环境变量 BILIBILI_COOKIE 传入",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        help="从文件读取 B 站 Cookie 字符串",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="接口返回空页或临时失败时的最大重试次数，默认 3",
    )
    return parser.parse_args()


def load_cookie_string(args: argparse.Namespace) -> str:
    if args.cookie_file:
        return args.cookie_file.expanduser().read_text(encoding="utf-8").strip()
    return (args.cookie or "").strip()


def apply_cookie_string(session: requests.Session, cookie_string: str) -> None:
    if not cookie_string:
        return

    # 如果是 Netscape 格式，使用标准库 MozillaCookieJar 解析
    if cookie_string.startswith("# Netscape"):
        import tempfile
        from http.cookiejar import MozillaCookieJar
        with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8") as tf:
            tf.write(cookie_string)
            temp_path = tf.name
        try:
            cj = MozillaCookieJar(temp_path)
            cj.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(cj)
        finally:
            os.remove(temp_path)
        return

    # 否则按常规 key=value 格式解析
    cookie = SimpleCookie()
    cookie.load(cookie_string)
    for morsel in cookie.values():
        session.cookies.set(morsel.key, morsel.value, domain=".bilibili.com")


def build_session(host_mid: str, cookie_string: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.headers["referer"] = f"https://space.bilibili.com/{host_mid}/dynamic"
    apply_cookie_string(session, cookie_string)
    return session


def fetch_page(
    session: requests.Session,
    host_mid: str,
    offset: str | None,
    timeout: float,
    max_retries: int,
    sleep_seconds: float,
) -> dict[str, Any]:
    params = {
        "host_mid": host_mid,
        "timezone_offset": str(DEFAULT_TIMEZONE_OFFSET),
    }
    if offset:
        params["offset"] = offset

    last_error: Exception | None = None
    attempts = max(1, max_retries)
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(API_URL, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0:
                raise RuntimeError(f"API 返回异常: {data}")

            payload = data["data"]
            items = payload.get("items") or []
            should_retry_empty_page = offset is not None and not items
            if should_retry_empty_page and attempt < attempts:
                time.sleep(max(sleep_seconds, 0.5) * attempt)
                continue
            return payload
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(max(sleep_seconds, 0.5) * attempt)

    raise last_error or RuntimeError("请求失败，未知错误")


def find_module(item: dict[str, Any], module_name: str) -> dict[str, Any] | None:
    for module in item.get("modules", []):
        if module_name in module:
            return module[module_name]
    return None


def normalize_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


def safe_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def extract_draw_records(item: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    dynamic = find_module(item, "module_dynamic")
    author = find_module(item, "module_author") or {}
    desc = find_module(item, "module_desc") or {}
    if not dynamic:
        return []

    dyn_draw = dynamic.get("dyn_draw")
    if not dyn_draw:
        return []

    user = author.get("user") or {}
    records: list[dict[str, Any]] = []
    for index, image in enumerate(dyn_draw.get("items", []), start=1):
        src = image.get("src")
        if not src:
            continue
        records.append(
            {
                "scope": scope,
                "source_dynamic_id": item.get("id_str"),
                "source_dynamic_type": item.get("type"),
                "author_mid": user.get("mid"),
                "author_name": user.get("name"),
                "pub_ts": author.get("pub_ts"),
                "pub_text": author.get("pub_text"),
                "text": safe_text(desc.get("text")),
                "image_index": index,
                "image_width": image.get("width"),
                "image_height": image.get("height"),
                "image_url": normalize_url(src),
            }
        )
    return records


def extract_records(item: dict[str, Any]) -> list[dict[str, Any]]:
    records = extract_draw_records(item, "self")
    dynamic = find_module(item, "module_dynamic")
    if not dynamic:
        return records

    forward = dynamic.get("dyn_forward")
    if not forward:
        return records

    original_item = forward.get("item")
    if not isinstance(original_item, dict):
        return records

    for nested in extract_draw_records(original_item, "forwarded"):
        nested["forwarded_by_dynamic_id"] = item.get("id_str")
        nested["forwarded_by_dynamic_type"] = item.get("type")
        records.append(nested)
    return records


def detect_suffix(url: str, response: requests.Response) -> str:
    path_suffix = Path(urlparse(url).path).suffix.lower()
    if path_suffix:
        return path_suffix
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    guessed = mimetypes.guess_extension(content_type) or ".bin"
    return guessed


def candidate_urls(url: str) -> list[str]:
    urls = [url]
    if url.startswith("http://"):
        urls.append("https://" + url[len("http://") :])
    elif url.startswith("https://"):
        urls.append("http://" + url[len("https://") :])
    return urls


def make_filename(record: dict[str, Any], suffix: str) -> str:
    scope = record["scope"]
    dynamic_id = record["source_dynamic_id"] or "unknown"
    image_index = int(record["image_index"])
    if scope == "forwarded":
        forwarded_by = record.get("forwarded_by_dynamic_id") or "unknown"
        return f"{forwarded_by}_forward_{dynamic_id}_{image_index:02d}{suffix}"
    return f"{dynamic_id}_{image_index:02d}{suffix}"


def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    timeout: float,
    overwrite: bool,
) -> str:
    if dest.exists() and not overwrite:
        return "exists"

    response = session.get(url, stream=True, timeout=timeout)
    response.raise_for_status()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with tmp.open("wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 128):
            if chunk:
                f.write(chunk)
    tmp.replace(dest)
    return "downloaded"


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    cookie_string = load_cookie_string(args)
    session = build_session(args.host_mid, cookie_string)

    all_items: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    offset: str | None = None
    page = 0

    while True:
        page += 1
        data = fetch_page(
            session,
            args.host_mid,
            offset,
            args.timeout,
            args.max_retries,
            args.sleep,
        )
        items = data.get("items") or []
        all_items.extend(items)
        page_records = []
        for item in items:
            page_records.extend(extract_records(item))
        all_records.extend(page_records)
        print(
            f"第 {page} 页: 动态 {len(items)} 条, 提取图片记录 {len(page_records)} 条, "
            f"累计记录 {len(all_records)} 条"
        )

        has_more = bool(data.get("has_more"))
        offset = data.get("offset")
        if not has_more or not offset:
            break
        time.sleep(args.sleep)

    if not cookie_string:
        print(
            "警告: 当前未携带 B 站登录 Cookie。该接口在未登录状态下通常只能拿到较新的公开动态，"
            "较早历史动态可能缺失；如需尽量拉全，请加 --cookie 或 --cookie-file。",
            file=sys.stderr,
        )

    metadata_path = output_dir / "bilibili_dynamic_records.json"
    metadata_path.write_text(json.dumps(all_records, ensure_ascii=False, indent=2), encoding="utf-8")

    url_to_dest: dict[str, str] = {}
    downloaded = 0
    existed = 0
    failed = 0

    for idx, record in enumerate(all_records, start=1):
        url = record["image_url"]
        if url in url_to_dest:
            record["saved_as"] = url_to_dest[url]
            record["download_status"] = "deduplicated"
            continue

        try:
            last_error: Exception | None = None
            dest: Path | None = None
            rel_name = ""
            status = ""

            for candidate in candidate_urls(url):
                try:
                    response = session.get(candidate, stream=True, timeout=args.timeout)
                    response.raise_for_status()
                    suffix = detect_suffix(candidate, response)
                    filename = make_filename(record, suffix)
                    dest = output_dir / filename
                    status = "exists" if dest.exists() and not args.overwrite else "downloaded"
                    if status == "downloaded":
                        tmp = dest.with_suffix(dest.suffix + ".part")
                        with tmp.open("wb") as f:
                            for chunk in response.iter_content(chunk_size=1024 * 128):
                                if chunk:
                                    f.write(chunk)
                        tmp.replace(dest)
                        downloaded += 1
                    else:
                        existed += 1
                    rel_name = dest.name
                    record["image_url_used"] = candidate
                    break
                except Exception as exc:
                    last_error = exc

            if not dest or not rel_name:
                raise last_error or RuntimeError("下载失败，未知错误")

            url_to_dest[url] = rel_name
            record["saved_as"] = rel_name
            record["download_status"] = status
            print(f"[{idx}/{len(all_records)}] {status}: {rel_name}")
        except Exception as exc:
            failed += 1
            record["download_status"] = f"error: {exc}"
            print(f"[{idx}/{len(all_records)}] error: {url} -> {exc}", file=sys.stderr)
        finally:
            time.sleep(args.sleep)

    metadata_path.write_text(json.dumps(all_records, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "host_mid": args.host_mid,
        "total_dynamics": len(all_items),
        "total_image_records": len(all_records),
        "unique_images": len(url_to_dest),
        "downloaded": downloaded,
        "already_exists": existed,
        "failed": failed,
        "generated_at": int(time.time()),
    }
    (output_dir / "bilibili_dynamic_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
