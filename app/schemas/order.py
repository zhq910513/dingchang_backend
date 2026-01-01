# encoding: utf-8
"""
订单相关 Pydantic 模型（Pydantic v1）

约束：
- 兼容 schemas/__init__.py 的导入：必须包含 OrderFilter
- 空值安全：dynamic_data / image_urls / images 给默认值，避免前端解构报错
- 兼容 ORM：orm_mode=True
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, validator


class ImageFileOut(BaseModel):
    id: int
    sha256: Optional[str] = None
    storage_key: Optional[str] = None
    original_name: Optional[str] = None
    content_type: Optional[str] = None
    url: str
    size: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class OrderImageOut(BaseModel):
    id: int
    order_id: int
    slot_key: str
    storage_key: Optional[str] = None
    image_url: str
    image_file_id: Optional[int] = None
    image_file: Optional[ImageFileOut] = None
    created_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class OrderInfoIn(BaseModel):
    insurance_expire_date: Optional[date] = None
    owner_phone: Optional[str] = None

    commercial_amount: Optional[float] = None
    compulsory_amount: Optional[float] = None
    vehicle_tax_amount: Optional[float] = None
    non_vehicle_amount: Optional[float] = None

    channel_commercial_point: Optional[float] = None

    # ✅ 新增：渠道-商业后补点位
    channel_commercial_supplement_point: Optional[float] = None

    channel_compulsory_point: Optional[float] = None
    channel_vehicle_tax_point: Optional[float] = None
    channel_non_vehicle_point: Optional[float] = None
    channel_reward: Optional[float] = None

    customer_commercial_point: Optional[float] = None

    # ✅ 新增：客户-商业后补点位
    customer_commercial_supplement_point: Optional[float] = None

    customer_compulsory_point: Optional[float] = None
    customer_vehicle_tax_point: Optional[float] = None
    customer_non_vehicle_point: Optional[float] = None
    customer_reward: Optional[float] = None


class OrderInfoOut(BaseModel):
    insurance_expire_date: Optional[date] = None
    owner_phone: str = ""

    commercial_amount: float = 0
    compulsory_amount: float = 0
    vehicle_tax_amount: float = 0
    non_vehicle_amount: float = 0
    premium_total: float = 0

    channel_commercial_point: float = 0

    # ✅ 新增：渠道-商业后补点位
    channel_commercial_supplement_point: float = 0

    channel_compulsory_point: float = 0
    channel_vehicle_tax_point: float = 0
    channel_non_vehicle_point: float = 0
    channel_reward: float = 0
    channel_total: float = 0

    customer_commercial_point: float = 0

    # ✅ 新增：客户-商业后补点位
    customer_commercial_supplement_point: float = 0

    customer_compulsory_point: float = 0
    customer_vehicle_tax_point: float = 0
    customer_non_vehicle_point: float = 0
    customer_reward: float = 0
    customer_total: float = 0

    profit: float = 0

    @validator("owner_phone", pre=True, always=True)
    def _owner_phone_none_to_empty(cls, v):
        if v is None:
            return ""
        return str(v)

    class Config:
        orm_mode = True


class OrderFilter(BaseModel):
    page: int = 1
    page_size: int = 20

    is_finished: Optional[bool] = None
    is_rebate: Optional[bool] = None
    is_paid: Optional[bool] = None

    salesperson_id: Optional[int] = None
    created_by: Optional[int] = None

    owner_name: Optional[str] = None
    id_number: Optional[str] = None
    plate_no: Optional[str] = None
    engine_no: Optional[str] = None
    vehicle_name: Optional[str] = None
    vehicle_model: Optional[str] = None
    vin: Optional[str] = None
    remark: Optional[str] = None
    created_date: Optional[str] = None  # YYYY-MM-DD

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None


class OrderCreate(BaseModel):
    module: Optional[str] = "order"

    salesperson_id: Optional[int] = None
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    image_urls: List[str] = Field(default_factory=list)
    ocr_raw_json: Optional[Dict[str, Any]] = None

    status: Optional[int] = 0
    audit_status: Optional[int] = 0

    is_finished: Optional[bool] = False
    is_rebate: Optional[bool] = False
    is_paid: Optional[bool] = False

    order_info: Optional[OrderInfoIn] = None


class OrderUpdate(BaseModel):
    module: Optional[str] = None

    salesperson_id: Optional[int] = None
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    dynamic_data: Optional[Dict[str, Any]] = None
    image_urls: Optional[List[str]] = None

    status: Optional[int] = None
    audit_status: Optional[int] = None

    is_finished: Optional[bool] = None
    is_rebate: Optional[bool] = None
    is_paid: Optional[bool] = None

    order_info: Optional[OrderInfoIn] = None


class OrderStatusUpdate(BaseModel):
    is_finished: Optional[bool] = None
    is_rebate: Optional[bool] = None
    is_paid: Optional[bool] = None


class OrderOut(BaseModel):
    id: int
    created_by: int

    salesperson_id: Optional[int] = None
    salesperson_name: Optional[str] = None

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    customer_group_name: Optional[str] = None
    channel_group_name: Optional[str] = None

    customer_group_market: Optional[str] = None

    is_finished: bool = False
    is_rebate: bool = False
    is_paid: bool = False

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)

    image_urls: List[str] = Field(default_factory=list)
    images: List[OrderImageOut] = Field(default_factory=list)

    order_info: Optional[OrderInfoOut] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class OrderListResponse(BaseModel):
    total: int = 0
    items: List[OrderOut] = Field(default_factory=list)
