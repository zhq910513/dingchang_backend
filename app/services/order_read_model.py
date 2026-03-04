# app/services/order_read_model.py
# encoding: utf-8
"""订单读取读模型（Read Model）

硬规则（本轮）：
- 只认新表（由冻结 models 的 __tablename__ 指向 *_new）
- 不做任何旧口径兼容/回填（不产生 dl_*，不从 ocr_raw_json 回填展示字段）
- 输出严格按 schemas.order.OrderOut 契约
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order import Order, OrderImage
from app.schemas.order import OrderInfoOut, OrderOut
from app.services.storage import StorageService


def _maybe_selectinload(model, attr_name: str):
    try:
        if hasattr(model, attr_name):
            return selectinload(getattr(model, attr_name))
    except Exception:
        return None
    return None


def _maybe_selectinload_nested(parent_model, parent_attr: str, child_model, child_attr: str):
    try:
        if hasattr(parent_model, parent_attr) and hasattr(child_model, child_attr):
            return selectinload(getattr(parent_model, parent_attr)).selectinload(getattr(child_model, child_attr))
        if hasattr(parent_model, parent_attr):
            return selectinload(getattr(parent_model, parent_attr))
    except Exception:
        return None
    return None


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


def _normalize_dynamic_data(dynamic_data: Any) -> Dict[str, Any]:
    # 新表唯一口径：仅保证 dict 类型，不做旧键回填
    if isinstance(dynamic_data, dict):
        return dict(dynamic_data)
    return {}


def _order_info_out(info) -> Optional[OrderInfoOut]:
    if not info:
        return None
    remark = getattr(info, "remark", None)
    return OrderInfoOut(remark=(str(remark).strip() if remark is not None and str(remark).strip() else None))


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
    imgs_loaded = _safe_get_loaded_images(o)
    if imgs_loaded is not None:
        setattr(o, "images", imgs_loaded)
    else:
        setattr(o, "images", images_by_order_id.get(int(getattr(o, "id", 0) or 0), []) or [])

    dyn_norm = _normalize_dynamic_data(getattr(o, "dynamic_data", None))
    ocr_raw = dict(getattr(o, "ocr_raw_json", None) or {})

    from app.utils.order_image_urls import build_slot_images  # local import
    slot_images = build_slot_images(o, storage)

    return OrderOut(
        id=int(getattr(o, "id", 0) or 0),
        module=str(getattr(o, "module", "") or "order"),
        created_by=int(getattr(o, "created_by", 0) or 0),
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        customer_group_id=getattr(o, "customer_group_id", None),
        channel_group_id=getattr(o, "channel_group_id", None),
        is_finished=bool(getattr(o, "is_finished", False)),
        is_rebate=bool(getattr(o, "is_rebate", False)),
        is_paid=bool(getattr(o, "is_paid", False)),
        status=int(getattr(o, "status", 0) or 0),
        audit_status=int(getattr(o, "audit_status", 0) or 0),
        dynamic_data=dyn_norm,
        ocr_raw_json=ocr_raw,
        slot_images=slot_images,
        order_info=_order_info_out(getattr(o, "order_info", None)),
        created_at=_dt_to_ymd(getattr(o, "created_at", None)),
        updated_at=_dt_to_ymd(getattr(o, "updated_at", None)),
    )


async def orders_to_list_items(db: AsyncSession, orders: List[Order]) -> List["OrderListItemOut"]:
    """批量把 Order ORM 转为列表项（严格按 schemas.order.OrderListItemOut）。"""
    if not orders:
        return []
    from app.schemas.order import OrderListItemOut  # local import to avoid cycles
    out: List[OrderListItemOut] = []
    for o in orders:
        out.append(
            OrderListItemOut(
                id=int(getattr(o, "id", 0) or 0),
                created_at=_dt_to_ymd(getattr(o, "created_at", None)),
                status=int(getattr(o, "status", 0) or 0),
                audit_status=int(getattr(o, "audit_status", 0) or 0),
                is_finished=bool(getattr(o, "is_finished", False)),
                customer_group_id=getattr(o, "customer_group_id", None),
                channel_group_id=getattr(o, "channel_group_id", None),
            )
        )
    return out
