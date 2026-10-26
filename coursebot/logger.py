"""统一日志。"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_CONFIGURED = False


def setup_logger(level: str = "INFO", logfile: str | None = None):
    global _CONFIGURED
    if _CONFIGURED:
        return logger
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{message}</cyan>",
        colorize=True,
    )
    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        logger.add(logfile, level="DEBUG", rotation="5 MB", retention=5,
                   encoding="utf-8", enqueue=True)
    _CONFIGURED = True
    return logger


log = logger
