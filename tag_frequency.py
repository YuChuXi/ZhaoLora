#!/usr/bin/env python3
"""
统计数据集中旁路 .txt 里逗号分隔 tag 的出现频率（与 clean_dataset_tags 相同的拆行规则：
换行当作逗号、strip 空段）。

默认（按图片样本）：每个 .txt 视为一张图；同一 caption 内重复出现的同一 tag 只计 1 次。
  - 某 tag 的「次数」= 有多少个样本（文件）包含该 tag
  - 百分比 = 次数 / 有效样本数（读取成功的 .txt 数）

可选 --per-occurrence：改回按「tag 出现次数」累加，百分比分母为所有 caption 中 tag 总出现次数。

可选 HTML 反转义、下划线→空格、小写归一化，便于合并 long_hair / long hair 等写法。
"""

from __future__ import annotations

import argparse
import html
import sys
from collections import Counter
from pathlib import Path


def split_tags_line(s: str) -> list[str]:
    return [x.strip() for x in s.replace("\n", ",").split(",") if x.strip()]


def normalize_tag(tag: str, *, lower: bool, underscores: bool) -> str:
    t = tag.strip()
    if underscores:
        t = " ".join(t.replace("_", " ").split())
    if lower:
        t = t.lower()
    return t


def iter_txt_files(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        return sorted(root.rglob("*.txt"))
    return sorted(root.glob("*.txt"))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "data_dir",
        type=Path,
        help="含 .txt 的目录",
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        help="递归子目录",
    )
    p.add_argument(
        "--per-occurrence",
        action="store_true",
        help="不按样本去重：同一条 caption 里重复写同一 tag 会计多次；百分比相对 tag 总出现次数",
    )
    p.add_argument(
        "--html-unescape",
        action="store_true",
        help="先对整段 caption 做 html.unescape（与清洗脚本一致）",
    )
    p.add_argument(
        "--lower",
        action="store_true",
        help="统计时转小写（合并大小写）",
    )
    p.add_argument(
        "--underscores-to-space",
        action="store_true",
        help="tag 内下划线改空格并压空白（合并 long_hair 与 long hair）",
    )
    p.add_argument(
        "--top",
        type=int,
        default=50,
        metavar="N",
        help="终端只打印频率最高的前 N 条，0 表示全部（默认 50）",
    )
    p.add_argument(
        "--min-count",
        type=int,
        default=1,
        metavar="K",
        help="只输出出现次数 >= K 的 tag",
    )
    args = p.parse_args()

    root = args.data_dir.resolve()
    if not root.is_dir():
        print(f"错误：不是目录 {root}", file=sys.stderr)
        sys.exit(1)

    files = iter_txt_files(root, args.recursive)
    if not files:
        print(f"未找到 .txt（recursive={args.recursive}）: {root}")
        return

    counter: Counter[str] = Counter()
    total_tag_occurrences = 0
    failed = 0
    per_sample = not args.per_occurrence

    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"跳过读取失败 {path}: {e}", file=sys.stderr)
            failed += 1
            continue
        text = raw.strip()
        if args.html_unescape:
            text = html.unescape(text)
        tags = split_tags_line(text)
        normed = [
            normalize_tag(
                t,
                lower=args.lower,
                underscores=args.underscores_to_space,
            )
            for t in tags
        ]
        normed = [t for t in normed if t]
        if per_sample:
            normed = list(dict.fromkeys(normed))
        counter.update(normed)
        total_tag_occurrences += len(normed)

    rows = [(tag, c) for tag, c in counter.items() if c >= args.min_count]
    rows.sort(key=lambda x: (-x[1], x[0]))

    n_files = len(files) - failed
    n_distinct = len(rows)
    if per_sample:
        print(
            f"样本数（有效 .txt）: {n_files}（读取失败 {failed}） | "
            f"计数方式: 每样本内同 tag 计 1 次 | "
            f"不同 tag 数（过 min 阈值后）: {n_distinct}"
        )
    else:
        print(
            f"样本数（有效 .txt）: {n_files}（读取失败 {failed}） | "
            f"tag 出现总次数: {total_tag_occurrences} | "
            f"计数方式: 按出现次数累加 | "
            f"不同 tag 数（过 min 阈值后）: {n_distinct}"
        )
    if not rows:
        return

    limit = args.top if args.top > 0 else len(rows)
    denom = n_files if per_sample else total_tag_occurrences
    pct_label = "占样本" if per_sample else "占 tag 总次"
    print(f"\n前 {min(limit, len(rows))} 个 tag（按次数降序；% = {pct_label}）:\n")
    w = max(len(str(r[1])) for r in rows[:limit])
    for tag, c in rows[:limit]:
        pct = 100.0 * c / denom if denom else 0.0
        print(f"{c:>{w}}  ({pct:5.2f}%)  {tag}")


if __name__ == "__main__":
    main()
