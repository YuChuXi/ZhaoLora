"""集中式日志体系 —— 统一格式化、级别、处理器与终端着色.

整个应用(含业务日志、错误日志、访问日志)的全部输出统一经标准库
``logging.getLogger(__name__)`` 获取 logger。本模块只在应用启动时由
``server.py:main`` 调用一次 ``setup_logging``, 为根日志器装配统一的输出格式与
处理器, 并接管 uvicorn 内置日志器:

- **标准输出/错误输出**: 交互式终端(``isatty`` 判定)下按**字段着色**——时间戳与
  ``" | "`` 分隔符固定灰, 日志级别名(DEBUG/INFO/WARNING/ERROR/CRITICAL)用各自专属色,
  来源名按 "." 点分段独立着色以呈现来源层级(访问日志 / 长任务 / 模型设备 / 各业务
  模块), 便于在终端区分是哪个子系统在输出。重定向到文件或管道时不加 ANSI 转义,
  避免污染文本。
- **日志文件**: 无色, 写入 ``--data-dir/logs`` 下, 超过 ``_FILE_MAX_BYTES``
  自动按大小滚动归档, 保留最近 ``_FILE_BACKUP_COUNT`` 份。
"""

from __future__ import annotations

import logging
import zlib
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 日志行格式: 时间 | 级别 | logger 名 | 消息 (无色)
_LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# 滚动日志文件名与归档策略
_FILE_NAME = "web_annotator.log"
_FILE_MAX_BYTES = 10 * 1024 * 1024
_FILE_BACKUP_COUNT = 5
# 级别名的显示宽度(与无色格式 ``-5s`` 对齐)
_LEVEL_WIDTH = 5

# 日志级别名到标准库级别常量的映射
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.FATAL,
}

_RESET = "\x1b[0m"
# 时间戳与 " | " 分隔符的固定色(灰), 使行内字段层次稳定可辨.
_FIXED_COLOR = "\x1b[38;5;244m"
# 各日志级别专属色, 深色终端下可读、彼此可辨.
_LEVEL_COLORS = {
    "DEBUG": "\x1b[38;5;245m",
    "INFO": "\x1b[38;5;40m",
    "WARN": "\x1b[38;5;214m",
    "ERROR": "\x1b[31m",
    "FATAL": "\x1b[41;97m",
}

# 常规日志(DEBUG/INFO)的来源色池: 取 xterm-256 中彼此可区分的前景色, 数量足以覆盖全部日志来源的点分段.
_MODULE_COLORS = [
    f"\x1b[38;5;{i}m"
    for i in (
        198, 46, 226, 33, 201, 45, 214, 118, 141, 87,
        178, 70, 205, 51, 172, 99, 130, 79, 167, 111,
        136, 75, 150, 116, 161, 34, 220, 27, 200, 80,
        128, 23,
    )
]


def _source_color(name: str) -> str:
    """为点分段来源名渲染多段彩色文本.

    按 "." 把 logger 名切成若干段, 每段独立着色以呈现来源层级(如 ``web_annotator.access``
    的 ``web_annotator`` 与 ``access`` 两段颜色不同), 段间"."用固定色.

    Args:
        name: 日志来源 logger 名.

    Returns:
        str: 各点分段独立着色、段间以固定色"."分隔的彩色字符串.

    Callers:
        - ``services/core/logging.py:_ColorFormatter.format``
    """
    out: list[str] = []
    for i, seg in enumerate(name.split(".")):
        if i:
            out.append(f"{_FIXED_COLOR}.{_RESET}")
        idx = zlib.crc32(seg.encode("utf-8")) % len(_MODULE_COLORS)
        out.append(f"{_MODULE_COLORS[idx]}{seg}{_RESET}")
    return "".join(out)


class _ColorFormatter(logging.Formatter):
    """仅用于交互式终端的着色格式化器.

    按字段分别着色: 时间戳与 ``" | "`` 分隔符固定灰; 级别名按各自专属色;
    来源名按 "." 点分段独立着色以呈现来源层级(访问日志 / 长任务 / 模型设备 /
    各业务模块); 消息保持无色。异常回溯文本保持无色, 便于阅读。
    """

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
        """按字段着色并格式化整条日志.

        Args:
            record: 待格式化的日志记录.

        Returns:
            str: 字段着色后的日志文本, 含异常回溯时追加其文本(保持无色).

        Callers:
            - ``logging`` 内部由 Handler 调用
        """
        date = self.formatTime(record, self.datefmt)
        level_color = _LEVEL_COLORS.get(record.levelname, _LEVEL_COLORS["INFO"])
        sep = f"{_FIXED_COLOR} | {_RESET}"
        body = (
            f"{_FIXED_COLOR}{date}{_RESET}{sep}"
            f"{level_color}{record.levelname:<{_LEVEL_WIDTH}}{_RESET}{sep}"
            f"{_source_color(record.name)}{sep}{record.getMessage()}"
        )
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            body = f"{body}\n{record.exc_text}"
        return body


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