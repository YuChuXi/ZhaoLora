"""Gelbooru 来源子包: 底层客户端 (``client``) 与 SourceClient 适配器 (``source``).

Callers:
    - ``services.online_sources.__init__`` (注册)
"""

from services.online_sources.gelbooru.source.source.source import GelbooruSource

__all__ = ["GelbooruSource"]