# encoding: utf-8
from __future__ import annotations

from typing import Any, Dict

from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.quote_platforms.platforms.taipingyang.base import TaipingyangBaseAdapter


class TaipingyangBusinessAdapter(TaipingyangBaseAdapter):
    async def quote(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(
            status="failed",
            message="太平洋报价流程尚未接入真实平台接口",
            data={"error_code": "platform_quote_not_implemented"},
        )
