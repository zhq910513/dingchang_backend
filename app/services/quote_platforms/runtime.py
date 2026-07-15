# encoding: utf-8
from __future__ import annotations

from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.quote_platforms.registry import get_quote_platform_adapter


async def login(ctx: PlatformAccountContext) -> PlatformRuntimeResult:
    return await get_quote_platform_adapter(ctx.platform_code).login(ctx)


async def submit_challenge(ctx: PlatformAccountContext, challenge: str) -> PlatformRuntimeResult:
    return await get_quote_platform_adapter(ctx.platform_code).submit_challenge(ctx, challenge)


async def keepalive(ctx: PlatformAccountContext) -> PlatformRuntimeResult:
    return await get_quote_platform_adapter(ctx.platform_code).keepalive(ctx)


async def check_quota(ctx: PlatformAccountContext) -> PlatformRuntimeResult:
    return await get_quote_platform_adapter(ctx.platform_code).check_quota(ctx)


async def quote(ctx: PlatformAccountContext, quote_payload):
    return await get_quote_platform_adapter(ctx.platform_code).quote(ctx, quote_payload)
