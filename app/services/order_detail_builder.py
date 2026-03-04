# app/services/order_detail_builder.py
# encoding: utf-8
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


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


from app.core.access_control import (
    ensure_order_read_acl_by_salesperson_id,
    pick_manager_id_from_salesperson,
    pick_manager_name_inline,
    split_team_names_any,
)
from app.core.slot_field_config import (
    slot_title,
)
from app.models.order import Order, OrderImage
from app.models.user import User
from app.services.storage import StorageService
from app.utils.order_image_urls import ensure_display_urls_for_order_images


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


def _slot_title(slot_key: str) -> str:
    """
    ✅ 防炸：历史脏数据/未知 slot_key 也必须能渲染（不抛异常）
    """
    sk = str(slot_key or "").strip()
    if not sk:
        return "unknown"
    try:
        t = slot_title(sk)
        ts = str(t or "").strip()
        return ts or sk
    except Exception:
        return sk


def _to_json_value(v: Any) -> Any:
    # FastAPI 的 jsonable_encoder 也能处理 Decimal/date/datetime，但这里先做最常见的显式转换更稳
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return v


def _fmt_dt(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    try:
        return str(v)
    except Exception:
        return None


def _field_item(key: str, label: str, value: Any) -> Dict[str, Any]:
    return {"key": str(key), "label": str(label), "value": _to_json_value(value)}


async def fetch_order_with_relations(db: AsyncSession, order_id: int) -> Optional[Order]:
    stmt = (
        select(Order)
        .where(Order.id == int(order_id))
        .options(*tuple([x for x in [
            _maybe_selectinload(Order, "creator"),
            _maybe_selectinload(Order, "salesperson"),
            _maybe_selectinload(Order, "customer_group"),
            _maybe_selectinload(Order, "channel_group"),
            _maybe_selectinload(Order, "order_info"),
            _maybe_selectinload_nested(Order, "images", OrderImage, "image_file"),
        ] if x is not None]))
    )
    return (await db.execute(stmt)).scalars().first()


async def _resolve_manager_info(db: AsyncSession, salesperson: Optional[User]) -> Tuple[Optional[int], Optional[str]]:
    if not salesperson:
        return None, None

    manager_name_val = pick_manager_name_inline(salesperson)
    manager_id_val: Optional[int] = None

    mid = pick_manager_id_from_salesperson(salesperson)
    if mid:
        manager_id_val = int(mid)
        if not manager_name_val:
            mgr = (await db.execute(select(User).where(User.id == int(mid)))).scalars().first()
            manager_name_val = _user_display_name(mgr)

    return manager_id_val, manager_name_val


def build_order_detail_sections(
        o: Order, *, manager_id_val: Optional[int], manager_name_val: Optional[str], storage: StorageService
) -> Dict[str, Any]:
    cg = getattr(o, "customer_group", None)
    chg = getattr(o, "channel_group", None)
    sp = getattr(o, "salesperson", None)
    info = getattr(o, "order_info", None)

    team_name_val = (getattr(sp, "team_name", None) or None) if sp else None
    team_names_val = split_team_names_any(getattr(sp, "team_names", None)) if sp else []
    if not team_names_val and team_name_val and str(team_name_val).strip():
        team_names_val = [str(team_name_val).strip()]

    # ✅ 先确保 images 中的 image_url 是可展示 URL（签名/公网/回退）
    ensure_display_urls_for_order_images(getattr(o, "images", None) or [], storage)

    # ✅ slot_images：严格按 slot_field_config 契约产出（唯一真源）
    from app.utils.order_image_urls import build_slot_images  # local import to avoid circular risk
    slot_images = build_slot_images(o, storage)
    channel_fields: List[Dict[str, Any]] = [
        _field_item("channel_group_id", "渠道组ID", getattr(o, "channel_group_id", None)),
        _field_item("channel_group_name", "渠道组", _group_code_name(chg)),
    ]
    if chg is not None:
        if hasattr(chg, "channel_code"):
            channel_fields.append(_field_item("channel_code", "渠道编码", getattr(chg, "channel_code", None)))
        if hasattr(chg, "channel_name"):
            channel_fields.append(_field_item("channel_name", "渠道名称", getattr(chg, "channel_name", None)))
        if hasattr(chg, "region"):
            channel_fields.append(_field_item("region", "归属地", getattr(chg, "region", None)))
        if hasattr(chg, "contacts"):
            channel_fields.append(_field_item("contacts", "联系方式", getattr(chg, "contacts", None)))

    customer_fields: List[Dict[str, Any]] = [
        _field_item("customer_group_id", "客户组ID", getattr(o, "customer_group_id", None)),
        _field_item("customer_group_name", "客户组", _group_code_name(cg)),
        _field_item("customer_group_market", "市场", getattr(cg, "market", None) if cg else None),
    ]
    if cg is not None:
        if hasattr(cg, "customer_code"):
            customer_fields.append(_field_item("customer_code", "客户编码", getattr(cg, "customer_code", None)))
        if hasattr(cg, "customer_name"):
            customer_fields.append(_field_item("customer_name", "客户名称", getattr(cg, "customer_name", None)))
        if hasattr(cg, "market"):
            customer_fields.append(_field_item("market", "市场", getattr(cg, "market", None)))

    order_fields: List[Dict[str, Any]] = [
        _field_item("id", "订单ID", getattr(o, "id", None)),
        _field_item("module", "模块", getattr(o, "module", None)),
        _field_item("created_by", "创建人ID", getattr(o, "created_by", None)),
        _field_item("salesperson_id", "业务员ID", getattr(o, "salesperson_id", None)),
        _field_item("salesperson_name", "业务员", _user_display_name(sp)),
        _field_item("manager_id", "主管ID", manager_id_val),
        _field_item("manager_name", "主管", manager_name_val),
        _field_item("team_name", "团队(单)",
                    str(team_name_val).strip() if team_name_val is not None and str(team_name_val).strip() else None),
        _field_item("team_names", "团队(多)", team_names_val),
        _field_item("is_finished", "是否完成", bool(getattr(o, "is_finished", False))),
        _field_item("is_rebate", "是否返利", bool(getattr(o, "is_rebate", False))),
        _field_item("is_paid", "是否支付", bool(getattr(o, "is_paid", False))),
        _field_item("status", "订单状态", getattr(o, "status", None)),
        _field_item("audit_status", "审核状态", getattr(o, "audit_status", None)),
        _field_item("created_at", "创建时间", _fmt_dt(getattr(o, "created_at", None))),
        _field_item("updated_at", "更新时间", _fmt_dt(getattr(o, "updated_at", None))),
    ]

    # order_info（1:1 扩展）——按 from_orm/字段存在性防守式输出
    if info is not None:
        # 常见字段尽量输出，缺则跳过
        for k, label in [
            ("remark", "订单备注"),
        ]:
            if hasattr(info, k):
                order_fields.append(_field_item(k, label, getattr(info, k, None)))

    return {
        "channel": {"key": "channel", "title": "渠道信息", "fields": channel_fields, "images": []},
        "customer": {"key": "customer", "title": "客户信息", "fields": customer_fields, "images": []},
        "order_info": {"key": "order_info", "title": "订单信息", "fields": order_fields, "images": []},
        "slot_images": slot_images,
    }


async def build_order_detail_blocks_from_order(
        db: AsyncSession,
        o: Order,
        *,
        storage: StorageService,
) -> Dict[str, Any]:
    sp = getattr(o, "salesperson", None)
    manager_id_val, manager_name_val = await _resolve_manager_info(db, sp)

    sections = build_order_detail_sections(
        o, manager_id_val=manager_id_val, manager_name_val=manager_name_val, storage=storage
    )

    team_name_val = (getattr(sp, "team_name", None) or None) if sp else None
    team_names_val = split_team_names_any(getattr(sp, "team_names", None)) if sp else []
    if not team_names_val and team_name_val and str(team_name_val).strip():
        team_names_val = [str(team_name_val).strip()]

    cg = getattr(o, "customer_group", None)

    return {
        "id": int(getattr(o, "id", 0) or 0),
        "base": {
            "created_by": getattr(o, "created_by", None),
            "salesperson_id": getattr(o, "salesperson_id", None),
            "salesperson_name": _user_display_name(sp),
            "customer_group_id": getattr(o, "customer_group_id", None),
            "channel_group_id": getattr(o, "channel_group_id", None),
            "customer_group_name": _group_code_name(cg),
            "channel_group_name": _group_code_name(getattr(o, "channel_group", None)),
            "customer_group_market": getattr(cg, "market", None) if cg else None,
            "manager_id": manager_id_val,
            "manager_name": manager_name_val,
            "team_name": (
                str(team_name_val).strip() if team_name_val is not None and str(team_name_val).strip() else None),
            "team_names": team_names_val,
            "is_finished": bool(getattr(o, "is_finished", False)),
            "is_rebate": bool(getattr(o, "is_rebate", False)),
            "is_paid": bool(getattr(o, "is_paid", False)),
            "created_at": _fmt_dt(getattr(o, "created_at", None)),
            "updated_at": _fmt_dt(getattr(o, "updated_at", None)),
        },
        "sections": sections,
    }


async def load_order_detail_blocks(
        db: AsyncSession,
        order_id: int,
        *,
        current_user: User,
        role_name: Optional[str],
        team_names: Tuple[str, ...],
        storage: StorageService,
        enforce_read_acl: bool = True,
) -> Dict[str, Any]:
    o = await fetch_order_with_relations(db, int(order_id))
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    if enforce_read_acl:
        await ensure_order_read_acl_by_salesperson_id(
            db,
            salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
            current_user=current_user,
            role_name=role_name,
            team_names=team_names,
        )

    return await build_order_detail_blocks_from_order(db, o, storage=storage)
