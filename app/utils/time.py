# encoding: utf-8
"""
@author: The King
@project: dingchang_backend
@file: time.py
@time: 2025/12/8 22:37
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    # 统一返回 naive UTC 时间（用于 DB）
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return utcnow().strftime(fmt)
