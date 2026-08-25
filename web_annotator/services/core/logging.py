"""集中式日志体系 —— 统一格式化、级别、处理器与终端着色.

整个应用(含业务日志、错误日志、访问日志)的全部输出统一经标准库
``logging.getLogger(__name__)`` 获取 logger。本模块只在应用启动时由
``server.py:main`` 调用一次 ``setup_logging``, 为根日志器装配统一的输出格式与
处理器, 并接管 uvicorn 内置日志器:

- **标准输出/错误输出**: 交互式终端(``isatty`` 判定)下按**来源着色**——以
  稳定 hash 给不同 logger 名(访问日志 / 长任务 / 模型设备 / 各业务模块)分配
  不同颜色, 便于区分是哪个子系统在输出; 告警级别(WARNING 黄 / ERROR 红 /
  CRITICAL 红底白字)固定覆盖为醒目色, 不会被大量 info 淹没。重定向到文件或
  管道时不加 ANSI 转义, 避免污染文本。
- **日志文件**: 无色, 写入 ``--data-dir/logs`` 下, 超过 ``_FILE_MAX_BYTES``
  自动按大小滚动归档, 保留最近 ``_FILE_BACKUP_COUNT`` 份。
"""

from __future__ import annotations

import logging
import zlib
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 日志行格式: 时间 | 级别 | logger 名 | 消息 (无色)
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# 滚动日志文件名与归档策略
_FILE_NAME = "web_annotator.log"
_FILE_MAX_BYTES = 10 * 1024 * 1024
_FILE_BACKUP_COUNT = 5
# 级别名的显示宽度(与无色格式 ``-7s`` 对齐)
_LEVEL_WIDTH = 7

# 日志级别名到标准库级别常量的映射
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# 告警级别固定配色, 覆盖来源色, 保证醒目.
_LEVEL_OVERRIDE = {
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[41;97m",
}

# 常规日志(DEBUG/INFO)的来源色板: 高对比、深色终端下可读的前景色.
_MODULE_COLORS = [
    "\x1b[32m",  # 绿
    "\x1b[36m",  # 青
    "\x1b[34m",  # 蓝
    "\x1b[35m",  # 品红
    "\x1b[92m",  # 亮绿
    "\x1b[96m",  # 亮青
    "\x1b[94m",  # 亮蓝
    "\x1b[95m",  # 亮品红
]


def _module_color(name: str) -> str:
    """由 logger 名稳定派生来源色.

    用 ``zlib.crc32`` 而非内建 ``hash``, 避免 ``PYTHONHASHSEED`` 随机化导致
    每次进程启动同一 logger 颜色漂移.

    Args:
        name: 日志来源 logger 名.

    Returns:
        str: 色板中的 ANSI 前景色码.

    Callers:
        - ``services/core/logging.py:_ColorFormatter.format``
    """
    return _MODULE_COLORS[zlib.crc32(name.encode("utf-8")) % len(_MODULE_COLORS)]


class _ColorFormatter(logging.Formatter):
    """仅用于交互式终端的着色格式化器.

    对整条日志着色: 告警级别(WARNING/ERROR/CRITICAL)用固定醒目色覆盖; 其余
    级别按日志来源(logger 名)稳定着色, 使不同子系统(访问日志 / 长任务 / 模型
    设备 / 业务模块)在终端中呈现不同色块。异常回溯文本保持无色, 便于阅读。
    """

    _RESET = "\x1b[0m"

    def __init__(self, datefmt: str) -> None:
        """构造基于时间格式的着色格式化器.

        Args:
            datefmt: 时间戳格式串, 与无色格式化器保持一致.

        Returns:
            None

        Callers:
            - ``services/core/logging.py:setup_logging``
        """
        super().__init__(_LOG_FORMAT, datefmt)

    def format(self, record: logging.LogRecord) -> str:
        """按来源/级别着色并格式化整条日志.

        Args:
            record: 待格式化的日志记录.

        Returns:
            str: 着色后的日志文本, 含异常回溯时追加其文本(保持无色).

        Callers:
            - ``logging`` 内部由 Handler 调用
        """
        date = self.formatTime(record, self.datefmt)
        level = record.levelname
        color = _LEVEL_OVERRIDE.get(level) or _module_color(record.name)
        body = f"{date} | {level:<{_LEVEL_WIDTH}} | {record.name} | {record.getMessage()}"
        text = f"{color}{body}{self._RESET}"
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            text = f"{text}\n{record.exc_text}"
        return text


def setup_logging(
    log_level: str,
    log_dir: Path | None = None,
) -> None:
    """配置全局根日志器, 统一格式 / 级别 / 处理器.

    为标准输出与(可选)滚动文件安装处理器, 终端按来源着色、文件无色, 并将
    uvicorn 内部日志器交由根日志器接管。可重复调用; 每次调用会先清理根日志器
    既有处理器以保证幂等。

    Args:
        log_level: 日志级别名, 限 ``debug`` / ``info`` / ``warning`` / ``error``.
        log_dir: 日志目录; 提供时在 ``log_dir/web_annotator.log`` 写入滚动文件,
            为 ``None`` 时仅输出到标准输出/错误输出.

    Returns:
        None.

    Raises:
        ValueError: ``log_level`` 不在 ``_LEVELS`` 中.

    Callers:
        - ``server.py:main``
    """
    if log_level.lower() not in _LEVELS:
        raise ValueError(f"未知日志级别: {log_level}")

    root = logging.getLogger()
    # 幂等清理, 避免重复调用时叠加处理器.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # 终端处理器: 交互式终端着色, 重定向时无色.
    stream = logging.StreamHandler()
    color = bool(
        getattr(stream, "stream", None)
        and getattr(stream.stream, "isatty", lambda: False)()
    )
    stream.setFormatter(
        _ColorFormatter(_DATE_FORMAT) if color else logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)
    )
    root.addHandler(stream)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / _FILE_NAME,
            maxBytes=_FILE_MAX_BYTES,
            backupCount=_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        # 日志文件保持无色, 便于用文本工具直接检索.
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
        root.addHandler(file_handler)

    root.setLevel(_LEVELS[log_level.lower()])

    # 接管 uvicorn 内置日志器, 交由根日志器统一格式化.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.asgi"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True