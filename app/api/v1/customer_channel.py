# app/api/v1/customer_channel.py
# encoding: utf-8
from __future__ import annotations

"""
客户/渠道管理（API 薄壳）

原则：
- Schemas 为真源：app.schemas.customer_channel
- 业务逻辑下沉到 services.customer_channel_service
"""

from typing import List, Optional, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_with_role_and_teams
from app.core.db import get_db
from app.schemas.customer_channel import CustomerGroupOut, ChannelGroupOut, OptionItem, OptionListOut
from app.services.customer_channel_service import (
    list_customer_groups as _list_customer_groups,
    list_channel_groups as _list_channel_groups,
    create_customer_group as _create_customer_group,
    create_channel_group as _create_channel_group,
)

router = APIRouter(prefix="/customer-channel", tags=["customer-channel"])


class CustomerGroupCreateIn(BaseModel):
    customer_code: str = Field(..., min_length=1)
    customer_name: str = Field(..., min_length=1)
    team_name: Optional[str] = None


class ChannelGroupCreateIn(BaseModel):
    channel_code: str = Field(..., min_length=1)
    channel_name: str = Field(..., min_length=1)
    team_name: Optional[str] = None
    region: Optional[str] = None
    contacts: List[Any] = Field(default_factory=list)


@router.get("/customers", response_model=List[CustomerGroupOut])
async def list_customers(
        keyword: Optional[str] = Query(None),
        db: AsyncSession = Depends(get_db),
        me=Depends(get_current_user_with_role_and_teams),
):
    rows = await _list_customer_groups(db=db, keyword=keyword)
    return [CustomerGroupOut.from_orm(r) for r in rows]


@router.get("/channels", response_model=List[ChannelGroupOut])
async def list_channels(
        keyword: Optional[str] = Query(None),
        db: AsyncSession = Depends(get_db),
        me=Depends(get_current_user_with_role_and_teams),
):
    rows = await _list_channel_groups(db=db, keyword=keyword)
    return [ChannelGroupOut.from_orm(r) for r in rows]


@router.post("/customers", response_model=CustomerGroupOut)
async def create_customer(data: CustomerGroupCreateIn, db: AsyncSession = Depends(get_db),
                          me=Depends(get_current_user_with_role_and_teams)):
    user, _role, _teams = me
    row = await _create_customer_group(db=db, customer_code=data.customer_code, customer_name=data.customer_name,
                                       team_name=data.team_name, created_by=user.id)
    return CustomerGroupOut.from_orm(row)


@router.post("/channels", response_model=ChannelGroupOut)
async def create_channel(data: ChannelGroupCreateIn, db: AsyncSession = Depends(get_db),
                         me=Depends(get_current_user_with_role_and_teams)):
    user, _role, _teams = me
    row = await _create_channel_group(
        db=db,
        channel_code=data.channel_code,
        channel_name=data.channel_name,
        team_name=data.team_name,
        region=data.region,
        contacts=data.contacts,
        created_by=user.id,
    )
    return ChannelGroupOut.from_orm(row)


@router.get("/options", response_model=OptionListOut)
async def options(db: AsyncSession = Depends(get_db), me=Depends(get_current_user_with_role_and_teams)):
    customers = await _list_customer_groups(db=db, keyword=None)
    channels = await _list_channel_groups(db=db, keyword=None)
    return OptionListOut(
        customers=[OptionItem(value=c.id, label=f"{c.customer_code} - {c.customer_name}") for c in customers],
        channels=[OptionItem(value=c.id, label=f"{c.channel_code} - {c.channel_name}") for c in channels],
    )
