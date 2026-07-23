# encoding: utf-8
from __future__ import annotations

from typing import Any, Dict

from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.quote_platforms.platforms.taipingyang.base import TaipingyangBaseAdapter


class TaipingyangBusinessAdapter(TaipingyangBaseAdapter):
    async def quote(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="success", message="模拟报价成功", data={"mode": "stub", "payload": quote_payload})
