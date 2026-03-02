# app/schemas/order_fact.py
# encoding: utf-8
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class OrderFactOut(BaseModel):
    order_id: int = Field(..., description="订单ID（PK&FK -> order.id，一对一投影）")

    vin: Optional[str] = Field(None, description="车架号VIN（规范字段）")
    plate_no: Optional[str] = Field(None, description="车牌号（规范字段）")
    owner_name: Optional[str] = Field(None, description="车主/所有人（规范字段）")
    engine_no: Optional[str] = Field(None, description="发动机号（规范字段）")
    vehicle_model: Optional[str] = Field(None, description="品牌型号/车辆型号（规范字段）")
    first_register_date: Optional[date] = Field(None, description="初登日期/注册日期（规范字段，DATE）")
    id_number: Optional[str] = Field(None, description="身份证号（规范字段）")

    class Config:
        from_attributes = True
        orm_mode = True


class OrderFactUpsertIn(BaseModel):
    order_id: int = Field(..., description="订单ID（PK&FK -> order.id，一对一投影）")

    vin: Optional[str] = Field(None, description="车架号VIN（规范字段）")
    plate_no: Optional[str] = Field(None, description="车牌号（规范字段）")
    owner_name: Optional[str] = Field(None, description="车主/所有人（规范字段）")
    engine_no: Optional[str] = Field(None, description="发动机号（规范字段）")
    vehicle_model: Optional[str] = Field(None, description="品牌型号/车辆型号（规范字段）")
    first_register_date: Optional[date] = Field(None, description="初登日期/注册日期（规范字段，DATE）")
    id_number: Optional[str] = Field(None, description="身份证号（规范字段）")
