# encoding: utf-8
"""
字段配置相关接口（API 薄壳）

原则：
- Schemas 为真源：app.schemas.field_config
- 业务逻辑下沉到 services.field_config_service
- /form-config 为前端表单分组读取接口（返回 group -> fields 结构）

承重墙（2026-03-05）：
- deps 统一返回 CurrentUserContext（不再解包 tuple）
- API 不自定义入参模型（schemas 约束 API）
- upsert 必须显式携带 module（FieldConfig.module NOT NULL 且唯一键依赖 module）
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserContext, get_current_user_with_role
from app.core.constants import ROLE_SUPER_ADMIN, ROLE_MANAGER
from app.core.db import get_db
from app.models.field_config import FieldConfig, FieldGroup, FieldGroupField
from app.schemas.field_config import (
    FieldConfigOut,
    FieldConfigListOut,
    FieldConfigUpsertIn,
    FieldFormItemOut,
    FieldGroupConfigOut,
)
from app.services.field_config_service import (
    list_field_configs as _list_field_configs,
    upsert_field_config as _upsert_field_config,
)

router = APIRouter(prefix="/field-config", tags=["field-config"])


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return bool(v)


def _roles_list(v: Any) -> Optional[List[str]]:
    if v is None:
        return None
    if isinstance(v, list):
        out: List[str] = []
        for x in v:
            s = str(x or "").strip()
            if s:
                out.append(s)
        return out
    return None


def _is_visible_for_role(
        *,
        view_roles: Any,
        role_name: Optional[str],
        default_visible: bool,
) -> bool:
    roles = _roles_list(view_roles)
    if roles is None:
        return default_visible
    if not role_name:
        return False
    return role_name in roles


def _is_editable_for_role(
        *,
        edit_roles: Any,
        role_name: Optional[str],
        default_editable: bool,
) -> bool:
    roles = _roles_list(edit_roles)
    if roles is None:
        return default_editable
    if not role_name:
        return False
    return role_name in roles


@router.get("", response_model=FieldConfigListOut)
async def list_configs(
        db: AsyncSession = Depends(get_db),
):
    rows = await _list_field_configs(db=db)
    items = [FieldConfigOut.from_orm(r) for r in rows]
    return FieldConfigListOut(items=items)


@router.get("/form-config", response_model=List[FieldGroupConfigOut])
async def get_form_config(
        module: str = Query("order"),
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role),
) -> List[FieldGroupConfigOut]:
    role_name = ctx.primary_role or ""
    module_name = str(module or "").strip() or "order"

    stmt_group = (
        select(FieldGroup)
        .where(FieldGroup.module == module_name)
        .order_by(FieldGroup.order_index.asc(), FieldGroup.id.asc())
    )
    groups = (await db.execute(stmt_group)).scalars().all()
    if not groups:
        return []

    group_ids = [int(g.id) for g in groups]
    stmt_map = (
        select(FieldGroupField)
        .where(FieldGroupField.group_id.in_(group_ids))
        .order_by(FieldGroupField.group_id.asc(), FieldGroupField.order_index.asc(), FieldGroupField.id.asc())
    )
    mappings = (await db.execute(stmt_map)).scalars().all()
    if not mappings:
        return []

    field_ids = list({int(m.field_id) for m in mappings})
    stmt_field = (
        select(FieldConfig)
        .where(
            FieldConfig.module == module_name,
            FieldConfig.id.in_(field_ids),
        )
        .order_by(FieldConfig.sort.asc(), FieldConfig.id.asc())
    )
    fields = (await db.execute(stmt_field)).scalars().all()
    field_map = {int(f.id): f for f in fields}

    mappings_by_group: dict[int, list[FieldGroupField]] = {}
    for m in mappings:
        gid = int(m.group_id)
        mappings_by_group.setdefault(gid, []).append(m)

    out: List[FieldGroupConfigOut] = []

    for g in groups:
        field_items: List[FieldFormItemOut] = []

        for m in mappings_by_group.get(int(g.id), []):
            f = field_map.get(int(m.field_id))
            if not f:
                continue

            visible = _is_visible_for_role(
                view_roles=getattr(f, "view_roles", None),
                role_name=role_name,
                default_visible=_as_bool(getattr(f, "visible", 1), True),
            )
            if not visible:
                continue

            editable = _is_editable_for_role(
                edit_roles=getattr(f, "edit_roles", None),
                role_name=role_name,
                default_editable=_as_bool(getattr(f, "editable", 1), True),
            )

            field_items.append(
                FieldFormItemOut(
                    field_name=str(getattr(f, "field_name", "") or "").strip(),
                    label=str(getattr(f, "label", "") or "").strip(),
                    type=str(getattr(f, "type", "") or "text").strip(),
                    required=_as_bool(getattr(f, "required", 0), False),
                    visible=True,
                    editable=editable,
                    sort=int(getattr(f, "sort", 0) or 0),
                    options=getattr(f, "options", None),
                    validators=getattr(f, "validators", None),
                    extra=getattr(f, "extra", None),
                )
            )

        out.append(
            FieldGroupConfigOut(
                group_key=str(getattr(g, "group_key", "") or "").strip(),
                group_name=str(getattr(g, "group_name", "") or "").strip(),
                fields=field_items,
            )
        )

    return out


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
