# encoding: utf-8
from __future__ import annotations

from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.quote_platforms.platforms.taipingyang.quote import TaipingyangQuoteAdapter


class TaipingyangPlatformAdapter(TaipingyangQuoteAdapter):
    async def login(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        username = str(ctx.account_username or "").lower()
        if "fail" in username or "error" in username:
            return PlatformRuntimeResult(status="failed", message="模拟登录失败")
        if "code" in username or ctx.payload.get("login_phone_mask"):
            return PlatformRuntimeResult(
                status="needs_code",
                message="需要短信验证码",
                challenge_type="sms",
                challenge_prompt="请输入太平洋平台短信验证码",
            )
        return PlatformRuntimeResult(status="success", message="模拟登录成功")

    async def submit_challenge(self, ctx: PlatformAccountContext, challenge: str) -> PlatformRuntimeResult:
        if str(challenge or "").strip() == "000000":
            return PlatformRuntimeResult(status="failed", message="模拟验证码校验失败")
        return PlatformRuntimeResult(status="success", message="模拟验证码校验成功")

    async def keepalive(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="success", message="模拟页面保活成功")

    async def check_quota(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="available", message="模拟额度可用")
