# app/schemas/order_slot_result.py
# encoding: utf-8
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class OrderSlotResultOut(BaseModel):
    id: int = Field(..., description="主键ID")
    order_id: int = Field(..., description="订单ID（FK -> order.id）")
    slot_key: str = Field(..., description="卡槽Key（slot_key，与 slot_field_config 对齐）")

    order_image_id: Optional[int] = Field(None, description="订单图片ID（FK -> order_image.id，用于追溯）")
    image_file_id: Optional[int] = Field(None, description="图片文件ID（FK -> image_file.id，用于追溯/复用）")

    provider: str = Field(..., description="OCR提供方（如 baidu）")
    api_type: str = Field(..., description="OCR接口类型（如 idcard/vehicle_license/vehicle_certificate）")
    side: str = Field(..., description="识别面（front/back/none，空串表示无）")

    status: str = Field(..., description="识别状态（ok/failed）")
    error_message: Optional[str] = Field(None, description="错误信息（失败时记录）")

    raw_json: Dict[str, Any] = Field(default_factory=dict, description="OCR原始返回JSON（用于追溯/回放）")
    recognized_json: Dict[str, Any] = Field(default_factory=dict, description="抽取后的结构化字段JSON（业务用/展示用）")

    class Config:
        from_attributes = True  # pydantic v2
        orm_mode = True         # pydantic v1 兼容


class OrderSlotResultUpsertIn(BaseModel):
    order_id: int = Field(..., description="订单ID（FK -> order.id）")
    slot_key: str = Field(..., description="卡槽Key（slot_key，与 slot_field_config 对齐）")

    order_image_id: Optional[int] = Field(None, description="订单图片ID（FK -> order_image.id，用于追溯）")
    image_file_id: Optional[int] = Field(None, description="图片文件ID（FK -> image_file.id，用于追溯/复用）")

    provider: str = Field("baidu", description="OCR提供方（默认 baidu）")
    api_type: str = Field(..., description="OCR接口类型（如 idcard/vehicle_license/vehicle_certificate）")
    side: str = Field("", description="识别面（front/back/none，空串表示无）")

    status: str = Field("ok", description="识别状态（ok/failed）")
    error_message: Optional[str] = Field(None, description="错误信息（失败时记录）")

    raw_json: Dict[str, Any] = Field(default_factory=dict, description="OCR原始返回JSON（用于追溯/回放）")
    recognized_json: Dict[str, Any] = Field(default_factory=dict, description="抽取后的结构化字段JSON（业务用/展示用）")
