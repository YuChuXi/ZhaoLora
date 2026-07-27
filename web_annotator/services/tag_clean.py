"""标签清洗：与仓库根目录 clean_dataset_tags.py 同一套规则。"""

from __future__ import annotations

import html
from pathlib import Path

from services.models import TagCleanResult, TagCleanSample
from services.scanner import IMAGE_EXTS

# 与 tag_key 一致：score_9 → "score 9"
SCORE_TAG_KEYS = frozenset(f"score {i}" for i in range(10))


def split_tags_line(s: str) -> list[str]:
    """按逗号 / ||| / 换行拆分 caption。"""
    return [x.strip() for x in s.replace("\n", ",").replace("|||", ",").split(",") if x.strip()]


def join_tags_line(tags: list[str]) -> str:
    """用逗号空格拼接 tag 列表。"""
    return ", ".join(tags)


def raw_to_pony(raw: float) -> int:
    """原始美学分 [0,1] → score_0 … score_9（与 aesthetic_score_dataset_pony 一致）。"""
    return int(max(0.0, min(0.99, float(raw))) * 10)


def format_pony_score_tag(raw: float) -> str:
    """生成 pony 美学 tag（保留下划线），如 score_9。"""
    return f"score_{raw_to_pony(raw)}"


def rel_image_for_txt(txt: Path, root: Path) -> str | None:
    """同 stem 旁路图相对数据集根的路径（用于查美学分）。"""
    root_r = root.resolve()
    parent = txt.parent
    stem = txt.stem
    for ext in IMAGE_EXTS:
        img = parent / f"{stem}{ext}"
        if img.is_file():
            return str(img.resolve().relative_to(root_r)).replace("\\", "/")
    return None


DROP_EXACT = frozenset({"highres", "original", "absurdres"})


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
    pony_score_tag: str | None = None,
    use_promote_separator: bool = False,
) -> str:
    """
    清洗单条 caption。

    顺序：HTML 实体还原与下划线规范化 → 删除 → 提升重排 → 美学档位 → 头部前置。
    pony_score_tag（如 score_9）写入头部前置之前，并移除既有 score_0…score_9。
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

    if pony_score_tag:
        kept = [t for t in kept if tag_key(t) not in SCORE_TAG_KEYS]

    header: list[str] = []
    if pony_score_tag and pony_score_tag.strip():
        # pony 训练用下划线形式，不做 underscore→space
        header.append(pony_score_tag.strip())
    prep = [normalize_tag_underscores(t) for t in prepend_tags if t.strip()]
    header.extend(prep)
    if header:
        header_keys = {tag_key(t) for t in header}
        kept = [t for t in kept if tag_key(t) not in header_keys]
        kept = header + kept
        n_promoted += len(header)

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
    inject_pony_score: bool = False,
    dataset_root: Path | None = None,
    aesthetic_scores: dict[str, float] | None = None,
    dry_run: bool = True,
    sample_limit: int = 40,
) -> TagCleanResult:
    """
    对若干目录内的 *.txt 执行清洗。

    Args:
        dirs: 绝对路径目录列表（不递归）。
        drop: 整词删除列表。
        drop_contains: 子串删除列表（另有内置 commentary/request）。
        promote / prepend: 提升与前置列表。
        promote_separator: 是否用 ||| 分隔提升块。
        inject_pony_score: 是否根据 .aesthetic_scores.json 写入 score_0…score_9（位于头部前置之前）。
        dataset_root: 数据集根目录（用于解析旁路图像相对路径）。
        aesthetic_scores: 相对路径 → 原始美学分 [0,1]。
        dry_run: True 仅预览，不写文件。
        sample_limit: 返回的前后对比样本上限。

    Returns:
        统计与可选样本对比。

    引用:
        ``routes.tag_stats.tag_clean``。
    """
    user_drop_exact = frozenset(tag_key(t) for t in expand_cli_list(drop or []))
    user_drop_contains = tuple(
        s.strip().lower() for s in expand_cli_list(drop_contains or []) if s.strip()
    )
    promote_tags = tuple(expand_cli_list(promote or []))
    prepend_tags = tuple(expand_cli_list(prepend or []))
    scores = aesthetic_scores or {}
    root = dataset_root.resolve() if dataset_root is not None else None

    files: list[Path] = []
    for d in dirs:
        if d.is_dir():
            files.extend(sorted(d.glob("*.txt")))

    changed = 0
    unchanged = 0
    failed = 0
    pony_injected = 0
    pony_missing = 0
    samples: list[TagCleanSample] = []

    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            failed += 1
            continue

        score_tag: str | None = None
        if inject_pony_score:
            if root is None:
                failed += 1
                continue
            rel = rel_image_for_txt(path, root)
            if rel is not None and rel in scores:
                score_tag = format_pony_score_tag(scores[rel])
                pony_injected += 1
            else:
                pony_missing += 1

        new = clean_caption(
            raw,
            user_drop_exact=user_drop_exact,
            user_drop_contains=user_drop_contains,
            promote_tags=promote_tags,
            prepend_tags=prepend_tags,
            pony_score_tag=score_tag,
            use_promote_separator=promote_separator,
        )
        if new == raw.strip():
            unchanged += 1
            continue
        changed += 1
        if len(samples) < sample_limit:
            samples.append(
                TagCleanSample(
                    path=str(path),
                    name=path.name,
                    before=raw.strip()[:500],
                    after=new[:500],
                )
            )
        if not dry_run:
            path.write_text(new + "\n", encoding="utf-8")

    return TagCleanResult(
        dry_run=dry_run,
        file_count=len(files),
        changed=changed,
        unchanged=unchanged,
        failed=failed,
        samples=samples,
        drop_exact_count=len(user_drop_exact),
        drop_contains=list(user_drop_contains),
        promote=list(promote_tags),
        prepend=list(prepend_tags),
        inject_pony_score=inject_pony_score,
        pony_injected=pony_injected,
        pony_missing=pony_missing,
    )


def batch_edit_tags(
    dirs: list[Path],
    *,
    delete_tags: list[str] | None = None,
    add_tags: list[str] | None = None,
    position: str = "after",
    insert_mode: str = "bottom",
    conditional: bool = False,
    keep_duplicates: bool = False,
    dry_run: bool = True,
    sample_limit: int = 40,
) -> TagCleanResult:
    """
    批量删除/添加标签。

    Args:
        dirs: 绝对路径目录列表（不递归）。
        delete_tags: 要删除的标签列表（tag_key 精确匹配）。
        add_tags: 要添加的标签列表。
        position: ``"before"`` 放置在 ``|||`` 之前，``"after"`` 之后。
        insert_mode: ``"top"`` 置顶 / ``"bottom"`` 置底 / ``"random"`` 随机插入。
        conditional: 仅当文件中已有同名 tag 时才插入。
        keep_duplicates: 不删除已有重复标签。
        dry_run: True 仅预览。
        sample_limit: 返回样本上限。

    Returns:
        统计与可选样本对比。

    引用：``routes.tag_stats.tag_batch_edit``。
    """
    import random as _random

    del_keys: set[str] = set()
    for t in expand_cli_list(delete_tags or []):
        k = tag_key(t)
        if k:
            del_keys.add(k)

    add_list = expand_cli_list(add_tags or [])
    scores = aesthetic_scores or {}
    root = dataset_root.resolve() if dataset_root is not None else None

    files: list[Path] = []
    for d in dirs:
        if d.is_dir():
            files.extend(sorted(d.glob("*.txt")))

    changed = 0
    unchanged = 0
    failed = 0
    pony_injected = 0
    pony_missing = 0
    samples: list[TagCleanSample] = []

    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            failed += 1
            continue

        raw_stripped = raw.strip()

        # Pony 美学档位：注入到最前，并移除既有 score_0…score_9
        score_tag: str | None = None
        if inject_pony_score and root is not None:
            rel = rel_image_for_txt(path, root)
            if rel is not None and rel in scores:
                score_tag = format_pony_score_tag(scores[rel])
                pony_injected += 1
            else:
                pony_missing += 1

        # 按 ||| 拆分
        raw_stripped = raw.strip()
        if "|||" in raw_stripped:
            parts = raw_stripped.split("|||", 1)
            header_str = parts[0].strip()
            body_str = parts[1].strip()
        else:
            header_str = raw_stripped
            body_str = ""

        header = split_tags_line(header_str)
        body = split_tags_line(body_str)

        # 移除既有 score_0…score_9
        header = [t for t in header if tag_key(t) not in SCORE_TAG_KEYS]
        body = [t for t in body if tag_key(t) not in SCORE_TAG_KEYS]

        # 删除
        if del_keys:
            header = [t for t in header if tag_key(t) not in del_keys]
            body = [t for t in body if tag_key(t) not in del_keys]

        # 添加
        to_add: list[str] = []
        for t in add_list:
            k = tag_key(t)
            if conditional:
                # 检查文件中是否已有同名 tag
                existing_keys = {tag_key(x) for x in header + body}
                if k not in existing_keys:
                    continue
            if not keep_duplicates:
                # 删除已有重复
                header = [x for x in header if tag_key(x) != k]
                body = [x for x in body if tag_key(x) != k]
            to_add.append(t)

        if to_add:
            if position == "before":
                if insert_mode == "top":
                    header = to_add + header
                elif insert_mode == "random":
                    for t in to_add:
                        idx = _random.randint(0, len(header)) if header else 0
                        header.insert(idx, t)
                else:  # bottom
                    header = header + to_add
            else:  # after
                if insert_mode == "top":
                    body = to_add + body
                elif insert_mode == "random":
                    for t in to_add:
                        idx = _random.randint(0, len(body)) if body else 0
                        body.insert(idx, t)
                else:  # bottom
                    body = body + to_add

        # 重组：pony 评分永远在最前
        if score_tag:
            header = [score_tag] + header
        if body:
            new = ", ".join(header) + " ||| " + ", ".join(body)
        else:
            new = ", ".join(header)

        if new == raw_stripped:
            unchanged += 1
            continue
        changed += 1
        if len(samples) < sample_limit:
            samples.append(
                TagCleanSample(
                    path=str(path),
                    name=path.name,
                    before=raw_stripped[:500],
                    after=new[:500],
                )
            )
        if not dry_run:
            path.write_text(new + "\n", encoding="utf-8")

    return TagCleanResult(
        dry_run=dry_run,
        file_count=len(files),
        changed=changed,
        unchanged=unchanged,
        failed=failed,
        samples=samples,
        inject_pony_score=inject_pony_score,
        pony_injected=pony_injected,
        pony_missing=pony_missing,
    )
