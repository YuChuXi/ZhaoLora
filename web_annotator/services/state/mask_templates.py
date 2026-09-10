"""蒙板模板持久化 —— 按数据集存为 ``.mask_templates.json``(图案以 base64 PNG 嵌入 JSON).

模板为标注辅助素材, 与单张图片/蒙板无关, 归属整个数据集, 因此采用与 ``.semantic_sort_cache.json``
一致的数据集根目录点文件约定.

Callers:
    - ``routes/alpha_paint.py:get_templates``
    - ``routes/alpha_paint.py:set_templates``
"""

from __future__ import annotations

import json
from pathlib import Path

from services.core.atomic import atomic_write_text


_TEMPLATES_FILE = ".mask_templates.json"


def templates_path(dataset_root: Path) -> Path:
    """计算数据集模板文件路径.

    Args:
        dataset_root (Path): 数据集根目录.

    Returns:
        Path: ``dataset_root/.mask_templates.json`` 的绝对路径.

    Callers:
        - ``services/state/mask_templates.py:load_templates``
        - ``services/state/mask_templates.py:save_templates``
    """
    return dataset_root / _TEMPLATES_FILE


def load_templates(dataset_root: Path) -> list[dict]:
    """读取数据集模板; 文件不存在或字段缺失时返回空列表.

    Args:
        dataset_root (Path): 数据集根目录.

    Returns:
        list[dict]: 模板项列表(含 slot/w/h/value/image_b64), 无模板时为空.

    Raises:
        OSError: 文件存在但无法读取.
        JSONDecodeError: 文件已损坏(非合法 JSON).

    Callers:
        - ``routes/alpha_paint.py:get_templates``
    """
    p = templates_path(dataset_root)
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    templates = data.get("templates", [])
    return templates if isinstance(templates, list) else []


def save_templates(dataset_root: Path, templates: list[dict]) -> None:
    """原子写入数据集模板(先写临时文件再替换, 避免中断产生半成品).

    Args:
        dataset_root (Path): 数据集根目录.
        templates (list[dict]): 模板项全量列表(≤10).

    Raises:
        OSError: 无法写入.

    Callers:
        - ``routes/alpha_paint.py:set_templates``
    """
    p = templates_path(dataset_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps({"templates": templates}, ensure_ascii=False))