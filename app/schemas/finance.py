# app/schemas/finance.py
# encoding: utf-8
from __future__ import annotations

from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field


class FinanceOrderStatusUpdate(BaseModel):
    is_paid: Optional[bool] = None
    is_rebate: Optional[bool] = None


class FinanceOrderOut(BaseModel):
    id: int

    col_01_date: Optional[str] = None
    col_02_channel: Optional[str] = None
    col_03_customer: Optional[str] = None
    col_04_market: Optional[str] = None
    col_05_owner: Optional[str] = None
    col_06_plate_no: Optional[str] = None
    col_07_insurance_expire_date: Optional[str] = None
    col_08_vin: Optional[str] = None
    col_09_engine_no: Optional[str] = None
    col_10_vehicle_model: Optional[str] = None
    col_11_first_register_date: Optional[str] = None
    col_12_id_number: Optional[str] = None
    col_13_owner_phone: Optional[str] = None
    col_14_commercial_amount: Optional[float] = None
    col_15_compulsory_amount: Optional[float] = None
    col_16_tax_amount: Optional[float] = None
    col_17_noncar_amount: Optional[float] = None

    # 渠道点位（补齐商业后补点位）
    col_18_ch_commercial_point: Optional[float] = None
    col_19_ch_commercial_supplement_point: Optional[float] = None
    col_20_ch_compulsory_point: Optional[float] = None
    col_21_ch_tax_point: Optional[float] = None
    col_22_ch_noncar_point: Optional[float] = None

    # 客户点位（补齐商业后补点位）
    col_23_cu_commercial_point: Optional[float] = None
    col_24_cu_commercial_supplement_point: Optional[float] = None
    col_25_cu_compulsory_point: Optional[float] = None
    col_26_cu_tax_point: Optional[float] = None
    col_27_cu_noncar_point: Optional[float] = None

    # 汇总 / 状态 / 奖励（列号顺延）
    col_28_receivable: Optional[float] = None
    col_29_payable: Optional[float] = None
    col_30_profit: Optional[float] = None
    col_31_is_paid: bool = False
    col_32_is_rebate: bool = False
    col_33_channel_reward: Optional[float] = None
    col_34_customer_reward: Optional[float] = None

    # ✅ 列表展示用，导出不带
    remark: Optional[str] = None

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    salesperson_id: Optional[int] = None
    salesperson_name: Optional[str] = None
    manager_id: Optional[int] = None
    manager_name: Optional[str] = None

    team_name: Optional[str] = None
    team_names: List[str] = Field(default_factory=list)

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)

    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class FinanceOrderListResponse(BaseModel):
    total: int
    items: List[FinanceOrderOut] = Field(default_factory=list)
