# encoding: utf-8
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas._base import OrmBaseModel


class FieldConfigOut(OrmBaseModel):
    id: int
    module: str
    field_name: str
    label: str
    type: str
    required: int
    visible: int
    editable: int
    sort: int
    options: Optional[Any] = None
    validators: Optional[Any] = None
    extra: Optional[Any] = None
    view_roles: Optional[Any] = None
    edit_roles: Optional[Any] = None

class FieldConfigUpsertIn(BaseModel):
    """
    Upsert 入参（body）

    注意：
    - module / field_name 由 path 提供（避免 body 重复与冲突）
    """
    label: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., min_length=1, max_length=50)

    options: Optional[Any] = None
    validators: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None

    required: bool = False
    visible: bool = True
    editable: bool = True
    sort: int = 0

    view_roles: Optional[List[str]] = None
    edit_roles: Optional[List[str]] = None


class FieldConfigListOut(BaseModel):
    items: List[FieldConfigOut] = Field(default_factory=list)


# =========================
# form-config（前端分组表单配置）
# =========================

class FieldFormItemOut(OrmBaseModel):
    field_name: str
    label: str
    type: str

    required: bool
    visible: bool
    editable: bool
    sort: int

    options: Optional[Any] = None
    validators: Optional[Any] = None
    extra: Optional[Any] = None

class FieldGroupConfigOut(BaseModel):
    group_key: str
    group_name: str
    fields: List[FieldFormItemOut] = Field(default_factory=list)
