# encoding: utf-8
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas._base import OrmBaseModel


class OrderInfoIn(BaseModel):
    insurance_expire_date: Optional[date] = None
    owner_phone: Optional[str] = None
    remark: Optional[str] = None

    commercial_amount: Optional[Any] = None
    compulsory_amount: Optional[Any] = None
    vehicle_tax_amount: Optional[Any] = None
    non_vehicle_amount: Optional[Any] = None

    channel_commercial_point: Optional[Any] = None
    channel_commercial_supplement_point: Optional[Any] = None
    channel_compulsory_point: Optional[Any] = None
    channel_vehicle_tax_point: Optional[Any] = None
    channel_non_vehicle_point: Optional[Any] = None
    channel_reward: Optional[Any] = None

    customer_commercial_point: Optional[Any] = None
    customer_commercial_supplement_point: Optional[Any] = None
    customer_compulsory_point: Optional[Any] = None
    customer_vehicle_tax_point: Optional[Any] = None
    customer_non_vehicle_point: Optional[Any] = None
    customer_reward: Optional[Any] = None


class OrderInfoOut(OrmBaseModel):
    id: int
    order_id: int

    insurance_expire_date: Optional[date] = None
    owner_phone: Optional[str] = None
    remark: Optional[str] = None

    commercial_amount: Any = 0
    compulsory_amount: Any = 0
    vehicle_tax_amount: Any = 0
    non_vehicle_amount: Any = 0
    premium_total: Any = 0

    channel_commercial_point: Any = 0
    channel_commercial_supplement_point: Any = 0
    channel_compulsory_point: Any = 0
    channel_vehicle_tax_point: Any = 0
    channel_non_vehicle_point: Any = 0
    channel_reward: Any = 0
    channel_total: Any = 0

    customer_commercial_point: Any = 0
    customer_commercial_supplement_point: Any = 0
    customer_compulsory_point: Any = 0
    customer_vehicle_tax_point: Any = 0
    customer_non_vehicle_point: Any = 0
    customer_reward: Any = 0
    customer_total: Any = 0

    profit: Any = 0

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class OrderImageOut(OrmBaseModel):
    id: int
    order_id: int
    slot_key: str
    storage_key: str
    image_url: str = ""
    image_file_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class OrderOut(BaseModel):
    # core
    id: int
    created_by: int
    salesperson_id: int
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    # derived
    manager_id: Optional[int] = None
    manager_name: Optional[str] = None
    team_name: Optional[str] = None
    team_names: List[str] = Field(default_factory=list)

    # flags
    is_finished: bool = False
    is_rebate: bool = False
    is_paid: bool = False

    # data
    dynamic_data: Dict[str, Any] = Field(default_factory=dict)

    image_urls: List[str] = Field(default_factory=list)
    images: List[OrderImageOut] = Field(default_factory=list)

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # display
    customer_group_name: Optional[str] = None
    channel_group_name: Optional[str] = None
    salesperson_name: Optional[str] = None
    customer_group_market: Optional[str] = None

    order_info: Optional[OrderInfoOut] = None


class OrderListMeta(BaseModel):
    source: str = "orders"
    capabilities: Dict[str, Any] = Field(default_factory=dict)


class OrderListResponse(BaseModel):
    total: int
    items: List[OrderOut] = Field(default_factory=list)
    meta: OrderListMeta = Field(default_factory=OrderListMeta)


class OrderCreate(BaseModel):
    module: str = "order"
    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    ocr_raw_json: Dict[str, Any] = Field(default_factory=dict)

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    salesperson_id: Optional[int] = None

    status: int = 0
    audit_status: int = 0
    is_finished: bool = False

    order_info: Optional[OrderInfoIn] = None


class OrderUpdate(BaseModel):
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    salesperson_id: Optional[int] = None
    dynamic_data: Optional[Dict[str, Any]] = None
    order_info: Optional[OrderInfoIn] = None


class OrderStatusUpdate(BaseModel):
    is_finished: Optional[bool] = None
    is_rebate: Optional[bool] = None
    is_paid: Optional[bool] = None


class OrderInfoPatch(BaseModel):
    order_info: Optional[OrderInfoIn] = None
