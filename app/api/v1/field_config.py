# app/api/v1/field_config.py
# encoding: utf-8
"""
字段配置相关接口（API 薄壳）

原则：
- Schemas 为真源：app.schemas.field_config
- 业务逻辑下沉到 services.field_config_service

承重墙（2026-03-05）：
- deps 统一返回 CurrentUserContext（不再解包 tuple）
- API 不自定义入参模型（schemas 约束 API）
- upsert 必须显式携带 module（FieldConfig.module NOT NULL 且唯一键依赖 module）
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserContext, get_current_user_with_role
from app.core.constants import ROLE_SUPER_ADMIN, ROLE_MANAGER
from app.core.db import get_db
from app.schemas.field_config import FieldConfigOut, FieldConfigListOut, FieldConfigUpsertIn
from app.services.field_config_service import (
    list_field_configs as _list_field_configs,
    upsert_field_config as _upsert_field_config,
)

router = APIRouter(prefix="/field-config", tags=["field-config"])


@router.get("", response_model=FieldConfigListOut)
async def list_configs(
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role),
):
    rows = await _list_field_configs(db=db)
    items = [FieldConfigOut.from_orm(r) for r in rows]
    return FieldConfigListOut(items=items)


@router.put("/{module}/{field_name}", response_model=FieldConfigOut)
async def upsert(
        module: str,
        field_name: str,
        data: FieldConfigUpsertIn,
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role),
):
    role = ctx.primary_role or ""
    if role not in {ROLE_SUPER_ADMIN, ROLE_MANAGER}:
        raise HTTPException(status_code=403, detail="无权限")

    row = await _upsert_field_config(
        db=db,
        module=str(module).strip(),
        field_name=str(field_name).strip(),
        label=data.label,
        type=data.type,
        options=data.options,
        validators=data.validators,
        extra=data.extra,
        required=data.required,
        visible=data.visible,
        editable=data.editable,
        sort=data.sort,
        view_roles=data.view_roles,
        edit_roles=data.edit_roles,
    )
    return FieldConfigOut.from_orm(row)
