# encoding: utf-8
from __future__ import annotations

from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.quote_platforms.platforms.taipingyang.business import TaipingyangBusinessAdapter


class TaipingyangPlatformAdapter(TaipingyangBusinessAdapter):
    async def login(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return self._not_implemented_result("登录")

    async def submit_challenge(self, ctx: PlatformAccountContext, challenge: str) -> PlatformRuntimeResult:
        return self._not_implemented_result("验证码校验")

    async def keepalive(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return self._not_implemented_result("保活")

    async def check_quota(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return self._not_implemented_result("额度检查")
