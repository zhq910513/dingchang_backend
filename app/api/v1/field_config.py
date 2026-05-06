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

性能收敛（2026-03-23）：
- /form-config 增加进程内只读缓存，按 (module, role_name) 维度缓存
- upsert 成功后失效对应 module 的缓存，避免返回旧配置
"""

from __future__ import annotations

import copy
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from app.api.deps import CurrentUserContext, get_current_user_with_role
from app.core.constants import ROLE_MANAGER, ROLE_SUPER_ADMIN
from app.core.db import get_db
from app.models.field_config import FieldConfig, FieldGroup, FieldGroupField
from app.schemas.field_config import (
    FieldConfigListOut,
    FieldConfigOut,
    FieldConfigUpsertIn,
    FieldFormItemOut,
    FieldGroupConfigOut,
)
from app.services.field_config_service import (
    list_field_configs as _list_field_configs,
    upsert_field_config as _upsert_field_config,
)

router = APIRouter(prefix="/field-config", tags=["field-config"])

_FORM_CONFIG_CACHE_TTL_SECONDS = 300
_FORM_CONFIG_CACHE: Dict[Tuple[str, str], Tuple[float, List[FieldGroupConfigOut]]] = {}


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


def _cache_key(module_name: str, role_name: str) -> Tuple[str, str]:
    return (str(module_name or "").strip(), str(role_name or "").strip())


def _copy_group_config_list(
        items: List[FieldGroupConfigOut],
) -> List[FieldGroupConfigOut]:
    return copy.deepcopy(items)


def _get_cached_form_config(
        *,
        module_name: str,
        role_name: str,
) -> Optional[List[FieldGroupConfigOut]]:
    key = _cache_key(module_name, role_name)
    cached = _FORM_CONFIG_CACHE.get(key)
    if cached is None:
        return None

    expires_at, payload = cached
    now_ts = time.monotonic()
    if now_ts >= expires_at:
        _FORM_CONFIG_CACHE.pop(key, None)
        return None

    return _copy_group_config_list(payload)


def _set_cached_form_config(
        *,
        module_name: str,
        role_name: str,
        payload: List[FieldGroupConfigOut],
) -> None:
    key = _cache_key(module_name, role_name)
    expires_at = time.monotonic() + float(_FORM_CONFIG_CACHE_TTL_SECONDS)
    _FORM_CONFIG_CACHE[key] = (expires_at, _copy_group_config_list(payload))


def _invalidate_form_config_cache_for_module(module_name: str) -> None:
    normalized_module = str(module_name or "").strip()
    stale_keys = [
        key for key in _FORM_CONFIG_CACHE.keys()
        if key[0] == normalized_module
    ]
    for key in stale_keys:
        _FORM_CONFIG_CACHE.pop(key, None)


@router.get("", response_model=FieldConfigListOut)
async def list_configs(
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role),
):
    role = ctx.primary_role or ""
    if role not in {ROLE_SUPER_ADMIN, ROLE_MANAGER}:
        raise HTTPException(status_code=403, detail="无权限")

    rows = await _list_field_configs(db=db)
    items = [FieldConfigOut.from_orm(r) for r in rows]
    return FieldConfigListOut(items=items)


@router.get("/form-config", response_model=List[FieldGroupConfigOut])
async def get_form_config(
        module: str = Query("order"),
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role),
) -> List[FieldGroupConfigOut]:
    role_name = str(ctx.primary_role or "").strip()
    module_name = str(module or "").strip() or "order"

    cached = _get_cached_form_config(
        module_name=module_name,
        role_name=role_name,
    )
    if cached is not None:
        return cached

    stmt_group = (
        select(FieldGroup)
        .options(lazyload("*"))
        .where(FieldGroup.module == module_name)
        .order_by(FieldGroup.order_index.asc(), FieldGroup.id.asc())
    )
    groups = (await db.execute(stmt_group)).scalars().all()
    if not groups:
        _set_cached_form_config(
            module_name=module_name,
            role_name=role_name,
            payload=[],
        )
        return []

    group_ids = [int(g.id) for g in groups]
    stmt_map = (
        select(FieldGroupField)
        .options(lazyload("*"))
        .where(FieldGroupField.group_id.in_(group_ids))
        .order_by(
            FieldGroupField.group_id.asc(),
            FieldGroupField.order_index.asc(),
            FieldGroupField.id.asc(),
        )
    )
    mappings = (await db.execute(stmt_map)).scalars().all()
    if not mappings:
        _set_cached_form_config(
            module_name=module_name,
            role_name=role_name,
            payload=[],
        )
        return []

    field_ids = list({int(m.field_id) for m in mappings})
    stmt_field = (
        select(FieldConfig)
        .options(lazyload("*"))
        .where(
            FieldConfig.module == module_name,
            FieldConfig.id.in_(field_ids),
        )
        .order_by(FieldConfig.sort.asc(), FieldConfig.id.asc())
    )
    fields = (await db.execute(stmt_field)).scalars().all()
    field_map = {int(f.id): f for f in fields}

    mappings_by_group: Dict[int, List[FieldGroupField]] = {}
    for mapping in mappings:
        group_id = int(mapping.group_id)
        mappings_by_group.setdefault(group_id, []).append(mapping)

    out: List[FieldGroupConfigOut] = []

    for group in groups:
        field_items: List[FieldFormItemOut] = []

        for mapping in mappings_by_group.get(int(group.id), []):
            field = field_map.get(int(mapping.field_id))
            if not field:
                continue

            visible = _is_visible_for_role(
                view_roles=getattr(field, "view_roles", None),
                role_name=role_name,
                default_visible=_as_bool(getattr(field, "visible", 1), True),
            )
            if not visible:
                continue

            editable = _is_editable_for_role(
                edit_roles=getattr(field, "edit_roles", None),
                role_name=role_name,
                default_editable=_as_bool(getattr(field, "editable", 1), True),
            )

            field_items.append(
                FieldFormItemOut(
                    field_name=str(getattr(field, "field_name", "") or "").strip(),
                    label=str(getattr(field, "label", "") or "").strip(),
                    type=str(getattr(field, "type", "") or "text").strip(),
                    required=_as_bool(getattr(field, "required", 0), False),
                    visible=True,
                    editable=editable,
                    sort=int(getattr(field, "sort", 0) or 0),
                    options=getattr(field, "options", None),
                    validators=getattr(field, "validators", None),
                    extra=getattr(field, "extra", None),
                )
            )

        out.append(
            FieldGroupConfigOut(
                group_key=str(getattr(group, "group_key", "") or "").strip(),
                group_name=str(getattr(group, "group_name", "") or "").strip(),
                fields=field_items,
            )
        )

    _set_cached_form_config(
        module_name=module_name,
        role_name=role_name,
        payload=out,
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

    normalized_module = str(module).strip()

    try:
        row = await _upsert_field_config(
            db=db,
            module=normalized_module,
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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "字段配置保存失败") from exc

    _invalidate_form_config_cache_for_module(normalized_module)
    return FieldConfigOut.from_orm(row)
