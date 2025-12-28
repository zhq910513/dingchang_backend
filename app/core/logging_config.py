# app/core/logging_config.py
# encoding: utf-8
"""
@author: The King
@project: dingchang_backend
@file: logging_config.py
@time: 2025/12/8 22:28
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings


class BeijingFormatter(logging.Formatter):
    """
    ✅ 统一日志时间为北京时间（Asia/Shanghai）
    并且按项目约定输出：%Y-%m-%d %H:%M:%S
    """

    def formatTime(self, record, datefmt=None):  # noqa: N802
        dt = datetime.fromtimestamp(record.created, tz=ZoneInfo("Asia/Shanghai"))
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def setup_logging():
    root_logger = logging.getLogger()

    # 避免重复添加 handler（热重载/多次初始化）
    if root_logger.handlers:
        for h in list(root_logger.handlers):
            root_logger.removeHandler(h)

    level = getattr(logging, str(getattr(settings, "LOG_LEVEL", "INFO")).upper(), logging.INFO)
    root_logger.setLevel(level)

    # 确保日志目录存在
    os.makedirs("logs", exist_ok=True)

    # ✅ 统一格式 + 统一北京时间
    fmt = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = BeijingFormatter(fmt=fmt, datefmt=datefmt)

    # 控制台日志
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    # 滚动文件日志（总量约 50MB）
    file_handler = RotatingFileHandler(
        filename="logs/app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # ✅ 关键：让 uvicorn 的日志也走 root handler（避免“有的日志不见了/格式不同”）
    # 注意：这里不再依赖 uvicorn 自己的 handlers
    uvicorn_levels = {
        "uvicorn": logging.INFO,
        "uvicorn.error": logging.INFO,
        "uvicorn.access": logging.INFO,
    }
    for name, lv in uvicorn_levels.items():
        lgr = logging.getLogger(name)
        lgr.setLevel(lv)
        lgr.handlers = []
        lgr.propagate = True

    # （可选）把 SQLAlchemy engine 的 SQL 打印压到 WARNING，避免刷屏
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
