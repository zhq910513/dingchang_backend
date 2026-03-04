# app/services/customer_channel_service.py
# encoding: utf-8
from __future__ import annotations

"""
客户/渠道管理服务（新表口径）
"""

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer_group import CustomerGroup
from app.models.channel_group import ChannelGroup

_BJ = ZoneInfo("Asia/Shanghai")


async def list_customer_groups(*, db: AsyncSession, keyword: Optional[str] = None) -> List[CustomerGroup]:
    q = select(CustomerGroup).where(CustomerGroup.is_deleted == 0)
    if keyword:
        like = f"%{keyword.strip()}%"
        q = q.where(CustomerGroup.customer_name.like(like) | CustomerGroup.customer_code.like(like))
    q = q.order_by(CustomerGroup.id.desc())
    return list((await db.execute(q)).scalars().all())


async def list_channel_groups(*, db: AsyncSession, keyword: Optional[str] = None) -> List[ChannelGroup]:
    q = select(ChannelGroup).where(ChannelGroup.is_deleted == 0)
    if keyword:
        like = f"%{keyword.strip()}%"
        q = q.where(ChannelGroup.channel_name.like(like) | ChannelGroup.channel_code.like(like))
    q = q.order_by(ChannelGroup.id.desc())
    return list((await db.execute(q)).scalars().all())


async def create_customer_group(*, db: AsyncSession, customer_code: str, customer_name: str, team_name: Optional[str],
                                created_by: Optional[int]) -> CustomerGroup:
    now = datetime.now(_BJ).replace(tzinfo=None)
    row = CustomerGroup(team_name=team_name, customer_code=customer_code, customer_name=customer_name,
                        created_by=created_by, created_at=now, updated_at=now)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def create_channel_group(*, db: AsyncSession, channel_code: str, channel_name: str, team_name: Optional[str],
                               region: Optional[str], contacts: list, created_by: Optional[int]) -> ChannelGroup:
    now = datetime.now(_BJ).replace(tzinfo=None)
    row = ChannelGroup(team_name=team_name, channel_code=channel_code, channel_name=channel_name, region=region or "",
                       contacts=contacts or [], created_by=created_by, created_at=now, updated_at=now)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
