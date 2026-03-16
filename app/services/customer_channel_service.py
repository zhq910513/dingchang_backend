# app/services/customer_channel_service.py
# encoding: utf-8
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.models.channel_group import ChannelGroup
from app.models.customer_group import CustomerGroup
from app.models.user import User

_BJ = ZoneInfo("Asia/Shanghai")


def _normalize_text(v: Optional[str]) -> str:
    return str(v or "").strip()


def _normalize_page(page: int) -> int:
    return max(int(page or 1), 1)


def _normalize_page_size(page_size: int) -> int:
    size = int(page_size or 20)
    if size <= 0:
        size = 20
    return min(size, 100)


def _now_bj_naive() -> datetime:
    return datetime.now(_BJ).replace(tzinfo=None)


def _display_name_expr(user_alias):
    return func.coalesce(
        func.nullif(func.trim(user_alias.real_name), ""),
        func.nullif(func.trim(user_alias.username), ""),
        "",
    )


def _friendly_integrity_error(exc: IntegrityError, default_msg: str) -> str:
    msg = str(getattr(exc, "orig", exc) or "").lower()
    if "duplicate" in msg or "unique" in msg:
        return "编码已存在或唯一约束冲突"
    if "cannot be null" in msg or "not null" in msg:
        return "存在必填字段为空"
    if "foreign key" in msg:
        return "关联数据不存在或已失效"
    return default_msg


async def _safe_commit(db: AsyncSession, default_msg: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(_friendly_integrity_error(exc, default_msg)) from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        raise ValueError(default_msg) from exc


# =========================
# 下拉 list（轻量）
# =========================

async def list_customer_groups(
    *,
    db: AsyncSession,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    page = _normalize_page(page)
    page_size = _normalize_page_size(page_size)
    offset = (page - 1) * page_size

    base_stmt = (
        select(
            CustomerGroup.id.label("id"),
            CustomerGroup.customer_code.label("customer_code"),
            CustomerGroup.customer_name.label("customer_name"),
        )
        .where(CustomerGroup.is_deleted == 0)
    )
    count_stmt = select(func.count(CustomerGroup.id)).where(CustomerGroup.is_deleted == 0)

    kw = _normalize_text(keyword)
    if kw:
        like = f"%{kw}%"
        cond = or_(
            CustomerGroup.customer_name.like(like),
            CustomerGroup.customer_code.like(like),
        )
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    base_stmt = (
        base_stmt
        .order_by(CustomerGroup.customer_name.asc(), CustomerGroup.id.asc())
        .offset(offset)
        .limit(page_size)
    )

    total = int((await db.execute(count_stmt)).scalar() or 0)
    items = list((await db.execute(base_stmt)).mappings().all())

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": (offset + len(items)) < total,
    }


async def list_channel_groups(
    *,
    db: AsyncSession,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    page = _normalize_page(page)
    page_size = _normalize_page_size(page_size)
    offset = (page - 1) * page_size

    base_stmt = (
        select(
            ChannelGroup.id.label("id"),
            ChannelGroup.channel_code.label("channel_code"),
            ChannelGroup.channel_name.label("channel_name"),
        )
        .where(ChannelGroup.is_deleted == 0)
    )
    count_stmt = select(func.count(ChannelGroup.id)).where(ChannelGroup.is_deleted == 0)

    kw = _normalize_text(keyword)
    if kw:
        like = f"%{kw}%"
        cond = or_(
            ChannelGroup.channel_name.like(like),
            ChannelGroup.channel_code.like(like),
        )
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    base_stmt = (
        base_stmt
        .order_by(ChannelGroup.channel_name.asc(), ChannelGroup.id.asc())
        .offset(offset)
        .limit(page_size)
    )

    total = int((await db.execute(count_stmt)).scalar() or 0)
    items = list((await db.execute(base_stmt)).mappings().all())

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": (offset + len(items)) < total,
    }


# =========================
# 管理页 list（完整）
# =========================

async def list_customer_groups_manage(
    *,
    db: AsyncSession,
    customer_code: Optional[str] = None,
    customer_name: Optional[str] = None,
    market: Optional[str] = None,
    region: Optional[str] = None,
    created_by_name: Optional[str] = None,
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    page = _normalize_page(page)
    page_size = _normalize_page_size(page_size)
    offset = (page - 1) * page_size

    creator = aliased(User)
    created_name_expr = _display_name_expr(creator)

    base_stmt = (
        select(
            CustomerGroup.id.label("id"),
            CustomerGroup.customer_code.label("customer_code"),
            CustomerGroup.customer_name.label("customer_name"),
            CustomerGroup.market.label("market"),
            CustomerGroup.region.label("region"),
            CustomerGroup.contacts.label("contacts"),
            CustomerGroup.created_by.label("created_by"),
            created_name_expr.label("created_by_name"),
            CustomerGroup.created_at.label("created_at"),
            CustomerGroup.updated_at.label("updated_at"),
            CustomerGroup.deleted_at.label("deleted_at"),
            CustomerGroup.is_deleted.label("is_deleted"),
        )
        .select_from(CustomerGroup)
        .outerjoin(creator, creator.id == CustomerGroup.created_by)
    )

    count_stmt = select(func.count(CustomerGroup.id)).select_from(CustomerGroup)

    if not include_deleted:
        cond = CustomerGroup.is_deleted == 0
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    cc = _normalize_text(customer_code)
    if cc:
        cond = CustomerGroup.customer_code.like(f"%{cc}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    cn = _normalize_text(customer_name)
    if cn:
        cond = CustomerGroup.customer_name.like(f"%{cn}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    mk = _normalize_text(market)
    if mk:
        cond = CustomerGroup.market.like(f"%{mk}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    rg = _normalize_text(region)
    if rg:
        cond = CustomerGroup.region.like(f"%{rg}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    cbn = _normalize_text(created_by_name)
    if cbn:
        count_stmt = count_stmt.outerjoin(creator, creator.id == CustomerGroup.created_by)
        cond = cast(created_name_expr, String).like(f"%{cbn}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    base_stmt = (
        base_stmt
        .order_by(CustomerGroup.customer_name.asc(), CustomerGroup.id.asc())
        .offset(offset)
        .limit(page_size)
    )

    total = int((await db.execute(count_stmt)).scalar() or 0)
    items = list((await db.execute(base_stmt)).mappings().all())

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": (offset + len(items)) < total,
    }


async def list_channel_groups_manage(
    *,
    db: AsyncSession,
    channel_code: Optional[str] = None,
    channel_name: Optional[str] = None,
    region: Optional[str] = None,
    created_by_name: Optional[str] = None,
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    page = _normalize_page(page)
    page_size = _normalize_page_size(page_size)
    offset = (page - 1) * page_size

    creator = aliased(User)
    created_name_expr = _display_name_expr(creator)

    base_stmt = (
        select(
            ChannelGroup.id.label("id"),
            ChannelGroup.channel_code.label("channel_code"),
            ChannelGroup.channel_name.label("channel_name"),
            ChannelGroup.region.label("region"),
            ChannelGroup.contacts.label("contacts"),
            ChannelGroup.created_by.label("created_by"),
            created_name_expr.label("created_by_name"),
            ChannelGroup.created_at.label("created_at"),
            ChannelGroup.updated_at.label("updated_at"),
            ChannelGroup.deleted_at.label("deleted_at"),
            ChannelGroup.is_deleted.label("is_deleted"),
        )
        .select_from(ChannelGroup)
        .outerjoin(creator, creator.id == ChannelGroup.created_by)
    )

    count_stmt = select(func.count(ChannelGroup.id)).select_from(ChannelGroup)

    if not include_deleted:
        cond = ChannelGroup.is_deleted == 0
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    cc = _normalize_text(channel_code)
    if cc:
        cond = ChannelGroup.channel_code.like(f"%{cc}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    cn = _normalize_text(channel_name)
    if cn:
        cond = ChannelGroup.channel_name.like(f"%{cn}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    rg = _normalize_text(region)
    if rg:
        cond = ChannelGroup.region.like(f"%{rg}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    cbn = _normalize_text(created_by_name)
    if cbn:
        count_stmt = count_stmt.outerjoin(creator, creator.id == ChannelGroup.created_by)
        cond = cast(created_name_expr, String).like(f"%{cbn}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    base_stmt = (
        base_stmt
        .order_by(ChannelGroup.channel_name.asc(), ChannelGroup.id.asc())
        .offset(offset)
        .limit(page_size)
    )

    total = int((await db.execute(count_stmt)).scalar() or 0)
    items = list((await db.execute(base_stmt)).mappings().all())

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": (offset + len(items)) < total,
    }


# =========================
# get one（显式预加载 creator，避免 Async 懒加载 500）
# =========================

async def get_customer_group_by_id(
    *,
    db: AsyncSession,
    group_id: int,
    with_creator: bool = False,
) -> Optional[CustomerGroup]:
    gid = int(group_id)
    if not with_creator:
        return await db.get(CustomerGroup, gid)

    stmt = (
        select(CustomerGroup)
        .options(selectinload(CustomerGroup.creator))
        .where(CustomerGroup.id == gid)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_channel_group_by_id(
    *,
    db: AsyncSession,
    group_id: int,
    with_creator: bool = False,
) -> Optional[ChannelGroup]:
    gid = int(group_id)
    if not with_creator:
        return await db.get(ChannelGroup, gid)

    stmt = (
        select(ChannelGroup)
        .options(selectinload(ChannelGroup.creator))
        .where(ChannelGroup.id == gid)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


# =========================
# create
# =========================

async def create_customer_group(
    *,
    db: AsyncSession,
    customer_code: str,
    customer_name: str,
    team_name: Optional[str],
    market: Optional[str],
    region: Optional[str],
    contacts: list,
    created_by: Optional[int],
) -> CustomerGroup:
    now = _now_bj_naive()
    row = CustomerGroup(
        team_name=_normalize_text(team_name) or None,
        customer_code=_normalize_text(customer_code),
        customer_name=_normalize_text(customer_name),
        market=_normalize_text(market) or None,
        region=_normalize_text(region) or None,
        contacts=contacts or [],
        created_by=created_by,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    db.add(row)
    await _safe_commit(db, "创建客户失败")
    return await get_customer_group_by_id(db=db, group_id=row.id, with_creator=True)


async def create_channel_group(
    *,
    db: AsyncSession,
    channel_code: str,
    channel_name: str,
    team_name: Optional[str],
    region: Optional[str],
    contacts: list,
    created_by: Optional[int],
) -> ChannelGroup:
    now = _now_bj_naive()
    row = ChannelGroup(
        team_name=_normalize_text(team_name) or None,
        channel_code=_normalize_text(channel_code),
        channel_name=_normalize_text(channel_name),
        region=_normalize_text(region) or None,
        contacts=contacts or [],
        created_by=created_by,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    db.add(row)
    await _safe_commit(db, "创建渠道失败")
    return await get_channel_group_by_id(db=db, group_id=row.id, with_creator=True)


# =========================
# update
# =========================

async def update_customer_group(
    *,
    db: AsyncSession,
    row: CustomerGroup,
    customer_code: str,
    customer_name: str,
    market: Optional[str],
    region: Optional[str],
    contacts: list,
) -> CustomerGroup:
    row.customer_code = _normalize_text(customer_code)
    row.customer_name = _normalize_text(customer_name)
    row.market = _normalize_text(market) or None
    row.region = _normalize_text(region) or None
    row.contacts = contacts or []
    row.updated_at = _now_bj_naive()

    await _safe_commit(db, "编辑客户失败")
    return await get_customer_group_by_id(db=db, group_id=row.id, with_creator=True)


async def update_channel_group(
    *,
    db: AsyncSession,
    row: ChannelGroup,
    channel_code: str,
    channel_name: str,
    region: Optional[str],
    contacts: list,
) -> ChannelGroup:
    row.channel_code = _normalize_text(channel_code)
    row.channel_name = _normalize_text(channel_name)
    row.region = _normalize_text(region) or None
    row.contacts = contacts or []
    row.updated_at = _now_bj_naive()

    await _safe_commit(db, "编辑渠道失败")
    return await get_channel_group_by_id(db=db, group_id=row.id, with_creator=True)


# =========================
# soft delete
# =========================

async def soft_delete_customer_group(*, db: AsyncSession, row: CustomerGroup) -> CustomerGroup:
    now = _now_bj_naive()
    row.deleted_at = now
    row.updated_at = now
    await _safe_commit(db, "删除客户失败")
    return await get_customer_group_by_id(db=db, group_id=row.id, with_creator=True)


async def soft_delete_channel_group(*, db: AsyncSession, row: ChannelGroup) -> ChannelGroup:
    now = _now_bj_naive()
    row.deleted_at = now
    row.updated_at = now
    await _safe_commit(db, "删除渠道失败")
    return await get_channel_group_by_id(db=db, group_id=row.id, with_creator=True)