# app/schemas/customer_channel.py
# encoding: utf-8
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, validator


# -------------------------
# ✅ 兼容旧代码/旧导入：宽松 ContactItem（用于输出/历史数据）
# -------------------------
class ContactItem(BaseModel):
    type: Optional[str] = Field(default="")
    value: Optional[str] = Field(default="")


# -------------------------
# ✅ 创建入参：严格（只允许手机号/座机）
# -------------------------
class ContactItemIn(BaseModel):
    type: str = Field(default="mobile", description="mobile=手机号, tel=座机")
    value: str = Field(default="", description="手机号/座机号码")

    @validator("type", pre=True, always=True)
    def _v_type(cls, v):
        s = str(v or "").strip().lower()
        if s not in ("mobile", "tel"):
            raise ValueError("contacts.type must be 'mobile' or 'tel'")
        return s

    @validator("value", pre=True, always=True)
    def _v_value_clean(cls, v):
        s = str(v or "").strip()
        if not s:
            raise ValueError("contacts.value is required")
        # 仅允许数字和横杠（去空格）
        s2 = "".join(ch for ch in s.replace(" ", "") if (ch.isdigit() or ch == "-"))
        if not s2:
            raise ValueError("contacts.value invalid")
        return s2

    @validator("value")
    def _v_value_format(cls, v, values):
        t = str(values.get("type") or "mobile").lower()
        raw = str(v or "").strip()
        digits = raw.replace("-", "")

        # 手机号：11位大陆手机号
        if t == "mobile":
            if not digits.isdigit():
                raise ValueError("手机号格式不正确")
            if len(digits) != 11:
                raise ValueError("手机号格式不正确")
            if not digits.startswith(("13", "14", "15", "16", "17", "18", "19")):
                raise ValueError("手机号格式不正确")
            return digits  # 归一：手机号不保留横杠

        # 座机：400/800 或 区号座机（可分机）
        if t == "tel":
            if digits.isdigit() and (digits.startswith("400") or digits.startswith("800")) and len(digits) == 10:
                return digits
            import re

            if not re.match(r"^(0\d{2,3}-?)?\d{7,8}(-\d{1,6})?$", raw):
                raise ValueError("座机格式不正确")
            return raw

        raise ValueError("contacts.type invalid")


# 兼容别名：有些地方可能用 ContactItemOut
ContactItemOut = ContactItem


class CustomerGroupCreate(BaseModel):
    customer_code: str
    customer_name: str
    market: Optional[str] = None
    region: Optional[str] = ""
    contacts: Optional[List[ContactItemIn]] = None


class ChannelGroupCreate(BaseModel):
    channel_code: str
    channel_name: str
    region: Optional[str] = ""
    contacts: Optional[List[ContactItemIn]] = None


# ✅ 编辑入参：字段沿用创建结构，保持一致性
class CustomerGroupUpdate(BaseModel):
    customer_code: str
    customer_name: str
    market: Optional[str] = None
    region: Optional[str] = ""
    contacts: Optional[List[ContactItemIn]] = None


class ChannelGroupUpdate(BaseModel):
    channel_code: str
    channel_name: str
    region: Optional[str] = ""
    contacts: Optional[List[ContactItemIn]] = None


class CustomerGroupOut(BaseModel):
    id: int
    customer_code: str
    customer_name: str
    market: Optional[str] = None

    # ✅ 团队（输出用；前端可用于展示/调试/筛选）
    team_name: Optional[str] = None

    # ✅ 前端下拉统一读这个（这里给“名称”，保证订单/列表展示为纯名称）
    group_name: str

    region: str = ""
    contacts: List[ContactItem] = Field(default_factory=list)
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None
    created_at: Optional[datetime] = None

    deleted_at: Optional[datetime] = None
    is_deleted: bool = False


class ChannelGroupOut(BaseModel):
    id: int
    channel_code: str
    channel_name: str

    # ✅ 团队（输出用；前端可用于展示/调试/筛选）
    team_name: Optional[str] = None

    # ✅ 前端下拉统一读这个（这里给“名称”）
    group_name: str

    region: str = ""
    contacts: List[ContactItem] = Field(default_factory=list)
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None
    created_at: Optional[datetime] = None

    deleted_at: Optional[datetime] = None
    is_deleted: bool = False


class CustomerGroupListResponse(BaseModel):
    total: int
    items: List[CustomerGroupOut]


class ChannelGroupListResponse(BaseModel):
    total: int
    items: List[ChannelGroupOut]
