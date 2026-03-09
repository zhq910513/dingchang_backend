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
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SlotImageNodeOut(OrmBaseModel):
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

class OrderInfoIn(OrmBaseModel):
    """订单扩展信息入参（最小化：只保留 remark，其余走 dynamic_data/事实层）。"""
    remark: Optional[str] = None


class OrderInfoOut(OrmBaseModel):
    """订单扩展信息出参（最小化）。"""
    remark: Optional[str] = None


# =========================
# Order（主对象）
# =========================

class OrderCreate(OrmBaseModel):
    """创建订单（新表口径）"""
    module: str = "order"
    created_by: Optional[int] = None
    salesperson_id: Optional[int] = None
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    # ✅ 与 orders API 行为对齐：创建时允许传入是否完成标记（默认为 False）
    is_finished: bool = False

    # ✅ 订单扩展信息（目前只允许 remark）
    order_info: Optional[OrderInfoIn] = None

    status: int = 0
    audit_status: int = 0

    # ✅ 新口径：dynamic_data / ocr_raw_json 允许为空对象，但不做任何旧键回填
    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    ocr_raw_json: Dict[str, Any] = Field(default_factory=dict)


class OrderUpdate(OrmBaseModel):
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


class OrderStatusUpdate(OrmBaseModel):
    """订单状态更新

    说明：
    - orders 模块只允许更新 is_finished
    - is_rebate/is_paid 属于财务字段：schema 显式声明以保证契约完整，但 orders API 会拒绝更新
    """
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


class OrderListItemOut(OrmBaseModel):
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


class OrderListMeta(OrmBaseModel):
    """列表元信息（UI 能力提示等）"""
    source: str = "orders"
    capabilities: Dict[str, Any] = Field(default_factory=dict)


class OrderListResponse(OrmBaseModel):
    """订单列表响应（与 API 输出一致）"""
    meta: Optional[OrderListMeta] = None
    total: int = 0
    items: List[OrderListItemOut] = Field(default_factory=list)
