# app/schemas/order.py
# encoding: utf-8

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class OrmBaseModel(BaseModel):
    """Pydantic v1/v2 兼容：允许 ORM 对象直接输出。"""

    model_config = {"from_attributes": True}

    class Config:
        orm_mode = True


# =========================
# Slot Images（唯一真源契约）
# =========================

class SlotImageItemOut(OrmBaseModel):
    """卡槽图片条目（字段固定，不多不少）"""
    order_image_id: int
    image_file_id: Optional[int] = None
    storage_key: str = ""
    url: str = ""

    md5: Optional[str] = None
    etag: Optional[str] = None
    size: Optional[int] = None
    content_type: Optional[str] = None
    original_name: Optional[str] = None

    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SlotImageNodeOut(OrmBaseModel):
    """卡槽节点（字段固定，不多不少）"""
    slot_key: str
    title: str
    multi: bool = False
    ocr: bool = False
    images: List[SlotImageItemOut] = Field(default_factory=list)


# 为了避免上游代码 import 炸裂，保留旧类名，但其字段已对齐到新契约
class OrderImageOut(SlotImageItemOut):
    pass


# =========================
# Order Info（扩展信息）
# =========================

class OrderInfoIn(OrmBaseModel):
    insurance_expire_date: Optional[str] = None
    owner_phone: Optional[str] = None

    commercial_amount: Optional[float] = None
    compulsory_amount: Optional[float] = None
    vehicle_tax_amount: Optional[float] = None
    non_vehicle_amount: Optional[float] = None
    premium_total: Optional[float] = None

    channel_commercial_point: Optional[float] = None
    channel_commercial_supplement_point: Optional[float] = None
    channel_compulsory_point: Optional[float] = None
    channel_vehicle_tax_point: Optional[float] = None
    channel_non_vehicle_point: Optional[float] = None
    channel_reward: Optional[float] = None
    channel_total: Optional[float] = None

    customer_commercial_point: Optional[float] = None
    customer_commercial_supplement_point: Optional[float] = None
    customer_compulsory_point: Optional[float] = None
    customer_vehicle_tax_point: Optional[float] = None
    customer_non_vehicle_point: Optional[float] = None
    customer_reward: Optional[float] = None
    customer_total: Optional[float] = None

    profit: Optional[float] = None
    remark: Optional[str] = None


class OrderInfoOut(OrmBaseModel):
    insurance_expire_date: Optional[str] = None
    owner_phone: Optional[str] = None

    commercial_amount: Optional[float] = None
    compulsory_amount: Optional[float] = None
    vehicle_tax_amount: Optional[float] = None
    non_vehicle_amount: Optional[float] = None
    premium_total: Optional[float] = None

    channel_commercial_point: Optional[float] = None
    channel_commercial_supplement_point: Optional[float] = None
    channel_compulsory_point: Optional[float] = None
    channel_vehicle_tax_point: Optional[float] = None
    channel_non_vehicle_point: Optional[float] = None
    channel_reward: Optional[float] = None
    channel_total: Optional[float] = None

    customer_commercial_point: Optional[float] = None
    customer_commercial_supplement_point: Optional[float] = None
    customer_compulsory_point: Optional[float] = None
    customer_vehicle_tax_point: Optional[float] = None
    customer_non_vehicle_point: Optional[float] = None
    customer_reward: Optional[float] = None
    customer_total: Optional[float] = None

    profit: Optional[float] = None
    remark: Optional[str] = None


# =========================
# Order List（列表专用契约）
# =========================

class OrderListDynamicDataOut(OrmBaseModel):
    owner_name: Optional[str] = None
    plate_no: Optional[str] = None
    vin: Optional[str] = None
    engine_no: Optional[str] = None
    vehicle_model: Optional[str] = None
    first_register_date: Optional[str] = None
    id_number: Optional[str] = None


class OrderListInfoOut(OrmBaseModel):
    insurance_expire_date: Optional[str] = None
    owner_phone: Optional[str] = None

    commercial_amount: Optional[float] = None
    compulsory_amount: Optional[float] = None
    vehicle_tax_amount: Optional[float] = None
    non_vehicle_amount: Optional[float] = None

    channel_commercial_point: Optional[float] = None
    channel_commercial_supplement_point: Optional[float] = None
    channel_compulsory_point: Optional[float] = None
    channel_vehicle_tax_point: Optional[float] = None
    channel_non_vehicle_point: Optional[float] = None
    channel_reward: Optional[float] = None
    channel_total: Optional[float] = None

    customer_commercial_point: Optional[float] = None
    customer_commercial_supplement_point: Optional[float] = None
    customer_compulsory_point: Optional[float] = None
    customer_vehicle_tax_point: Optional[float] = None
    customer_non_vehicle_point: Optional[float] = None
    customer_reward: Optional[float] = None
    customer_total: Optional[float] = None

    profit: Optional[float] = None


# =========================
# Order（主对象）
# =========================

class OrderCreate(OrmBaseModel):
    module: str = "order"
    created_by: Optional[int] = None
    salesperson_id: Optional[int] = None
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    is_finished: bool = False
    order_info: Optional[OrderInfoIn] = None

    status: int = 0
    audit_status: int = 0

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    ocr_raw_json: Dict[str, Any] = Field(default_factory=dict)


class OrderUpdate(OrmBaseModel):
    salesperson_id: Optional[int] = None
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    status: Optional[int] = None
    audit_status: Optional[int] = None
    dynamic_data: Optional[Dict[str, Any]] = None
    ocr_raw_json: Optional[Dict[str, Any]] = None

    order_info: Optional[OrderInfoIn] = None


class OrderStatusUpdate(OrmBaseModel):
    is_finished: Optional[bool] = None
    is_rebate: Optional[bool] = None
    is_paid: Optional[bool] = None


class OrderOut(OrmBaseModel):
    id: int
    module: str

    created_by: int
    salesperson_id: int

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    customer_group_name: Optional[str] = None
    channel_group_name: Optional[str] = None

    is_finished: bool = False
    is_rebate: bool = False
    is_paid: bool = False

    status: int = 0
    audit_status: int = 0

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    ocr_raw_json: Dict[str, Any] = Field(default_factory=dict)

    slot_images: List[SlotImageNodeOut] = Field(default_factory=list)

    order_info: Optional[OrderInfoOut] = None

    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OrderListItemOut(OrmBaseModel):
    id: int

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    salesperson_id: Optional[int] = None

    customer_group_name: Optional[str] = None
    channel_group_name: Optional[str] = None
    customer_group_market: Optional[str] = None

    salesperson_name: Optional[str] = None
    manager_name: Optional[str] = None
    team_name: Optional[str] = None
    team_names: List[str] = Field(default_factory=list)

    is_finished: bool = False
    is_rebate: bool = False
    is_paid: bool = False

    status: int = 0
    audit_status: int = 0

    dynamic_data: OrderListDynamicDataOut = Field(default_factory=OrderListDynamicDataOut)
    order_info: Optional[OrderListInfoOut] = None

    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OrderListMeta(OrmBaseModel):
    source: str = "orders"
    capabilities: Dict[str, Any] = Field(default_factory=dict)


class OrderListResponse(OrmBaseModel):
    meta: Optional[OrderListMeta] = None
    total: int = 0
    items: List[OrderListItemOut] = Field(default_factory=list)