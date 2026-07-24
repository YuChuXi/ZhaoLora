"""标注文件标签频率统计。"""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path


from services.models import TagStatRow, TagStatsResult, TxtDirInfo


def split_tags_line(s: str) -> list[str]:
    """按逗号拆分标签行。引用：``count_tags`` / ``tag_clean``。"""
    return [x.strip() for x in s.replace("\n", ",").replace("|||", ",").split(",") if x.strip()]


def normalize_tag(tag: str, *, lower: bool, underscores: bool) -> str:
    """规范化单个标签。引用：``count_tags``。"""
    t = tag.strip()
    if underscores:
        t = " ".join(t.replace("_", " ").split())
    if lower:
        t = t.lower()
    return t


def list_txt_subdirs(root: Path) -> list[TxtDirInfo]:
    """列出根下含 ``.txt`` 的一级子目录（``main`` 置顶）。

    引用:
        ``resolve_selected_dirs``；``routes.tag_stats``。
    """
    root = root.resolve()
    out: list[TxtDirInfo] = []
    if not root.is_dir():
        return out

    root_txt = sorted(root.glob("*.txt"))
    if root_txt:
        out.append(TxtDirInfo(name=".", label="(根目录)", txt_count=len(root_txt)))

    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        n = sum(1 for _ in p.glob("*.txt"))
        if n > 0 or p.name == "main":
            out.append(TxtDirInfo(name=p.name, label=p.name, txt_count=n))

    root_item = [d for d in out if d.name == "."]
    rest = [d for d in out if d.name != "."]
    main = [d for d in rest if d.name == "main"]
    others = [d for d in rest if d.name != "main"]
    if not main and (root / "main").is_dir():
        n = sum(1 for _ in (root / "main").glob("*.txt"))
        main = [TxtDirInfo(name="main", label="main", txt_count=n)]
    return root_item + main + others


def resolve_selected_dirs(root: Path, names: list[str] | None) -> list[Path]:
    """将选中的 txt 子目录名解析为路径；空则全选。

    引用:
        ``routes.tag_stats``。
    """
    root = root.resolve()
    available = {
        d.name: root if d.name == "." else root / d.name
        for d in list_txt_subdirs(root)
    }
    if not names:
        return [available[k] for k in available]
    dirs: list[Path] = []
    for name in names:
        name = (name or "").strip()
        if name not in available:
            continue
        dirs.append(available[name])
    return dirs


def load_translation_map(root: Path) -> dict[str, str]:
    """合并当前数据集及同级其它数据集下的翻译缓存。"""
    root = root.resolve()
    out: dict[str, str] = {}
    search_roots = [root]
    parent = root.parent
    if parent.is_dir():
        search_roots.append(parent)

    seen_files: set[Path] = set()
    for base in search_roots:
        for path in base.rglob(".guo582_translation_cache.json"):
            rp = path.resolve()
            if rp in seen_files:
                continue
            seen_files.add(rp)
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                continue
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str) and k and v:
                    out[k] = v
                    # 归一化键也挂一份，便于 long_hair / long hair
                    nk = " ".join(k.replace("_", " ").split()).lower()
                    if nk and nk not in out:
                        out[nk] = v
    return out


def lookup_zh(tag: str, zh_map: dict[str, str]) -> str:
    """查标签中文译名；支持下划线与空格键。引用：count_tags。"""
    if tag in zh_map:
        return zh_map[tag]
    nk = " ".join(tag.replace("_", " ").split()).lower()
    if nk in zh_map:
        return zh_map[nk]
    return ""


def count_tags(
    dirs: list[Path],
    *,
    zh_map: dict[str, str] | None = None,
    lower: bool = True,
    underscores: bool = True,
    html_unescape: bool = True,
    per_sample: bool = True,
    min_count: int = 1,
) -> TagStatsResult:
    """统计若干目录内旁路 ``.txt`` 的标签频率。

    引用:
        ``routes.tag_stats.tag_stats``。
    """
    zh_map = zh_map or {}
    counter: Counter[str] = Counter()
    total_tag_occurrences = 0
    failed = 0
    files: list[Path] = []

    for d in dirs:
        if not d.is_dir():
            continue
        files.extend(sorted(d.glob("*.txt")))

    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            failed += 1
            continue
        text = raw.strip()
        if html_unescape:
            text = html.unescape(text)
        tags = split_tags_line(text)
        normed = [
            normalize_tag(t, lower=lower, underscores=underscores)
            for t in tags
        ]
        normed = [t for t in normed if t]
        if per_sample:
            normed = list(dict.fromkeys(normed))
        counter.update(normed)
        total_tag_occurrences += len(normed)

    n_files = len(files) - failed
    denom = n_files if per_sample else total_tag_occurrences
    rows: list[TagStatRow] = []
    for tag, c in counter.items():
        if c < min_count:
            continue
        pct = 100.0 * c / denom if denom else 0.0
        zh = lookup_zh(tag, zh_map)
        rows.append(
            TagStatRow(
                tag=tag,
                zh=zh,
                count=c,
                pct=round(pct, 4),
            )
        )
    rows.sort(key=lambda x: (-x.pct, -x.count, x.tag))

    return TagStatsResult(
        sample_count=n_files,
        failed=failed,
        file_count=len(files),
        tag_count=len(rows),
        total_occurrences=total_tag_occurrences,
        per_sample=per_sample,
        tags=rows,
    )
