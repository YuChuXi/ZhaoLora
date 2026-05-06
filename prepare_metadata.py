#!/usr/bin/env python3
"""
从「图片 + 同名 txt」目录生成 HuggingFace ImageFolder 用的 metadata.jsonl。
可通过 --config 指定多段子目录与 repeat 权重。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path("datasets/guo582_washed"),
        help="数据集根目录（内含图片与同名 .txt）",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出的 metadata.jsonl；默认 <data-dir>/metadata.jsonl",
    )
    p.add_argument(
        "--subdir",
        type=str,
        default="",
        help="相对 data-dir 的子目录（不设 config 时使用）",
    )
    p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="不设 config 时，每条样本在 jsonl 中重复写入次数（加权）",
    )
    p.add_argument(
        "--config",
        type=str,
        default=None,
        help='JSON 列表覆盖子目录配置，如 [{"subdir":"", "repeat":1}, {"subdir":"extra","repeat":3}]',
    )
    return p.parse_args()


def _load_datasets_config(args: argparse.Namespace) -> list[dict]:
    if args.config is not None:
        raw = json.loads(args.config)
        if not isinstance(raw, list):
            print("错误: --config 必须是 JSON 数组", file=sys.stderr)
            sys.exit(1)
        out = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict) or "subdir" not in item or "repeat" not in item:
                print(f"错误: config[{i}] 需含 subdir、repeat 字段", file=sys.stderr)
                sys.exit(1)
            out.append({"subdir": str(item["subdir"]), "repeat": int(item["repeat"])})
        return out
    if args.repeat < 1:
        print("错误: --repeat 须 >= 1", file=sys.stderr)
        sys.exit(1)
    return [{"subdir": args.subdir, "repeat": args.repeat}]


def main() -> None:
    args = _parse_args()
    base_dir = args.data_dir.resolve()
    output_file = (args.output or (base_dir / "metadata.jsonl")).resolve()

    if not base_dir.is_dir():
        print(f"错误: 找不到目录 {base_dir}", file=sys.stderr)
        sys.exit(1)

    datasets_config = _load_datasets_config(args)

    print(f"开始生成 {output_file}...")

    count = 0
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        for ds in datasets_config:
            subdir = ds["subdir"]
            repeat = ds["repeat"]
            if repeat < 1:
                print(f"警告: repeat={repeat} 无效，跳过条目 {ds}", file=sys.stderr)
                continue
            full_path = base_dir / subdir

            if not full_path.is_dir():
                print(f"错误: 找不到目录 {full_path}")
                continue

            image_files = [
                fn
                for fn in os.listdir(full_path)
                if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            ]

            print(f"处理目录: {full_path}, 找到 {len(image_files)} 张图片, 权重(repeat): {repeat}")

            for img_fn in sorted(image_files):
                base_name = os.path.splitext(img_fn)[0]
                txt_fn = base_name + ".txt"
                txt_path = full_path / txt_fn

                if txt_path.is_file():
                    caption = txt_path.read_text(encoding="utf-8").strip()
                    record = {
                        "file_name": os.path.join(subdir, img_fn) if subdir else img_fn,
                        "text": caption,
                    }
                    for _ in range(repeat):
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        count += 1
                else:
                    print(f"警告: 找不到标签文件 {txt_path}，跳过该图片。")

    print(f"成功生成 {output_file}，共写入 {count} 个样本条目。")


if __name__ == "__main__":
    main()
