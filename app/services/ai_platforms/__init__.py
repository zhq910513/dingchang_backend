# app/services/ai_platforms/__init__.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Dict, List, Optional

from app.services.ai_platforms.base import AiPlatformAdapter, StubPlatformAdapter


# ✅ 注册中心：后续每接一个平台，只要在这里 register
_REGISTRY: Dict[str, AiPlatformAdapter] = {}


def register_adapter(adapter: AiPlatformAdapter) -> None:
    code = (adapter.platform_code or "").strip()
    if not code:
        return
    _REGISTRY[code] = adapter


def get_adapter(platform_code: str) -> Optional[AiPlatformAdapter]:
    code = (platform_code or "").strip()
    if not code:
        return None
    return _REGISTRY.get(code)


def list_adapters() -> List[AiPlatformAdapter]:
    return list(_REGISTRY.values())


def list_enabled_adapters() -> List[AiPlatformAdapter]:
    out: List[AiPlatformAdapter] = []
    for a in _REGISTRY.values():
        try:
            if a.enabled():
                out.append(a)
        except Exception:
            continue
    return out


# ✅ 默认注册一个 STUB（用于公共入口先跑通）
register_adapter(StubPlatformAdapter())


__all__ = [
    "AiPlatformAdapter",
    "register_adapter",
    "get_adapter",
    "list_adapters",
    "list_enabled_adapters",
]