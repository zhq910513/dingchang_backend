# app/api/v1/orders.py
# encoding: utf-8
from __future__ import annotations

import inspect
from typing import Optional, Any, Dict, List, Tuple, Set

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user_with_role_and_teams, CurrentUserContext
from app.core.access_control import (
    split_team_names_any as _ac_split_team_names_any,
    pick_manager_id_from_salesperson as _ac_pick_manager_id_from_salesperson,
    pick_manager_name_inline as _ac_pick_manager_name_inline,
    normalize_team_names as _ac_normalize_team_names,
    user_team_match_expr as _ac_user_team_match_expr,
    order_salesperson_in_teams_expr as _ac_order_salesperson_in_teams_expr,
    current_team_names_or_403 as _ac_current_team_names_or_403,
    effective_team_filter_for_query as _ac_effective_team_filter_for_query,
    salesperson_in_current_teams_or_403 as _ac_salesperson_in_current_teams_or_403,
    require_team_for_non_super_admin as _ac_require_team_for_non_super_admin,
    require_single_team_for_strict_roles as _ac_require_single_team_for_strict_roles,
    allowed_teams_for_user as _ac_allowed_teams_for_user,
    require_team_filter_allowed as _ac_require_team_filter_allowed,
    ensure_user_in_teams as _ac_ensure_user_in_teams,
    ensure_order_read_acl_by_salesperson_id as _ac_ensure_order_read_acl_by_salesperson_id,
    ensure_order_write_acl_by_salesperson_id as _ac_ensure_order_write_acl_by_salesperson_id,
    apply_orders_list_acl as _ac_apply_orders_list_acl,
)
from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_SALES, ROLE_MARKET
from app.core.db import get_db
from app.models.order import Order, OrderImage
from app.models.order_info import OrderInfo
from app.models.user import User
from app.schemas.order import (
    OrderCreate,
    OrderUpdate,
    OrderOut,
    OrderListResponse,
    OrderStatusUpdate,
    OrderInfoIn,
)
from app.services.order_read_model import (
    to_order_out as _rm_to_order_out,
    orders_to_list_items as _rm_orders_to_list_items,
)
from app.services.storage import StorageService

router = APIRouter(prefix="/orders", tags=["orders"])
storage = StorageService()


def _maybe_selectinload(model, attr_name: str):
    """Return selectinload(model.attr) if attr exists, else None."""
    try:
        if hasattr(model, attr_name):
            return selectinload(getattr(model, attr_name))
    except Exception:
        return None
    return None


def _maybe_selectinload_nested(parent_model, parent_attr: str, child_model, child_attr: str):
    """Return selectinload(parent.attr).selectinload(child.attr) if both attrs exist; else best-effort."""
    try:
        if hasattr(parent_model, parent_attr) and hasattr(child_model, child_attr):
            return selectinload(getattr(parent_model, parent_attr)).selectinload(getattr(child_model, child_attr))
        if hasattr(parent_model, parent_attr):
            return selectinload(getattr(parent_model, parent_attr))
    except Exception:
        return None
    return None


async def _maybe_await(v):
    if inspect.isawaitable(v):
        return await v
    return v


def _model_fields_set(m: Any) -> Set[str]:
    """兼容 Pydantic v1/v2：取本次 payload 明确传入的字段集合。"""
    fs = getattr(m, "model_fields_set", None)
    if isinstance(fs, set):
        return {str(x) for x in fs}
    fs2 = getattr(m, "__fields_set__", None)
    if isinstance(fs2, set):
        return {str(x) for x in fs2}
    return set()


async def _ensure_salesperson_exists(db: AsyncSession, salesperson_id: int) -> None:
    """Ensure salesperson user exists. No legacy fallback; new-table-only."""
    try:
        sid = int(salesperson_id)
    except Exception:
        raise HTTPException(status_code=400, detail="salesperson_id 非法")
    q = select(User).where(User.id == sid)
    u = (await db.execute(q)).scalar_one_or_none()
    if u is None:
        raise HTTPException(status_code=400, detail="salesperson_id 不存在")


def _ensure_orders_access(role_name: Optional[str]) -> None:
    rn = role_name or ""
    if rn not in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_FINANCE, ROLE_MARKET, ROLE_SALES):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_orders_write_access(role_name: Optional[str]) -> None:
    if role_name in (ROLE_FINANCE, ROLE_MARKET):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_required_customer_channel(*, customer_group_id: Optional[int], channel_group_id: Optional[int]) -> None:
    if customer_group_id is None:
        raise HTTPException(status_code=400, detail="customer_group_id is required")
    if channel_group_id is None:
        raise HTTPException(status_code=400, detail="channel_group_id is required")


def _apply_order_info_patch_remark(info: OrderInfo, payload: OrderInfoIn) -> None:
    """严格按 schemas.order.OrderInfoIn：只允许写 remark。"""
    if payload is None:
        return
    fs = _model_fields_set(payload)
    if "remark" in fs:
        v = getattr(payload, "remark", None)
        info.remark = None if (v is None or v == "") else str(v).strip()


async def _load_order_out(
        db: AsyncSession,
        order_id: int,
        *,
        current_user: User,
        role_name: Optional[str],
        team_names: Tuple[str, ...],
) -> OrderOut:
    """加载订单并按 services.read_model 映射为 OrderOut（唯一真源：services + schemas）。"""
    stmt = select(Order).where(Order.id == order_id)

    opt1 = _maybe_selectinload(Order, "images")
    if opt1 is not None:
        stmt = stmt.options(opt1)
    opt2 = _maybe_selectinload_nested(Order, "images", OrderImage, "image_file")
    if opt2 is not None:
        stmt = stmt.options(opt2)

    opt3 = _maybe_selectinload(Order, "order_info")
    if opt3 is not None:
        stmt = stmt.options(opt3)

    o = (await db.execute(stmt)).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    salesperson_id = int(getattr(o, "salesperson_id", 0) or 0)
    await _maybe_await(
        _ac_ensure_order_read_acl_by_salesperson_id(
            db=db,
            current_user=current_user,
            role_name=role_name,
            team_names=team_names,
            salesperson_id=salesperson_id,
        )
    )

    images_by_order_id: Dict[int, List[OrderImage]] = {int(order_id): list(getattr(o, "images", None) or [])}
    return _rm_to_order_out(o, storage=storage, images_by_order_id=images_by_order_id)


@router.get("", response_model=OrderListResponse)
async def list_orders(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=200),
        is_finished: Optional[bool] = Query(None),
        salesperson_id: Optional[int] = Query(None),
        created_by: Optional[int] = Query(None),
        customer_group_id: Optional[int] = Query(None),
        channel_group_id: Optional[int] = Query(None),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
) -> OrderListResponse:
    """订单列表（严格按 schemas.order.OrderListResponse）。"""
    role_name = ctx.primary_role or ""
    team_names = tuple(ctx.team_names or ())

    clauses: List = []
    if is_finished is not None:
        clauses.append(Order.is_finished == bool(is_finished))
    if salesperson_id is not None:
        clauses.append(Order.salesperson_id == int(salesperson_id))
    if created_by is not None:
        clauses.append(Order.created_by == int(created_by))
    if customer_group_id is not None:
        clauses.append(Order.customer_group_id == int(customer_group_id))
    if channel_group_id is not None:
        clauses.append(Order.channel_group_id == int(channel_group_id))

    await _ac_apply_orders_list_acl(
        current_user=ctx.user,
        role_name=role_name,
        team_names=team_names,
        clauses=clauses,
    )

    q = select(Order).order_by(Order.id.desc())
    if clauses:
        q = q.where(and_(*clauses))
    q = q.offset((page - 1) * page_size).limit(page_size)

    cq = select(func.count()).select_from(Order)
    if clauses:
        cq = cq.where(and_(*clauses))
    total = int((await db.execute(cq)).scalar() or 0)

    rows = (await db.execute(q)).scalars().all()
    items = await _rm_orders_to_list_items(rows)
    return OrderListResponse(total=total, items=items)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order_detail(
        order_id: int,
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
) -> OrderOut:
    role_name = ctx.primary_role
    tns = _ac_normalize_team_names(ctx.team_names)

    _ensure_orders_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, tns)

    return await _load_order_out(
        db,
        int(order_id),
        current_user=ctx.user,
        role_name=role_name,
        team_names=tns,
    )


@router.post("", response_model=OrderOut)
async def create_order(
        payload: OrderCreate,
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
) -> OrderOut:
    role_name = ctx.primary_role
    tns = _ac_normalize_team_names(ctx.team_names)

    _ensure_orders_access(role_name)
    _ensure_orders_write_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, tns)

    _ensure_required_customer_channel(
        customer_group_id=payload.customer_group_id,
        channel_group_id=payload.channel_group_id,
    )

    if role_name == ROLE_SALES:
        spid = int(ctx.user.id)
    else:
        spid = int(payload.salesperson_id or ctx.user.id)

    await _ensure_salesperson_exists(db, spid)
    await _ac_ensure_order_write_acl_by_salesperson_id(
        db,
        salesperson_id=spid,
        current_user=ctx.user,
        role_name=role_name,
        team_names=tns,
    )

    o = Order(
        module=payload.module or "order",
        created_by=ctx.user.id,
        salesperson_id=spid,
        customer_group_id=payload.customer_group_id,
        channel_group_id=payload.channel_group_id,
        dynamic_data=payload.dynamic_data or {},
        ocr_raw_json=payload.ocr_raw_json or {},
        status=payload.status or 0,
        audit_status=payload.audit_status or 0,
        is_finished=False,
        is_rebate=False,
        is_paid=False,
    )
    db.add(o)
    await db.flush()

    # ✅ schema 仅声明 remark：这里只确保存在 1:1 OrderInfo 行
    info = OrderInfo(order_id=int(o.id))
    db.add(info)

    await db.commit()
    return await _load_order_out(db, int(o.id), current_user=ctx.user, role_name=role_name, team_names=tns)


@router.put("/{order_id}", response_model=OrderOut)
async def update_order_detail(
        order_id: int,
        payload: OrderUpdate,
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
) -> OrderOut:
    role_name = ctx.primary_role
    tns = _ac_normalize_team_names(ctx.team_names)

    _ensure_orders_access(role_name)
    _ensure_orders_write_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, tns)

    o = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    await _ac_ensure_order_write_acl_by_salesperson_id(
        db,
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        current_user=ctx.user,
        role_name=role_name,
        team_names=tns,
    )

    if getattr(o, "is_finished", False) and role_name not in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        raise HTTPException(status_code=403, detail="Finished order cannot be edited")

    if payload.salesperson_id is not None:
        spid = int(payload.salesperson_id)
        await _ensure_salesperson_exists(db, spid)
        await _ac_ensure_order_write_acl_by_salesperson_id(
            db,
            salesperson_id=spid,
            current_user=ctx.user,
            role_name=role_name,
            team_names=tns,
        )
        o.salesperson_id = spid

    if payload.customer_group_id is not None:
        o.customer_group_id = payload.customer_group_id
    if payload.channel_group_id is not None:
        o.channel_group_id = payload.channel_group_id

    # ✅ 与 schema 对齐：status/audit_status/ocr_raw_json/dynamic_data 都允许更新
    if payload.status is not None:
        o.status = int(payload.status)
    if payload.audit_status is not None:
        o.audit_status = int(payload.audit_status)
    if payload.ocr_raw_json is not None:
        o.ocr_raw_json = payload.ocr_raw_json or {}
    if payload.dynamic_data is not None:
        o.dynamic_data = {**(o.dynamic_data or {}), **(payload.dynamic_data or {})}

    if payload.order_info is not None:
        info = (await db.execute(select(OrderInfo).where(OrderInfo.order_id == int(order_id)))).scalar_one_or_none()
        if not info:
            info = OrderInfo(order_id=int(order_id))
            db.add(info)
        _apply_order_info_patch_remark(info, payload.order_info)

    await db.commit()
    return await _load_order_out(db, int(order_id), current_user=ctx.user, role_name=role_name, team_names=tns)


@router.patch("/{order_id}/status")
async def update_order_status(
        order_id: int,
        payload: OrderStatusUpdate,
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    role_name = ctx.primary_role
    tns = _ac_normalize_team_names(ctx.team_names)

    _ensure_orders_access(role_name)
    _ensure_orders_write_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, tns)

    o = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    await _ac_ensure_order_write_acl_by_salesperson_id(
        db,
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        current_user=ctx.user,
        role_name=role_name,
        team_names=tns,
    )

    if payload.is_finished is not None:
        if getattr(o, "is_finished", False) and payload.is_finished is False and role_name not in (
                ROLE_SUPER_ADMIN,
                ROLE_MANAGER,
        ):
            raise HTTPException(status_code=403, detail="Only manager/super_admin can reopen finished order")

        if bool(payload.is_finished) is True:
            _ensure_required_customer_channel(
                customer_group_id=getattr(o, "customer_group_id", None),
                channel_group_id=getattr(o, "channel_group_id", None),
            )

        o.is_finished = bool(payload.is_finished)

    # 财务字段：orders 模块拒绝更新（schema 允许声明以保证契约完整）
    if payload.is_rebate is not None or payload.is_paid is not None:
        raise HTTPException(status_code=400, detail="Finance fields cannot be updated in orders module")

    await db.commit()
    return {"ok": True}


# === ACL shared overrides ===
_split_team_names_any = _ac_split_team_names_any
_pick_manager_id_from_salesperson = _ac_pick_manager_id_from_salesperson
_pick_manager_name_inline = _ac_pick_manager_name_inline
_normalize_team_names = _ac_normalize_team_names
_user_team_match_expr = _ac_user_team_match_expr
_order_salesperson_in_teams_expr = _ac_order_salesperson_in_teams_expr
_current_team_names_or_403 = _ac_current_team_names_or_403
_effective_team_filter_for_query = _ac_effective_team_filter_for_query
_salesperson_in_current_teams_or_403 = _ac_salesperson_in_current_teams_or_403
_require_team_for_non_super_admin = _ac_require_team_for_non_super_admin
_require_single_team_for_strict_roles = _ac_require_single_team_for_strict_roles
_allowed_teams_for_user = _ac_allowed_teams_for_user
_require_team_filter_allowed = _ac_require_team_filter_allowed
_ac_ensure_user_in_teams = _ac_ensure_user_in_teams
_ac_ensure_order_read_acl_by_salesperson_id = _ac_ensure_order_read_acl_by_salesperson_id
_ac_ensure_order_write_acl_by_salesperson_id = _ac_ensure_order_write_acl_by_salesperson_id
_apply_orders_list_acl = _ac_apply_orders_list_acl
