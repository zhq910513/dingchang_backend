# app/api/v1/customer_channel.py
# encoding: utf-8
from __future__ import annotations

"""
客户/渠道管理（API 薄壳）

原则：
- Schemas 为真源：app.schemas.customer_channel
- 业务逻辑下沉到 services.customer_channel_service

承重墙：
- deps 统一返回 CurrentUserContext（不再解包 tuple）
- API 不自定义入参模型（schemas 约束 API）
- 只在确实需要 current_user（写 created_by）时才注入 ctx，避免未使用形参污染
"""

import json
from typing import List, Optional, Any

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserContext, get_current_user_with_role_and_teams
from app.core.db import get_db
from app.schemas.customer_channel import CustomerGroupOut, ChannelGroupOut, OptionItem, OptionListOut
from app.services.customer_channel_service import (
    list_customer_groups as _list_customer_groups,
    list_channel_groups as _list_channel_groups,
    create_customer_group as _create_customer_group,
    create_channel_group as _create_channel_group,
)

router = APIRouter(prefix="/customer-channel", tags=["customer-channel"])


@router.get("/customers", response_model=List[CustomerGroupOut])
async def list_customers(
        keyword: Optional[str] = Query(None),
        db: AsyncSession = Depends(get_db),
):
    rows = await _list_customer_groups(db=db, keyword=keyword)
    return [CustomerGroupOut.from_orm(r) for r in rows]


@router.get("/channels", response_model=List[ChannelGroupOut])
async def list_channels(
        keyword: Optional[str] = Query(None),
        db: AsyncSession = Depends(get_db),
):
    rows = await _list_channel_groups(db=db, keyword=keyword)
    return [ChannelGroupOut.from_orm(r) for r in rows]


@router.post("/customers", response_model=CustomerGroupOut)
async def create_customer(
        customer_code: str = Query(..., min_length=1),
        customer_name: str = Query(..., min_length=1),
        team_name: Optional[str] = Query(None),
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    created_by = int(getattr(ctx.user, "id", 0) or 0)
    row = await _create_customer_group(
        db=db,
        customer_code=customer_code,
        customer_name=customer_name,
        team_name=team_name,
        created_by=created_by,
    )
    return CustomerGroupOut.from_orm(row)


@router.post("/channels", response_model=ChannelGroupOut)
async def create_channel(
        channel_code: str = Query(..., min_length=1),
        channel_name: str = Query(..., min_length=1),
        team_name: Optional[str] = Query(None),
        region: Optional[str] = Query(None),
        contacts_json: Optional[str] = Query(None, description="联系方式 JSON 数组字符串（可空）"),
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    created_by = int(getattr(ctx.user, "id", 0) or 0)

    contacts: List[Any] = []
    if contacts_json is not None and str(contacts_json).strip():
        try:
            parsed = json.loads(contacts_json)
        except Exception:
            raise HTTPException(status_code=400, detail="contacts_json 必须是合法 JSON")
        if not isinstance(parsed, list):
            raise HTTPException(status_code=400, detail="contacts_json 必须解析为 JSON 数组")
        contacts = parsed

    row = await _create_channel_group(
        db=db,
        channel_code=channel_code,
        channel_name=channel_name,
        team_name=team_name,
        region=region,
        contacts=contacts,
        created_by=created_by,
    )
    return ChannelGroupOut.from_orm(row)


@router.get("/options", response_model=OptionListOut)
async def options(
        db: AsyncSession = Depends(get_db),
):
    customers = await _list_customer_groups(db=db, keyword=None)
    channels = await _list_channel_groups(db=db, keyword=None)
    return OptionListOut(
        customers=[OptionItem(value=c.id, label=f"{c.customer_code} - {c.customer_name}") for c in customers],
        channels=[OptionItem(value=c.id, label=f"{c.channel_code} - {c.channel_name}") for c in channels],
    )
