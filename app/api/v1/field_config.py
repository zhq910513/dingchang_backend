# encoding: utf-8
"""
字段配置相关接口（API 薄壳）

原则：
- Schemas 为真源：app.schemas.field_config
- 业务逻辑下沉到 services.field_config_service
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_with_role
from app.core.constants import ROLE_SUPER_ADMIN, ROLE_MANAGER
from app.core.db import get_db
from app.schemas.field_config import FieldConfigOut, FieldConfigListOut
from app.services.field_config_service import list_field_configs as _list_field_configs, \
    upsert_field_config as _upsert_field_config

router = APIRouter(prefix="/field-config", tags=["field-config"])


class FieldConfigUpsertIn(BaseModel):
    label: str = Field(..., min_length=1)
    type: str = Field(..., min_length=1)
    options: Optional[list[Any]] = None
    validators: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None
    required: bool = False
    visible: bool = True
    editable: bool = True
    sort: int = 0
    view_roles: Optional[list[str]] = None
    edit_roles: Optional[list[str]] = None


@router.get("", response_model=FieldConfigListOut)
async def list_configs(db: AsyncSession = Depends(get_db), me=Depends(get_current_user_with_role)):
    _user, _role, _teams = me
    rows = await _list_field_configs(db=db)
    items = [FieldConfigOut.from_orm(r) for r in rows]
    return FieldConfigListOut(items=items)


@router.put("/{field_name}", response_model=FieldConfigOut)
async def upsert(field_name: str, data: FieldConfigUpsertIn, db: AsyncSession = Depends(get_db),
                 me=Depends(get_current_user_with_role)):
    _user, role, _teams = me
    if role not in {ROLE_SUPER_ADMIN, ROLE_MANAGER}:
        raise HTTPException(status_code=403, detail="无权限")
    row = await _upsert_field_config(
        db=db,
        field_name=field_name,
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
