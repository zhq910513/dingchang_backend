# encoding: utf-8
from __future__ import annotations

from typing import Dict, Type

from app.services.quote_platforms.base import QuotePlatformAdapter
from app.services.quote_platforms.platforms.picc import PiccPlatformAdapter
from app.services.quote_platforms.platforms.taipingyang.login import TaipingyangPlatformAdapter


_REGISTRY: Dict[str, Type[QuotePlatformAdapter]] = {
    "PICC": PiccPlatformAdapter,
    "TP": TaipingyangPlatformAdapter,
}


def get_quote_platform_adapter(platform_code: str) -> QuotePlatformAdapter:
    cls = _REGISTRY.get(str(platform_code or "").strip().upper(), QuotePlatformAdapter)
    return cls()
