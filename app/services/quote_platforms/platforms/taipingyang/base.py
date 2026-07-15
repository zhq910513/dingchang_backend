# encoding: utf-8
from __future__ import annotations

from app.services.quote_platforms.base import QuotePlatformAdapter


class TaipingyangBaseAdapter(QuotePlatformAdapter):
    platform_code = "TP"
    platform_name = "太平洋"
