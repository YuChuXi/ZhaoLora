"""标签清洗: 与仓库根目录 clean_dataset_tags.py 同一套规则. """

from __future__ import annotations

import html
import json
import random
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from services.core.natural import natural_key
from services.models import TagCleanResult, TagCleanSample
from services.core.scanner import IMAGE_EXTS

# 与 tag_key 一致: score_9 → "score 9"
SCORE_TAG_KEYS = frozenset(f"score {i}" for i in range(10))

# 质量标签 tag_key (已是小写无下划线, 与 tag_key() 输出一致)
QUALITY_TAG_KEYS = frozenset({"masterpiece", "best quality", "good quality", "normal quality", "low quality", "worst quality"})

# 年份标签(如 "year 2025")的匹配模式, 用于注入前清理旧标签
_YEAR_TAG_RE = re.compile(r"^year \d{4}$")

# 始终置顶的标签 (分数 + 质量)
PRIORITY_TAG_KEYS: frozenset[str] = SCORE_TAG_KEYS | QUALITY_TAG_KEYS


def split_tags_line(s: str) -> list[str]:
    """按逗号 / ||| / 换行拆分 caption.

    Args:
        s (str): 原始 caption 文本.

    Returns:
        list[str]: 拆分并去空白后的非空标签列表.

    Callers:
        - ``services/tagging/clean.py:split_header_body``
        - ``services/tagging/clean.py:clean_caption``
    """
    return [x.strip() for x in s.replace("\n", ",").replace("|||", ",").split(",") if x.strip()]


def join_tags_line(tags: list[str]) -> str:
    """用逗号空格拼接 tag 列表.

    Args:
        tags (list[str]): 标签列表.

    Returns:
        str: 形如 ``tag1, tag2`` 的拼接结果.

    Callers:
        - ``services/tagging/clean.py:clean_caption``
        - ``services/tagging/clean.py:add_tag_to_caption``
        - ``services/tagging/clean.py:delete_tag_from_caption``
        - ``services/tagging/clean.py:_compose_caption``
        - ``routes/tag_stats.py:delete_tag_from_caption_fuzzy``
    """
    return ", ".join(tags)


def split_header_body(raw: str) -> tuple[list[str], list[str], bool]:
    """将 caption 按 ``|||`` 拆分为头部与正文标签列表.

    Args:
        raw (str): 原始 caption 文本.

    Returns:
        tuple[list[str], list[str], bool]: ``(header, body, has_separator)``.
        无 ``|||`` 时 body 为空列表、has_separator 为 False.

    Callers:
        - ``services/tagging/clean.py:check_tag_in_caption``
        - ``services/tagging/clean.py:add_tag_to_caption``
        - ``services/tagging/clean.py:delete_tag_from_caption``
        - ``services/tagging/clean.py:batch_edit_tags``
        - ``routes/tag_stats.py:delete_tag_from_caption_fuzzy``
    """
    text = raw.strip()
    if "|||" in text:
        head_str, rest_str = text.split("|||", 1)
        return split_tags_line(head_str), split_tags_line(rest_str), True
    return split_tags_line(text), [], False


def _first_content_index(tags: list[str]) -> int:
    """返回首个非优先标签(质量/分数)的索引.

    优先标签 (PRIORITY_TAG_KEYS) 始终置顶, 内容标签从其后开始.
    若全部为优先标签则返回列表长度.

    Args:
        tags (list[str]): 标签原文列表.

    Returns:
        int: 首个非优先标签的索引; 全为优先标签时返回列表长度.
    Callers:
        - ``services/tagging/clean.py:add_tag_to_caption``
        - ``services/tagging/clean.py:check_tag_in_caption``
    """
    for i, t in enumerate(tags):
        if tag_key(t) not in PRIORITY_TAG_KEYS:
            return i
    return len(tags)


def check_tag_in_caption(raw: str, tag: str, position: str) -> tuple[bool, bool]:
    """检查标签在 caption 中是否存在及位置是否匹配.

    Args:
        raw (str): 原始 caption 文本.
        tag (str): 待检查的标签(按 ``tag_key`` 归一化比较).
        position (str): 位置选项 — ``any`` / ``top`` / ``bottom`` /
            ``before-sep`` / ``after-sep`` / ``bottom-before``.

    Returns:
        tuple[bool, bool]: ``(exists, position_ok)``. 标签不存在时两者均为 False.

    Callers:
        - ``routes/tag_stats.py:_build_check_item``
    """
    header, body, _ = split_header_body(raw)
    key = tag_key(tag)
    header_keys = [tag_key(t) for t in header]
    body_keys = [tag_key(t) for t in body]
    exists = key in header_keys or key in body_keys
    if not exists:
        return False, False

    if position == "any":
        return True, True
    if position == "top":
        first_idx = _first_content_index(header_keys)
        return True, first_idx < len(header_keys) and header_keys[first_idx] == key
    if position == "bottom":
        all_tags = header + body
        return True, len(all_tags) > 0 and tag_key(all_tags[-1]) == key
    if position == "before-sep":
        return True, key in header_keys
    if position == "bottom-before":
        return True, len(header_keys) > 0 and header_keys[-1] == key
    if position == "after-sep":
        return True, key in body_keys
    return True, False


def add_tag_to_caption(raw: str, tag: str, position: str) -> str:
    """在 caption 指定位置添加标签(先按 ``tag_key`` 去重再插入).

    Args:
        raw (str): 原始 caption 文本.
        tag (str): 待添加的标签原文.
        position (str): 目标位置 — ``top`` / ``bottom`` / ``before-sep`` /
            ``after-sep``.
            各位置插入策略:
            - ``top``: 头部首位(跳过质量/分数等优先标签后插入)
            - ``bottom``: 有 ``|||`` 时追加到正文末尾, 否则头部末尾
            - ``before-sep``: 头部末尾
            - ``after-sep``: 正文首位(无 ``|||`` 时自动创建)

    Returns:
        str: 编辑后的 caption 字符串.

    Callers:
        - ``routes/tag_stats.py:edit_one``
        - ``routes/tag_stats.py:embedding_batch_tag``
    """
    header, body, has_sep = split_header_body(raw)
    key = tag_key(tag)
    header = [t for t in header if tag_key(t) != key]
    body = [t for t in body if tag_key(t) != key]

    if position == "top":
        idx = _first_content_index(header)
        header.insert(idx, tag)
    elif position == "before-sep":
        header.append(tag)
    elif position == "after-sep":
        body.insert(0, tag)
    elif position == "bottom":
        if has_sep:
            body.append(tag)
        else:
            header.append(tag)
    else:
        raise ValueError(f"不支持的插入位置: {position!r}")

    if body:
        return join_tags_line(header) + " ||| " + join_tags_line(body)
    return join_tags_line(header)


def delete_tag_from_caption(raw: str, tag: str) -> str:
    """从 caption 中按 ``tag_key`` 移除指定标签的所有出现.

    Args:
        raw (str): 原始 caption 文本.
        tag (str): 待删除的标签.

    Returns:
        str: 删除后的 caption 字符串. 若标签不存在则返回原文.

    Callers:
        - ``routes/tag_stats.py:delete_one``
        - ``routes/tag_stats.py:embedding_batch_tag``
    """
    header, body, has_sep = split_header_body(raw)
    key = tag_key(tag)
    header = [t for t in header if tag_key(t) != key]
    body = [t for t in body if tag_key(t) != key]
    if body:
        return join_tags_line(header) + " ||| " + join_tags_line(body)
    return join_tags_line(header)


def raw_to_pony(raw: float) -> int:
    """原始美学分 [0,1] → score_0 … score_9.

    Args:
        raw (float): 原始美学分, 超出 [0, 0.99] 时夹到边界.

    Returns:
        int: 0-9 的美学档位.

    Callers:
        - ``services/tagging/clean.py:format_pony_score_tag``
    """
    return int(max(0.0, min(0.99, float(raw))) * 10)


def format_pony_score_tag(raw: float) -> str:
    """生成 pony 美学标签(保留下划线), 如 score_9.

    Args:
        raw (float): 原始美学分 [0,1].

    Returns:
        str: 形如 ``score_0`` … ``score_9`` 的标签.

    Callers:
        - ``services/tagging/clean.py:_lookup_pony_quality``
    """
    return f"score_{raw_to_pony(raw)}"


def rel_image_for_txt(txt: Path, root: Path) -> str | None:
    """同 stem 旁路图片相对数据集根的路径(用于查美学分/质量分).

    Args:
        txt (Path): 旁路 txt 文件路径.
        root (Path): 数据集根目录.

    Returns:
        str | None: 命中图片的 posix 相对路径; 无同 stem 图片时为 None.

    Callers:
        - ``services/tagging/clean.py:_lookup_pony_quality``
    """
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
    """Danbooru 风格下划线词转为空格.

    Args:
        tag (str): 原始标签.

    Returns:
        str: 下划线转空格并折叠空白后的标签.

    Callers:
        - ``services/tagging/clean.py:tag_key``
        - ``services/tagging/clean.py:clean_caption``
    """
    return " ".join(tag.replace("_", " ").split())


def tag_key(tag: str) -> str:
    """忽略大小写与下划线差异的比较键.

    Args:
        tag (str): 原始标签.

    Returns:
        str: 小写、下划线转空格并折叠空白的归一化键.

    Callers:
        - ``services/tagging/clean.py:should_drop_tag``
        - ``services/tagging/clean.py:promote_keys_ordered``
        - ``services/tagging/clean.py:apply_promote_tags``
        - ``services/tagging/clean.py:clean_caption``
        - ``services/tagging/clean.py:check_tag_in_caption``
        - ``services/tagging/clean.py:add_tag_to_caption``
        - ``services/tagging/clean.py:delete_tag_from_caption``
        - ``services/tagging/clean.py:classify_patterns``
        - ``services/tagging/clean.py:clean_txt_dirs``
        - ``services/tagging/clean.py:batch_edit_tags``
        - ``services/tagging/stats.py:count_tags``
    """
    return normalize_tag_underscores(tag).strip().lower()


def expand_cli_list(items: list[str]) -> list[str]:
    """合并多次传入与逗号分隔的列表项.

    Args:
        items (list[str]): 原始列表项, 可含逗号分隔的多值.

    Returns:
        list[str]: 展开并去空白后的非空列表.

    Callers:
        - ``services/tagging/clean.py:clean_txt_dirs``
        - ``services/tagging/clean.py:classify_patterns``
        - ``services/tagging/clean.py:batch_edit_tags``
    """
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
    """是否删除该标签(整词表 + commentary/request + 子串).

    Args:
        tag (str): 原始标签.
        user_drop_exact (frozenset[str]): 用户整词删除表(已按 ``tag_key`` 归一化).
        user_drop_contains (tuple[str, ...]): 用户子串删除表(小写).

    Returns:
        bool: 命中删除规则则为 True.

    Callers:
        - ``services/tagging/clean.py:clean_caption``
    """
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
    """promote 列表 → 去重且保序的 tag_key 序列.

    Args:
        promote_tags (tuple[str, ...]): 待提升的标签原文列表.

    Returns:
        list[str]: 去重保序后的 tag_key 列表.

    Callers:
        - ``services/tagging/clean.py:clean_caption``
    """
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
    """将 promote 列表中的标签移到最前(先精确后子串匹配).

    Args:
        kept (list[str]): 待重排的标签原文列表.
        promote_order_keys (list[str]): promote 的 tag_key 顺序.

    Returns:
        tuple[list[str], int]: ``(新列表, 被提升数量)``.

    Callers:
        - ``services/tagging/clean.py:clean_caption``
    """
    if not promote_order_keys:
        return kept, 0

    promote_set = frozenset(promote_order_keys)
    # 精确匹配: promote key → 首个匹配标签文本
    exact: dict[str, str] = {}
    for t in kept:
        k = tag_key(t)
        if k in promote_set and k not in exact:
            exact[k] = t
    # 子串匹配: promote key → 首个含该子串的标签
    consumed = set(exact)
    substr: dict[str, str] = {}
    for pk in promote_order_keys:
        if pk in exact:
            continue
        for t in kept:
            k = tag_key(t)
            if k in consumed:
                continue
            if pk in k:
                substr[pk] = t
                consumed.add(k)
                break

    # 按 promote 顺序收集提升的标签(先精确后子串)
    promoted = [exact[pk] for pk in promote_order_keys if pk in exact]
    promoted += [substr[pk] for pk in promote_order_keys if pk in substr]
    remaining = [t for t in kept if tag_key(t) not in consumed]
    return promoted + remaining, len(promoted)


def clean_caption(
    raw: str,
    *,
    user_drop_exact: frozenset[str] | None = None,
    user_drop_contains: tuple[str, ...] = (),
    promote_tags: tuple[str, ...] = (),
    prepend_tags: tuple[str, ...] = (),
    pony_score_tag: str | None = None,
    quality_tag: str | None = None,
    use_promote_separator: bool = False,
) -> str:
    """清洗单条 caption.

    顺序: HTML 实体还原与下划线规范化 → 删除 → 提升重排 → 美学/质量标签 → 头部前置.
    pony_score_tag(如 score_9)和 quality_tag(如 masterpiece)写入头部前置之前,
    两者随机排序, 并移除既有 score_0…score_9 和质量标签.

    Args:
        raw (str): 原始 caption 文本.
        user_drop_exact (frozenset[str] | None): 整词删除表(已按 ``tag_key`` 归一化).
        user_drop_contains (tuple[str, ...]): 子串删除表(小写).
        promote_tags (tuple[str, ...]): 提升至头部的标签列表.
        prepend_tags (tuple[str, ...]): 头部前置标签列表(可新增).
        pony_score_tag (str | None): 美学档位标签, 如 ``score_9``.
        quality_tag (str | None): 质量标签, 如 ``masterpiece``.
        use_promote_separator (bool): 是否用 ``|||`` 分隔提升块.

    Returns:
        str: 清洗后的 caption 字符串.

    Callers:
        - ``services/tagging/clean.py:clean_txt_dirs``
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

    # 移除既有的美学档位标签和质量标签
    if pony_score_tag:
        kept = [t for t in kept if tag_key(t) not in SCORE_TAG_KEYS]
    if quality_tag:
        kept = [t for t in kept if tag_key(t) not in QUALITY_TAG_KEYS]

    # 头部: score/质量随机置顶 + prepend 前置
    header = _priority_tags(pony_score_tag, quality_tag) + [
        normalize_tag_underscores(t) for t in prepend_tags if t.strip()
    ]
    if header:
        header_keys = {tag_key(t) for t in header}
        kept = [t for t in kept if tag_key(t) not in header_keys]
        kept = header + kept
        n_promoted += len(header)

    if use_promote_separator:
        return join_tags_line(kept[:n_promoted]) + " ||| " + join_tags_line(kept[n_promoted:])

    return join_tags_line(kept)


def _collect_txt_files(dirs: list[Path]) -> list[Path]:
    """收集目录下所有 ``*.txt``(不递归, 排序).

    Args:
        dirs (list[Path]): 目录绝对路径列表.

    Returns:
        list[Path]: 排序后的 txt 文件路径列表.
    Callers:
        - ``services/tagging/clean.py:_run_batch_edit``
    """
    files: list[Path] = []
    for d in dirs:
        if d.is_dir():
            files.extend(sorted(d.glob("*.txt"), key=lambda p: natural_key(p.name)))
    return files


def _lookup_pony_quality(path: Path, root: Path | None, *,
                         inject_pony_score: bool, scores: dict[str, float],
                         inject_quality_tag: bool, q_scores: dict[str, float]) -> tuple[str | None, str | None, int, int, int, int]:
    """按旁路图片得分查美学/质量档位标签并返回计数增量.

    Args:
        path (Path): txt 文件路径.
        root (Path | None): 数据集根目录; None 时无法解析相对路径, 记缺失.
        inject_pony_score (bool): 是否注入美学档位.
        scores (dict[str, float]): 相对路径 → 原始美学分 [0,1].
        inject_quality_tag (bool): 是否注入质量标签.
        q_scores (dict[str, float]): 相对路径 → 质量分 [0,1].

    Returns:
        tuple[str | None, str | None, int, int, int, int]:
        ``(score_tag, quality_tag_val, pony_injected, pony_missing,
        quality_injected, quality_missing)``; 未注入的维度计数为 0.
    Callers:
        - ``services/tagging/clean.py:_run_batch_edit``
    """
    score_tag: str | None = None
    quality_tag_val: str | None = None
    pony_injected = pony_missing = 0
    quality_injected = quality_missing = 0

    if inject_pony_score and root is not None:
        rel = rel_image_for_txt(path, root)
        if rel is not None and rel in scores:
            score_tag = format_pony_score_tag(scores[rel])
            pony_injected = 1
        else:
            pony_missing = 1
    if inject_quality_tag and root is not None:
        rel = rel_image_for_txt(path, root)
        if rel is not None and rel in q_scores:
            from services.analysis.scoring import quality_to_tag
            quality_tag_val = quality_to_tag(q_scores[rel])
            quality_injected = 1
        else:
            quality_missing = 1
    return score_tag, quality_tag_val, pony_injected, pony_missing, quality_injected, quality_missing


def _lookup_year_tag(txt: Path) -> str | None:
    """从同目录在线来源 meta JSON 的发布时间构造年份标签 ``year YYYY``.

    Args:
        txt (Path): txt 标注文件路径; meta JSON 位于同目录的 ``.{来源}_meta/{同名}.json``.

    Returns:
        str | None: 年份标签(如 ``year 2025``); 无 meta、无发布时间或文件不可读时为 None.

    Callers:
        - ``services/tagging/clean.py:_run_batch_edit``
    """
    meta_file = next(txt.parent.glob(f".*_meta/{txt.stem}.json"), None)
    if meta_file is None:
        return None
    try:
        posted_at = json.loads(meta_file.read_text(encoding="utf-8")).get("posted_at", "")
    except (OSError, json.JSONDecodeError):
        # meta 损坏视为缺失, 计入 year_missing 统计而非中断批量任务
        return None
    if posted_at[:4].isdigit():
        return f"year {posted_at[:4]}"
    return None


def _record_sample(raw: str, new: str, *, path: Path, root: Path | None, dry_run: bool,
                   samples: list[TagCleanSample], sample_limit: int) -> None:
    """变更时追加前后对比样本并写文件; path 字段存相对数据集根的路径.

    Args:
        raw (str): 原始 caption.
        new (str): 编辑后的 caption.
        path (Path): txt 文件绝对路径.
        root (Path | None): 数据集根目录; 为 None 时无相对路径可用, 以文件名为标识.
        dry_run (bool): True 仅预览, 不写文件.
        samples (list[TagCleanSample]): 样本收集列表(就地追加).
        sample_limit (int): 样本上限.

    Returns:
        None
    Callers:
        - ``services/tagging/clean.py:_run_batch_edit``
    """
    if len(samples) < sample_limit:
        samples.append(
            TagCleanSample(
                path=str(path.relative_to(root)) if root is not None else path.name,
                name=path.name,
                before=raw.strip()[:500],
                after=new[:500],
            )
        )
    if not dry_run:
        path.write_text(new + "\n", encoding="utf-8")


class _BatchStats(NamedTuple):
    """批量处理统计与样本."""
    files: list[Path]
    changed: int
    unchanged: int
    failed: int
    pony_injected: int
    pony_missing: int
    quality_injected: int
    quality_missing: int
    year_injected: int
    year_missing: int
    samples: list[TagCleanSample]


def _run_batch_edit(
    dirs: list[Path],
    *,
    root: Path | None,
    inject_pony_score: bool, scores: dict[str, float],
    inject_quality_tag: bool, q_scores: dict[str, float],
    inject_year_tag: bool = False,
    dry_run: bool, sample_limit: int,
    edit: Callable[[str, str | None, str | None, str | None], tuple[str, Any]],
) -> tuple[_BatchStats, list[Any]]:
    """通用批量编辑骨架: 遍历 txt、查分、编辑、统计并记录样本.

    Args:
        dirs (list[Path]): 绝对路径目录列表(不递归).
        root (Path | None): 数据集根目录(解析旁路图片相对路径).
        inject_pony_score (bool): 美学档位注入开关.
        scores (dict[str, float]): 相对路径 → 原始美学分 [0,1].
        inject_quality_tag (bool): 质量标签注入开关.
        q_scores (dict[str, float]): 相对路径 → 质量分 [0,1].
        inject_year_tag (bool): 年份标签注入开关(由 meta JSON 发布时间生成).
        dry_run (bool): True 仅预览, 不写文件.
        sample_limit (int): 返回样本上限.
        edit (Callable[[str, str | None, str | None, str | None], tuple[str, Any]]): 单文件编辑函数
            ``(raw, score_tag, quality_tag_val, year_tag) -> (new, extra)``,
            ``extra`` 为编辑附带数据(无则为 None), 按文件顺序收集返回.

    Returns:
        tuple[_BatchStats, list[Any]]: ``(_BatchStats, extras)``; extras 与 files 一一对应, 读取失败的文件为 None.
    Callers:
        - ``services/tagging/clean.py:batch_edit_tags``
        - ``services/tagging/clean.py:clean_txt_dirs``
    """
    files = _collect_txt_files(dirs)
    changed = unchanged = failed = 0
    pony_injected = pony_missing = 0
    quality_injected = quality_missing = 0
    year_injected = year_missing = 0
    samples: list[TagCleanSample] = []
    extras: list[Any] = []

    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            failed += 1
            extras.append(None)
            continue

        score_tag, quality_tag_val, pi, pm, qi, qm = _lookup_pony_quality(
            path, root,
            inject_pony_score=inject_pony_score, scores=scores,
            inject_quality_tag=inject_quality_tag, q_scores=q_scores,
        )
        pony_injected += pi
        pony_missing += pm
        quality_injected += qi
        quality_missing += qm

        year_tag = _lookup_year_tag(path) if inject_year_tag else None
        if inject_year_tag:
            year_injected += year_tag is not None
            year_missing += year_tag is None

        new, extra = edit(raw, score_tag, quality_tag_val, year_tag)
        extras.append(extra)
        if new == raw.strip():
            unchanged += 1
        else:
            changed += 1
            _record_sample(raw, new, path=path, root=root, dry_run=dry_run, samples=samples, sample_limit=sample_limit)

    stats = _BatchStats(
        files, changed, unchanged, failed,
        pony_injected, pony_missing, quality_injected, quality_missing,
        year_injected, year_missing, samples,
    )
    return stats, extras


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
    inject_quality_tag: bool = False,
    quality_scores: dict[str, float] | None = None,
    dry_run: bool = True,
    sample_limit: int = 40,
) -> TagCleanResult:
    """对若干目录内的 *.txt 执行清洗.

    Args:
        dirs (list[Path]): 绝对路径目录列表(不递归).
        drop (list[str] | None): 整词删除列表.
        drop_contains (list[str] | None): 子串删除列表(另有内置 commentary/request).
        promote (list[str] | None): 提升列表.
        prepend (list[str] | None): 前置列表.
        promote_separator (bool): 是否用 ||| 分隔提升块.
        inject_pony_score (bool): 是否根据 .aesthetic_scores.json 写入 score_0…score_9.
        inject_quality_tag (bool): 是否根据 .quality_scores.json 写入质量标签.
        dataset_root (Path | None): 数据集根目录(用于解析旁路图像相对路径).
        aesthetic_scores (dict[str, float] | None): 相对路径 → 原始美学分 [0,1].
        quality_scores (dict[str, float] | None): 相对路径 → 质量分 [0,1].
        dry_run (bool): True 仅预览, 不写文件.
        sample_limit (int): 返回的前后对比样本上限.

    Returns:
        TagCleanResult: 统计与可选样本对比.

    Callers:
        - ``routes/tag_stats.py:clean``
    """
    user_drop_exact = frozenset(tag_key(t) for t in expand_cli_list(drop or []))
    user_drop_contains = tuple(
        s.strip().lower() for s in expand_cli_list(drop_contains or []) if s.strip()
    )
    promote_tags = tuple(expand_cli_list(promote or []))
    prepend_tags = tuple(expand_cli_list(prepend or []))
    scores = aesthetic_scores or {}
    q_scores = quality_scores or {}
    root = dataset_root.resolve() if dataset_root is not None else None

    def _edit(raw: str, score_tag: str | None, quality_tag_val: str | None, _year_tag: str | None) -> tuple[str, object]:
        """单文件清洗闭包, 由 ``_run_batch_edit`` 逐文件调用; 不支持年份注入."""
        return clean_caption(
            raw,
            user_drop_exact=user_drop_exact,
            user_drop_contains=user_drop_contains,
            promote_tags=promote_tags,
            prepend_tags=prepend_tags,
            pony_score_tag=score_tag,
            quality_tag=quality_tag_val,
            use_promote_separator=promote_separator,
        ), None

    st, _ = _run_batch_edit(
        dirs, root=root,
        inject_pony_score=inject_pony_score, scores=scores,
        inject_quality_tag=inject_quality_tag, q_scores=q_scores,
        dry_run=dry_run, sample_limit=sample_limit,
        edit=_edit,
    )
    return TagCleanResult(
        dry_run=dry_run,
        file_count=len(st.files),
        changed=st.changed,
        unchanged=st.unchanged,
        failed=st.failed,
        samples=st.samples,
        drop_exact_count=len(user_drop_exact),
        drop_contains=list(user_drop_contains),
        promote=list(promote_tags),
        prepend=list(prepend_tags),
        inject_pony_score=inject_pony_score,
        pony_injected=st.pony_injected,
        pony_missing=st.pony_missing,
        inject_quality_tag=inject_quality_tag,
        quality_injected=st.quality_injected,
        quality_missing=st.quality_missing,
    )


def classify_patterns(patterns: list[str], mode: str) -> tuple[set[str], dict[str, re.Pattern[str]]]:
    """把标签/模式分类为 精确 key 集合 与 模式正则字典.

    Args:
        patterns (list[str]): 标签或模式列表. 仅 ``exact`` 模式按逗号二次展开.
        mode (str): ``exact``(tag_key 精确) | ``regex``(正则搜索) | ``danbooru``(* 通配).

    Returns:
        tuple[set[str], dict[str, re.Pattern[str]]]: ``(exact_keys, pattern_re)``;
        exact_keys 为 ``set[str]``(归一化 tag_key), pattern_re 为
        ``dict[模式原文, re.Pattern]``; exact 模式 pattern_re 为空.

    Callers:
        - ``services/tagging/clean.py:make_tag_matcher``
        - ``services/tagging/clean.py:batch_edit_tags``
    """
    if mode == "exact":
        return {tag_key(p) for p in expand_cli_list(patterns)}, {}
    out: dict[str, re.Pattern[str]] = {}
    for p in patterns:
        if not p:
            continue
        if mode == "regex":
            out[p] = re.compile(p)
        else:  # danbooru
            k = tag_key(p)
            out[p] = re.compile(f"^{re.escape(k).replace('\\*', '.*')}$")
    return set(), out


_BACKREF_RE = re.compile(r"\\\d+|\$\d+")


def _has_backref(template: str) -> bool:
    """替换模板是否含分组占位符(``\\1`` 或 ``$1``).

    Args:
        template (str): 替换模板原文.

    Returns:
        bool: 含分组占位符则为 True.
    Callers:
        - ``services/tagging/clean.py:_regex_replace``
    """
    return _BACKREF_RE.search(template) is not None


def _expand_template(match: re.Match[str], template: str) -> str:
    """展开替换模板, 支持 ``\\1`` 与 ``$1`` 两种分组占位符语法.

    Args:
        match (re.Match[str]): 已命中的正则匹配对象.
        template (str): 替换模板, 可用 ``\\1`` / ``$1`` 引用捕获组.

    Returns:
        str: 展开后的替换文本.
    Callers:
        - ``services/tagging/clean.py:_regex_replace``
    """
    if "$" in template:
        template = re.sub(r"\$(\d+)", r"\\g<\1>", template)
    return match.expand(template)


def _regex_replace(key: str, rx: re.Pattern[str], template: str) -> str | None:
    """正则替换: 命中返回展开模板(支持分组占位符), 未命中返回 None.

    Args:
        key (str): 待匹配的 tag_key 文本.
        rx (re.Pattern[str]): 编译后的正则.
        template (str): 替换模板, 可用 ``\\1`` / ``$1`` 引用捕获组.

    Returns:
        str | None: 命中时返回展开后的文本; 未命中返回 None.
    Callers:
        - ``services/tagging/clean.py:_transform``
        - ``services/tagging/clean.py:batch_edit_tags``
    """
    m = rx.search(key)
    if m is None:
        return None
    return _expand_template(m, template) if _has_backref(template) else template


def make_tag_matcher(patterns: list[str], mode: str) -> Callable[[str], bool]:
    """按匹配模式构造标签命中判定函数(任一模式命中即命中).

    Args:
        patterns (list[str]): 标签或模式列表. 仅 ``exact`` 模式支持逗号分隔批量传入
            (其余模式按原样使用, 逗号是模式字符).
        mode (str): 匹配模式, 语义同 ``classify_patterns``.

    Returns:
        Callable[[str], bool]: 接受 ``tag_key`` 归一化字符串、返回是否命中的函数.

    Callers:
        - ``services/tagging/clean.py:batch_edit_tags``
        - ``services/tagging/stats.py:count_tags``
    """
    keys, pats = classify_patterns(patterns, mode)
    return lambda key: key in keys or any(re_.search(key) for re_ in pats.values())


def _priority_tags(score_tag: str | None, quality_tag: str | None) -> list[str]:
    """收集 score/质量标签并随机排序(两者都存在时).

    Args:
        score_tag (str | None): 美学档位标签.
        quality_tag (str | None): 质量标签.

    Returns:
        list[str]: 随机排序后的优先标签列表.
    Callers:
        - ``services/tagging/clean.py:_compose_caption``
        - ``services/tagging/clean.py:clean_caption``
    """
    priority = [t.strip() for t in (score_tag, quality_tag) if t and t.strip()]
    if len(priority) > 1:
        random.shuffle(priority)
    return priority


def _compose_caption(header: list[str], body: list[str], *,
                     score_tag: str | None, quality_tag_val: str | None,
                     use_separator: str, n_added_head: int) -> str:
    """重组(score/质量标签置顶)并按分隔符模式拼接 caption.

    Args:
        header (list[str]): 头部标签列表.
        body (list[str]): 正文标签列表.
        score_tag (str | None): 美学档位标签.
        quality_tag_val (str | None): 质量标签.
        use_separator (str): ``|||`` 模式: ``add`` / ``keep`` / ``remove``.
        n_added_head (int): 本次添加到头部的标签数.

    Returns:
        str: 拼接后的 caption 字符串.
    Callers:
        - ``services/tagging/clean.py:batch_edit_tags``
    """
    priority = _priority_tags(score_tag, quality_tag_val)
    header = priority + header

    n_head = len(priority) + n_added_head
    if body:
        new = ", ".join(header) + " ||| " + ", ".join(body)
        if use_separator == "remove":
            new = ", ".join(header + body)
    elif use_separator == "add" and n_head > 0:
        new = ", ".join(header[:n_head]) + " ||| " + ", ".join(header[n_head:])
    else:
        new = ", ".join(header)
    return new


def batch_edit_tags(
    dirs: list[Path],
    *,
    delete_tags: list[str] | None = None,
    add_tags: list[str] | None = None,
    replace_pairs: list[tuple[str, str]] | None = None,
    match_mode: str = "exact",
    position: str = "after",
    insert_mode: str = "bottom",
    conditional: bool = False,
    keep_duplicates: bool = False,
    inject_pony_score: bool = False,
    dataset_root: Path | None = None,
    aesthetic_scores: dict[str, float] | None = None,
    inject_quality_tag: bool = False,
    quality_scores: dict[str, float] | None = None,
    use_separator: str = "keep",
    inject_year_tag: bool = False,
    dry_run: bool = True,
    sample_limit: int = 40,
) -> TagCleanResult:
    """批量删除/添加标签.

    Args:
        dirs (list[Path]): 绝对路径目录列表(不递归).
        delete_tags (list[str] | None): 要删除的标签列表.
        add_tags (list[str] | None): 要添加的标签列表.
        replace_pairs (list[tuple[str, str]] | None): 原位置替换对 ``(原标签, 新标签)``.
        match_mode (str): 应用于删除、替换原标签及 conditional 判定的匹配模式:
            ``exact``(tag_key 精确) | ``regex``(正则搜索) | ``danbooru``(* 通配).
        position (str): ``"before"`` 放置在 ``|||`` 之前, ``"after"`` 之后.
        insert_mode (str): ``"top"`` 置顶 / ``"bottom"`` 置底 / ``"random"`` 随机插入.
        conditional (bool): 仅当文件中已有匹配标签(按 match_mode)时才插入.
        keep_duplicates (bool): 不删除已有重复标签.
        inject_pony_score (bool): 根据美学分注入 score_0…score_9.
        inject_quality_tag (bool): 根据质量分注入质量标签.
        use_separator (str): ||| 分隔模式: "add"(添加) | "keep"(保留) | "remove"(删除).
        inject_year_tag (bool): 按在线来源 meta JSON 的发布时间注入年份标签 ``year YYYY``,
            置于 ``|||`` 之前头部最前(先清理旧年份标签).
        dataset_root (Path | None): 数据集根目录(解析旁路图像路径).
        aesthetic_scores (dict[str, float] | None): 相对路径 → 原始美学分 [0,1].
        quality_scores (dict[str, float] | None): 相对路径 → 质量分 [0,1].
        dry_run (bool): True 仅预览.
        sample_limit (int): 返回样本上限.

    Returns:
        TagCleanResult: 统计与可选样本对比.

    Callers:
        - ``routes/tag_stats.py:batch_edit``
    """
    del_keys, del_pats = classify_patterns(delete_tags or [], match_mode)

    # 替换: exact 用 key→新标签映射; 模式用 pattern→(编译正则, 新标签)
    replace_map: dict[str, str] = {}
    replace_pats: dict[str, tuple[re.Pattern[str], str]] = {}
    for src, dst in (replace_pairs or []):
        dst_t = dst.strip()
        if not dst_t:
            continue
        if match_mode == "regex":
            replace_pats[src] = (re.compile(src), dst_t)
        elif match_mode == "danbooru":
            k = tag_key(src)
            replace_pats[src] = (re.compile(f"^{re.escape(k).replace('\\*', '.*')}$"), dst_t)
        else:  # exact
            replace_map[tag_key(src)] = dst_t

    add_list = expand_cli_list(add_tags or [])
    scores = aesthetic_scores or {}
    q_scores = quality_scores or {}
    root = dataset_root.resolve() if dataset_root is not None else None

    def _edit(raw: str, score_tag: str | None, quality_tag_val: str | None, year_tag: str | None) -> tuple[str, object]:
        """单文件批量编辑闭包, 由 ``_run_batch_edit`` 逐文件调用.

        返回 ``(new, matched_file)``.
        """
        # 整理食材: 拆分; 仅在注入对应维度时移除旧 score/质量标签,
        # 否则保留(除非用户明确指定删除, 由 _transform 处理)
        header, body, _ = split_header_body(raw)
        if inject_pony_score:
            header = [t for t in header if tag_key(t) not in SCORE_TAG_KEYS]
            body = [t for t in body if tag_key(t) not in SCORE_TAG_KEYS]
        if inject_quality_tag:
            header = [t for t in header if tag_key(t) not in QUALITY_TAG_KEYS]
            body = [t for t in body if tag_key(t) not in QUALITY_TAG_KEYS]
        if inject_year_tag:
            header = [t for t in header if not _YEAR_TAG_RE.match(tag_key(t))]
            body = [t for t in body if not _YEAR_TAG_RE.match(tag_key(t))]
        if year_tag:
            # 年份标签置于 ||| 之前头部最前(美学/质量优先标签在重组时仍插于其前)
            header = [year_tag, *header]
        matched_file: dict[str, list[str]] = {}

        def _record(pat: str, k: str) -> None:
            """记录模式命中(仅非精确模式, 本文件去重)."""
            if match_mode != "exact" and k not in matched_file.setdefault(pat, []):
                matched_file[pat].append(k)

        def _transform(t: str) -> str | None:
            """单个标签变换: 命中删除→None; 命中替换→新标签; 否则原样."""
            k = tag_key(t)
            if k in del_keys:
                return None
            for pat, re_ in del_pats.items():
                if re_.search(k):
                    _record(pat, k)
                    return None
            dst = replace_map.get(k)
            if dst is not None:
                return dst
            for pat, (re_, dst_t) in replace_pats.items():
                m = re_.search(k)
                if m is not None:
                    _record(pat, k)
                    return _regex_replace(k, re_, dst_t)
            return t

        # for 生成头部
        header = [x for t in header if (x := _transform(t)) is not None]
        # for 生成尾部
        body = [x for t in body if (x := _transform(t)) is not None]

        # 添加: conditional 时仅当已有匹配标签才加入; 模糊模式补入命中的实际标签
        to_add: list[str] = []
        existing = header + body
        for t in add_list:
            if conditional:
                add_matcher = make_tag_matcher([t], match_mode)
                hits = [x for x in existing if add_matcher(tag_key(x))]
                if not hits:
                    continue
                if match_mode != "exact":
                    for h in hits:
                        _record(t, tag_key(h))
                items = hits if match_mode != "exact" else [t]
            else:
                items = [t]
            for item in items:
                ik = tag_key(item)
                if not keep_duplicates:
                    header = [x for x in header if tag_key(x) != ik]
                    body = [x for x in body if tag_key(x) != ik]
                to_add.append(item)

        # 插入 to_add
        n_added_head = 0
        if to_add:
            if position == "before":
                if insert_mode == "top":
                    header = to_add + header
                    n_added_head = len(to_add)
                elif insert_mode == "random":
                    for t in to_add:
                        header.insert(random.randint(0, len(header)), t)
                else:
                    header = header + to_add
            else:
                if insert_mode == "top":
                    body = to_add + body
                elif insert_mode == "random":
                    for t in to_add:
                        body.insert(random.randint(0, len(body)), t)
                else:
                    body = body + to_add

        new = _compose_caption(
            header, body,
            score_tag=score_tag, quality_tag_val=quality_tag_val,
            use_separator=use_separator, n_added_head=n_added_head,
        )
        return new, matched_file

    st, extras = _run_batch_edit(
        dirs, root=root,
        inject_pony_score=inject_pony_score, scores=scores,
        inject_quality_tag=inject_quality_tag, q_scores=q_scores,
        inject_year_tag=inject_year_tag,
        dry_run=dry_run, sample_limit=sample_limit,
        edit=_edit,
    )

    # 聚合各文件命中到全局(跨文件去重)
    matched_tags: dict[str, list[str]] = {}
    for matched_file in extras:
        if not matched_file:
            continue
        for pat, tags in matched_file.items():
            for k in tags:
                if k not in matched_tags.setdefault(pat, []):
                    matched_tags[pat].append(k)

    return TagCleanResult(
        dry_run=dry_run,
        file_count=len(st.files),
        changed=st.changed,
        unchanged=st.unchanged,
        failed=st.failed,
        samples=st.samples,
        matched_tags=matched_tags,
        inject_pony_score=inject_pony_score,
        pony_injected=st.pony_injected,
        pony_missing=st.pony_missing,
        inject_quality_tag=inject_quality_tag,
        quality_injected=st.quality_injected,
        quality_missing=st.quality_missing,
        inject_year_tag=inject_year_tag,
        year_injected=st.year_injected,
        year_missing=st.year_missing,
    )
