# encoding: utf-8
from __future__ import annotations

from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.quote_platforms.session_manager import session_manager


async def _with_db(
    db: AsyncSession | None,
    fn: Callable[[AsyncSession], Awaitable[PlatformRuntimeResult]],
) -> PlatformRuntimeResult:
    if db is not None:
        return await fn(db)
    async with async_session_factory() as session:
        try:
            result = await fn(session)
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise


async def login(ctx: PlatformAccountContext, db: AsyncSession | None = None) -> PlatformRuntimeResult:
    return await _with_db(db, lambda session: session_manager.login(session, ctx))


async def submit_challenge(ctx: PlatformAccountContext, challenge: str, db: AsyncSession | None = None) -> PlatformRuntimeResult:
    return await _with_db(db, lambda session: session_manager.submit_challenge(session, ctx, challenge))


async def keepalive(ctx: PlatformAccountContext, db: AsyncSession | None = None) -> PlatformRuntimeResult:
    return await _with_db(db, lambda session: session_manager.keepalive(session, ctx))


async def check_quota(ctx: PlatformAccountContext, db: AsyncSession | None = None) -> PlatformRuntimeResult:
    return await _with_db(db, lambda session: session_manager.check_quota(session, ctx))


async def quote(ctx: PlatformAccountContext, quote_payload: dict[str, Any], db: AsyncSession | None = None) -> PlatformRuntimeResult:
    return await _with_db(db, lambda session: session_manager.quote(session, ctx, quote_payload))


async def query_renewal(
    ctx: PlatformAccountContext,
    quote_payload: dict[str, Any],
    db: AsyncSession | None = None,
) -> PlatformRuntimeResult:
    return await _with_db(db, lambda session: session_manager.query_renewal(session, ctx, quote_payload))


async def query_joint_sales_plan(
    ctx: PlatformAccountContext,
    quote_payload: dict[str, Any],
    db: AsyncSession | None = None,
) -> PlatformRuntimeResult:
    return await _with_db(db, lambda session: session_manager.query_joint_sales_plan(session, ctx, quote_payload))


async def query_repair_codes(
    ctx: PlatformAccountContext,
    quote_payload: dict[str, Any],
    db: AsyncSession | None = None,
) -> PlatformRuntimeResult:
    return await _with_db(db, lambda session: session_manager.query_repair_codes(session, ctx, quote_payload))
