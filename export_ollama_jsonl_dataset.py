#!/usr/bin/env python3
"""
将 Ollama 生成的 captions jsonl + 对应图片导出到单一目录，并生成多行 .txt 标注文件。

输入 jsonl 格式（来自 ollama_dataset_caption.py）每行示例：
  {"path": "/abs/to/img.png", "rel_path":"img.png", "model":"...", "caption": {...}}

导出结果：
  out_dir/
    123.png
    123.txt          # 多行文本（caption 展开）
    export.jsonl     # 路径已更新为导出目录内文件
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


KEY_ORDER = ("整体", "画风", "角色", "物品", "环境", "主观感受", "补充")


def _sanitize_rel_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    return name or "unnamed"


def _unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.stem
    suf = dest.suffix
    parent = dest.parent
    for i in range(1, 10000):
        cand = parent / f"{stem}__{i}{suf}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"无法为文件生成唯一名称: {dest}")


def _as_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        out: list[str] = []
        for it in value:
            ls = _as_lines(it)
            if not ls:
                continue
            if len(ls) == 1:
                out.append(f"- {ls[0]}")
            else:
                out.append(f"- {ls[0]}")
                out.extend(f"  {x}" for x in ls[1:])
        return out
    if isinstance(value, dict):
        return [json.dumps(value, ensure_ascii=False, indent=2)]
    return [str(value)]


def caption_to_multiline_txt(caption: Any) -> str:
    if not isinstance(caption, dict):
        return json.dumps(caption, ensure_ascii=False, indent=2) + "\n"
    lines: list[str] = []
    used: set[str] = set()
    for k in KEY_ORDER:
        if k not in caption:
            continue
        used.add(k)
        v = caption.get(k)
        ls = _as_lines(v)
        if not ls:
            continue
        lines.append(f"{k}:")
        lines.extend(ls)
        lines.append("")
    # 兜底：写入未知字段
    for k in sorted(caption.keys()):
        if k in used:
            continue
        ls = _as_lines(caption.get(k))
        if not ls:
            continue
        lines.append(f"{k}:")
        lines.extend(ls)
        lines.append("")
    # 确保以换行结束
    text = "\n".join(lines).rstrip() + "\n"
    return text


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="导出 ollama captions jsonl + 图片到单目录，并生成多行 txt")
    ap.add_argument("jsonl", type=Path, help="输入 jsonl（ollama_captions.jsonl）")
    ap.add_argument("out_dir", type=Path, help="导出目录（平铺）")
    ap.add_argument("--no-copy", action="store_true", help="不复制图片，只生成 txt 与 export.jsonl（要求源图片路径仍可用）")
    args = ap.parse_args(argv)

    jsonl_path = args.jsonl.expanduser().resolve()
    if not jsonl_path.is_file():
        raise SystemExit(f"jsonl 不存在: {jsonl_path}")

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    export_jsonl = out_dir / "export.jsonl"

    written = 0
    skipped = 0
    missing = 0

    with jsonl_path.open("r", encoding="utf-8") as f_in, export_jsonl.open("w", encoding="utf-8") as f_out:
        for idx, line in enumerate(f_in, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"第 {idx} 行 JSON 解析失败: {e}") from e

            src = rec.get("path")
            if not isinstance(src, str):
                raise SystemExit(f"第 {idx} 行缺少 path: {rec!r}")
            src_path = Path(src)
            if not src_path.is_file():
                missing += 1
                continue

            rel = rec.get("rel_path")
            if isinstance(rel, str) and rel.strip():
                base = _sanitize_rel_name(rel.strip())
            else:
                base = src_path.name

            dest_img = _unique_path(out_dir / base)
            dest_txt = dest_img.with_suffix(".txt")

            if dest_txt.exists() and dest_txt.stat().st_size > 0 and dest_img.exists() and dest_img.stat().st_size > 0:
                skipped += 1
                # 仍写 export.jsonl，保证导出的 jsonl 完整
            else:
                if not args.no_copy:
                    dest_img.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, dest_img)
                txt = caption_to_multiline_txt(rec.get("caption"))
                dest_txt.write_text(txt, encoding="utf-8")
                written += 1

            out_rec = dict(rec)
            out_rec["path"] = str(dest_img)
            out_rec["rel_path"] = dest_img.name
            f_out.write(json.dumps(out_rec, ensure_ascii=False) + "\n")

    print(
        f"完成: out={out_dir} | 写入/更新 {written} | 跳过已存在 {skipped} | 源图缺失 {missing}",
        flush=True,
    )


if __name__ == "__main__":
    main()

