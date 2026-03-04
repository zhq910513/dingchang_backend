# app/services/field_config_service.py
# encoding: utf-8
from __future__ import annotations

"""
字段配置服务（新字段方案 / 不兼容旧字段名）
"""

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.field_config import FieldConfig

_BJ = ZoneInfo("Asia/Shanghai")


async def list_field_configs(*, db: AsyncSession) -> List[FieldConfig]:
    q = select(FieldConfig).where(FieldConfig.is_deleted == 0).order_by(FieldConfig.sort.asc(), FieldConfig.id.asc())
    return list((await db.execute(q)).scalars().all())


async def upsert_field_config(*, db: AsyncSession, field_name: str, **kwargs) -> FieldConfig:
    now = datetime.now(_BJ).replace(tzinfo=None)
    row = (await db.execute(
        select(FieldConfig).where(FieldConfig.field_name == field_name, FieldConfig.is_deleted == 0))).scalars().first()
    if not row:
        row = FieldConfig(field_name=field_name, created_at=now, updated_at=now, **kwargs)
        db.add(row)
    else:
        for k, v in kwargs.items():
            setattr(row, k, v)
        row.updated_at = now
    await db.commit()
    await db.refresh(row)
    return row
