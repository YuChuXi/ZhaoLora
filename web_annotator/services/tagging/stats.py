"""标注文件标签频率统计. """

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

import httpx

from services.core.work_dir import list_work_dirs
from services.models import TagStatRow, TagStatsResult

_SHARED_CACHE_NAME = ".translation_cache.json"
_BATCH_SIZE = 128


def split_tags_line(s: str) -> list[str]:
    """按逗号拆分标签行. 

    Callers:
        - ``count_tags``
        - ``services/tagging/clean.py``
    """
    return [x.strip() for x in s.replace("\n", ",").replace("|||", ",").split(",") if x.strip()]


def normalize_tag(tag: str, *, lower: bool, underscores: bool) -> str:
    """规范化单个标签. 

    Callers:
        - ``count_tags``
    """
    t = tag.strip()
    if underscores:
        t = " ".join(t.replace("_", " ").split())
    if lower:
        t = t.lower()
    return t


def resolve_selected_dirs(root: Path, names: list[str] | None) -> list[Path]:
    """将选中的工作子目录名解析为路径；空则全选.

    引用:
        ``routes/tag_stats.py``.
    """
    root = root.resolve()
    available = {d.name: root / d.name for d in list_work_dirs(root) if d.name != "."}
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
    """
    加载共享翻译缓存 ``datasets/.translation_cache.json``. 

    引用: POST /tags/stats、POST /tags/translate-missing. 
    """
    cache_path = root.resolve().parent / _SHARED_CACHE_NAME
    if not cache_path.is_file():
        return {}
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str) and k and v:
            out[k] = v
            nk = " ".join(k.replace("_", " ").split()).lower()
            if nk and nk not in out:
                out[nk] = v
    return out


def lookup_zh(tag: str, zh_map: dict[str, str]) -> str:
    """查标签中文译名；支持下划线与空格键. 

    Callers:
        - ``count_tags``
    """
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
    """统计若干目录内旁路 ``.txt`` 的标签频率. 

    引用:
        ``routes/tag_stats.py``. 
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


async def translate_missing_tags(tags: list[str], root: Path) -> dict[str, str]:
    """
    批量翻译标签(en → zh-CN), 写入共享缓存 ``.translation_cache.json``. 

    引用: POST /tags/translate-missing. 
    """
    if not tags:
        return {}
    root = root.resolve()
    cache_path = root.parent / _SHARED_CACHE_NAME

    existing: dict[str, str] = {}
    if cache_path.is_file():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            existing = {str(k): str(v) for k, v in data.items() if k and v}

    uniq = list(dict.fromkeys(tags))
    missing = [t for t in uniq if t not in existing]
    if not missing:
        return {t: existing[t] for t in uniq if t in existing}

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(missing), _BATCH_SIZE):
            chunk = missing[i : i + _BATCH_SIZE]
            params: list[tuple[str, str]] = [
                ("client", "gtx"), ("sl", "en"), ("tl", "zh-CN"),
            ]
            for t in chunk:
                params.append(("q", t))
            resp = await client.get(
                "https://translate.google.com/translate_a/t",
                params=params,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            data = resp.json()
            translations = [str(v) for v in data] if isinstance(data, list) else chunk
            for en, zh in zip(chunk, translations):
                existing[en] = zh.strip() if zh else en

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {t: existing[t] for t in uniq if t in existing}
