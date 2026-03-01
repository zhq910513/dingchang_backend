# encoding: utf-8
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field


class CustomerGroupOut(BaseModel):
    id: int
    team_name: Optional[str] = None
    customer_code: str
    customer_name: str
    market: Optional[str] = None
    region: Optional[str] = None
    contacts: Any = None
    deleted_at: Optional[str] = None
    is_deleted: int = 0


class ChannelGroupOut(BaseModel):
    id: int
    team_name: Optional[str] = None
    channel_code: str
    channel_name: str
    region: Optional[str] = None
    contacts: Any = None
    deleted_at: Optional[str] = None
    is_deleted: int = 0


class OptionItem(BaseModel):
    id: int
    group_name: str


class OptionListOut(BaseModel):
    items: List[OptionItem] = Field(default_factory=list)
