# encoding: utf-8
"""
字段配置相关接口（统一新版字段方案）

说明：
- 仅保留新字段：
    * field_name / label / type
    * options / validators / extra
    * required / visible / editable / sort
    * view_roles / edit_roles
- 不再兼容旧的 field_key / field_label / field_type 等老字段名。
"""

import logging
from typing import List, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.db import get_db
from app.core.constants import ROLE_SUPER_ADMIN, ROLE_MANAGER
from app.models.field_config import FieldConfig, FieldGroup, FieldGroupField
from app.api.deps import get_current_user_with_role
from app.schemas.field_config import FieldConfigOut, FieldConfigListOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/field-config", tags=["field-config"])


# ----------------------------
# Pydantic Models
# ----------------------------


class FieldDefinitionCreate(BaseModel):
    """
    新字段定义
    """
    module: str = "order"

    field_name: str = Field(..., description="字段唯一标识（module 内唯一）")
    label: str = Field(..., description="展示名称")
    type: str = Field("text", description="字段类型：text/number/date/select/amount/...")

    # JSON 扩展
    options: Optional[Dict[str, Any]] = None       # 下拉/枚举/远程选项定义等
    validators: Optional[Dict[str, Any]] = None    # 校验规则
    extra: Optional[Dict[str, Any]] = None         # 任意扩展字段

    # 通用控制
    required: bool = False
    visible: bool = True
    editable: bool = True
    sort: int = 0

    # 角色控制（JSON list）
    view_roles: Optional[List[str]] = None   # ["super_admin", "manager", ...]
    edit_roles: Optional[List[str]] = None


class FieldGroupCreate(BaseModel):
    module: str = "order"

    group_key: str
    group_name: str
    order_index: int = 0


class GroupFieldMappingCreate(BaseModel):
    group_id: int
    field_id: int
    order_index: int = 0


class FieldOut(BaseModel):
    field_name: str
    label: str
    type: str

    required: bool
    visible: bool
    editable: bool
    sort: int

    options: Optional[Dict[str, Any]] = None
    validators: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None

    class Config:
        orm_mode = True


class FieldGroupConfigOut(BaseModel):
    group_key: str
    group_name: str
    fields: List[FieldOut]


# ----------------------------
# Helpers
# ----------------------------

def _to_field_config_out(x: FieldConfig) -> FieldConfigOut:
    return FieldConfigOut(
        id=int(getattr(x, "id", 0) or 0),
        module=str(getattr(x, "module", "") or ""),
        field_name=str(getattr(x, "field_name", "") or ""),
        label=str(getattr(x, "label", "") or ""),
        type=str(getattr(x, "type", "") or ""),
        required=int(bool(getattr(x, "required", False))),
        visible=int(bool(getattr(x, "visible", True))),
        editable=int(bool(getattr(x, "editable", True))),
        sort=int(getattr(x, "sort", 0) or 0),
        options=getattr(x, "options", None),
        validators=getattr(x, "validators", None),
        extra=getattr(x, "extra", None),
        view_roles=getattr(x, "view_roles", None),
        edit_roles=getattr(x, "edit_roles", None),
    )


def _is_editable_for_role(
    edit_roles: Optional[List[str]],
    role_name: Optional[str],
    default_editable: bool,
) -> bool:
    """
    计算当前角色下字段是否可编辑：
    - edit_roles 为空：使用字段本身 editable
    - 否则：仅当 role_name 在 edit_roles 中时可编辑
    """
    if edit_roles is None:
        return default_editable
    if role_name is None:
        return False
    return role_name in edit_roles


def _is_visible_for_role(
    view_roles: Optional[List[str]],
    role_name: Optional[str],
    default_visible: bool,
) -> bool:
    """
    计算当前角色下字段是否可见：
    - view_roles 为空：使用字段本身 visible
    - 否则：仅当 role_name 在 view_roles 中时可见
    """
    if view_roles is None:
        return default_visible
    if role_name is None:
        return False
    return role_name in view_roles


# ----------------------------
# Routes
# ----------------------------


@router.post("/field", status_code=201)
async def create_field_definition(
    payload: FieldDefinitionCreate,
    db: AsyncSession = Depends(get_db),
    user_role=Depends(get_current_user_with_role),
):
    user, role_name = user_role
    if role_name not in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        raise HTTPException(status_code=403, detail="No permission")

    stmt = select(FieldConfig).where(
        FieldConfig.module == payload.module,
        FieldConfig.field_name == payload.field_name,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="field already exists in module")

    f = FieldConfig(
        module=payload.module,
        field_name=payload.field_name,
        label=payload.label,
        type=payload.type,
        required=payload.required,
        visible=payload.visible,
        editable=payload.editable,
        sort=payload.sort,
        options=payload.options,
        validators=payload.validators,
        extra=payload.extra,
        view_roles=payload.view_roles,
        edit_roles=payload.edit_roles,
    )

    db.add(f)
    await db.commit()
    await db.refresh(f)

    logger.info(
        "FieldConfig created: module=%s, field_name=%s, user_id=%s",
        payload.module,
        payload.field_name,
        user.id,
    )
    return {"id": f.id}


@router.post("/group", status_code=201)
async def create_field_group(
    payload: FieldGroupCreate,
    db: AsyncSession = Depends(get_db),
    user_role=Depends(get_current_user_with_role),
):
    user, role_name = user_role
    if role_name not in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        raise HTTPException(status_code=403, detail="No permission")

    stmt = select(FieldGroup).where(
        FieldGroup.module == payload.module,
        FieldGroup.group_key == payload.group_key,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="group_key already exists in module")

    g = FieldGroup(
        module=payload.module,
        group_key=payload.group_key,
        group_name=payload.group_name,
        order_index=payload.order_index,
    )
    db.add(g)
    await db.commit()
    await db.refresh(g)

    logger.info(
        "FieldGroup created: module=%s, key=%s, user_id=%s",
        payload.module,
        payload.group_key,
        user.id,
    )
    return {"id": g.id}


@router.post("/group-mapping", status_code=201)
async def create_group_mapping(
    payload: GroupFieldMappingCreate,
    db: AsyncSession = Depends(get_db),
    user_role=Depends(get_current_user_with_role),
):
    user, role_name = user_role
    if role_name not in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        raise HTTPException(status_code=403, detail="No permission")

    # 防重复
    stmt_exist = select(FieldGroupField).where(
        FieldGroupField.group_id == payload.group_id,
        FieldGroupField.field_id == payload.field_id,
    )
    r = await db.execute(stmt_exist)
    if r.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="mapping already exists")

    m = FieldGroupField(
        group_id=payload.group_id,
        field_id=payload.field_id,
        order_index=payload.order_index,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)

    logger.info(
        "FieldGroupField created: group_id=%s, field_id=%s, user_id=%s",
        payload.group_id,
        payload.field_id,
        user.id,
    )
    return {"id": m.id}


@router.get("/form-config", response_model=List[FieldGroupConfigOut])
async def get_form_config(
    module: str = "order",
    db: AsyncSession = Depends(get_db),
    user_role=Depends(get_current_user_with_role),
):
    user, role_name = user_role

    # 取 module 下所有 group
    stmt_group = (
        select(FieldGroup)
        .where(FieldGroup.module == module)
        .order_by(FieldGroup.order_index)
    )
    result_group = await db.execute(stmt_group)
    groups = result_group.scalars().all()

    if not groups:
        return []

    group_ids = [g.id for g in groups]

    # 取 mapping
    stmt_map = select(FieldGroupField).where(FieldGroupField.group_id.in_(group_ids))
    result_map = await db.execute(stmt_map)
    mappings = result_map.scalars().all()

    field_ids = [m.field_id for m in mappings]
    if not field_ids:
        return []

    # 取字段定义（只取同 module 的）
    stmt_field = select(FieldConfig).where(
        FieldConfig.module == module,
        FieldConfig.id.in_(field_ids),
    )
    result_field = await db.execute(stmt_field)
    fields = result_field.scalars().all()
    field_map = {f.id: f for f in fields}

    # 组装
    from collections import defaultdict

    group_fields_map = defaultdict(list)
    for m in mappings:
        group_fields_map[m.group_id].append(m)

    res: List[FieldGroupConfigOut] = []

    for g in groups:
        field_items: List[FieldOut] = []

        for m in sorted(group_fields_map[g.id], key=lambda x: x.order_index):
            f = field_map.get(m.field_id)
            if not f:
                continue

            # 角色可见过滤
            if not _is_visible_for_role(f.view_roles, role_name, f.visible):
                continue

            editable_for_role = _is_editable_for_role(
                f.edit_roles,
                role_name,
                f.editable,
            )

            field_items.append(
                FieldOut(
                    field_name=f.field_name,
                    label=f.label,
                    type=f.type,
                    required=f.required,
                    visible=True,  # 通过上面的过滤后，对当前角色来说就是可见
                    editable=editable_for_role,
                    sort=f.sort,
                    options=f.options,
                    validators=f.validators,
                    extra=f.extra,
                )
            )

        res.append(
            FieldGroupConfigOut(
                group_key=g.group_key,
                group_name=g.group_name,
                fields=field_items,
            )
        )

    return res
