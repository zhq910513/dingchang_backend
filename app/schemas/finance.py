# app/schemas/finance.py
# encoding: utf-8
from __future__ import annotations

from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field


class OrmBaseModel(BaseModel):
    class Config:
        orm_mode = True
        anystr_strip_whitespace = True


class FinanceOrderOut(OrmBaseModel):
    # 基础
    id: int

    # 1-28 固定列（字段名先用英文 key，前端表头按中文展示即可）
    col_01_date: Optional[str] = None  # 日期（订单插入日期）
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

    col_18_ch_commercial_point: Optional[float] = None
    col_19_ch_compulsory_point: Optional[float] = None
    col_20_ch_tax_point: Optional[float] = None
    col_21_ch_noncar_point: Optional[float] = None

    col_22_cu_commercial_point: Optional[float] = None
    col_23_cu_compulsory_point: Optional[float] = None
    col_24_cu_tax_point: Optional[float] = None
    col_25_cu_noncar_point: Optional[float] = None

    col_26_receivable: Optional[float] = None
    col_27_payable: Optional[float] = None
    col_28_profit: Optional[float] = None

    # 29-30（财务追加）
    col_29_is_paid: bool = False
    col_30_is_rebate: bool = False

    # ✅ 新增：财务管理列表新增两个字段
    col_31_channel_reward: Optional[float] = None  # 渠道奖励（对应 order_info.channel_reward）
    col_32_customer_reward: Optional[float] = None  # 客户奖励（对应 order_info.customer_reward）

    # ✅ 新增：订单备注（仅列表/详情展示；导出不需要）
    remark: Optional[str] = None  # 订单备注（对应 order_info.remark）

    # ✅ 列表末尾追加：所属经理/所属团队（满足“所有订单展示列表、财务管理列表”追加字段）
    manager_id: Optional[int] = None
    manager_name: Optional[str] = None
    team_name: Optional[str] = None
    team_names: List[str] = Field(default_factory=list)

    # 兼容/辅助
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    salesperson_id: Optional[int] = None
    salesperson_name: Optional[str] = None

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)

    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class FinanceOrderListResponse(OrmBaseModel):
    total: int = 0
    items: List[FinanceOrderOut] = Field(default_factory=list)


class FinanceOrderStatusUpdate(OrmBaseModel):
    is_rebate: Optional[bool] = None
    is_paid: Optional[bool] = None
