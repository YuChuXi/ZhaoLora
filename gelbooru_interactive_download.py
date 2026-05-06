#!/usr/bin/env python3
"""
交互式从 Gelbooru 按帖子 id 或帖子页 URL 下载原图与标签到指定目录。

支持输入示例:
  11668926
  https://gelbooru.com/index.php?page=post&s=view&id=11668926&tags=crucifixion

标签写入 <id>.txt（空格 tag → 逗号分隔，与 download_dataset 一致），
元数据写入 .gelbooru_meta/<id>.json。

空行退出交互循环。也可用 --id 非交互下载一条。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import traceback
import urllib.parse
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from DanbooruApi.Gelbooru import Gelbooru  # noqa: E402

GELBOORU_META_SUBDIR = ".gelbooru_meta"

# 与 download_dataset 一致，并允许 webp/gif（魔数校验在 Gelbooru.getImage 内）
_ALLOWED_EXT = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})


def parse_gelbooru_post_id(text: str) -> int | None:
    """从纯数字或 Gelbooru 帖子页 URL 解析 id。"""
    t = text.strip()
    if not t:
        return None
    if re.fullmatch(r"\d+", t):
        return int(t)
    low = t.lower()
    if "gelbooru.com" in low or "page=post" in low or "s=view" in low:
        if not t.startswith(("http://", "https://")):
            t = "https://" + t.lstrip("/")
        parsed = urllib.parse.urlparse(t)
        qs = urllib.parse.parse_qs(parsed.query)
        if "id" in qs and qs["id"]:
            try:
                return int(str(qs["id"][0]).strip())
            except ValueError:
                pass
        m = re.search(r"[?&]id=(\d+)", t, re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"/post/view/(\d+)", parsed.path, re.I)
        if m:
            return int(m.group(1))
    return None


def _write_meta(output_dir: Path, post: dict[str, Any]) -> None:
    meta_dir = output_dir / GELBOORU_META_SUBDIR
    meta_dir.mkdir(parents=True, exist_ok=True)
    pid = post.get("id", "unknown")
    (meta_dir / f"{pid}.json").write_text(
        json.dumps(post, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def download_post_by_id(
    api: Gelbooru,
    post_id: int,
    output_dir: Path,
    *,
    overwrite: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    post = await api.getPost(post_id)
    image_url = str(post.get("file_url", ""))
    if not image_url:
        print(f"帖子 {post_id} 无 file_url", file=sys.stderr)
        return
    ext = os.path.splitext(urllib.parse.urlparse(image_url).path)[1].lower()
    if not ext:
        ext = ".jpg"
    if ext not in _ALLOWED_EXT:
        print(
            f"跳过非预期扩展名 {ext!r}（仅 {sorted(_ALLOWED_EXT)}）: {image_url}",
            file=sys.stderr,
        )
        return

    pid = int(post.get("id", post_id))
    image_path = output_dir / f"{pid}{ext}"
    tags_path = output_dir / f"{pid}.txt"

    if (
        not overwrite
        and image_path.is_file()
        and tags_path.is_file()
    ):
        print(f"已存在，跳过（加 --overwrite 可覆盖）: {image_path.name}")
        _write_meta(output_dir, post)
        return

    post_out: dict[str, Any] = post
    if overwrite or not image_path.is_file():
        post_out = await api.getImage(pid, str(image_path))
    tags_final = str(post_out.get("tags", "")).strip()
    tags_path.write_text(
        tags_final.replace(" ", ", ") + ("\n" if tags_final else ""),
        encoding="utf-8",
    )
    _write_meta(output_dir, post_out)
    print(f"完成: {image_path.name} + {tags_path.name}")


def _prompt_loop(output_dir: Path, api: Gelbooru, overwrite: bool) -> None:
    print(f"输出目录: {output_dir.resolve()}")
    print("输入 Gelbooru 帖子数字 id 或完整帖子页 URL，空行退出。")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not line:
            print("再见。")
            break
        pid = parse_gelbooru_post_id(line)
        if pid is None:
            print("无法解析帖子 id，请贴 gelbooru.com 的 view 链接或纯数字。")
            continue
        try:
            asyncio.run(download_post_by_id(api, pid, output_dir, overwrite=overwrite))
        except Exception:
            traceback.print_exc()


async def _cli_batch(
    api: Gelbooru,
    ids: list[int],
    output_dir: Path,
    overwrite: bool,
) -> int:
    ok = 0
    for pid in ids:
        try:
            await download_post_by_id(api, pid, output_dir, overwrite=overwrite)
            ok += 1
        except Exception:
            traceback.print_exc()
    return 0 if ok == len(ids) else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        default=Path("datasets/gelbooru_grab"),
        help="保存图片与 txt 的目录（默认 datasets/gelbooru_grab）",
    )
    p.add_argument(
        "--id",
        type=str,
        action="append",
        default=[],
        metavar="ID_OR_URL",
        help="非交互：下载指定 id 或 URL（可多次）",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP 超时（秒）",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已存在的同 id 图片与 txt",
    )
    args = p.parse_args()

    out = args.output_dir.resolve()
    api = Gelbooru()

    if args.id:
        parsed: list[int] = []
        for raw in args.id:
            pid = parse_gelbooru_post_id(raw)
            if pid is None:
                print(f"无法解析: {raw!r}", file=sys.stderr)
                raise SystemExit(1)
            parsed.append(pid)
        raise SystemExit(
            asyncio.run(_cli_batch(api, parsed, out, args.overwrite))
        )

    _prompt_loop(out, api, args.overwrite)


if __name__ == "__main__":
    main()
