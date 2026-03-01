# app/services/order_read_model.py
# encoding: utf-8
"""
订单读取读模型（Read Model）

目标：
- 统一 Order -> OrderOut 的映射逻辑（orders / finance 共用）
- 统一预加载（selectinload）清单，避免“财务列表字段为空”这类分裂
- 不做任何权限判断：权限/团队过滤由路由层（orders.py / finance.py）负责

注意：
- 本模块只做“读/组装”，不做写操作、不做 ACL。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order import Order, OrderImage
from app.models.user import User
from app.schemas.order import OrderOut, OrderInfoOut
from app.services.storage import StorageService
from app.utils.order_image_urls import ensure_display_urls_for_order_images, safe_image_urls
from app.core.access_control import (
    split_team_names_any as _ac_split_team_names_any,
    pick_manager_id_from_salesperson as _ac_pick_manager_id_from_salesperson,
    pick_manager_name_inline as _ac_pick_manager_name_inline,
)


def preload_options():
    """统一的 Order 列表预加载清单（orders / finance 必须一致）。"""
    return (
        selectinload(Order.creator),
        selectinload(Order.salesperson),
        selectinload(Order.customer_group),
        selectinload(Order.channel_group),
        selectinload(Order.order_info),
        selectinload(Order.images).selectinload(OrderImage.image_file),
    )


def _user_display_name(u: Optional[User]) -> Optional[str]:
    if not u:
        return None
    return getattr(u, "full_name", None) or getattr(u, "real_name", None) or getattr(u, "username", None)


def _group_display_name(g) -> Optional[str]:
    if not g:
        return None
    return (
        getattr(g, "channel_name", None)
        or getattr(g, "customer_name", None)
        or getattr(g, "group_name", None)
        or getattr(g, "name", None)
        or getattr(g, "customer_code", None)
        or getattr(g, "channel_code", None)
    )


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

    code_s = str(code).strip() if code is not None and str(code).strip() else ""
    name_s = str(name).strip() if name is not None and str(name).strip() else ""

    if code_s and name_s:
        return f"{code_s} - {name_s}"
    if name_s:
        return name_s
    if code_s:
        return code_s

    fallback = _group_display_name(g)
    return str(fallback).strip() if fallback is not None and str(fallback).strip() else None


async def _build_manager_maps(db: AsyncSession, orders: List[Order]) -> Tuple[Dict[int, int], Dict[int, User]]:
    """批量计算：salesperson_id -> manager_id，以及 manager_id -> User."""
    manager_ids: Set[int] = set()
    salesperson_to_manager_id: Dict[int, int] = {}

    for o in orders:
        sp = getattr(o, "salesperson", None)
        if not sp:
            continue
        mid = _ac_pick_manager_id_from_salesperson(sp)
        if mid:
            sid = int(getattr(sp, "id", 0) or 0)
            if sid > 0:
                salesperson_to_manager_id[sid] = int(mid)
            manager_ids.add(int(mid))

    managers_by_id: Dict[int, User] = {}
    if manager_ids:
        mgr_rows = (await db.execute(select(User).where(User.id.in_(list(manager_ids))))).scalars().all()
        managers_by_id = {int(getattr(u, "id", 0) or 0): u for u in mgr_rows if getattr(u, "id", None) is not None}

    return salesperson_to_manager_id, managers_by_id


def _order_info_out(info) -> Optional[OrderInfoOut]:
    if not info:
        return None
    return OrderInfoOut.from_orm(info)


def to_order_out(
    o: Order,
    *,
    storage: StorageService,
    salesperson_to_manager_id: Dict[int, int],
    managers_by_id: Dict[int, User],
) -> OrderOut:
    """统一 Order ORM -> OrderOut 映射。"""
    ensure_display_urls_for_order_images(getattr(o, "images", None) or [], storage)

    cg = getattr(o, "customer_group", None)
    sp = getattr(o, "salesperson", None)

    team_name_val = (getattr(sp, "team_name", None) or None) if sp else None
    team_names_val = _ac_split_team_names_any(getattr(sp, "team_names", None)) if sp else []
    if not team_names_val and team_name_val and str(team_name_val).strip():
        team_names_val = [str(team_name_val).strip()]

    manager_id_val = None
    manager_name_val = None
    if sp:
        manager_name_val = _ac_pick_manager_name_inline(sp)
        sp_id_int = int(getattr(sp, "id", 0) or 0)
        mid = salesperson_to_manager_id.get(sp_id_int) or _ac_pick_manager_id_from_salesperson(sp)
        if mid:
            manager_id_val = int(mid)
            if not manager_name_val:
                manager_name_val = _user_display_name(managers_by_id.get(int(mid)))

    return OrderOut(
        id=o.id,
        created_by=o.created_by,
        salesperson_id=o.salesperson_id,
        customer_group_id=o.customer_group_id,
        channel_group_id=o.channel_group_id,
        manager_id=manager_id_val,
        manager_name=manager_name_val,
        team_name=(str(team_name_val).strip() if team_name_val is not None and str(team_name_val).strip() else None),
        team_names=team_names_val,
        is_finished=bool(o.is_finished),
        is_rebate=bool(getattr(o, "is_rebate", False)),
        is_paid=bool(getattr(o, "is_paid", False)),
        dynamic_data=o.dynamic_data or {},
        image_urls=safe_image_urls(o, storage),
        images=getattr(o, "images", None) or [],
        created_at=getattr(o, "created_at", None),
        updated_at=getattr(o, "updated_at", None),
        customer_group_name=_group_code_name(cg),
        channel_group_name=_group_code_name(getattr(o, "channel_group", None)),
        salesperson_name=_user_display_name(getattr(o, "salesperson", None)),
        customer_group_market=getattr(cg, "market", None) if cg else None,
        order_info=_order_info_out(getattr(o, "order_info", None)),
    )


async def orders_to_out_list(db: AsyncSession, orders: List[Order], *, storage: StorageService) -> List[OrderOut]:
    if not orders:
        return []
    salesperson_to_manager_id, managers_by_id = await _build_manager_maps(db, orders)
    return [
        to_order_out(
            o,
            storage=storage,
            salesperson_to_manager_id=salesperson_to_manager_id,
            managers_by_id=managers_by_id,
        )
        for o in orders
    ]
