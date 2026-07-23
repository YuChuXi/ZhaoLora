#!/usr/bin/env python3
"""
清洗数据集旁路 txt 中的 tag 行（逗号分隔）。

逻辑实现见 web_annotator/services/tag_clean.py。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "web_annotator"))

from services.tag_clean import (  # noqa: E402
    clean_caption,
    expand_cli_list,
    tag_key,
)


def load_drop_file(path: Path) -> list[str]:
    """读取每行一个 tag 的删除列表（# 为注释）。"""
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            lines.append(s)
    return lines


def iter_txt_files(root: Path, recursive: bool) -> list[Path]:
    """列举目录内 txt。"""
    if recursive:
        return sorted(root.rglob("*.txt"))
    return sorted(root.glob("*.txt"))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("data_dir", type=Path, help="含 .txt 标签文件的目录")
    p.add_argument("--recursive", action="store_true", help="递归子目录中的 .txt")
    p.add_argument("--out_dir", type=Path, default=None, help="输出根目录；不设则原地覆盖")
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="只打印将修改的文件，不写文件")
    p.add_argument("--drop", action="append", default=[], metavar="TAG", help="整词删除")
    p.add_argument("--drop-file", type=Path, default=None, help="每行一个要整词删除的 tag")
    p.add_argument("--drop-contains", action="append", default=[], metavar="SUBSTR", help="子串删除")
    p.add_argument("--promote", action="append", default=[], metavar="TAG", help="已存在 tag 提升到最前")
    p.add_argument("--prepend", action="append", default=[], metavar="TAG", help="插到 caption 最前")
    p.add_argument(
        "--promote-separator",
        action="store_true",
        dest="promote_separator",
        help="用 ||| 分隔 promote/prepend 与其余 tag",
    )
    args = p.parse_args()
    root: Path = args.data_dir.resolve()
    if not root.is_dir():
        print(f"错误：不是目录 {root}", file=sys.stderr)
        sys.exit(1)

    out_root = args.out_dir.resolve() if args.out_dir else None
    if out_root:
        out_root.mkdir(parents=True, exist_ok=True)

    drop_from_file: list[str] = []
    if args.drop_file is not None:
        df = args.drop_file.resolve()
        if not df.is_file():
            print(f"错误：--drop-file 不是文件 {df}", file=sys.stderr)
            sys.exit(1)
        drop_from_file = load_drop_file(df)

    user_drop_exact = frozenset(tag_key(t) for t in expand_cli_list(args.drop) + drop_from_file)
    user_drop_contains = tuple(
        s.strip().lower() for s in expand_cli_list(args.drop_contains) if s.strip()
    )
    promote_tags = tuple(expand_cli_list(args.promote))
    prepend_tags = tuple(expand_cli_list(args.prepend))

    files = iter_txt_files(root, args.recursive)
    if not files:
        print(f"未找到 .txt（recursive={args.recursive}）: {root}")
        return

    changed = 0
    unchanged = 0
    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"跳过读取失败 {path}: {e}", file=sys.stderr)
            continue
        new = clean_caption(
            raw,
            user_drop_exact=user_drop_exact,
            user_drop_contains=user_drop_contains,
            promote_tags=promote_tags,
            prepend_tags=prepend_tags,
            use_promote_separator=args.promote_separator,
        )
        if new == raw.strip():
            unchanged += 1
            continue
        changed += 1
        rel = path.relative_to(root)
        if args.dry_run:
            old_one = raw.strip().replace("\n", " ")[:200]
            new_one = new[:200]
            print(f"[dry-run] {rel}")
            print(f"  - {old_one}")
            print(f"  + {new_one}")
            continue
        dest = path
        if out_root is not None:
            dest = out_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(new + "\n", encoding="utf-8")

    print(f"完成：变更 {changed}，未改 {unchanged}，共 {len(files)} 个 .txt")


if __name__ == "__main__":
    main()
