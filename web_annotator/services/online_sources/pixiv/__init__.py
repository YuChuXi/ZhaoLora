"""pixiv 来源子包: 底层网络 (``client``) 与 SourceClient 适配器 (``source``).

Callers:
    - ``services.online_sources.__init__`` (注册)
    - ``routes/online_sources.py`` (图片代理, ``fetch_bytes``)
"""

from services.online_sources.pixiv.client import fetch_bytes
from services.online_sources.pixiv.source import PixivSource

__all__ = ["PixivSource", "fetch_bytes"]