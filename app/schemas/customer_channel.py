# app/schemas/customer_channel.py
# encoding: utf-8
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field

from app.schemas._base import OrmBaseModel


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


class CustomerGroupCreateIn(BaseModel):
    customer_code: str = Field(..., min_length=1)
    customer_name: str = Field(..., min_length=1)
    market: Optional[str] = None
    region: Optional[str] = None
    contacts: List[ContactItemIn] = Field(default_factory=list)


class CustomerGroupUpdateIn(BaseModel):
    customer_code: str = Field(..., min_length=1)
    customer_name: str = Field(..., min_length=1)
    market: Optional[str] = None
    region: Optional[str] = None
    contacts: List[ContactItemIn] = Field(default_factory=list)


class ChannelGroupCreateIn(BaseModel):
    channel_code: str = Field(..., min_length=1)
    channel_name: str = Field(..., min_length=1)
    region: Optional[str] = None
    contacts: List[ContactItemIn] = Field(default_factory=list)


class ChannelGroupUpdateIn(BaseModel):
    channel_code: str = Field(..., min_length=1)
    channel_name: str = Field(..., min_length=1)
    region: Optional[str] = None
    contacts: List[ContactItemIn] = Field(default_factory=list)


class OptionItem(BaseModel):
    id: int
    group_name: str


class OptionListOut(BaseModel):
    items: List[OptionItem] = Field(default_factory=list)
