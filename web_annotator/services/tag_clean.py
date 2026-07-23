"""标签清洗：与仓库根目录 clean_dataset_tags.py 同一套规则。"""

from __future__ import annotations

import html
from pathlib import Path


def split_tags_line(s: str) -> list[str]:
    """按逗号 / ||| / 换行拆分 caption。"""
    return [x.strip() for x in s.replace("\n", ",").replace("|||", ",").split(",") if x.strip()]


def join_tags_line(tags: list[str]) -> str:
    """用逗号空格拼接 tag 列表。"""
    return ", ".join(tags)


DROP_EXACT = frozenset()


def normalize_tag_underscores(tag: str) -> str:
    """Danbooru 风格下划线词转为空格。"""
    return " ".join(tag.replace("_", " ").split())


def tag_key(tag: str) -> str:
    """忽略大小写与下划线差异的比较键。"""
    return normalize_tag_underscores(tag).strip().lower()


def expand_cli_list(items: list[str]) -> list[str]:
    """合并多次传入与逗号分隔的列表项。"""
    out: list[str] = []
    for it in items:
        for part in it.split(","):
            p = part.strip()
            if p:
                out.append(p)
    return out


def should_drop_tag(
    tag: str,
    *,
    user_drop_exact: frozenset[str],
    user_drop_contains: tuple[str, ...],
) -> bool:
    """是否删除该 tag（整词表 + commentary/request + 子串）。"""
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
    """promote 列表 → 去重且保序的 tag_key 序列。"""
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


def apply_promote_tags(kept: list[str], promote_order_keys: list[str]) -> tuple[list[str], int]:
    """
    将 promote_order_keys 中在 kept 里出现过的 tag 移到最前。

    先精确匹配，未命中则子串匹配。返回 (新列表, 被提升数量)。
    """
    if not promote_order_keys:
        return kept, 0

    promote_set = frozenset(promote_order_keys)
    first_text: dict[str, str] = {}
    for t in kept:
        k = tag_key(t)
        if k in promote_set and k not in first_text:
            first_text[k] = t

    consumed_tag_keys: set[str] = {tag_key(v) for v in first_text.values()}
    substr_matched: dict[str, str] = {}
    for pk in promote_order_keys:
        if pk in first_text:
            continue
        for t in kept:
            k = tag_key(t)
            if k in consumed_tag_keys:
                continue
            if pk in k:
                substr_matched[pk] = t
                consumed_tag_keys.add(k)
                break

    promoted: list[str] = []
    for pk in promote_order_keys:
        if pk in first_text:
            promoted.append(first_text[pk])
        elif pk in substr_matched:
            promoted.append(substr_matched[pk])

    remaining = [t for t in kept if tag_key(t) not in consumed_tag_keys]
    return promoted + remaining, len(promoted)


def clean_caption(
    raw: str,
    *,
    user_drop_exact: frozenset[str] | None = None,
    user_drop_contains: tuple[str, ...] = (),
    promote_tags: tuple[str, ...] = (),
    prepend_tags: tuple[str, ...] = (),
    use_promote_separator: bool = False,
) -> str:
    """
    清洗单条 caption。

    顺序：反转义与下划线归一 → 删除 → promote → prepend。
    """
    s = html.unescape(raw.strip())
    tags = [normalize_tag_underscores(t) for t in split_tags_line(s)]
    exact = user_drop_exact or frozenset()
    kept = [
        t
        for t in tags
        if not should_drop_tag(t, user_drop_exact=exact, user_drop_contains=user_drop_contains)
    ]

    promote_keys = promote_keys_ordered(promote_tags)
    kept, n_promoted = apply_promote_tags(kept, promote_keys)

    prep = [normalize_tag_underscores(t) for t in prepend_tags if t.strip()]
    if prep:
        prep_keys = {tag_key(t) for t in prep}
        kept = [t for t in kept if tag_key(t) not in prep_keys]
        kept = prep + kept
        n_promoted += len(prep)

    if use_promote_separator:
        return join_tags_line(kept[:n_promoted]) + " ||| " + join_tags_line(kept[n_promoted:])

    return join_tags_line(kept)


def clean_txt_dirs(
    dirs: list[Path],
    *,
    drop: list[str] | None = None,
    drop_contains: list[str] | None = None,
    promote: list[str] | None = None,
    prepend: list[str] | None = None,
    promote_separator: bool = False,
    dry_run: bool = True,
    sample_limit: int = 40,
) -> dict:
    """
    对若干目录内的 *.txt 执行清洗。

    Args:
        dirs: 绝对路径目录列表（不递归）。
        drop: 整词删除列表。
        drop_contains: 子串删除列表（另有内置 commentary/request）。
        promote / prepend: 提升与前置列表。
        promote_separator: 是否用 ||| 分隔提升块。
        dry_run: True 只预览不写文件。
        sample_limit: 返回的前后对比条数上限。

    Returns:
        统计与可选样本对比。
    """
    user_drop_exact = frozenset(tag_key(t) for t in expand_cli_list(drop or []))
    user_drop_contains = tuple(
        s.strip().lower() for s in expand_cli_list(drop_contains or []) if s.strip()
    )
    promote_tags = tuple(expand_cli_list(promote or []))
    prepend_tags = tuple(expand_cli_list(prepend or []))

    files: list[Path] = []
    for d in dirs:
        if d.is_dir():
            files.extend(sorted(d.glob("*.txt")))

    changed = 0
    unchanged = 0
    failed = 0
    samples: list[dict] = []

    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            failed += 1
            continue
        new = clean_caption(
            raw,
            user_drop_exact=user_drop_exact,
            user_drop_contains=user_drop_contains,
            promote_tags=promote_tags,
            prepend_tags=prepend_tags,
            use_promote_separator=promote_separator,
        )
        if new == raw.strip():
            unchanged += 1
            continue
        changed += 1
        if len(samples) < sample_limit:
            samples.append({
                "path": str(path),
                "name": path.name,
                "before": raw.strip()[:500],
                "after": new[:500],
            })
        if not dry_run:
            path.write_text(new + "\n", encoding="utf-8")

    return {
        "dry_run": dry_run,
        "file_count": len(files),
        "changed": changed,
        "unchanged": unchanged,
        "failed": failed,
        "samples": samples,
        "drop_exact_count": len(user_drop_exact),
        "drop_contains": list(user_drop_contains),
        "promote": list(promote_tags),
        "prepend": list(prepend_tags),
    }
