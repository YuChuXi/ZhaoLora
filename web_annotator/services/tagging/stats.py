"""标注文件标签频率统计. """

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

import httpx

from services.core.natural import natural_key
from services.core.work_dir import list_work_dirs
from services.models import TagStatRow, TagStatsResult
from services.tagging.clean import make_tag_matcher, tag_key

_SHARED_CACHE_NAME = ".translation_cache.json"
_BATCH_SIZE = 128


def split_tags_line(s: str) -> list[str]:
    """按逗号拆分标签行.

    Args:
        s (str): 原始标签行, 可含换行与 ``|||`` 分隔符.

    Returns:
        list[str]: 拆分并去空白后的非空标签列表.

    Callers:
        - ``services/tagging/stats.py:count_tags``
        - ``services/tagging/clean.py:split_header_body``
        - ``services/tagging/clean.py:clean_caption``
    """
    return [x.strip() for x in s.replace("\n", ",").replace("|||", ",").split(",") if x.strip()]


def normalize_tag(tag: str, *, lower: bool, underscores: bool) -> str:
    """规范化单个标签(去空白 / 下划线转空格 / 小写).

    Args:
        tag (str): 原始标签.
        lower (bool): 是否统一小写.
        underscores (bool): 是否将下划线归一为空格.

    Returns:
        str: 规范化后的标签.

    Callers:
        - ``services/tagging/stats.py:count_tags``
    """
    t = tag.strip()
    if underscores:
        t = " ".join(t.replace("_", " ").split())
    if lower:
        t = t.lower()
    return t


def resolve_selected_dirs(root: Path, names: list[str] | None) -> list[Path]:
    """将选中的工作子目录名解析为绝对路径; 空则返回全部工作目录.

    Args:
        root (Path): 数据集根目录.
        names (list[str] | None): 选中的子目录名列表; None/空表示全选.

    Returns:
        list[Path]: 解析后的子目录绝对路径列表.

    Callers:
        - ``routes/tag_stats.py:stats``
        - ``routes/tag_stats.py:clean``
        - ``routes/tag_stats.py:batch_edit``
        - ``routes/tag_stats.py:check``
        - ``routes/tag_stats.py:embedding_query``
        - ``routes/tag_stats.py:concept_query``
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
    """加载共享翻译缓存 ``datasets/.translation_cache.json``.

    缓存键为原始标签(含下划线), 值为中文译名; 同时写入下划线转空格的小写键
    以兼容 ``lookup_zh`` 的归一化查询.

    Args:
        root (Path): 数据集根目录, 缓存文件位于其父目录.

    Returns:
        dict[str, str]: 标签到中文译名的映射; 无缓存或格式非法时返回空字典.

    Callers:
        - ``routes/tag_stats.py:stats``
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
    """查标签中文译名, 支持下划线与空格键互换匹配.

    Args:
        tag (str): 原始标签.
        zh_map (dict[str, str]): 翻译缓存(由 ``load_translation_map`` 提供).

    Returns:
        str: 中文译名; 无匹配时返回空字符串.

    Callers:
        - ``services/tagging/stats.py:count_tags``
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
    section: str = "all",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    match_mode: str = "exact",
) -> TagStatsResult:
    """统计若干目录内旁路 ``.txt`` 的标签频率.

    ``section`` 限定统计范围:
    - ``"all"``: 整行所有标签.
    - ``"before"``: 只统计 ``|||`` 之前(头部)的标签.
    - ``"after"``: 只统计 ``|||`` 之后(正文)的标签.

    ``include`` / ``exclude`` 按指定 ``match_mode`` 对样本(图片)做筛选:
    命中 include 任一模式的样本才纳入统计; 命中 exclude 任一模式的样本被排除.
    匹配基于 ``tag_key``(小写无下划线) 归一化, 与 ``batch_edit_tags`` 的
    ``match_mode`` 语义一致(``exact`` | ``regex`` | ``danbooru``).

    Args:
        dirs: 待统计的工作目录列表.
        zh_map: 中文译名缓存.
        lower: 是否统一小写.
        underscores: 是否将下划线归一为空格.
        html_unescape: 是否反转义 HTML 实体.
        per_sample: 是否按图去重.
        min_count: 最少出现次数过滤.
        section: 统计范围, ``"all"`` / ``"before"`` / ``"after"``.
        include: 样本须命中的标签模式列表; None/空表示不限.
        exclude: 样本须未命中的标签模式列表; None/空表示不限.
        match_mode: 标签匹配模式, ``"exact"`` / ``"regex"`` / ``"danbooru"``.

    Returns:
        TagStatsResult: 统计结果, 含被筛选跳过的样本数 ``skipped``.

    Raises:
        ValueError: ``section`` 或 ``match_mode`` 非法.

    Callers:
        - ``routes/tag_stats.py:stats``
    """
    if section not in ("all", "before", "after"):
        raise ValueError(f"section 非法: {section}")
    if match_mode not in ("exact", "regex", "danbooru"):
        raise ValueError(f"match_mode 非法: {match_mode}")
    zh_map = zh_map or {}
    include_matcher = make_tag_matcher(include, match_mode) if include else None
    exclude_matcher = make_tag_matcher(exclude, match_mode) if exclude else None
    counter: Counter[str] = Counter()
    total_tag_occurrences = 0
    failed = 0
    skipped = 0
    files: list[Path] = []

    for d in dirs:
        if not d.is_dir():
            continue
        files.extend(sorted(d.glob("*.txt"), key=lambda p: natural_key(p.name)))

    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            failed += 1
            continue
        text = raw.strip()
        if html_unescape:
            text = html.unescape(text)
        if section == "before":
            text = text.split("|||", 1)[0]
        elif section == "after":
            parts = text.split("|||", 1)
            text = parts[1] if len(parts) > 1 else ""
        tags = split_tags_line(text)
        # 样本筛选: 基于 tag_key 归一化与匹配模式判定
        if include_matcher is not None or exclude_matcher is not None:
            keys = [tag_key(t) for t in tags]
            if include_matcher is not None and not any(include_matcher(k) for k in keys):
                skipped += 1
                continue
            if exclude_matcher is not None and any(exclude_matcher(k) for k in keys):
                skipped += 1
                continue
        normed = [
            normalize_tag(t, lower=lower, underscores=underscores)
            for t in tags
        ]
        normed = [t for t in normed if t]
        if per_sample:
            normed = list(dict.fromkeys(normed))
        counter.update(normed)
        total_tag_occurrences += len(normed)

    n_files = len(files) - failed - skipped
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
    rows.sort(key=lambda x: (-x.pct, -x.count, natural_key(x.tag)))

    return TagStatsResult(
        sample_count=n_files,
        failed=failed,
        skipped=skipped,
        file_count=len(files),
        tag_count=len(rows),
        total_occurrences=total_tag_occurrences,
        per_sample=per_sample,
        tags=rows,
    )


async def translate_missing_tags(tags: list[str], root: Path) -> dict[str, str]:
    """批量翻译标签(en → zh-CN), 写入共享缓存 ``.translation_cache.json``.

    经 Google Translate API 批量翻译缺失译名的标签, 合并已有缓存后回写磁盘.

    Args:
        tags (list[str]): 待翻译的标签列表.
        root (Path): 数据集根目录, 缓存文件位于其父目录.

    Returns:
        dict[str, str]: 标签到中文译名的映射(含已有缓存与新翻译).

    Callers:
        - ``routes/tag_stats.py:translate_missing``
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
