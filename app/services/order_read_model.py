# app/services/order_read_model.py
# encoding: utf-8
"""订单读取读模型（Read Model）

硬规则（本轮）：
- 只认新表（由冻结 models 的 __tablename__ 指向 *_new）
- 不做任何旧口径兼容/回填（不产生 dl_*，不从 ocr_raw_json 回填展示字段）
- 输出严格按 schemas.order 中的契约：
    * OrderOut（详情）
    * OrderListItemOut（列表项）
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from app.models.order import Order, OrderImage
from app.schemas.order import (
    OrderInfoOut,
    OrderOut,
    OrderListDynamicDataOut,
    OrderListInfoOut,
)
from app.services.ocr_cleaner import norm_fuzzy_date_text
from app.services.order_owner_name import resolve_owner_name as _shared_resolve_owner_name
from app.services.storage import StorageService

if TYPE_CHECKING:
    from app.schemas.order import OrderListItemOut


_EMPTY_DICT: Dict[str, Any] = {}
_EMPTY_LIST: List[Any] = []


def _dt_to_ymd(v: Any) -> Optional[str]:
    try:
        if v is None:
            return None
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d")
    except Exception:
        pass
    s = str(v or "").strip()
    return s or None


def _trim_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _trim_or_empty(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _safe_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else _EMPTY_DICT


def _pick_first_non_empty(d: Dict[str, Any], *keys: str) -> Optional[str]:
    if not d:
        return None
    for k in keys:
        val = d.get(k)
        if val is None:
            continue
        s = str(val).strip()
        if s:
            return s
    return None


def _pick_ocr_words_result_text(block: Dict[str, Any], field_name: str) -> Optional[str]:
    if not isinstance(block, dict):
        return None
    words_result = block.get("words_result")
    if not isinstance(words_result, dict):
        return None
    node = words_result.get(field_name)
    if not isinstance(node, dict):
        return None
    return _trim_or_none(node.get("words"))


def _resolve_owner_name(dd: Dict[str, Any], ocr_raw: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """车主新口径：仅按 dynamic_data 解析；ocr_raw 不再参与展示字段回填。"""
    return _shared_resolve_owner_name(dd)


def _normalize_dynamic_data(dynamic_data: Any, ocr_raw_json: Any = None) -> Dict[str, Any]:
    src = _safe_dict(dynamic_data)
    if not src:
        dd: Dict[str, Any] = {}
    else:
        dd = dict(src)

    first_register_date = dd.get("first_register_date")
    if first_register_date is not None:
        dd["first_register_date"] = norm_fuzzy_date_text(first_register_date)

    resolved_owner_name = _resolve_owner_name(dd)
    if resolved_owner_name:
        dd["owner_name"] = resolved_owner_name
    elif "owner_name" in dd:
        dd["owner_name"] = _trim_or_none(dd.get("owner_name"))

    return dd


def _list_dynamic_data_out(dynamic_data: Any, ocr_raw_json: Any = None) -> OrderListDynamicDataOut:
    """
    列表页专用 dynamic_data 输出：
    仅保留当前前端真实消费字段。
    """
    dd = _normalize_dynamic_data(dynamic_data, ocr_raw_json=ocr_raw_json)

    first_register_date = dd.get("first_register_date")
    if first_register_date is not None:
        first_register_date = norm_fuzzy_date_text(first_register_date)

    return OrderListDynamicDataOut(
        owner_name=_trim_or_none(dd.get("owner_name")),
        plate_no=_trim_or_none(dd.get("plate_no")),
        vin=_trim_or_none(dd.get("vin")),
        engine_no=_trim_or_none(dd.get("engine_no")),
        vehicle_model=_trim_or_none(dd.get("vehicle_model")),
        first_register_date=first_register_date,
        id_number=_trim_or_none(dd.get("id_number")),
    )


def _split_team_names_csv(v: Any) -> List[str]:
    raw = str(v or "").strip()
    if not raw:
        return []

    parts = raw.split(",")
    out: List[str] = []
    seen = set()

    for part in parts:
        s = part.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _group_code_name(g) -> Optional[str]:
    if not g:
        return None

    code = (
        getattr(g, "channel_code", None)
        or getattr(g, "customer_code", None)
        or getattr(g, "group_code", None)
        or getattr(g, "code", None)
    )
    name = (
        getattr(g, "channel_name", None)
        or getattr(g, "customer_name", None)
        or getattr(g, "group_name", None)
        or getattr(g, "name", None)
    )

    code_s = _trim_or_empty(code)
    name_s = _trim_or_empty(name)

    if code_s and name_s:
        return f"{code_s} - {name_s}"
    if name_s:
        return name_s
    if code_s:
        return code_s
    return None


def _order_info_out(info) -> Optional[OrderInfoOut]:
    """
    详情页专用 order_info 输出：保持完整口径。
    """
    if not info:
        return None

    insurance_expire_date = _dt_to_ymd(getattr(info, "insurance_expire_date", None))
    owner_phone = _trim_or_none(getattr(info, "owner_phone", None))
    remark = _trim_or_none(getattr(info, "remark", None))

    return OrderInfoOut(
        insurance_expire_date=insurance_expire_date,
        owner_phone=owner_phone,

        commercial_amount=_to_float(getattr(info, "commercial_amount", None)),
        compulsory_amount=_to_float(getattr(info, "compulsory_amount", None)),
        vehicle_tax_amount=_to_float(getattr(info, "vehicle_tax_amount", None)),
        non_vehicle_amount=_to_float(getattr(info, "non_vehicle_amount", None)),
        premium_total=_to_float(getattr(info, "premium_total", None)),

        channel_commercial_point=_to_float(getattr(info, "channel_commercial_point", None)),
        channel_commercial_supplement_point=_to_float(getattr(info, "channel_commercial_supplement_point", None)),
        channel_compulsory_point=_to_float(getattr(info, "channel_compulsory_point", None)),
        channel_vehicle_tax_point=_to_float(getattr(info, "channel_vehicle_tax_point", None)),
        channel_non_vehicle_point=_to_float(getattr(info, "channel_non_vehicle_point", None)),
        channel_reward=_to_float(getattr(info, "channel_reward", None)),
        channel_total=_to_float(getattr(info, "channel_total", None)),

        customer_commercial_point=_to_float(getattr(info, "customer_commercial_point", None)),
        customer_commercial_supplement_point=_to_float(getattr(info, "customer_commercial_supplement_point", None)),
        customer_compulsory_point=_to_float(getattr(info, "customer_compulsory_point", None)),
        customer_vehicle_tax_point=_to_float(getattr(info, "customer_vehicle_tax_point", None)),
        customer_non_vehicle_point=_to_float(getattr(info, "customer_non_vehicle_point", None)),
        customer_reward=_to_float(getattr(info, "customer_reward", None)),
        customer_total=_to_float(getattr(info, "customer_total", None)),

        profit=_to_float(getattr(info, "profit", None)),
        remark=remark,
    )


def _list_order_info_out(info) -> Optional[OrderListInfoOut]:
    """
    列表页专用 order_info 输出：
    仅保留当前订单列表 / 财务列表真实消费字段。
    """
    if not info:
        return None

    return OrderListInfoOut(
        insurance_expire_date=_dt_to_ymd(getattr(info, "insurance_expire_date", None)),
        owner_phone=_trim_or_none(getattr(info, "owner_phone", None)),

        commercial_amount=_to_float(getattr(info, "commercial_amount", None)),
        compulsory_amount=_to_float(getattr(info, "compulsory_amount", None)),
        vehicle_tax_amount=_to_float(getattr(info, "vehicle_tax_amount", None)),
        non_vehicle_amount=_to_float(getattr(info, "non_vehicle_amount", None)),

        channel_commercial_point=_to_float(getattr(info, "channel_commercial_point", None)),
        channel_commercial_supplement_point=_to_float(getattr(info, "channel_commercial_supplement_point", None)),
        channel_compulsory_point=_to_float(getattr(info, "channel_compulsory_point", None)),
        channel_vehicle_tax_point=_to_float(getattr(info, "channel_vehicle_tax_point", None)),
        channel_non_vehicle_point=_to_float(getattr(info, "channel_non_vehicle_point", None)),
        channel_reward=_to_float(getattr(info, "channel_reward", None)),
        channel_total=_to_float(getattr(info, "channel_total", None)),

        customer_commercial_point=_to_float(getattr(info, "customer_commercial_point", None)),
        customer_commercial_supplement_point=_to_float(getattr(info, "customer_commercial_supplement_point", None)),
        customer_compulsory_point=_to_float(getattr(info, "customer_compulsory_point", None)),
        customer_vehicle_tax_point=_to_float(getattr(info, "customer_vehicle_tax_point", None)),
        customer_non_vehicle_point=_to_float(getattr(info, "customer_non_vehicle_point", None)),
        customer_reward=_to_float(getattr(info, "customer_reward", None)),
        customer_total=_to_float(getattr(info, "customer_total", None)),

        profit=_to_float(getattr(info, "profit", None)),
    )


def _safe_get_loaded_images(order: Order) -> Optional[List[OrderImage]]:
    try:
        images = getattr(order, "images", None)
        if images is None:
            return None
        if isinstance(images, list):
            return images
        return list(images)
    except Exception:
        return None


def to_order_out(
    o: Order,
    *,
    storage: StorageService,
    images_by_order_id: Dict[int, List[OrderImage]],
) -> OrderOut:
    """统一 Order ORM -> OrderOut 映射（严格按 schemas.order.OrderOut 契约）。"""
    order_id = int(getattr(o, "id", 0) or 0)

    imgs_loaded = _safe_get_loaded_images(o)
    if imgs_loaded is not None:
        setattr(o, "images", imgs_loaded)
    else:
        setattr(o, "images", images_by_order_id.get(order_id, _EMPTY_LIST) or _EMPTY_LIST)

    ocr_raw = dict(getattr(o, "ocr_raw_json", None) or {})
    dyn_norm = _normalize_dynamic_data(getattr(o, "dynamic_data", None), ocr_raw_json=ocr_raw)

    from app.utils.order_image_urls import build_slot_images  # local import

    slot_images = build_slot_images(o, storage)

    customer_group = getattr(o, "customer_group", None)
    channel_group = getattr(o, "channel_group", None)
    order_info = getattr(o, "order_info", None)

    return OrderOut(
        id=order_id,
        module=str(getattr(o, "module", "") or "order"),
        created_by=int(getattr(o, "created_by", 0) or 0),
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        customer_group_id=getattr(o, "customer_group_id", None),
        channel_group_id=getattr(o, "channel_group_id", None),
        customer_group_name=_group_code_name(customer_group),
        channel_group_name=_group_code_name(channel_group),
        is_finished=bool(getattr(o, "is_finished", False)),
        is_rebate=bool(getattr(o, "is_rebate", False)),
        is_paid=bool(getattr(o, "is_paid", False)),
        status=int(getattr(o, "status", 0) or 0),
        audit_status=int(getattr(o, "audit_status", 0) or 0),
        dynamic_data=dyn_norm,
        ocr_raw_json=ocr_raw,
        slot_images=slot_images,
        order_info=_order_info_out(order_info),
        created_at=_dt_to_ymd(getattr(o, "created_at", None)),
        updated_at=_dt_to_ymd(getattr(o, "updated_at", None)),
    )


async def orders_to_list_items(orders: List[Order]) -> List["OrderListItemOut"]:
    """批量把 Order ORM 转为列表项（严格按 schemas.order.OrderListItemOut）。"""
    if not orders:
        return []

    from app.schemas.order import OrderListItemOut  # local import to avoid cycles

    out: List[OrderListItemOut] = []
    append_item = out.append

    for o in orders:
        salesperson = getattr(o, "salesperson", None)
        customer_group = getattr(o, "customer_group", None)
        channel_group = getattr(o, "channel_group", None)
        order_info = getattr(o, "order_info", None)
        ocr_raw = getattr(o, "ocr_raw_json", None)

        sp_real_name = _trim_or_empty(getattr(salesperson, "real_name", None))
        sp_username = _trim_or_empty(getattr(salesperson, "username", None))
        salesperson_name = sp_real_name or sp_username or None

        parent = getattr(salesperson, "parent", None)
        manager_real_name = _trim_or_empty(getattr(parent, "real_name", None))
        manager_username = _trim_or_empty(getattr(parent, "username", None))
        manager_name = manager_real_name or manager_username or None

        team_name = _trim_or_none(getattr(salesperson, "team_name", None))
        team_names = _split_team_names_csv(getattr(salesperson, "team_names", None))
        if team_name and team_name not in team_names:
            team_names.append(team_name)

        append_item(
            OrderListItemOut(
                id=int(getattr(o, "id", 0) or 0),

                customer_group_id=getattr(o, "customer_group_id", None),
                channel_group_id=getattr(o, "channel_group_id", None),
                salesperson_id=getattr(o, "salesperson_id", None),

                customer_group_name=_trim_or_none(getattr(customer_group, "customer_name", None)),
                channel_group_name=_trim_or_none(getattr(channel_group, "channel_name", None)),
                customer_group_market=_trim_or_none(getattr(customer_group, "market", None)),

                salesperson_name=salesperson_name,
                manager_name=manager_name,
                team_name=team_name,
                team_names=team_names,

                is_finished=bool(getattr(o, "is_finished", False)),
                is_rebate=bool(getattr(o, "is_rebate", False)),
                is_paid=bool(getattr(o, "is_paid", False)),
                status=int(getattr(o, "status", 0) or 0),
                audit_status=int(getattr(o, "audit_status", 0) or 0),

                dynamic_data=_list_dynamic_data_out(getattr(o, "dynamic_data", None), ocr_raw_json=ocr_raw),
                order_info=_list_order_info_out(order_info),

                created_at=_dt_to_ymd(getattr(o, "created_at", None)),
                updated_at=_dt_to_ymd(getattr(o, "updated_at", None)),
            )
        )

    return out
