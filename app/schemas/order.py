# app/schemas/order.py
# encoding: utf-8
"""
订单相关 Pydantic 模型（Pydantic v1）

约束：
- 兼容 schemas/__init__.py 的导入：必须包含 OrderFilter
- 空值安全：dynamic_data / image_urls / images 给默认值，避免前端解构报错
- 兼容 ORM：orm_mode=True

字段命名（本轮定稿）：
- ✅ remark：订单备注（唯一口径），位于 OrderInfo.remark
  - 前端用法：row.order_info.remark
  - 财务侧与订单侧同一个备注字段（无额外“财务备注/订单备注”拆分）
- ❌ 不再存在：order_remark / order_note / order_remark 等兼容字段

对齐：
- ✅ created_at / updated_at 统一输出为 "%Y-%m-%d %H:%M:%S"（与 finance 域一致，避免 ISO 8601 的 "T"）
- ✅ OrderInfoIn：允许前端用 "" / null 显式清空数字字段（避免 float 解析报错）
- ✅ OrderInfoOut：金额/点位/合计字段保持 Optional（None 原样输出为 null），与前端“默认空，不默认 0.00”一致
- ✅ OrderFilter：补齐 list_orders 已支持的筛选参数（team_name / created_date_start|end / first_register_date_start|end）
- ✅ 订单列表/财务列表展示“订单备注”：通过 order_info.remark 展示；导出是否包含由导出接口控制
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, validator

# 仅用于“展示兜底”的时区对象（正常情况下 DB 存北京时间 naive，不做换算）
BJ_TZ = ZoneInfo("Asia/Shanghai")


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    """
    与 finance 域 _fmt_dt 同口径：
    - DB DATETIME 若为 naive：直接格式化输出（禁止无脑 +8）
    - 若被错误贴了 UTC tzinfo（offset=0）：去 tzinfo 再格式化（避免 +8）
    - 其它 aware：兜底转 Asia/Shanghai 再格式化（极少见）
    """
    if not dt:
        return None

    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    try:
        off = dt.utcoffset()
        if off is not None and abs(off.total_seconds()) < 1:
            return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    try:
        return dt.astimezone(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def _to_float_or_none(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _to_int_or_zero(v) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(v)
    except Exception:
        return 0


def _to_str_or_empty(v) -> str:
    if v is None:
        return ""
    try:
        return str(v).strip()
    except Exception:
        return ""


def _to_str_or_none(v) -> Optional[str]:
    s = _to_str_or_empty(v)
    return s or None


class OrmBaseModel(BaseModel):
    class Config:
        orm_mode = True
        anystr_strip_whitespace = True


class ImageFileOut(OrmBaseModel):
    id: int
    sha256: Optional[str] = None
    storage_key: Optional[str] = None
    original_name: Optional[str] = None
    content_type: Optional[str] = None
    url: str = ""
    size: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @validator("url", pre=True, always=True)
    def _url_none_to_empty(cls, v):
        return _to_str_or_empty(v)

    @validator("size", pre=True, always=True)
    def _size_none_to_zero(cls, v):
        return _to_int_or_zero(v)

    @validator("created_at", "updated_at", pre=True, always=True)
    def _dt_to_str(cls, v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return _fmt_dt(v)
        s = _to_str_or_empty(v)
        return s or None


class OrderImageOut(OrmBaseModel):
    id: int
    order_id: int
    slot_key: str
    storage_key: Optional[str] = None
    image_url: str = ""
    image_file_id: Optional[int] = None
    image_file: Optional[ImageFileOut] = None
    created_at: Optional[str] = None

    @validator("image_url", pre=True, always=True)
    def _image_url_none_to_empty(cls, v):
        return _to_str_or_empty(v)

    @validator("created_at", pre=True, always=True)
    def _created_at_to_str(cls, v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return _fmt_dt(v)
        s = _to_str_or_empty(v)
        return s or None


class OrderInfoIn(OrmBaseModel):
    insurance_expire_date: Optional[date] = None
    owner_phone: Optional[str] = None

    # ✅ 订单备注（唯一口径）
    remark: Optional[str] = None

    commercial_amount: Optional[float] = None
    compulsory_amount: Optional[float] = None
    vehicle_tax_amount: Optional[float] = None
    non_vehicle_amount: Optional[float] = None

    channel_commercial_point: Optional[float] = None

    # ✅ 渠道-商业后补点位
    channel_commercial_supplement_point: Optional[float] = None

    channel_compulsory_point: Optional[float] = None
    channel_vehicle_tax_point: Optional[float] = None
    channel_non_vehicle_point: Optional[float] = None
    channel_reward: Optional[float] = None

    customer_commercial_point: Optional[float] = None

    # ✅ 客户-商业后补点位
    customer_commercial_supplement_point: Optional[float] = None

    customer_compulsory_point: Optional[float] = None
    customer_vehicle_tax_point: Optional[float] = None
    customer_non_vehicle_point: Optional[float] = None
    customer_reward: Optional[float] = None

    @validator("insurance_expire_date", pre=True, always=True)
    def _date_empty_to_none(cls, v):
        if v is None or v == "":
            return None
        return v

    @validator("owner_phone", pre=True, always=True)
    def _owner_phone_strip(cls, v):
        s = _to_str_or_empty(v)
        return s or None

    @validator("remark", pre=True, always=True)
    def _remark_empty_to_none(cls, v):
        # ✅ 允许 "" / null 清空
        return _to_str_or_none(v)

    @validator(
        "commercial_amount",
        "compulsory_amount",
        "vehicle_tax_amount",
        "non_vehicle_amount",
        "channel_commercial_point",
        "channel_commercial_supplement_point",
        "channel_compulsory_point",
        "channel_vehicle_tax_point",
        "channel_non_vehicle_point",
        "channel_reward",
        "customer_commercial_point",
        "customer_commercial_supplement_point",
        "customer_compulsory_point",
        "customer_vehicle_tax_point",
        "customer_non_vehicle_point",
        "customer_reward",
        pre=True,
        always=True,
    )
    def _float_empty_to_none(cls, v):
        # ✅ 允许 "" / null 清空；其它尽量转 float（失败也给 None，避免 422）
        return _to_float_or_none(v)


class OrderInfoOut(OrmBaseModel):
    insurance_expire_date: Optional[date] = None
    owner_phone: str = ""

    # ✅ 订单备注（唯一口径）
    remark: Optional[str] = None

    commercial_amount: Optional[float] = None
    compulsory_amount: Optional[float] = None
    vehicle_tax_amount: Optional[float] = None
    non_vehicle_amount: Optional[float] = None
    premium_total: Optional[float] = None

    channel_commercial_point: Optional[float] = None

    # ✅ 渠道-商业后补点位
    channel_commercial_supplement_point: Optional[float] = None

    channel_compulsory_point: Optional[float] = None
    channel_vehicle_tax_point: Optional[float] = None
    channel_non_vehicle_point: Optional[float] = None
    channel_reward: Optional[float] = None
    channel_total: Optional[float] = None

    customer_commercial_point: Optional[float] = None

    # ✅ 客户-商业后补点位
    customer_commercial_supplement_point: Optional[float] = None

    customer_compulsory_point: Optional[float] = None
    customer_vehicle_tax_point: Optional[float] = None
    customer_non_vehicle_point: Optional[float] = None
    customer_reward: Optional[float] = None
    customer_total: Optional[float] = None

    profit: Optional[float] = None

    @validator("owner_phone", pre=True, always=True)
    def _owner_phone_none_to_empty(cls, v):
        return _to_str_or_empty(v)

    @validator("remark", pre=True, always=True)
    def _remark_keep_none(cls, v):
        # ✅ None 输出 null；"" 视为 None
        return _to_str_or_none(v)

    @validator(
        "commercial_amount",
        "compulsory_amount",
        "vehicle_tax_amount",
        "non_vehicle_amount",
        "premium_total",
        "channel_commercial_point",
        "channel_commercial_supplement_point",
        "channel_compulsory_point",
        "channel_vehicle_tax_point",
        "channel_non_vehicle_point",
        "channel_reward",
        "channel_total",
        "customer_commercial_point",
        "customer_commercial_supplement_point",
        "customer_compulsory_point",
        "customer_vehicle_tax_point",
        "customer_non_vehicle_point",
        "customer_reward",
        "customer_total",
        "profit",
        pre=True,
        always=True,
    )
    def _float_keep_none(cls, v):
        # ✅ None 原样输出 null；"" 也视为 None；其它尽量转 float
        return _to_float_or_none(v)


class OrderFilter(OrmBaseModel):
    page: int = 1
    page_size: int = 20

    # ✅ 与 list_orders 对齐：仅支持 is_finished
    is_finished: Optional[bool] = None

    salesperson_id: Optional[int] = None
    created_by: Optional[int] = None

    owner_name: Optional[str] = None
    id_number: Optional[str] = None
    plate_no: Optional[str] = None
    engine_no: Optional[str] = None
    vehicle_name: Optional[str] = None
    vehicle_model: Optional[str] = None
    vin: Optional[str] = None

    # ✅ 订单备注筛选（唯一口径：order_info.remark）
    remark: Optional[str] = None

    # 单日（兼容历史）
    created_date: Optional[str] = None  # YYYY-MM-DD

    # ✅ 新增：起止日期（list_orders 已支持）
    created_date_start: Optional[str] = None  # YYYY-MM-DD
    created_date_end: Optional[str] = None  # YYYY-MM-DD（包含当天）

    # ✅ 新增：初登日期起止（list_orders 已支持）
    first_register_date_start: Optional[str] = None  # YYYY-MM-DD
    first_register_date_end: Optional[str] = None  # YYYY-MM-DD（包含当天）

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    # ✅ 新增：团队筛选（list_orders 已支持）
    team_name: Optional[str] = None


class OrderCreate(OrmBaseModel):
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

    # ✅ 订单详情保存时允许提交 order_info（其中含 remark）
    order_info: Optional[OrderInfoIn] = None


class OrderUpdate(OrmBaseModel):
    module: Optional[str] = None

    salesperson_id: Optional[int] = None
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    dynamic_data: Optional[Dict[str, Any]] = None
    image_urls: Optional[List[str]] = None

    # ✅ 与 update_order_detail 对齐：该接口的订单信息块
    order_info: Optional[OrderInfoIn] = None


class OrderStatusUpdate(OrmBaseModel):
    # ✅ 与 update_order_status 对齐：只允许更新 is_finished
    is_finished: Optional[bool] = None


class OrderOut(OrmBaseModel):
    id: int
    created_by: int

    salesperson_id: Optional[int] = None
    salesperson_name: Optional[str] = None

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    customer_group_name: Optional[str] = None
    channel_group_name: Optional[str] = None

    customer_group_market: Optional[str] = None

    # ✅ 所属经理/所属团队（与 finance 域对齐；orders 域回填）
    manager_id: Optional[int] = None
    manager_name: Optional[str] = None
    team_name: Optional[str] = None
    team_names: List[str] = Field(default_factory=list)

    is_finished: bool = False
    is_rebate: bool = False
    is_paid: bool = False

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)

    image_urls: List[str] = Field(default_factory=list)
    images: List[OrderImageOut] = Field(default_factory=list)

    # ✅ 订单备注在这里：order_info.remark（唯一口径）
    order_info: Optional[OrderInfoOut] = None

    # ✅ 统一输出格式：与 finance 域一致（空格分隔，不要 "T"）
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @validator("created_at", "updated_at", pre=True, always=True)
    def _order_dt_to_str(cls, v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return _fmt_dt(v)
        s = _to_str_or_empty(v)
        return s or None


class OrderListResponse(OrmBaseModel):
    total: int = 0
    items: List[OrderOut] = Field(default_factory=list)
