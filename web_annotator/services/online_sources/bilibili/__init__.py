"""Bilibili 来源: 底层动态拉取与 SourceClient 适配器.

本子包聚合 B 站图源的全部实现: ``client``(底层动态拉取/图片代理)、
``models``(动态数据模型)、``source``(实现 ``SourceClient`` 的适配器)。

Callers:
    - ``services/online_sources/__init__.py:`` (注册)
    - ``routes/online_sources.py:`` (图片代理)
"""

from services.online_sources.bilibili.client import (
    fetch_bili_bytes,
    fetch_dynamics,
    resolve_image,
)
from services.online_sources.bilibili.models import DynamicRecord
from services.online_sources.bilibili.source import BilibiliSource

__all__ = [
    "BilibiliSource",
    "DynamicRecord",
    "fetch_bili_bytes",
    "fetch_dynamics",
    "resolve_image",
]
