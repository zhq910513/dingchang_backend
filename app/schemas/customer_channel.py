# app/schemas/customer_channel.py
# encoding: utf-8
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, root_validator, validator

from app.schemas._base import OrmBaseModel

_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
_LANDLINE_RE = re.compile(r"^(0\d{2,3}-?)?\d{7,8}(-\d{1,6})?$")
_SERVICE_RE = re.compile(r"^(400|800)\d{7}$")


def _trim_text(v: Any) -> str:
    return str(v or "").strip()


def _clean_contact_value(v: Any) -> str:
    s = str(v or "").replace(" ", "")
    return re.sub(r"[^0-9\-]", "", s)


class CustomerGroupOptionOut(OrmBaseModel):
    id: int
    customer_code: str
    customer_name: str


class ChannelGroupOptionOut(OrmBaseModel):
    id: int
    channel_code: str
    channel_name: str


class CustomerGroupOptionPageOut(BaseModel):
    items: List[CustomerGroupOptionOut] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0
    has_more: bool = False


class ChannelGroupOptionPageOut(BaseModel):
    items: List[ChannelGroupOptionOut] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0
    has_more: bool = False


class CustomerChannelPageCapabilitiesOut(BaseModel):
    can_create: bool = False
    can_edit: bool = False
    can_delete: bool = False
    can_view_deleted: bool = False


class CustomerGroupRowMetaOut(BaseModel):
    capabilities: Dict[str, bool] = Field(default_factory=dict)


class CustomerGroupPaginationMetaOut(BaseModel):
    page: int = 1
    page_size: int = 20


class CustomerGroupPageMetaOut(BaseModel):
    capabilities: Dict[str, bool] = Field(default_factory=dict)
    scopes: Dict[str, Any] = Field(default_factory=dict)
    pagination: CustomerGroupPaginationMetaOut = Field(default_factory=CustomerGroupPaginationMetaOut)


class ChannelGroupRowMetaOut(BaseModel):
    capabilities: Dict[str, bool] = Field(default_factory=dict)


class ChannelGroupPaginationMetaOut(BaseModel):
    page: int = 1
    page_size: int = 20


class ChannelGroupPageMetaOut(BaseModel):
    capabilities: Dict[str, bool] = Field(default_factory=dict)
    scopes: Dict[str, Any] = Field(default_factory=dict)
    pagination: ChannelGroupPaginationMetaOut = Field(default_factory=ChannelGroupPaginationMetaOut)


class CustomerGroupListItemOut(BaseModel):
    id: int
    customer_code: str
    customer_name: str
    market: Optional[str] = None
    region: Optional[str] = None
    contacts: Any = None
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    deleted_at: Optional[str] = None
    is_deleted: int = 0
    meta: CustomerGroupRowMetaOut = Field(default_factory=CustomerGroupRowMetaOut)


class ChannelGroupListItemOut(BaseModel):
    id: int
    channel_code: str
    channel_name: str
    region: Optional[str] = None
    contacts: Any = None
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    deleted_at: Optional[str] = None
    is_deleted: int = 0
    meta: ChannelGroupRowMetaOut = Field(default_factory=ChannelGroupRowMetaOut)


class CustomerGroupListPageOut(BaseModel):
    total: int = 0
    items: List[CustomerGroupListItemOut] = Field(default_factory=list)
    meta: CustomerGroupPageMetaOut = Field(default_factory=CustomerGroupPageMetaOut)


class ChannelGroupListPageOut(BaseModel):
    total: int = 0
    items: List[ChannelGroupListItemOut] = Field(default_factory=list)
    meta: ChannelGroupPageMetaOut = Field(default_factory=ChannelGroupPageMetaOut)


class CustomerGroupOut(BaseModel):
    id: int
    customer_code: str
    customer_name: str
    market: Optional[str] = None
    region: Optional[str] = None
    contacts: Any = None
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    deleted_at: Optional[str] = None
    is_deleted: int = 0


class ChannelGroupOut(BaseModel):
    id: int
    channel_code: str
    channel_name: str
    region: Optional[str] = None
    contacts: Any = None
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    deleted_at: Optional[str] = None
    is_deleted: int = 0


class ContactItemIn(BaseModel):
    type: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)

    class Config:
        extra = "forbid"

    @validator("type", pre=True)
    def normalize_type(cls, v: Any) -> str:
        s = _trim_text(v).lower()
        if s not in {"mobile", "tel"}:
            raise ValueError("联系方式类型仅支持：mobile/tel")
        return s

    @validator("value", pre=True)
    def normalize_value(cls, v: Any) -> str:
        s = _clean_contact_value(v)
        if not s:
            raise ValueError("联系方式不能为空")
        return s

    @root_validator(skip_on_failure=True)
    def validate_pair(cls, values):
        contact_type = values.get("type")
        value = values.get("value")

        if not contact_type or not value:
            return values

        if contact_type == "mobile":
            digits = value.replace("-", "")
            if not _MOBILE_RE.fullmatch(digits):
                raise ValueError("手机号格式不正确（需 11 位大陆手机号）")
            values["value"] = digits
            return values

        digits = value.replace("-", "")
        if _SERVICE_RE.fullmatch(digits):
            values["value"] = digits
            return values

        if not _LANDLINE_RE.fullmatch(value):
            raise ValueError("座机格式不正确（示例：010-88888888 / 0571-8888888 / 010-88888888-123 / 400xxxxxxx）")

        return values


class CustomerGroupCreateIn(BaseModel):
    customer_code: str = Field(..., min_length=1)
    customer_name: str = Field(..., min_length=1)
    market: Optional[str] = None
    region: Optional[str] = None
    contacts: List[ContactItemIn] = Field(default_factory=list)

    class Config:
        extra = "forbid"

    @validator("customer_code", "customer_name", pre=True)
    def normalize_required_text(cls, v: Any) -> str:
        s = _trim_text(v)
        if not s:
            raise ValueError("必填字段不能为空")
        return s

    @validator("market", "region", pre=True, always=True)
    def normalize_optional_text(cls, v: Any) -> Optional[str]:
        s = _trim_text(v)
        return s or None


class CustomerGroupUpdateIn(BaseModel):
    customer_code: str = Field(..., min_length=1)
    customer_name: str = Field(..., min_length=1)
    market: Optional[str] = None
    region: Optional[str] = None
    contacts: List[ContactItemIn] = Field(default_factory=list)

    class Config:
        extra = "forbid"

    @validator("customer_code", "customer_name", pre=True)
    def normalize_required_text(cls, v: Any) -> str:
        s = _trim_text(v)
        if not s:
            raise ValueError("必填字段不能为空")
        return s

    @validator("market", "region", pre=True, always=True)
    def normalize_optional_text(cls, v: Any) -> Optional[str]:
        s = _trim_text(v)
        return s or None


class ChannelGroupCreateIn(BaseModel):
    channel_code: str = Field(..., min_length=1)
    channel_name: str = Field(..., min_length=1)
    region: Optional[str] = None
    contacts: List[ContactItemIn] = Field(default_factory=list)

    class Config:
        extra = "forbid"

    @validator("channel_code", "channel_name", pre=True)
    def normalize_required_text(cls, v: Any) -> str:
        s = _trim_text(v)
        if not s:
            raise ValueError("必填字段不能为空")
        return s

    @validator("region", pre=True, always=True)
    def normalize_optional_text(cls, v: Any) -> Optional[str]:
        s = _trim_text(v)
        return s or None


class ChannelGroupUpdateIn(BaseModel):
    channel_code: str = Field(..., min_length=1)
    channel_name: str = Field(..., min_length=1)
    region: Optional[str] = None
    contacts: List[ContactItemIn] = Field(default_factory=list)

    class Config:
        extra = "forbid"

    @validator("channel_code", "channel_name", pre=True)
    def normalize_required_text(cls, v: Any) -> str:
        s = _trim_text(v)
        if not s:
            raise ValueError("必填字段不能为空")
        return s

    @validator("region", pre=True, always=True)
    def normalize_optional_text(cls, v: Any) -> Optional[str]:
        s = _trim_text(v)
        return s or None


class OptionItem(BaseModel):
    id: int
    group_name: str


class OptionListOut(BaseModel):
    items: List[OptionItem] = Field(default_factory=list)
