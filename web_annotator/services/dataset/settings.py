"""数据集级前端设置存储.

各业务页的输入参数(目录选择、排序、列数、阈值等)按数据集持久化为
``datasets/xxx/.annotator_settings.json``: 顶层键为页面/组件名, 值为任意
JSON 结构; 读取返回全量, 写入按键合并, 同键整体替换.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.core.atomic import atomic_write_text
from services.dataset.layout import SETTINGS_FILENAME


def load_settings(root: Path) -> dict:
    """读取数据集级设置全量.

    Args:
        root (Path): 数据集根目录(``datasets/xxx``).

    Returns:
        dict: 键到任意 JSON 值的映射; 文件不存在时为空映射.

    Raises:
        ValueError: 设置文件顶层不是 JSON 对象.

    Callers:
        - ``services/dataset/settings.py:update_settings``
        - ``routes/settings.py:read_settings``
    """
    path = root / SETTINGS_FILENAME
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"设置文件顶层须为对象: {path}")
    return data


def update_settings(root: Path, patch: dict) -> dict:
    """按键合并写入设置并返回合并后的全量.

    Args:
        root (Path): 数据集根目录(``datasets/xxx``).
        patch (dict): 本次写入的键值映射, 键须为非空字符串, 值须可 JSON 序列化;
            同键整体替换, 未提及的键保持不变.

    Returns:
        dict: 合并后的设置全量.

    Raises:
        ValueError: 键缺失或非字符串.

    Callers:
        - ``routes/settings.py:write_settings``
    """
    for key in patch:
        if not isinstance(key, str) or not key:
            raise ValueError(f"设置键须为非空字符串: {key!r}")
    data = load_settings(root)
    data.update(patch)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(root / SETTINGS_FILENAME, text)
    return data
