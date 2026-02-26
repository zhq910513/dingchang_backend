# app/schemas/order.py
# encoding: utf-8
"""
订单相关 Pydantic 模型（Pydantic v1）
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, validator

BJ_TZ = ZoneInfo("Asia/Shanghai")


# =========================
# ✅ 卡槽配置（schema 侧输出骨架）
# =========================
# 说明：
# - key: 前端/接口统一槽位名
# - multi: 是否多图槽
# - ocr: 是否 OCR 槽（仅供前端/UI 参考；后端实际是否创建 OCR 任务由 orders.py 决定）
# - required: 是否“业务上常用主槽”（这里只做结构标识，不做校验）
IMAGE_SLOT_CONFIG: Dict[str, Dict[str, Any]] = {
    "vehicle_cert": {"multi": False, "ocr": True, "required": True},
    "idcard_front": {"multi": False, "ocr": True, "required": True},
    "idcard_back": {"multi": False, "ocr": True, "required": False},
    "driving_license_main": {"multi": False, "ocr": True, "required": True},
    "driving_license_sub": {"multi": False, "ocr": True, "required": False},
    "related": {"multi": True, "ocr": False, "required": False},
}


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
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


def _clean_str_list(v) -> List[str]:
    if v is None:
        return []
    if not isinstance(v, (list, tuple)):
        return []
    out: List[str] = []
    for x in v:
        s = _to_str_or_empty(x)
        if s:
            out.append(s)
    return out


def _slot_skeleton() -> Dict[str, Any]:
    """
    ✅ 生成统一 image_urls 输出骨架（前端可以稳定渲染，不用猜字段）
    单图槽：
      {
        "vehicle_cert": {"slot_key":"vehicle_cert","multi":False,"ocr":True,"required":True,"url":None,"urls":[]}
      }
    多图槽：
      {
        "related": {"slot_key":"related","multi":True,"ocr":False,"required":False,"url":None,"urls":[...]}
      }

    兼容保留：
    - "_all": ["..."] （老前端兜底）
    """
    out: Dict[str, Any] = {}
    for sk, conf in IMAGE_SLOT_CONFIG.items():
        out[sk] = {
            "slot_key": sk,
            "multi": bool(conf.get("multi", False)),
            "ocr": bool(conf.get("ocr", False)),
            "required": bool(conf.get("required", False)),
            "url": None,      # 单图槽主图（多图槽通常为 None）
            "urls": [],       # 多图槽/统一兜底列表
        }
    out["_all"] = []
    return out


def _append_all_unique(all_list: List[str], vals: List[str]) -> None:
    seen = set(all_list)
    for x in vals:
        s = _to_str_or_empty(x)
        if s and s not in seen:
            all_list.append(s)
            seen.add(s)


def _clean_image_urls_out(v) -> Dict[str, Any]:
    """
    ✅ 统一 image_urls 输出为“按卡槽完整结构（配置化）”
    兼容输入历史形态：
    1) 旧：list[str]
       ["u1","u2"] -> 填充到 _all，related.urls 也会同步一份（便于前端兜底）
    2) 旧：dict[str, str/list]
       {"idcard_front":"u1","related":["u2"]} -> 自动映射到新结构
    3) 新（如果后端未来直接输出 slot object）：
       {"idcard_front":{"url":"u1","urls":["u1"]...}, ...} -> 也能容忍并清洗
    """
    out = _slot_skeleton()

    if v is None:
        return out

    # 兼容旧结构：纯 URL 列表
    if isinstance(v, (list, tuple)):
        arr = _clean_str_list(v)
        if arr:
            out["_all"] = arr
            # 旧前端常把全量图当 related，用 related 做兜底不亏
            if "related" in out:
                out["related"]["urls"] = list(arr)
        return out

    # 非 dict 直接返回骨架
    if not isinstance(v, dict):
        return out

    # 遍历输入 key，写入对应槽
    for raw_k, raw_val in v.items():
        key = _to_str_or_empty(raw_k)
        if not key:
            continue

        if key == "_all":
            arr = _clean_str_list(raw_val if isinstance(raw_val, (list, tuple)) else [raw_val])
            if arr:
                out["_all"] = arr
            continue

        # 如果是“未知槽”，尽量塞进 _all（防脏数据丢失）
        if key not in IMAGE_SLOT_CONFIG:
            if isinstance(raw_val, str):
                s = _to_str_or_empty(raw_val)
                if s:
                    _append_all_unique(out["_all"], [s])
            elif isinstance(raw_val, (list, tuple)):
                _append_all_unique(out["_all"], _clean_str_list(raw_val))
            elif isinstance(raw_val, dict):
                # 尝试读 url/urls
                s1 = _to_str_or_empty(raw_val.get("url"))
                arr1 = _clean_str_list(raw_val.get("urls"))
                vals = ([s1] if s1 else []) + arr1
                _append_all_unique(out["_all"], vals)
            else:
                s = _to_str_or_empty(raw_val)
                if s:
                    _append_all_unique(out["_all"], [s])
            continue

        slot_obj = out[key]
        is_multi = bool(slot_obj.get("multi", False))

        # 兼容新形态 slot object
        if isinstance(raw_val, dict):
            s_url = _to_str_or_none(raw_val.get("url"))
            arr_urls = _clean_str_list(raw_val.get("urls"))

            if is_multi:
                vals = arr_urls[:]
                if s_url and s_url not in vals:
                    vals.insert(0, s_url)
                slot_obj["urls"] = vals
                slot_obj["url"] = vals[0] if vals else None
            else:
                final_url = s_url or (arr_urls[0] if arr_urls else None)
                slot_obj["url"] = final_url
                slot_obj["urls"] = [final_url] if final_url else []

            _append_all_unique(out["_all"], slot_obj["urls"])
            continue

        # 旧形态：单图字符串
        if isinstance(raw_val, str):
            s = _to_str_or_none(raw_val)
            if not s:
                continue
            if is_multi:
                slot_obj["urls"] = [s]
                slot_obj["url"] = s
            else:
                slot_obj["url"] = s
                slot_obj["urls"] = [s]
            _append_all_unique(out["_all"], [s])
            continue

        # 旧形态：多图数组
        if isinstance(raw_val, (list, tuple)):
            arr = _clean_str_list(raw_val)
            if not arr:
                continue
            if is_multi:
                slot_obj["urls"] = arr
                slot_obj["url"] = arr[0]
            else:
                # 单图槽只取最后一张（与 finalize 非 multi 槽保留最后一张一致）
                last = arr[-1]
                slot_obj["url"] = last
                slot_obj["urls"] = [last]
            _append_all_unique(out["_all"], slot_obj["urls"])
            continue

        # 其他脏类型兜底
        s = _to_str_or_none(raw_val)
        if s:
            if is_multi:
                slot_obj["urls"] = [s]
                slot_obj["url"] = s
            else:
                slot_obj["url"] = s
                slot_obj["urls"] = [s]
            _append_all_unique(out["_all"], [s])

    # 如果 _all 为空，但各槽有值，回填一次（保险）
    if not out["_all"]:
        vals: List[str] = []
        for sk in IMAGE_SLOT_CONFIG.keys():
            so = out.get(sk) or {}
            vals.extend(_clean_str_list(so.get("urls")))
        out["_all"] = vals

    return out


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
    remark: Optional[str] = None

    commercial_amount: Optional[float] = None
    # ✅ 兼容可选字段（后端 _apply_order_info_patch 已兼容 hasattr 检测）
    commercial_after_amount: Optional[float] = None

    compulsory_amount: Optional[float] = None
    vehicle_tax_amount: Optional[float] = None
    non_vehicle_amount: Optional[float] = None

    channel_commercial_point: Optional[float] = None
    channel_commercial_supplement_point: Optional[float] = None
    channel_compulsory_point: Optional[float] = None
    channel_vehicle_tax_point: Optional[float] = None
    channel_non_vehicle_point: Optional[float] = None
    channel_reward: Optional[float] = None

    customer_commercial_point: Optional[float] = None
    customer_commercial_supplement_point: Optional[float] = None
    customer_compulsory_point: Optional[float] = None
    customer_vehicle_tax_point: Optional[float] = None
    customer_non_vehicle_point: Optional[float] = None
    customer_reward: Optional[float] = None

    @validator("insurance_expire_date", pre=True, always=True)
    def _date_empty_to_none(cls, v):
        return None if v is None or v == "" else v

    @validator("owner_phone", pre=True, always=True)
    def _owner_phone_strip(cls, v):
        s = _to_str_or_empty(v)
        return s or None

    @validator("remark", pre=True, always=True)
    def _remark_empty_to_none(cls, v):
        return _to_str_or_none(v)

    @validator(
        "commercial_amount", "commercial_after_amount",
        "compulsory_amount", "vehicle_tax_amount", "non_vehicle_amount",
        "channel_commercial_point", "channel_commercial_supplement_point", "channel_compulsory_point",
        "channel_vehicle_tax_point", "channel_non_vehicle_point", "channel_reward",
        "customer_commercial_point", "customer_commercial_supplement_point", "customer_compulsory_point",
        "customer_vehicle_tax_point", "customer_non_vehicle_point", "customer_reward",
        pre=True, always=True,
    )
    def _float_empty_to_none(cls, v):
        return _to_float_or_none(v)


class OrderInfoOut(OrmBaseModel):
    insurance_expire_date: Optional[date] = None
    owner_phone: str = ""
    remark: Optional[str] = None

    commercial_amount: Optional[float] = None
    # ✅ 若数据库/模型存在该字段可透出；不存在也不影响 from_orm
    commercial_after_amount: Optional[float] = None

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

    @validator("owner_phone", pre=True, always=True)
    def _owner_phone_none_to_empty(cls, v):
        return _to_str_or_empty(v)

    @validator("remark", pre=True, always=True)
    def _remark_keep_none(cls, v):
        return _to_str_or_none(v)

    @validator(
        "commercial_amount", "commercial_after_amount",
        "compulsory_amount", "vehicle_tax_amount", "non_vehicle_amount", "premium_total",
        "channel_commercial_point", "channel_commercial_supplement_point", "channel_compulsory_point",
        "channel_vehicle_tax_point", "channel_non_vehicle_point", "channel_reward", "channel_total",
        "customer_commercial_point", "customer_commercial_supplement_point", "customer_compulsory_point",
        "customer_vehicle_tax_point", "customer_non_vehicle_point", "customer_reward", "customer_total", "profit",
        pre=True, always=True,
    )
    def _float_keep_none(cls, v):
        return _to_float_or_none(v)


class OrderFilter(OrmBaseModel):
    page: int = 1
    page_size: int = 20

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
    remark: Optional[str] = None

    created_date: Optional[str] = None
    created_date_start: Optional[str] = None
    created_date_end: Optional[str] = None

    # ✅ 补齐单日兼容参数（对应 orders.list 接口）
    first_register_date: Optional[str] = None
    first_register_date_start: Optional[str] = None
    first_register_date_end: Optional[str] = None

    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
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

    # ✅ 补齐：orders.create_order 已在使用，避免 AttributeError
    is_rebate: Optional[bool] = False
    is_paid: Optional[bool] = False

    order_info: Optional[OrderInfoIn] = None

    @validator("image_urls", pre=True, always=True)
    def _image_urls_clean(cls, v):
        return _clean_str_list(v)


class OrderUpdate(OrmBaseModel):
    module: Optional[str] = None
    salesperson_id: Optional[int] = None
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None

    dynamic_data: Optional[Dict[str, Any]] = None
    image_urls: Optional[List[str]] = None
    order_info: Optional[OrderInfoIn] = None

    @validator("image_urls", pre=True, always=True)
    def _image_urls_clean_optional(cls, v):
        if v is None:
            return None
        return _clean_str_list(v)


class OrderStatusUpdate(OrmBaseModel):
    is_finished: Optional[bool] = None
    # ✅ 补齐占位字段：接口层会显式拒绝 finance 字段在 orders 域更新
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

    manager_id: Optional[int] = None
    manager_name: Optional[str] = None

    team_name: Optional[str] = None
    team_names: List[str] = Field(default_factory=list)

    is_finished: bool = False
    is_rebate: bool = False
    is_paid: bool = False

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    # ✅ 配置化结构输出（兼容保留 _all）
    image_urls: Dict[str, Any] = Field(default_factory=dict)
    images: List[OrderImageOut] = Field(default_factory=list)

    order_info: Optional[OrderInfoOut] = None

    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @validator("team_names", pre=True, always=True)
    def _team_names_clean(cls, v):
        return _clean_str_list(v)

    @validator("image_urls", pre=True, always=True)
    def _image_urls_clean(cls, v):
        return _clean_image_urls_out(v)

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
