# app/api/v1/customer_channel.py
# encoding: utf-8
"""
客户/渠道管理 API

✅ Schemas 已冻结：app.schemas.customer_channel 仅包含
- CustomerGroupOut / ChannelGroupOut
- OptionItem / OptionListOut

因此本文件：
- response_model 严格使用上述 schema（或其容器 List[...]）
- 请求入参模型（Create/Update）放在 API 内部定义，不污染 schemas 冻结包
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_with_role_and_teams
from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_SALES, ROLE_MARKET
from app.core.db import get_db
from app.models.channel_group import ChannelGroup
from app.models.customer_group import CustomerGroup
from app.schemas.customer_channel import CustomerGroupOut, ChannelGroupOut, OptionItem, OptionListOut

router = APIRouter(tags=["customer_channel"])


def _ensure_access(role_name: Optional[str]) -> None:
    rn = role_name or ""
    if rn not in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_FINANCE, ROLE_MARKET, ROLE_SALES):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_write_access(role_name: Optional[str]) -> None:
    rn = role_name or ""
    # 销售/财务默认只读（如需放开再细化权限）
    if rn in (ROLE_SALES, ROLE_FINANCE):
        raise HTTPException(status_code=403, detail="No permission")


def _dt_to_str(dt: Any) -> Optional[str]:
    if not dt:
        return None
    if isinstance(dt, datetime):
        try:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return dt.isoformat(sep=" ", timespec="seconds")
    try:
        return str(dt)
    except Exception:
        return None


def _to_customer_out(x: CustomerGroup) -> CustomerGroupOut:
    return CustomerGroupOut(
        id=int(getattr(x, "id", 0) or 0),
        team_name=(str(getattr(x, "team_name", "") or "").strip() or None),
        customer_code=str(getattr(x, "customer_code", "") or ""),
        customer_name=str(getattr(x, "customer_name", "") or ""),
        market=(str(getattr(x, "market", "") or "").strip() or None),
        region=(str(getattr(x, "region", "") or "").strip() or None),
        contacts=getattr(x, "contacts", None),
        deleted_at=_dt_to_str(getattr(x, "deleted_at", None)),
        is_deleted=int(getattr(x, "is_deleted", 0) or 0),
    )


def _to_channel_out(x: ChannelGroup) -> ChannelGroupOut:
    return ChannelGroupOut(
        id=int(getattr(x, "id", 0) or 0),
        team_name=(str(getattr(x, "team_name", "") or "").strip() or None),
        channel_code=str(getattr(x, "channel_code", "") or ""),
        channel_name=str(getattr(x, "channel_name", "") or ""),
        region=(str(getattr(x, "region", "") or "").strip() or None),
        contacts=getattr(x, "contacts", None),
        deleted_at=_dt_to_str(getattr(x, "deleted_at", None)),
        is_deleted=int(getattr(x, "is_deleted", 0) or 0),
    )


def _code_name(code: Any, name: Any) -> str:
    c = str(code or "").strip()
    n = str(name or "").strip()
    if c and n:
        return f"{c} - {n}"
    return n or c or ""


class CustomerGroupIn(BaseModel):
    team_name: Optional[str] = None
    customer_code: str = Field(..., min_length=1, max_length=64)
    customer_name: str = Field(..., min_length=1, max_length=128)
    market: Optional[str] = None
    region: Optional[str] = None
    contacts: Any = None


class ChannelGroupIn(BaseModel):
    team_name: Optional[str] = None
    channel_code: str = Field(..., min_length=1, max_length=64)
    channel_name: str = Field(..., min_length=1, max_length=128)
    region: Optional[str] = None
    contacts: Any = None


@router.get("/customer-groups", response_model=List[CustomerGroupOut])
async def list_customer_groups(
    include_deleted: bool = Query(False, description="默认不返回已删除"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, _team_names, _team_ids = user_with_role
    _ensure_access(role_name)

    stmt = select(CustomerGroup).order_by(CustomerGroup.id.asc())
    if not include_deleted and hasattr(CustomerGroup, "deleted_at"):
        stmt = stmt.where(getattr(CustomerGroup, "deleted_at").is_(None))

    rows = (await db.execute(stmt)).scalars().all()
    return [_to_customer_out(x) for x in rows]


@router.get("/channel-groups", response_model=List[ChannelGroupOut])
async def list_channel_groups(
    include_deleted: bool = Query(False, description="默认不返回已删除"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, _team_names, _team_ids = user_with_role
    _ensure_access(role_name)

    stmt = select(ChannelGroup).order_by(ChannelGroup.id.asc())
    if not include_deleted and hasattr(ChannelGroup, "deleted_at"):
        stmt = stmt.where(getattr(ChannelGroup, "deleted_at").is_(None))

    rows = (await db.execute(stmt)).scalars().all()
    return [_to_channel_out(x) for x in rows]


@router.get("/customer-groups/options", response_model=OptionListOut)
async def customer_group_options(
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, _team_names, _team_ids = user_with_role
    _ensure_access(role_name)

    stmt = select(CustomerGroup).order_by(CustomerGroup.id.asc())
    if hasattr(CustomerGroup, "deleted_at"):
        stmt = stmt.where(getattr(CustomerGroup, "deleted_at").is_(None))
    rows = (await db.execute(stmt)).scalars().all()

    items = [
        OptionItem(id=int(x.id), group_name=_code_name(getattr(x, "customer_code", None), getattr(x, "customer_name", None)))
        for x in rows
    ]
    return OptionListOut(items=items)


@router.get("/channel-groups/options", response_model=OptionListOut)
async def channel_group_options(
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, _team_names, _team_ids = user_with_role
    _ensure_access(role_name)

    stmt = select(ChannelGroup).order_by(ChannelGroup.id.asc())
    if hasattr(ChannelGroup, "deleted_at"):
        stmt = stmt.where(getattr(ChannelGroup, "deleted_at").is_(None))
    rows = (await db.execute(stmt)).scalars().all()

    items = [
        OptionItem(id=int(x.id), group_name=_code_name(getattr(x, "channel_code", None), getattr(x, "channel_name", None)))
        for x in rows
    ]
    return OptionListOut(items=items)


@router.post("/customer-groups", response_model=CustomerGroupOut)
async def create_customer_group(
    payload: CustomerGroupIn,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, _team_names, _team_ids = user_with_role
    _ensure_access(role_name)
    _ensure_write_access(role_name)

    obj = CustomerGroup(
        team_name=(payload.team_name.strip() if payload.team_name else None),
        customer_code=str(payload.customer_code).strip(),
        customer_name=str(payload.customer_name).strip(),
        market=(payload.market.strip() if payload.market else None),
        region=(payload.region.strip() if payload.region else None),
        contacts=payload.contacts if payload.contacts is not None else [],
        created_by=getattr(_user, "id", None),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return _to_customer_out(obj)


@router.post("/channel-groups", response_model=ChannelGroupOut)
async def create_channel_group(
    payload: ChannelGroupIn,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, _team_names, _team_ids = user_with_role
    _ensure_access(role_name)
    _ensure_write_access(role_name)

    obj = ChannelGroup(
        team_name=(payload.team_name.strip() if payload.team_name else None),
        channel_code=str(payload.channel_code).strip(),
        channel_name=str(payload.channel_name).strip(),
        region=(payload.region.strip() if payload.region else None),
        contacts=payload.contacts if payload.contacts is not None else [],
        created_by=getattr(_user, "id", None),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return _to_channel_out(obj)
