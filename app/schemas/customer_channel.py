# app/schemas/customer_channel.py
# encoding: utf-8
from __future__ import annotations

import re
from typing import Any, List, Optional

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


class CustomerGroupListPageOut(BaseModel):
    items: List[CustomerGroupListItemOut] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0
    has_more: bool = False
    capabilities: CustomerChannelPageCapabilitiesOut = Field(default_factory=CustomerChannelPageCapabilitiesOut)


class ChannelGroupListPageOut(BaseModel):
    items: List[ChannelGroupListItemOut] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0
    has_more: bool = False
    capabilities: CustomerChannelPageCapabilitiesOut = Field(default_factory=CustomerChannelPageCapabilitiesOut)


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

    @root_validator
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
