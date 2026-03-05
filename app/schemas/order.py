# app/schemas/order.py
# encoding: utf-8
from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


# =========================
# Slot Images（唯一真源契约）
# =========================

class SlotImageItemOut(BaseModel):
    """卡槽图片条目（字段固定，不多不少）"""
    order_image_id: int
    image_file_id: Optional[int] = None
    storage_key: str = ""
    url: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SlotImageNodeOut(BaseModel):
    """卡槽节点（字段固定，不多不少）"""
    slot_key: str
    title: str
    multi: bool = False
    ocr: bool = False
    images: List[SlotImageItemOut] = Field(default_factory=list)


# 为了避免上游代码 import 炸裂，保留旧类名，但其字段已对齐到新契约（不再输出旧字段 id/image_url/image_file）
class OrderImageOut(SlotImageItemOut):
    """（弃用）历史类名保留，但字段已收口为 SlotImageItemOut。"""
    pass


# =========================
# Order Info（扩展信息）
# =========================

class OrderInfoIn(BaseModel):
    """订单扩展信息入参（最小化：只保留 remark，其余走 dynamic_data/事实层）。"""
    remark: Optional[str] = None


class OrderInfoOut(BaseModel):
    """订单扩展信息出参（最小化）。"""
    remark: Optional[str] = None

    # ✅ 兼容 Pydantic v1/v2：允许 from_orm/from_attributes
    model_config = {"from_attributes": True}

    class Config:
        orm_mode = True


# =========================
# Order（主对象）
# =========================

class OrderCreate(BaseModel):
    """创建订单（新表口径）"""
    module: str = "order"
    created_by: Optional[int] = None
    salesperson_id: Optional[int] = None
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    status: int = 0
    audit_status: int = 0

    # ✅ 新口径：dynamic_data / ocr_raw_json 允许为空对象，但不做任何旧键回填
    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    ocr_raw_json: Dict[str, Any] = Field(default_factory=dict)


class OrderUpdate(BaseModel):
    """
    更新订单（仅允许更新明确字段；其余通过事实层/业务专用接口）

    重要：
    - is_finished 不在这里更新，只允许走 /orders/{id}/status
    """
    salesperson_id: Optional[int] = None
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    status: Optional[int] = None
    audit_status: Optional[int] = None
    dynamic_data: Optional[Dict[str, Any]] = None
    ocr_raw_json: Optional[Dict[str, Any]] = None

    # ✅ 订单扩展信息（目前只允许 remark）
    order_info: Optional[OrderInfoIn] = None


class OrderStatusUpdate(BaseModel):
    """订单状态更新（收口：只允许维护 is_finished）"""
    is_finished: Optional[bool] = None


class OrderOut(BaseModel):
    id: int
    module: str

    created_by: int
    salesperson_id: int

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    is_finished: bool = False
    is_rebate: bool = False
    is_paid: bool = False

    status: int = 0
    audit_status: int = 0

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    ocr_raw_json: Dict[str, Any] = Field(default_factory=dict)

    # ✅ 唯一图片结构：slot_images（按 slot_field_config 的固定契约）
    slot_images: List[SlotImageNodeOut] = Field(default_factory=list)

    # 扩展信息（如后端已实现 1:1）
    order_info: Optional[OrderInfoOut] = None

    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OrderListItemOut(BaseModel):
    """
    列表项（精简专用 schema）

    说明：
    - 列表项不强制等同 OrderOut，避免 read_model 输出精简字段时 schema 校验必炸
    - 详情仍以 OrderOut 为准
    """
    id: int

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    is_finished: bool = False
    status: int = 0
    audit_status: int = 0

    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OrderListMeta(BaseModel):
    """列表元信息（UI 能力提示等）"""
    source: str = "orders"
    capabilities: Dict[str, Any] = Field(default_factory=dict)


class OrderListResponse(BaseModel):
    """订单列表响应（与 API 输出一致）"""
    meta: Optional[OrderListMeta] = None
    total: int = 0
    items: List[OrderListItemOut] = Field(default_factory=list)
