"""前端设置存储(全局缺省层与数据集覆盖层共用).

各业务页的输入参数按页面/组件名持久化为 ``.annotator_settings.json``:
全局缺省层在任务根 ``datasets/`` 下(跨任务通用的参数习惯), 数据集层在
``datasets/xxx/`` 下(任务特有的目录选择、描述规则、裁剪部位). 顶层键为
页面/组件名, 值为任意 JSON 结构; 读取返回单层全量, 写入按键合并, 同键
整体替换; 两层按键合并后的生效值由 ``routes/settings.py`` 返回.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.core.atomic import atomic_write_text
from services.dataset.layout import SETTINGS_FILENAME


def load_settings(root: Path) -> dict:
    """读取单层设置全量.

    Args:
        root (Path): 设置文件所在根目录(全局层为任务根 ``datasets/``,
            数据集层为 ``datasets/xxx``).

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
    """按键合并写入单层设置并返回合并后的全量.

    Args:
        root (Path): 设置文件所在根目录(全局层为任务根 ``datasets/``,
            数据集层为 ``datasets/xxx``).
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
