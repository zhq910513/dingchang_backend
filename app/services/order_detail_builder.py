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

from app.core.access_control import (
    ensure_order_read_acl_by_salesperson_id,
    pick_manager_id_from_salesperson,
    pick_manager_name_inline,
    split_team_names_any,
)
from app.core.slot_field_config import ORDERED_SLOT_KEYS, get_slot_field_defs, get_slot_title
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
        t = get_slot_title(sk)
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


def _build_images_by_slot(order_images: List[OrderImage]) -> Dict[str, List[Dict[str, Any]]]:
    by_slot: Dict[str, List[Dict[str, Any]]] = {}
    for oi in (order_images or []):
        sk = str(getattr(oi, "slot_key", "") or "").strip() or "unknown"
        imf = getattr(oi, "image_file", None)
        row = {
            "id": int(getattr(oi, "id", 0) or 0) if getattr(oi, "id", None) is not None else None,
            "slot_key": sk,
            "storage_key": getattr(oi, "storage_key", None),
            "image_url": getattr(oi, "image_url", None) or getattr(imf, "url", None),
            "image_file_id": int(getattr(oi, "image_file_id", 0) or 0)
            if getattr(oi, "image_file_id", None) is not None
            else None,
            "created_at": _fmt_dt(getattr(oi, "created_at", None)),
            "image_file": None,
        }
        if imf:
            row["image_file"] = {
                "id": int(getattr(imf, "id", 0) or 0) if getattr(imf, "id", None) is not None else None,
                "sha256": getattr(imf, "sha256", None),
                "storage_key": getattr(imf, "storage_key", None),
                "original_name": getattr(imf, "original_name", None),
                "content_type": getattr(imf, "content_type", None),
                "url": getattr(imf, "url", None),
                "size": int(getattr(imf, "size", 0) or 0) if getattr(imf, "size", None) is not None else None,
                "created_at": _fmt_dt(getattr(imf, "created_at", None)),
                "updated_at": _fmt_dt(getattr(imf, "updated_at", None)),
            }
        by_slot.setdefault(sk, []).append(row)

    for k, arr in by_slot.items():
        by_slot[k] = sorted(arr, key=lambda x: (x.get("id") is None, x.get("id") or 0))
    return by_slot


def _slot_fields_from_dynamic(slot_key: str, dynamic_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    ✅ 防炸：未知 slot_key / 配置缺失时，返回空字段列表（不抛异常）
    """
    d = dynamic_data or {}
    try:
        defs = get_slot_field_defs(slot_key)
        defs = defs or []
    except Exception:
        defs = []

    out: List[Dict[str, Any]] = []
    for f in defs:
        try:
            source_key = str((f or {}).get("source_key") or "").strip()
        except Exception:
            source_key = ""
        if not source_key:
            continue
        key = str((f or {}).get("key") or source_key)
        label = str((f or {}).get("label") or key)
        out.append(_field_item(key, label, d.get(source_key)))
    return out


async def fetch_order_with_relations(db: AsyncSession, order_id: int) -> Optional[Order]:
    opts = [
        selectinload(Order.salesperson),
        selectinload(Order.customer_group),
        selectinload(Order.channel_group),
        selectinload(Order.order_info),
        selectinload(Order.images).selectinload(OrderImage.image_file),
    ]
    if hasattr(Order, "creator"):
        opts.insert(0, selectinload(getattr(Order, "creator")))
    stmt = (
        select(Order)
        .where(Order.id == int(order_id))
        .options(*tuple(opts))
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
    dyn = getattr(o, "dynamic_data", None) or {}

    team_name_val = (getattr(sp, "team_name", None) or None) if sp else None
    team_names_val = split_team_names_any(getattr(sp, "team_names", None)) if sp else []
    if not team_names_val and team_name_val and str(team_name_val).strip():
        team_names_val = [str(team_name_val).strip()]

    ensure_display_urls_for_order_images(getattr(o, "images", None) or [], storage)
    images_by_slot = _build_images_by_slot(getattr(o, "images", None) or [])

    channel_fields: List[Dict[str, Any]] = [
        _field_item("channel_group_id", "渠道组ID", getattr(o, "channel_group_id", None)),
        _field_item("channel_group_name", "渠道组", _group_code_name(chg)),
    ]
    if chg is not None:
        if hasattr(chg, "channel_code"):
            channel_fields.append(_field_item("channel_code", "渠道编码", getattr(chg, "channel_code", None)))
        if hasattr(chg, "channel_name"):
            channel_fields.append(_field_item("channel_name", "渠道名称", getattr(chg, "channel_name", None)))

    customer_fields: List[Dict[str, Any]] = [
        _field_item("customer_group_id", "客户组ID", getattr(o, "customer_group_id", None)),
        _field_item("customer_group_name", "客户组", _group_code_name(cg)),
        _field_item("customer_group_market", "客户渠道市场", getattr(cg, "market", None) if cg else None),
        _field_item("salesperson_id", "业务员ID", getattr(o, "salesperson_id", None)),
        _field_item("salesperson_name", "业务员", _user_display_name(sp)),
        _field_item("manager_id", "经理ID", manager_id_val),
        _field_item("manager_name", "经理", manager_name_val),
        _field_item(
            "team_name",
            "团队",
            (str(team_name_val).strip() if team_name_val is not None and str(team_name_val).strip() else None),
        ),
        _field_item("team_names", "团队列表", team_names_val),
    ]
    if cg is not None:
        if hasattr(cg, "customer_code"):
            customer_fields.append(_field_item("customer_code", "客户编码", getattr(cg, "customer_code", None)))
        if hasattr(cg, "customer_name"):
            customer_fields.append(_field_item("customer_name", "客户名称", getattr(cg, "customer_name", None)))

    order_fields: List[Dict[str, Any]] = [
        _field_item("id", "订单ID", getattr(o, "id", None)),
        _field_item("created_by", "创建人ID", getattr(o, "created_by", None)),
        _field_item("is_finished", "已完成", bool(getattr(o, "is_finished", False))),
        _field_item("is_rebate", "已返点", bool(getattr(o, "is_rebate", False))),
        _field_item("is_paid", "已回款", bool(getattr(o, "is_paid", False))),
        _field_item("created_at", "创建时间", _fmt_dt(getattr(o, "created_at", None))),
        _field_item("updated_at", "更新时间", _fmt_dt(getattr(o, "updated_at", None))),
    ]

    if info is not None:
        order_info_keys = [
            ("insurance_expire_date", "保险到期日"),
            ("owner_phone", "车主电话"),
            ("remark", "订单备注"),
            ("commercial_amount", "商业险保费"),
            ("commercial_after_amount", "商业险折后保费"),
            ("compulsory_amount", "交强险保费"),
            ("vehicle_tax_amount", "车船税"),
            ("non_vehicle_amount", "非车险保费"),
            ("premium_total", "保费合计"),
            ("channel_commercial_point", "渠道商业险点位"),
            ("channel_commercial_supplement_point", "渠道商业险补充点位"),
            ("channel_compulsory_point", "渠道交强险点位"),
            ("channel_vehicle_tax_point", "渠道车船税点位"),
            ("channel_non_vehicle_point", "渠道非车险点位"),
            ("channel_reward", "渠道奖励"),
            ("channel_total", "渠道应收"),
            ("customer_commercial_point", "客户商业险点位"),
            ("customer_commercial_supplement_point", "客户商业险补充点位"),
            ("customer_compulsory_point", "客户交强险点位"),
            ("customer_vehicle_tax_point", "客户车船税点位"),
            ("customer_non_vehicle_point", "客户非车险点位"),
            ("customer_reward", "客户奖励"),
            ("customer_total", "客户应付"),
            ("profit", "利润"),
        ]
        for k, label in order_info_keys:
            if hasattr(info, k):
                order_fields.append(_field_item(k, label, getattr(info, k, None)))

    slot_sections: Dict[str, Any] = {}
    ordered_slots = list(ORDERED_SLOT_KEYS)
    for unknown_sk in images_by_slot.keys():
        if unknown_sk not in ordered_slots:
            ordered_slots.append(unknown_sk)

    for sk in ordered_slots:
        slot_sections[sk] = {
            "slot_key": sk,
            "title": _slot_title(sk),
            "fields": _slot_fields_from_dynamic(sk, dyn),
            "images": images_by_slot.get(sk, []),
        }

    return {
        "channel": {"key": "channel", "title": "渠道信息", "fields": channel_fields, "images": []},
        "customer": {"key": "customer", "title": "客户信息", "fields": customer_fields, "images": []},
        "order_info": {"key": "order_info", "title": "订单信息", "fields": order_fields, "images": []},
        "slots": slot_sections,
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
            "team_name": (str(team_name_val).strip() if team_name_val is not None and str(team_name_val).strip() else None),
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
