#!/usr/bin/env python3
"""
清洗数据集旁路 txt 中的 tag 行（逗号分隔）：

- HTML/XML 反转义：&lt; &gt; &amp; &quot; &#123; 等
- 每个 tag 内：下划线替换为空格并折叠连续空白（如 long_hair → long hair）
- 删除整词（大小写不敏感）：内置列表 + --drop / --drop-file
- 删除任一 tag 若其小写形式包含子串：内置 commentary、request + --drop-contains
- --promote：把**已存在**的 tag 按给定顺序移到「正文块」最前（不存在的不会新增）；同 key 在文中多处时只保留最前一条写法，其余去掉
- --prepend：按顺序插在整条 caption 最前（比 --promote 更靠前）；若正文中已有同名 tag 会先去掉以免重复

处理顺序：先删 tag → --promote → --prepend。

默认原地覆盖；可用 --out_dir 写到另一目录并保持相对路径。
"""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path


def split_tags_line(s: str) -> list[str]:
    return [x.strip() for x in s.replace("\n", ",").split(",") if x.strip()]


def join_tags_line(tags: list[str]) -> str:
    return ", ".join(tags)


DROP_EXACT = frozenset(
    {
        "highres",
        "absurdres",
        "arknights: endfield",
        "arknights",
        "original",
        "virtual youtuber"
    }
)


def normalize_tag_underscores(tag: str) -> str:
    """Danbooru 风格下划线词转为空格，便于与 CLIP/SDXL 习惯一致。"""
    return " ".join(tag.replace("_", " ").split())


def tag_key(tag: str) -> str:
    return normalize_tag_underscores(tag).strip().lower()


def expand_cli_list(items: list[str]) -> list[str]:
    """支持多次 --flag a --flag b,c，逗号分隔合并为列表。"""
    out: list[str] = []
    for it in items:
        for part in it.split(","):
            p = part.strip()
            if p:
                out.append(p)
    return out


def load_drop_file(path: Path) -> list[str]:
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            lines.append(s)
    return lines


def should_drop_tag(
    tag: str,
    *,
    user_drop_exact: frozenset[str],
    user_drop_contains: tuple[str, ...],
) -> bool:
    low = tag.strip().lower()
    if not low:
        return True
    key = tag_key(tag)
    if key in DROP_EXACT or key in user_drop_exact:
        return True
    if "commentary" in low:
        return True
    if "request" in low:
        return True
    for sub in user_drop_contains:
        if sub and sub in low:
            return True
    return False


def promote_keys_ordered(promote_tags: tuple[str, ...]) -> list[str]:
    """用户给出的 promote 列表 → tag_key 序列，去重且保持首次出现顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for t in promote_tags:
        if not t.strip():
            continue
        k = tag_key(t)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def apply_promote_tags(kept: list[str], promote_order_keys: list[str]) -> list[str]:
    """将 promote_order_keys 中在 kept 里出现过的 tag 整段移到 kept 最前，其余保持原相对顺序。"""
    if not promote_order_keys:
        return kept

    promote_set = frozenset(promote_order_keys)
    first_text: dict[str, str] = {}
    for t in kept:
        k = tag_key(t)
        if k in promote_set and k not in first_text:
            first_text[k] = t

    promoted = [first_text[k] for k in promote_order_keys if k in first_text]
    remaining = [t for t in kept if tag_key(t) not in promote_set]
    return promoted + remaining


def clean_caption(
    raw: str,
    *,
    user_drop_exact: frozenset[str] | None = None,
    user_drop_contains: tuple[str, ...] = (),
    promote_tags: tuple[str, ...] = (),
    prepend_tags: tuple[str, ...] = (),
) -> str:
    s = html.unescape(raw.strip())
    tags = [normalize_tag_underscores(t) for t in split_tags_line(s)]
    exact = user_drop_exact or frozenset()
    kept = [
        t
        for t in tags
        if not should_drop_tag(t, user_drop_exact=exact, user_drop_contains=user_drop_contains)
    ]

    promote_keys = promote_keys_ordered(promote_tags)
    kept = apply_promote_tags(kept, promote_keys)

    prep = [normalize_tag_underscores(t) for t in prepend_tags if t.strip()]
    if prep:
        prep_keys = {tag_key(t) for t in prep}
        kept = [t for t in kept if tag_key(t) not in prep_keys]
        kept = prep + kept

    return join_tags_line(kept)


def iter_txt_files(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        return sorted(root.rglob("*.txt"))
    return sorted(root.glob("*.txt"))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "data_dir",
        type=Path,
        help="含 .txt 标签文件的目录（与图片同名的 foo.txt）",
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        help="递归子目录中的 .txt",
    )
    p.add_argument(
        "--out_dir",
        type=Path,
        default=None,
        help="输出根目录；不设则原地覆盖",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="只打印将修改的文件与前后摘要，不写盘",
    )
    p.add_argument(
        "--drop",
        action="append",
        default=[],
        metavar="TAG",
        help="额外按整词删除（可多次；可与逗号连用）。匹配前会做下划线→空格与大小写忽略",
    )
    p.add_argument(
        "--drop-file",
        type=Path,
        default=None,
        help="每行一个要整词删除的 tag（# 开头行为注释）",
    )
    p.add_argument(
        "--drop-contains",
        action="append",
        default=[],
        metavar="SUBSTR",
        help="删除小写形式包含该子串的 tag（可多次）",
    )
    p.add_argument(
        "--promote",
        action="append",
        default=[],
        metavar="TAG",
        help="将已存在的 tag 按顺序移到正文最前（可多次；逗号分隔）。文中没有的不会添加；重复只保留一条",
    )
    p.add_argument(
        "--prepend",
        action="append",
        default=[],
        metavar="TAG",
        help="按顺序插在整条 caption 最前（在 --promote 之后执行，因此更靠左）。正文里同名 tag 会先去掉以免重复",
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

    user_drop_exact = frozenset(
        tag_key(t) for t in expand_cli_list(args.drop) + drop_from_file
    )
    user_drop_contains = tuple(
        s.strip().lower()
        for s in expand_cli_list(args.drop_contains)
        if s.strip()
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
