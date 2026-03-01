# encoding: utf-8
from __future__ import annotations

from typing import Any, List, Optional
from pydantic import BaseModel, Field


class FieldConfigOut(BaseModel):
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


class FieldConfigListOut(BaseModel):
    items: List[FieldConfigOut] = Field(default_factory=list)
