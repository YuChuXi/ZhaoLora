"""自然排序键: 数字段按数值比较, 供各处名称/路径排序统一使用. """

from __future__ import annotations

import re

_DIGIT_SPLIT = re.compile(r"(\d+)")


def natural_key(s: str) -> tuple:
    """自然排序键: ``img2_abc10`` → ``(('img'段), (2段), ('_abc'段), (10段))``.

    每段统一为 ``(段类型, 数值, 文本)`` 三元组: 文本段 ``(0, 0, 小写文本)``、
    数字段 ``(1, int数值, '')``, 使任意两个键的对应段均可安全比较,
    规避 int 与 str 直接比较的 TypeError(如 ``001.png`` 与 ``abc.png`` 混排);
    数字段按数值比较, 因此 ``img2`` 排在 ``img10`` 之前.

    Args:
        s (str): 待解析的名称或路径字符串.

    Returns:
        tuple: 逐段三元组组成的元组.

    Callers:
        - ``services/core/scanner.py:collect_images`` / ``scan_images``
        - ``services/core/work_dir.py:pin_main``
        - ``services/core/video_scanner.py:scan_videos``
        - ``services/dataset/layout.py:DatasetLayout.list_part_dirs``
        - ``services/dataset/runtime.py:DatasetRegistry.names``
        - ``services/state/tasks.py:TaskStore.list_entries`` / ``reorder``
        - ``services/state/archiver.py:_list_files``
        - ``services/tagging/stats.py:count_tags``
        - ``services/tagging/clean.py:_collect_txt_files``
        - ``services/analysis/wd14.py:list_image_subdirs``
        - ``services/analysis/birefnet.py:collect_images``
        - ``services/analysis/dedupe.py:collect_paths`` / ``load_records`` / ``find_duplicate_groups``
        - ``services/ready.py:_selected_names``
        - ``routes/crop.py:list_crop_images``
        - ``routes/tasks.py:list_tasks`` / ``list_candidates``
        - ``routes/tag_stats.py:embedding_query``
        - ``routes/online_sources.py:_run_refresh``
        - ``services/online_sources/framework/media.py:regenerate_captions``
    """
    parts = _DIGIT_SPLIT.split(s)
    return tuple(
        (1, int(p), "") if p.isdigit() else (0, 0, p.lower())
        for p in parts
        if p
    )
