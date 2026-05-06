# app/services/field_config_service.py
# encoding: utf-8
from __future__ import annotations

"""
字段配置服务（新字段方案 / 零旧字段口径）

承重墙（2026-03-05）：
- FieldConfig 不存在 is_deleted/deleted_at：本服务不做软删过滤
- upsert 唯一键严格使用 (module, field_name)
- module 为必传字段（FieldConfig.module NOT NULL）
- required/visible/editable 在 DB 中是 int(0/1)，本服务统一做 bool -> 0/1 归一化
"""

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Optional, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from app.core.constants import ROLE_ALL
from app.models.field_config import FieldConfig

_BJ = ZoneInfo("Asia/Shanghai")


def _now_bj_naive() -> datetime:
    return datetime.now(_BJ).replace(tzinfo=None)


def _b2i(v: Any) -> int:
    return 1 if bool(v) else 0


def _normalize_roles(v: Any) -> Optional[list[str]]:
    if v is None:
        return None
    if not isinstance(v, list):
        raise ValueError("roles must be a list or null")

    allowed_roles = set(ROLE_ALL)
    out: list[str] = []
    for item in v:
        role_name = str(item or "").strip().lower()
        if not role_name:
            continue
        if role_name not in allowed_roles:
            raise ValueError(f"unknown role: {role_name}")
        if role_name not in out:
            out.append(role_name)
    return out


async def list_field_configs(*, db: AsyncSession, module: Optional[str] = None) -> List[FieldConfig]:
    """
    列表查询（零旧字段口径）：
    - 不做 is_deleted 过滤（模型无该字段）
    - 可选按 module 过滤
    """
    q = select(FieldConfig).options(lazyload("*"))
    if module is not None:
        mod = str(module).strip()
        q = q.where(FieldConfig.module == mod)
        q = q.order_by(FieldConfig.sort.asc(), FieldConfig.id.asc())
    else:
        # 无 module 时仍保证稳定顺序（便于审计与调试）
        q = q.order_by(FieldConfig.module.asc(), FieldConfig.sort.asc(), FieldConfig.id.asc())

    return list((await db.execute(q)).scalars().all())


async def upsert_field_config(
        *,
        db: AsyncSession,
        module: str,
        field_name: str,
        label: str,
        type: str,
        options=None,
        validators=None,
        extra=None,
        required: bool = False,
        visible: bool = True,
        editable: bool = True,
        sort: int = 0,
        view_roles=None,
        edit_roles=None,
) -> FieldConfig:
    """
    Upsert（唯一键：module + field_name）
    """
    mod = str(module).strip()
    fname = str(field_name).strip()
    if not mod:
        raise ValueError("module is required")
    if not fname:
        raise ValueError("field_name is required")
    normalized_view_roles = _normalize_roles(view_roles)
    normalized_edit_roles = _normalize_roles(edit_roles)

    now = _now_bj_naive()

    row = (
        await db.execute(
            select(FieldConfig)
            .options(lazyload("*"))
            .where(FieldConfig.module == mod, FieldConfig.field_name == fname)
        )
    ).scalars().first()

    if not row:
        row = FieldConfig(
            module=mod,
            field_name=fname,
            label=str(label).strip(),
            type=str(type).strip(),
            options=options,
            validators=validators,
            extra=extra,
            required=_b2i(required),
            visible=_b2i(visible),
            editable=_b2i(editable),
            sort=int(sort or 0),
            view_roles=normalized_view_roles,
            edit_roles=normalized_edit_roles,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.label = str(label).strip()
        row.type = str(type).strip()
        row.options = options
        row.validators = validators
        row.extra = extra
        row.required = _b2i(required)
        row.visible = _b2i(visible)
        row.editable = _b2i(editable)
        row.sort = int(sort or 0)
        row.view_roles = normalized_view_roles
        row.edit_roles = normalized_edit_roles
        row.updated_at = now

    await db.commit()
    await db.refresh(row)
    return row
