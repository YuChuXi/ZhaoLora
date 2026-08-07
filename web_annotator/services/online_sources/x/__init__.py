"""X(Twitter) 来源子包: SourceClient 适配器 (``source``).

Callers:
    - ``services.online_sources.__init__`` (注册)
"""

from services.online_sources.x.source import XSource

__all__ = ["XSource"]