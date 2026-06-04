#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import json
import mimetypes
import re
import sys
import time
from pathlib import Path
from typing import Any
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urlparse

import requests


# 使用非桌面版 API 端点，该端点对历史动态的覆盖更全
API_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
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
    parser.add_argument(
        "--download-forwarded",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否下载转发动态中的图片，默认下载；使用 --no-download-forwarded 跳过",
    )
    parser.add_argument(
        "--type",
        type=str,
        default=None,
        dest="dynamic_type",
        help=(
            "按动态类型过滤，多个类型用逗号分隔。"
            "可选值: all(全部), draw(图片), forward(转发), av(视频), article(专栏), audio(音频)。"
            "默认不过滤，获取全部类型"
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="最大拉取页数，0 表示不限制，默认 0",
    )
    parser.add_argument(
        "--fallback-endpoint",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="当标准端点返回空时，是否回退到桌面端点重试，默认开启",
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


DESKTOP_API_URL = "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/feed/space"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# WBI 签名混排表（固定，B站官方算法）
_WBI_MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 52, 44, 34,
]

# B 站动态类型常量（数值）
DYNAMIC_TYPE_DRAW = 2       # 图片动态
DYNAMIC_TYPE_FORWARD = 4    # 转发动态
DYNAMIC_TYPE_AV = 8         # 视频动态
DYNAMIC_TYPE_ARTICLE = 64   # 专栏
DYNAMIC_TYPE_AUDIO = 256    # 音频


# ---- WBI 签名 ----

_wbi_key_cache: tuple[str, float] | None = None  # (mixin_key, fetched_at)


def _fetch_wbi_keys(session: requests.Session, timeout: float) -> str:
    """从 nav 接口获取并计算 WBI 混排密钥，缓存 1 小时。"""
    global _wbi_key_cache
    now = time.time()
    if _wbi_key_cache and (now - _wbi_key_cache[1]) < 3600:
        return _wbi_key_cache[0]

    resp = session.get(NAV_URL, timeout=timeout)
    resp.raise_for_status()
    nav_data = resp.json()
    # nav 接口即使返回 code != 0（如 -101 未登录）也可能包含 wbi_img
    wbi_img = nav_data.get("data", {}).get("wbi_img", {})
    if not wbi_img:
        raise RuntimeError(f"nav 接口未返回 wbi_img: {nav_data}")

    img_key = (wbi_img.get("img_url", "") or "").split("/")[-1].split(".")[0][:32]
    sub_key = (wbi_img.get("sub_url", "") or "").split("/")[-1].split(".")[0][:32]
    if len(img_key) < 32 or len(sub_key) < 32:
        raise RuntimeError(f"WBI 密钥长度不足: img_key={len(img_key)}, sub_key={len(sub_key)}")

    combined = img_key + sub_key
    mixin = "".join(combined[idx] for idx in _WBI_MIXIN_TABLE if idx < len(combined))
    _wbi_key_cache = (mixin, now)
    return mixin


def _sign_params(params: dict[str, str], mixin_key: str) -> dict[str, str]:
    """对参数字典添加 wts 和 w_rid 签名，返回新字典。"""
    signed = dict(params)
    signed["wts"] = str(int(time.time()))
    # 按 key 排序后拼接
    ordered = sorted(signed.items(), key=lambda kv: kv[0])
    raw = urlencode(ordered) + mixin_key
    signed["w_rid"] = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return signed

# ---- 动态类型过滤 ----


def parse_dynamic_type_filter(raw: str | None) -> set[int] | None:
    """将用户输入的动态类型字符串解析为类型 ID 集合，None 表示不过滤。"""
    if not raw:
        return None
    type_map = {
        "all": None,  # all 特殊处理，表示不过滤
        "draw": DYNAMIC_TYPE_DRAW,
        "forward": DYNAMIC_TYPE_FORWARD,
        "av": DYNAMIC_TYPE_AV,
        "article": DYNAMIC_TYPE_ARTICLE,
        "audio": DYNAMIC_TYPE_AUDIO,
    }
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if part == "all":
            return None  # all 表示不过滤
        tid = type_map.get(part)
        if tid is None:
            raise ValueError(f"未知的动态类型: {part!r}，可选值: {', '.join(type_map.keys())}")
        result.add(tid)
    return result if result else None


def fetch_page(
    session: requests.Session,
    host_mid: str,
    offset: str | None,
    timeout: float,
    max_retries: int,
    sleep_seconds: float,
    *,
    use_desktop_endpoint: bool = False,
    wbi_mixin_key: str | None = None,
) -> dict[str, Any]:
    api_url = DESKTOP_API_URL if use_desktop_endpoint else API_URL
    params: dict[str, str] = {
        "host_mid": host_mid,
        "timezone_offset": str(DEFAULT_TIMEZONE_OFFSET),
    }
    if offset:
        params["offset"] = offset

    # 标准端点需要 WBI 签名，桌面端点不需要
    if not use_desktop_endpoint and wbi_mixin_key:
        params = _sign_params(params, wbi_mixin_key)

    last_error: Exception | None = None
    attempts = max(1, max_retries)
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(api_url, params=params, timeout=timeout)
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
    modules = item.get("modules")
    # 新版 API：modules 是一个 dict，key 为模块名
    if isinstance(modules, dict):
        return modules.get(module_name)
    # 兼容旧格式：modules 是 list
    if isinstance(modules, list):
        for module in modules:
            if isinstance(module, dict) and module_name in module:
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


def _get_draw_items(dynamic: dict[str, Any]) -> list[dict[str, Any]] | None:
    """从 module_dynamic 中提取图片列表，优先从 major.draw 获取，回退到 dyn_draw。"""
    # 新版 API 将图片放在 major.draw.items 中
    major = dynamic.get("major")
    if isinstance(major, dict):
        draw = major.get("draw")
        if isinstance(draw, dict):
            draw_items = draw.get("items")
            if draw_items:
                return draw_items
    # 旧版或兼容格式：dyn_draw.items
    dyn_draw = dynamic.get("dyn_draw")
    if isinstance(dyn_draw, dict):
        draw_items = dyn_draw.get("items")
        if draw_items:
            return draw_items
    return None


def extract_draw_records(item: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    dynamic = find_module(item, "module_dynamic")
    author = find_module(item, "module_author") or {}
    desc = find_module(item, "module_desc") or {}
    if not dynamic:
        return []

    draw_items = _get_draw_items(dynamic)
    if not draw_items:
        return []

    user = author.get("user") or {}
    records: list[dict[str, Any]] = []
    for index, image in enumerate(draw_items, start=1):
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


def _extract_forwarded_items(forward_data: dict[str, Any], forwarded_by_id: str | None, forwarded_by_type: str | None, include_forwarded: bool = True) -> list[dict[str, Any]]:
    """递归提取转发链中的所有图片记录。"""
    original_item = forward_data.get("item")
    if not isinstance(original_item, dict):
        return []

    records: list[dict[str, Any]] = []
    for nested in extract_draw_records(original_item, "forwarded"):
        nested["forwarded_by_dynamic_id"] = forwarded_by_id
        nested["forwarded_by_dynamic_type"] = forwarded_by_type
        records.append(nested)

    if include_forwarded:
        # 递归处理嵌套转发
        nested_dynamic = find_module(original_item, "module_dynamic")
        if nested_dynamic:
            nested_forward = nested_dynamic.get("dyn_forward")
            if isinstance(nested_forward, dict):
                records.extend(
                    _extract_forwarded_items(
                        nested_forward,
                        forwarded_by_id=original_item.get("id_str"),
                        forwarded_by_type=original_item.get("type"),
                        include_forwarded=True,
                    )
                )
    return records


def extract_records(item: dict[str, Any], *, include_forwarded: bool = True) -> list[dict[str, Any]]:
    records = extract_draw_records(item, "self")
    if not include_forwarded:
        return records

    dynamic = find_module(item, "module_dynamic")
    if not dynamic:
        return records

    forward = dynamic.get("dyn_forward")
    if not isinstance(forward, dict):
        return records

    records.extend(
        _extract_forwarded_items(
            forward,
            forwarded_by_id=item.get("id_str"),
            forwarded_by_type=item.get("type"),
            include_forwarded=True,
        )
    )
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

    # 解析动态类型过滤
    type_filter = parse_dynamic_type_filter(args.dynamic_type)

    # 预取 WBI 签名密钥（标准端点需要）
    wbi_mixin_key: str | None = None
    try:
        wbi_mixin_key = _fetch_wbi_keys(session, args.timeout)
        print("WBI 签名密钥已就绪")
    except Exception as exc:
        print(f"获取 WBI 签名密钥失败 ({exc})，标准端点页面可能受限", file=sys.stderr)

    all_items: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    offset: str | None = None
    page = 0
    consecutive_empty_pages = 0
    max_consecutive_empty = 3
    use_desktop_fallback = False  # 先用标准端点

    while True:
        if args.max_pages > 0 and page >= args.max_pages:
            print(f"已达到最大页数限制 {args.max_pages}，停止拉取")
            break

        page += 1
        try:
            data = fetch_page(
                session,
                args.host_mid,
                offset,
                args.timeout,
                args.max_retries,
                args.sleep,
                use_desktop_endpoint=use_desktop_fallback,
                wbi_mixin_key=wbi_mixin_key,
            )
        except Exception as exc:
            if args.fallback_endpoint and not use_desktop_fallback:
                print(f"标准端点请求失败 ({exc})，尝试回退到桌面端点...", file=sys.stderr)
                use_desktop_fallback = True
                page -= 1  # 不计入页数，重试当前页
                time.sleep(args.sleep)
                continue
            else:
                print(f"请求失败: {exc}", file=sys.stderr)
                break

        items = data.get("items") or []

        # 按类型过滤
        if type_filter is not None and items:
            before = len(items)
            items = [it for it in items if it.get("type") in type_filter]
            if len(items) < before:
                print(f"  类型过滤: {before} -> {len(items)} 条")

        all_items.extend(items)

        page_records = []
        for item in items:
            page_records.extend(extract_records(item, include_forwarded=args.download_forwarded))
        all_records.extend(page_records)

        endpoint_label = "桌面端点" if use_desktop_fallback else "标准端点"
        print(
            f"第 {page} 页 ({endpoint_label}): 动态 {len(items)} 条, 提取图片记录 {len(page_records)} 条, "
            f"累计动态 {len(all_items)} 条, 累计记录 {len(all_records)} 条"
        )

        has_more = bool(data.get("has_more"))
        new_offset = data.get("offset")

        # 空页面处理
        if not items:
            consecutive_empty_pages += 1
            if consecutive_empty_pages >= max_consecutive_empty:
                print(f"连续 {consecutive_empty_pages} 页为空，停止拉取")
                break

            if args.fallback_endpoint and not use_desktop_fallback:
                print("标准端点返回空页，尝试回退到桌面端点...")
                use_desktop_fallback = True
                page -= 1  # 重试
                time.sleep(args.sleep)
                continue
        else:
            consecutive_empty_pages = 0

        # has_more 为 false 但还有 offset 时，可能是 API 的假阴性，尝试继续
        if not has_more:
            if new_offset:
                print("API 返回 has_more=false 但提供了 offset，继续尝试拉取下一页...")
                offset = new_offset
                time.sleep(args.sleep)
                continue
            elif consecutive_empty_pages >= max_consecutive_empty:
                break
            else:
                # 尝试用桌面端点再拉一轮
                if args.fallback_endpoint and not use_desktop_fallback:
                    print("标准端点返回 has_more=false，尝试切换到桌面端点继续拉取...")
                    use_desktop_fallback = True
                    time.sleep(args.sleep)
                    continue
                else:
                    break

        if not new_offset:
            print("API 未返回 offset，停止拉取")
            break

        offset = new_offset
        time.sleep(args.sleep)

    if not cookie_string:
        print(
            "\n⚠ 警告: 当前未携带 B 站登录 Cookie。该接口在未登录状态下通常只能拿到较新的公开动态，"
            "较早历史动态可能缺失。如需尽量拉全，请通过 --cookie 或 --cookie-file 传入登录 Cookie。",
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
        "download_forwarded": args.download_forwarded,
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
