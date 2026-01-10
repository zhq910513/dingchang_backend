# encoding: utf-8
"""
订单相关 Pydantic 模型（Pydantic v1）

约束：
- 兼容 schemas/__init__.py 的导入：必须包含 OrderFilter
- 空值安全：dynamic_data / image_urls / images 给默认值，避免前端解构报错
- 兼容 ORM：orm_mode=True

本轮修复：
- ✅ created_at / updated_at 统一输出为 "%Y-%m-%d %H:%M:%S"（与 finance 域一致，避免 ISO 8601 的 "T"）
- ✅ 明显隐患收口：旧数据/NULL 字段导致响应序列化报错（None -> 0/""）
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


def _to_float_or_zero(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


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
        # 允许已经是字符串的情况（避免重复处理）
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


class OrderInfoOut(OrmBaseModel):
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
        return _to_str_or_empty(v)

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
    def _float_none_to_zero(cls, v):
        return _to_float_or_zero(v)


class OrderFilter(OrmBaseModel):
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
    is_rebate: Optional[bool] = False
    is_paid: Optional[bool] = False

    order_info: Optional[OrderInfoIn] = None


class OrderUpdate(OrmBaseModel):
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


class OrderStatusUpdate(OrmBaseModel):
    is_finished: Optional[bool] = None
    is_rebate: Optional[bool] = None
    is_paid: Optional[bool] = None


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

    # ✅ 新增：所属经理/所属团队（与 finance 域对齐；orders 域回填）
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
