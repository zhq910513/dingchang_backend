# app/utils/time.py
# encoding: utf-8
"""
时间工具（全局口径：北京时间 Asia/Shanghai，返回 naive datetime）

说明：
- DB 存 naive DATETIME（北京时间）
- 业务侧统一用北京时间，避免 UTC/时区换算导致前端展示偏移
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_BJ_TZ = ZoneInfo("Asia/Shanghai")


def now_bj() -> datetime:
    """返回北京时间 naive datetime（tzinfo=None）"""
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return now_bj().strftime(fmt)
