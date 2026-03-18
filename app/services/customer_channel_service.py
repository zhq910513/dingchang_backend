# app/services/customer_channel_service.py
# encoding: utf-8
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from sqlalchemy.sql.expression import false as sql_false

from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_MARKET, ROLE_SALES, ROLE_SUPER_ADMIN
from app.models.channel_group import ChannelGroup
from app.models.customer_group import CustomerGroup
from app.models.user import User

_BJ = ZoneInfo("Asia/Shanghai")
_CHANNEL_GROUP_LIST_ALLOWED_ROLES = {
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_MARKET,
    ROLE_FINANCE,
    ROLE_SALES,
}


@dataclass(frozen=True)
class ChannelGroupAclBundle:
    role_name: str
    can_list_view: bool
    can_create: bool
    can_update: bool
    can_delete: bool
    can_view_deleted: bool


def compile_channel_group_acl_bundle(*, role_name: Optional[str]) -> ChannelGroupAclBundle:
    normalized_role_name = str(role_name or "").strip()
    return ChannelGroupAclBundle(
        role_name=normalized_role_name,
        can_list_view=normalized_role_name in _CHANNEL_GROUP_LIST_ALLOWED_ROLES,
        can_create=normalized_role_name != ROLE_FINANCE,
        can_update=normalized_role_name in (ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_MARKET),
        can_delete=normalized_role_name != ROLE_FINANCE,
        can_view_deleted=normalized_role_name == ROLE_SUPER_ADMIN,
    )


def build_channel_group_page_capabilities(*, acl_bundle: ChannelGroupAclBundle) -> Dict[str, bool]:
    return {
        "channel.create": bool(acl_bundle.can_create),
        "channel.list.view": bool(acl_bundle.can_list_view),
    }


def build_channel_group_row_capabilities(
    *,
    acl_bundle: ChannelGroupAclBundle,
    row: Mapping[str, Any],
) -> Dict[str, bool]:
    is_deleted = int(row.get("is_deleted", 0) or 0) == 1
    return {
        "channel.update": bool(acl_bundle.can_update and not is_deleted),
        "channel.delete": bool(acl_bundle.can_delete and not is_deleted),
    }


def apply_channel_group_list_acl(*, stmt, acl_bundle: ChannelGroupAclBundle):
    if acl_bundle.can_list_view:
        return stmt
    return stmt.where(sql_false())


_CUSTOMER_GROUP_LIST_ALLOWED_ROLES = {
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_MARKET,
    ROLE_FINANCE,
    ROLE_SALES,
}


@dataclass(frozen=True)
class CustomerGroupAclBundle:
    role_name: str
    can_list_view: bool
    can_create: bool
    can_update: bool
    can_delete: bool
    can_view_deleted: bool


def compile_customer_group_acl_bundle(*, role_name: Optional[str]) -> CustomerGroupAclBundle:
    normalized_role_name = str(role_name or "").strip()
    return CustomerGroupAclBundle(
        role_name=normalized_role_name,
        can_list_view=normalized_role_name in _CUSTOMER_GROUP_LIST_ALLOWED_ROLES,
        can_create=normalized_role_name != ROLE_FINANCE,
        can_update=normalized_role_name in (ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_MARKET),
        can_delete=normalized_role_name != ROLE_FINANCE,
        can_view_deleted=normalized_role_name == ROLE_SUPER_ADMIN,
    )


def build_customer_group_page_capabilities(*, acl_bundle: CustomerGroupAclBundle) -> Dict[str, bool]:
    return {
        "customer.create": bool(acl_bundle.can_create),
        "customer.list.view": bool(acl_bundle.can_list_view),
    }


def build_customer_group_row_capabilities(
    *,
    acl_bundle: CustomerGroupAclBundle,
    row: Mapping[str, Any],
) -> Dict[str, bool]:
    is_deleted = int(row.get("is_deleted", 0) or 0) == 1
    return {
        "customer.update": bool(acl_bundle.can_update and not is_deleted),
        "customer.delete": bool(acl_bundle.can_delete and not is_deleted),
    }


def apply_customer_group_list_acl(*, stmt, acl_bundle: CustomerGroupAclBundle):
    if acl_bundle.can_list_view:
        return stmt
    return stmt.where(sql_false())


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
        .order_by(CustomerGroup.updated_at.desc(), CustomerGroup.id.desc())
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
        .order_by(ChannelGroup.updated_at.desc(), ChannelGroup.id.desc())
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
    role_name: Optional[str] = None,
    customer_code: Optional[str] = None,
    customer_name: Optional[str] = None,
    market: Optional[str] = None,
    region: Optional[str] = None,
    created_by_name: Optional[str] = None,
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    acl_bundle = compile_customer_group_acl_bundle(role_name=role_name)
    page = _normalize_page(page)
    page_size = _normalize_page_size(page_size)
    offset = (page - 1) * page_size

    creator = aliased(User)
    created_name_expr = _display_name_expr(creator)
    effective_include_deleted = bool(include_deleted and acl_bundle.can_view_deleted)

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

    base_stmt = apply_customer_group_list_acl(stmt=base_stmt, acl_bundle=acl_bundle)
    count_stmt = apply_customer_group_list_acl(stmt=count_stmt, acl_bundle=acl_bundle)

    if not effective_include_deleted:
        cond = CustomerGroup.is_deleted == 0
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    normalized_customer_code = _normalize_text(customer_code)
    if normalized_customer_code:
        cond = CustomerGroup.customer_code.like(f"%{normalized_customer_code}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    normalized_customer_name = _normalize_text(customer_name)
    if normalized_customer_name:
        cond = CustomerGroup.customer_name.like(f"%{normalized_customer_name}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    normalized_market = _normalize_text(market)
    if normalized_market:
        cond = CustomerGroup.market.like(f"%{normalized_market}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    normalized_region = _normalize_text(region)
    if normalized_region:
        cond = CustomerGroup.region.like(f"%{normalized_region}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    normalized_created_by_name = _normalize_text(created_by_name)
    if normalized_created_by_name:
        count_stmt = count_stmt.outerjoin(creator, creator.id == CustomerGroup.created_by)
        cond = cast(created_name_expr, String).like(f"%{normalized_created_by_name}%")
        base_stmt = base_stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    base_stmt = (
        base_stmt
        .order_by(CustomerGroup.updated_at.desc(), CustomerGroup.id.desc())
        .offset(offset)
        .limit(page_size)
    )

    total = int((await db.execute(count_stmt)).scalar() or 0)
    raw_items = list((await db.execute(base_stmt)).mappings().all())
    items = []
    for raw_row in raw_items:
        row = dict(raw_row)
        row["meta"] = {
            "capabilities": build_customer_group_row_capabilities(
                acl_bundle=acl_bundle,
                row=row,
            )
        }
        items.append(row)

    return {
        "total": total,
        "items": items,
        "meta": {
            "capabilities": build_customer_group_page_capabilities(acl_bundle=acl_bundle),
            "scopes": {},
            "pagination": {
                "page": page,
                "page_size": page_size,
            },
        },
    }



async def list_channel_groups_manage(
    *,
    db: AsyncSession,
    role_name: Optional[str],
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

    acl_bundle = compile_channel_group_acl_bundle(role_name=role_name)
    if not acl_bundle.can_list_view:
        raise HTTPException(status_code=403, detail="无权限查看渠道列表")

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

    base_stmt = apply_channel_group_list_acl(stmt=base_stmt, acl_bundle=acl_bundle)
    count_stmt = apply_channel_group_list_acl(stmt=count_stmt, acl_bundle=acl_bundle)

    include_deleted = bool(include_deleted and acl_bundle.can_view_deleted)
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
        .order_by(ChannelGroup.updated_at.desc(), ChannelGroup.id.desc())
        .offset(offset)
        .limit(page_size)
    )

    total = int((await db.execute(count_stmt)).scalar() or 0)
    raw_items = list((await db.execute(base_stmt)).mappings().all())
    items = []
    for raw_row in raw_items:
        item = dict(raw_row)
        item["meta"] = {
            "capabilities": build_channel_group_row_capabilities(
                acl_bundle=acl_bundle,
                row=raw_row,
            )
        }
        items.append(item)

    return {
        "items": items,
        "total": total,
        "meta": {
            "capabilities": build_channel_group_page_capabilities(acl_bundle=acl_bundle),
            "scopes": {},
            "pagination": {
                "page": page,
                "page_size": page_size,
            },
        },
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
