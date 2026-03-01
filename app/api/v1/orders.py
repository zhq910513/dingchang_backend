# app/api/v1/orders.py
# encoding: utf-8
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Any, Dict, List, Tuple, Set

import anyio
import requests
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy import func, select, and_, or_, cast, String, distinct, delete
from sqlalchemy.exc import IntegrityError
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


from app.api.deps import get_current_user_with_role_and_teams
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
from app.core.db import get_db, engine
from app.models.channel_group import ChannelGroup
from app.models.customer_group import CustomerGroup
from app.models.image_file import ImageFile
from app.models.ocr_task import OcrTask
from app.models.order import Order, OrderImage
from app.models.order_info import OrderInfo
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole
from app.schemas.order import (
    OrderCreate,
    OrderUpdate,
    OrderOut,
    OrderListResponse,
    OrderStatusUpdate,
    OrderInfoIn,
    OrderInfoOut,
    OrderListMeta,
)
from app.services.storage import StorageService
from app.services.order_detail_builder import load_order_detail_blocks
from app.services.order_read_model import preload_options as _rm_preload_options, orders_to_out_list as _rm_orders_to_out_list
from app.utils.order_image_urls import ensure_display_urls_for_order_images, safe_image_urls

router = APIRouter(prefix="/orders", tags=["orders"])
storage = StorageService()

OCR_SLOTS = {
    "vehicle_cert",
    "idcard_front",
    "idcard_back",
    "driving_license_main",
    "driving_license_sub",
}
NON_OCR_SLOTS = {"related"}
ALL_SLOTS = OCR_SLOTS | NON_OCR_SLOTS

# 多图槽
MULTI_SLOTS = {"related"}

def _build_list_meta(*, role_name: str) -> OrderListMeta:
    rn = str(role_name or "")
    # orders侧：业务字段可编辑范围由后端ACL+接口限制决定；这里仅给前端“UI能力提示”
    caps = {
        "can_edit_paid": False,
        "can_edit_rebate": False,
        "can_return_to_unfinished": False,
        "can_download": True,
        "can_mark_finished": rn in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_SALES, ROLE_MARKET),
        "can_reopen": rn in (ROLE_SUPER_ADMIN, ROLE_MANAGER),
        "image_slots_writable": ["vehicle_cert","idcard_front","idcard_back","driving_license_main","driving_license_sub","related"],
    }
    return OrderListMeta(source="orders", capabilities=caps)


def _ensure_orders_access(role_name: Optional[str], *, allow_finance: bool = False) -> None:
    rn = role_name or ""
    if rn not in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_FINANCE, ROLE_MARKET, ROLE_SALES):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_orders_write_access(role_name: Optional[str]) -> None:
    if role_name in (ROLE_FINANCE, ROLE_MARKET):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_finance_related_only_slot(slot_key: str) -> None:
    if str(slot_key or "").strip() != "related":
        raise HTTPException(status_code=403, detail="Finance can only operate related images")


def _ensure_finance_finalize_payload_related_only(payload) -> None:
    if getattr(payload, "salesperson_id", None) is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update salesperson_id in orders.finalize")
    if getattr(payload, "customer_group_id", None) is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update customer_group_id in orders.finalize")
    if getattr(payload, "channel_group_id", None) is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update channel_group_id in orders.finalize")
    if getattr(payload, "order_info", None) is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update order_info in orders.finalize")

    dyn = getattr(payload, "dynamic_data", None) or {}
    if isinstance(dyn, dict) and len(dyn) > 0:
        raise HTTPException(status_code=403, detail="Finance cannot update dynamic_data in orders.finalize")

    clear_slots = [str(x or "").strip() for x in (getattr(payload, "clear_slots", None) or [])]
    clear_slots = [x for x in clear_slots if x]
    for sk in clear_slots:
        if sk != "related":
            raise HTTPException(status_code=403, detail="Finance can only clear related slot")

    for im in getattr(payload, "images", None) or []:
        sk = str(getattr(im, "slot_key", "") or "").strip()
        if sk != "related":
            raise HTTPException(status_code=403, detail="Finance can only finalize related images")


def _ensure_required_customer_channel(*, customer_group_id: Optional[int], channel_group_id: Optional[int]) -> None:
    if customer_group_id is None:
        raise HTTPException(status_code=400, detail="customer_group_id is required")
    if channel_group_id is None:
        raise HTTPException(status_code=400, detail="channel_group_id is required")


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


def _to_decimal_or_none(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_decimal_or_zero(v: Any) -> Decimal:
    d = _to_decimal_or_none(v)
    return d if d is not None else Decimal("0")


def _ensure_non_negative(v: Decimal, field: str) -> None:
    if v < 0:
        raise HTTPException(status_code=400, detail=f"{field} must be >= 0")


def _model_fields_set(m: Any) -> Set[str]:
    fs = getattr(m, "model_fields_set", None)
    if isinstance(fs, set):
        return {str(x) for x in fs}
    fs2 = getattr(m, "__fields_set__", None)
    if isinstance(fs2, set):
        return {str(x) for x in fs2}
    return set()


def _recalc_order_info(info: OrderInfo) -> None:
    cm = _to_decimal_or_zero(getattr(info, "commercial_amount", None))
    ca = _to_decimal_or_zero(getattr(info, "compulsory_amount", None))
    vta = _to_decimal_or_zero(getattr(info, "vehicle_tax_amount", None))
    nva = _to_decimal_or_zero(getattr(info, "non_vehicle_amount", None))

    _ensure_non_negative(cm, "commercial_amount")
    _ensure_non_negative(ca, "compulsory_amount")
    _ensure_non_negative(vta, "vehicle_tax_amount")
    _ensure_non_negative(nva, "non_vehicle_amount")

    info.commercial_amount = cm
    info.compulsory_amount = ca
    info.vehicle_tax_amount = vta
    info.non_vehicle_amount = nva

    ch_cm_p = _to_decimal_or_zero(getattr(info, "channel_commercial_point", None))
    ch_cm_supp_p = _to_decimal_or_zero(getattr(info, "channel_commercial_supplement_point", None))
    ch_ca_p = _to_decimal_or_zero(getattr(info, "channel_compulsory_point", None))
    ch_vta_p = _to_decimal_or_zero(getattr(info, "channel_vehicle_tax_point", None))
    ch_nva_p = _to_decimal_or_zero(getattr(info, "channel_non_vehicle_point", None))
    ch_reward = _to_decimal_or_zero(getattr(info, "channel_reward", None))

    cu_cm_p = _to_decimal_or_zero(getattr(info, "customer_commercial_point", None))
    cu_cm_supp_p = _to_decimal_or_zero(getattr(info, "customer_commercial_supplement_point", None))
    cu_ca_p = _to_decimal_or_zero(getattr(info, "customer_compulsory_point", None))
    cu_vta_p = _to_decimal_or_zero(getattr(info, "customer_vehicle_tax_point", None))
    cu_nva_p = _to_decimal_or_zero(getattr(info, "customer_non_vehicle_point", None))
    cu_reward = _to_decimal_or_zero(getattr(info, "customer_reward", None))

    info.channel_commercial_point = ch_cm_p
    info.channel_commercial_supplement_point = ch_cm_supp_p
    info.channel_compulsory_point = ch_ca_p
    info.channel_vehicle_tax_point = ch_vta_p
    info.channel_non_vehicle_point = ch_nva_p
    info.channel_reward = ch_reward

    info.customer_commercial_point = cu_cm_p
    info.customer_commercial_supplement_point = cu_cm_supp_p
    info.customer_compulsory_point = cu_ca_p
    info.customer_vehicle_tax_point = cu_vta_p
    info.customer_non_vehicle_point = cu_nva_p
    info.customer_reward = cu_reward

    info.premium_total = cm + ca + vta + nva

    channel_total = (
        (cm * (ch_cm_p / Decimal("100")))
        + (cm * (ch_cm_supp_p / Decimal("100")))
        + (ca * (ch_ca_p / Decimal("100")))
        + (vta * (ch_vta_p / Decimal("100")))
        + (nva * (ch_nva_p / Decimal("100")))
        + ch_reward
    )
    customer_total = (
        (cm * (cu_cm_p / Decimal("100")))
        + (cm * (cu_cm_supp_p / Decimal("100")))
        + (ca * (cu_ca_p / Decimal("100")))
        + (vta * (cu_vta_p / Decimal("100")))
        + (nva * (cu_nva_p / Decimal("100")))
        + cu_reward
    )

    info.channel_total = channel_total
    info.customer_total = customer_total
    info.profit = channel_total - customer_total


def _apply_order_info_patch(info: OrderInfo, payload: OrderInfoIn) -> None:
    if payload is None:
        return

    fs = _model_fields_set(payload)

    if "insurance_expire_date" in fs:
        v = getattr(payload, "insurance_expire_date", None)
        info.insurance_expire_date = None if (v is None or v == "") else v

    if "owner_phone" in fs:
        info.owner_phone = str(getattr(payload, "owner_phone", "") or "").strip()

    if hasattr(payload, "remark") and "remark" in fs:
        v = getattr(payload, "remark", None)
        setattr(info, "remark", None if (v is None or v == "") else str(v).strip())

    if "commercial_amount" in fs:
        info.commercial_amount = _to_decimal_or_zero(getattr(payload, "commercial_amount", None))

    if hasattr(payload, "commercial_after_amount") and "commercial_after_amount" in fs:
        setattr(info, "commercial_after_amount", _to_decimal_or_zero(getattr(payload, "commercial_after_amount", None)))

    if "compulsory_amount" in fs:
        info.compulsory_amount = _to_decimal_or_zero(getattr(payload, "compulsory_amount", None))
    if "vehicle_tax_amount" in fs:
        info.vehicle_tax_amount = _to_decimal_or_zero(getattr(payload, "vehicle_tax_amount", None))
    if "non_vehicle_amount" in fs:
        info.non_vehicle_amount = _to_decimal_or_zero(getattr(payload, "non_vehicle_amount", None))

    if "channel_commercial_point" in fs:
        info.channel_commercial_point = _to_decimal_or_zero(getattr(payload, "channel_commercial_point", None))

    if hasattr(payload, "channel_commercial_supplement_point") and "channel_commercial_supplement_point" in fs:
        info.channel_commercial_supplement_point = _to_decimal_or_zero(
            getattr(payload, "channel_commercial_supplement_point", None)
        )

    if "channel_compulsory_point" in fs:
        info.channel_compulsory_point = _to_decimal_or_zero(getattr(payload, "channel_compulsory_point", None))
    if "channel_vehicle_tax_point" in fs:
        info.channel_vehicle_tax_point = _to_decimal_or_zero(getattr(payload, "channel_vehicle_tax_point", None))
    if "channel_non_vehicle_point" in fs:
        info.channel_non_vehicle_point = _to_decimal_or_zero(getattr(payload, "channel_non_vehicle_point", None))
    if "channel_reward" in fs:
        info.channel_reward = _to_decimal_or_zero(getattr(payload, "channel_reward", None))

    if "customer_commercial_point" in fs:
        info.customer_commercial_point = _to_decimal_or_zero(getattr(payload, "customer_commercial_point", None))

    if hasattr(payload, "customer_commercial_supplement_point") and "customer_commercial_supplement_point" in fs:
        info.customer_commercial_supplement_point = _to_decimal_or_zero(
            getattr(payload, "customer_commercial_supplement_point", None)
        )

    if "customer_compulsory_point" in fs:
        info.customer_compulsory_point = _to_decimal_or_zero(getattr(payload, "customer_compulsory_point", None))
    if "customer_vehicle_tax_point" in fs:
        info.customer_vehicle_tax_point = _to_decimal_or_zero(getattr(payload, "customer_vehicle_tax_point", None))
    if "customer_non_vehicle_point" in fs:
        info.customer_non_vehicle_point = _to_decimal_or_zero(getattr(payload, "customer_non_vehicle_point", None))
    if "customer_reward" in fs:
        info.customer_reward = _to_decimal_or_zero(getattr(payload, "customer_reward", None))

    _recalc_order_info(info)


def _order_info_out(info: Optional[OrderInfo]) -> Optional[OrderInfoOut]:
    if not info:
        return None
    return OrderInfoOut.from_orm(info)


async def _load_order_out(
    db: AsyncSession,
    order_id: int,
    *,
    current_user: User,
    role_name: Optional[str],
    team_names: Tuple[str, ...],
) -> OrderOut:
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .options(*tuple([x for x in [
            _maybe_selectinload(Order, "creator"),
            _maybe_selectinload(Order, "salesperson"),
            _maybe_selectinload(Order, "customer_group"),
            _maybe_selectinload(Order, "channel_group"),
            _maybe_selectinload(Order, "order_info"),
            _maybe_selectinload_nested(Order, "images", OrderImage, "image_file"),
        ] if x is not None]))
    )
    o = (await db.execute(stmt)).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    await _ensure_order_read_acl_by_salesperson_id(
        db,
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        current_user=current_user,
        role_name=role_name,
        team_names=team_names,
    )

    ensure_display_urls_for_order_images(getattr(o, "images", None) or [], storage)

    sp = getattr(o, "salesperson", None)
    team_name_val = (getattr(sp, "team_name", None) or None) if sp else None
    team_names_val = _split_team_names_any(getattr(sp, "team_names", None)) if sp else []
    if not team_names_val and team_name_val and str(team_name_val).strip():
        team_names_val = [str(team_name_val).strip()]

    manager_id_val = None
    manager_name_val = None
    if sp:
        manager_name_val = _pick_manager_name_inline(sp)
        mid = _pick_manager_id_from_salesperson(sp)
        if mid:
            manager_id_val = int(mid)
            if not manager_name_val:
                mgr = (await db.execute(select(User).where(User.id == int(mid)))).scalars().first()
                manager_name_val = _user_display_name(mgr)

    cg = getattr(o, "customer_group", None)
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


async def _ensure_salesperson_exists(db: AsyncSession, salesperson_id: int) -> None:
    u = (await db.execute(select(User.id, User.status).where(User.id == int(salesperson_id)))).first()
    if not u:
        raise HTTPException(status_code=400, detail="salesperson_id not found")
    if int(u.status or 0) != 1:
        raise HTTPException(status_code=400, detail="salesperson account is disabled")


async def _get_or_create_image_file(
    db: AsyncSession,
    *,
    storage_key: str,
    url: str,
    size: int,
    original_name: Optional[str],
    content_type: Optional[str],
    etag: Optional[str],
    md5: str,
) -> ImageFile:
    storage_key = (storage_key or "").strip().lstrip("/")
    md5 = (md5 or "").strip().lower()

    obj = (await db.execute(select(ImageFile).where(ImageFile.storage_key == storage_key))).scalar_one_or_none()
    if obj:
        if url and not (obj.url or "").strip():
            obj.url = url
        if size and int(getattr(obj, "size", 0) or 0) <= 0:
            obj.size = int(size)
        if content_type and not getattr(obj, "content_type", None):
            obj.content_type = content_type
        if original_name and not getattr(obj, "original_name", None):
            obj.original_name = original_name
        if etag and not getattr(obj, "etag", None):
            obj.etag = etag
        if md5 and not getattr(obj, "md5", None):
            obj.md5 = md5
        await db.flush()
        return obj

    obj = ImageFile(
        sha256=None,
        md5=md5 or None,
        storage_key=storage_key,
        url=url or "",
        size=int(size or 0),
        original_name=original_name,
        content_type=content_type,
        etag=etag,
    )
    db.add(obj)

    try:
        async with db.begin_nested():
            await db.flush()
        return obj
    except IntegrityError:
        obj2 = (await db.execute(select(ImageFile).where(ImageFile.storage_key == storage_key))).scalar_one_or_none()
        if obj2:
            return obj2

        if md5:
            obj3 = (await db.execute(select(ImageFile).where(ImageFile.md5 == md5))).scalar_one_or_none()
            if obj3:
                if url and not (obj3.url or "").strip():
                    obj3.url = url
                if size and int(getattr(obj3, "size", 0) or 0) <= 0:
                    obj3.size = int(size)
                if content_type and not getattr(obj3, "content_type", None):
                    obj3.content_type = content_type
                if original_name and not getattr(obj3, "original_name", None):
                    obj3.original_name = original_name
                if etag and not getattr(obj3, "etag", None):
                    obj3.etag = etag
                await db.flush()
                return obj3

        raise


class OptionItem(BaseModel):
    id: int
    group_name: str
    customer_code: Optional[str] = None
    customer_name: Optional[str] = None
    channel_code: Optional[str] = None
    channel_name: Optional[str] = None


class OptionListOut(BaseModel):
    items: List[OptionItem] = Field(default_factory=list)


@router.get("/customer-groups", response_model=OptionListOut)
async def list_customer_groups(
    status: Optional[int] = Query(None, description="可选：启用状态过滤（若模型有该字段）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_orders_access(role_name)

    tns = _normalize_team_names(team_names)
    _require_team_for_non_super_admin(role_name, tns)

    stmt = select(CustomerGroup).order_by(CustomerGroup.id.asc())
    if hasattr(CustomerGroup, "deleted_at"):
        stmt = stmt.where(getattr(CustomerGroup, "deleted_at").is_(None))
    if status is not None and hasattr(CustomerGroup, "status"):
        stmt = stmt.where(getattr(CustomerGroup, "status") == int(status))

    rows = (await db.execute(stmt)).scalars().all()

    items: List[OptionItem] = []
    for x in rows:
        c_code = getattr(x, "customer_code", None)
        c_name = getattr(x, "customer_name", None)

        c_code_s = str(c_code).strip() if c_code is not None and str(c_code).strip() else None
        c_name_s = str(c_name).strip() if c_name is not None and str(c_name).strip() else None

        items.append(
            OptionItem(
                id=int(x.id),
                group_name=str(_group_code_name(x) or _group_display_name(x) or ""),
                customer_code=c_code_s,
                customer_name=c_name_s,
            )
        )

    return OptionListOut(items=items)


@router.get("/channel-groups", response_model=OptionListOut)
async def list_channel_groups(
    status: Optional[int] = Query(None, description="可选：启用状态过滤（若模型有该字段）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_orders_access(role_name)
    _require_team_for_non_super_admin(role_name, _normalize_team_names(team_names))

    stmt = select(ChannelGroup).order_by(ChannelGroup.id.asc())
    if hasattr(ChannelGroup, "deleted_at"):
        stmt = stmt.where(getattr(ChannelGroup, "deleted_at").is_(None))
    if status is not None and hasattr(ChannelGroup, "status"):
        stmt = stmt.where(getattr(ChannelGroup, "status") == int(status))

    rows = (await db.execute(stmt)).scalars().all()

    items: List[OptionItem] = []
    for x in rows:
        ch_code = getattr(x, "channel_code", None)
        ch_name = getattr(x, "channel_name", None)

        ch_code_s = str(ch_code).strip() if ch_code is not None and str(ch_code).strip() else None
        ch_name_s = str(ch_name).strip() if ch_name is not None and str(ch_name).strip() else None

        items.append(
            OptionItem(
                id=int(x.id),
                group_name=str(_group_code_name(x) or _group_display_name(x) or ""),
                channel_code=ch_code_s,
                channel_name=ch_name_s,
            )
        )

    return OptionListOut(items=items)


class TeamItem(BaseModel):
    team_name: str


class TeamListOut(BaseModel):
    items: List[TeamItem] = Field(default_factory=list)


@router.get("/teams", response_model=TeamListOut)
async def list_teams(
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _ = db
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_orders_access(role_name)

    allowed = _allowed_teams_for_user(role_name, _normalize_team_names(team_names))
    items = [TeamItem(team_name=str(t)) for t in allowed if str(t).strip()]
    return TeamListOut(items=items)


class SalespersonItem(BaseModel):
    id: int
    username: str
    real_name: Optional[str] = None


class SalespersonListOut(BaseModel):
    items: List[SalespersonItem] = Field(default_factory=list)


@router.get("/salespersons", response_model=SalespersonListOut)
async def list_salespersons(
    status: int = Query(1, description="默认仅返回启用账号；传 0 可查禁用"),
    team_name: Optional[str] = Query(None, description="可选：按团队过滤业务员下拉（用于前端“职位下拉框跟随团队”）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    current_user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name)
    _require_team_for_non_super_admin(role_name, tns)

    if role_name not in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_SALES, ROLE_MARKET, ROLE_FINANCE):
        raise HTTPException(status_code=403, detail="No permission")

    tf = str(team_name or "").strip()
    if tf:
        _require_team_filter_allowed(role_name=role_name, team_names=tns, team_filter=tf)

    stmt = (
        select(distinct(User.id).label("id"), User.username, User.real_name)
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.role_name == ROLE_SALES)
        .order_by(User.id.asc())
    )
    stmt = stmt.where(User.status == int(status))

    if role_name != ROLE_SUPER_ADMIN:
        if role_name == ROLE_MANAGER:
            stmt = stmt.where(_user_team_match_expr(tns))
        else:
            my_team = _require_single_team_for_strict_roles(role_name, tns)
            stmt = stmt.where(_user_team_match_expr((my_team,)))

    if role_name == ROLE_SALES:
        stmt = stmt.where(User.id == current_user.id)

    if tf:
        stmt = stmt.where(_user_team_match_expr((tf,)))

    rows = (await db.execute(stmt)).all()
    return SalespersonListOut(items=[SalespersonItem(id=int(r.id), username=str(r.username), real_name=r.real_name) for r in rows])


class OcrTaskItemOut(BaseModel):
    id: int
    order_id: Optional[int] = None
    status: str
    progress: int = 0
    error_message: Optional[str] = None


class OcrTaskListOut(BaseModel):
    items: List[OcrTaskItemOut] = Field(default_factory=list)


async def _apply_ocr_task_acl(
    db: AsyncSession,
    *,
    current_user: User,
    role_name: Optional[str],
    team_names: Tuple[str, ...],
    stmt,
):
    _ = db
    rn = role_name or ""

    if rn == ROLE_MARKET:
        raise HTTPException(status_code=403, detail="No permission")

    if rn == ROLE_SUPER_ADMIN:
        return stmt

    _require_team_for_non_super_admin(role_name, team_names)
    tns = _normalize_team_names(team_names)

    stmt = stmt.join(Order, and_(Order.id == OcrTask.scope_id, OcrTask.scope_type == "order"))

    if rn == ROLE_MANAGER:
        team_user_ids = select(User.id).where(_user_team_match_expr(tns))
    else:
        my_team = _require_single_team_for_strict_roles(role_name, tns)
        team_user_ids = select(User.id).where(_user_team_match_expr((my_team,)))

    stmt = stmt.where(Order.salesperson_id.in_(team_user_ids))

    if rn == ROLE_SALES:
        return stmt.where(Order.salesperson_id == int(current_user.id))

    if rn in (ROLE_MANAGER, ROLE_FINANCE):
        return stmt

    raise HTTPException(status_code=403, detail="No permission")


@router.get("/ocr-tasks", response_model=OcrTaskListOut)
async def list_order_ocr_tasks(
    limit: int = Query(50, ge=1, le=200),
    order_id: Optional[int] = Query(None),
    active_only: bool = Query(False),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    current_user, role_name, team_names, _team_ids = user_with_role
    _ensure_orders_access(role_name)

    stmt = select(OcrTask).where(OcrTask.scope_type == "order").order_by(OcrTask.id.desc())

    if order_id is not None:
        stmt = stmt.where(OcrTask.scope_id == int(order_id))
    if active_only:
        stmt = stmt.where(OcrTask.active_scope_id.isnot(None))
    if status:
        stmt = stmt.where(OcrTask.status == str(status).strip())

    stmt = await _apply_ocr_task_acl(
        db,
        current_user=current_user,
        role_name=role_name,
        team_names=_normalize_team_names(team_names),
        stmt=stmt,
    )
    stmt = stmt.limit(int(limit))

    rows = (await db.execute(stmt)).scalars().all()
    items: List[OcrTaskItemOut] = []
    for t in rows:
        items.append(
            OcrTaskItemOut(
                id=int(t.id),
                order_id=int(getattr(t, "scope_id", 0) or 0) if getattr(t, "scope_id", None) is not None else None,
                status=str(getattr(t, "status", "") or ""),
                progress=int(getattr(t, "progress", 0) or 0),
                error_message=getattr(t, "error_message", None),
            )
        )

    return OcrTaskListOut(items=items)


class BosStsOut(BaseModel):
    accessKeyId: str
    secretAccessKey: str
    sessionToken: str
    expiration: str
    bosHost: str


@router.get("/bos-sts", response_model=BosStsOut)
async def get_bos_sts(
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_orders_access(role_name)
    _ensure_orders_write_access(role_name)
    _require_team_for_non_super_admin(role_name, _normalize_team_names(team_names))

    _ = db

    if not storage.enabled:
        raise HTTPException(status_code=400, detail="BOS 未启用（BOS_ENABLED=false）")

    cred = storage.assume_role(duration_seconds=900)
    return BosStsOut(
        accessKeyId=cred.access_key_id,
        secretAccessKey=cred.secret_access_key,
        sessionToken=cred.session_token,
        expiration=cred.expiration,
        bosHost=storage.vhost,
    )


def _guess_ext(filename: str, content_type: str) -> str:
    n = (filename or "").lower()
    if n.endswith(".jpeg") or n.endswith(".jpg"):
        return ".jpg"
    if n.endswith(".png"):
        return ".png"
    if n.endswith(".webp"):
        return ".webp"
    if n.endswith(".bmp"):
        return ".bmp"
    if n.endswith(".heic"):
        return ".heic"
    ct = (content_type or "").lower()
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "png" in ct:
        return ".png"
    if "bmp" in ct:
        return ".bmp"
    if "webp" in ct:
        return ".webp"
    return ".bin"


async def _compute_md5_and_size(up: UploadFile) -> Tuple[str, int]:
    md5 = hashlib.md5()
    size = 0
    while True:
        chunk = await up.read(1024 * 1024)
        if not chunk:
            break
        md5.update(chunk)
        size += len(chunk)
    await up.seek(0)
    return md5.hexdigest(), size


class BosProxyUploadOut(BaseModel):
    slot_key: str
    md5: str
    storage_key: str
    etag: Optional[str] = None
    size: int = 0
    content_type: Optional[str] = None
    original_name: Optional[str] = None
    url: str


@router.post("/bos-upload", response_model=BosProxyUploadOut)
async def bos_upload_proxy(
    slot_key: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name, allow_finance=True)
    _require_team_for_non_super_admin(role_name, tns)
    _ = db

    if role_name == ROLE_MARKET:
        raise HTTPException(status_code=403, detail="No permission")

    if role_name == ROLE_FINANCE:
        _ensure_finance_related_only_slot(slot_key)

    if not storage.enabled:
        raise HTTPException(status_code=400, detail="BOS 未启用（BOS_ENABLED=false）")

    skey = (slot_key or "").strip()
    if skey not in ALL_SLOTS:
        raise HTTPException(status_code=400, detail=f"非法 slot_key: {slot_key}")

    if not file:
        raise HTTPException(status_code=400, detail="file 不能为空")

    md5_hex, size = await _compute_md5_and_size(file)
    content_type = (file.content_type or "application/octet-stream").strip()
    original_name = (file.filename or "file").strip()

    ext = _guess_ext(original_name, content_type)
    storage_key = storage.build_key_by_md5(scene=skey, md5_hex=md5_hex, ext=ext).lstrip("/")
    if not storage.validate_b1_key(scene=skey, storage_key=storage_key, md5_hex=md5_hex):
        raise HTTPException(status_code=400, detail="storage_key 不符合B1规则或不属于该slot")

    def _head_obj() -> Tuple[bool, str]:
        return storage.head_object(storage_key)

    def _put_obj() -> str:
        return storage.put_object(storage_key, data=file.file, content_type=content_type)

    try:
        try:
            exists, etag = await anyio.to_thread.run_sync(_head_obj)
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"BOS HEAD network error: {str(e) or e.__class__.__name__}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"BOS HEAD failed: {str(e) or e.__class__.__name__}")

        if not exists:
            try:
                etag = await anyio.to_thread.run_sync(_put_obj)
            except requests.RequestException as e:
                raise HTTPException(status_code=502, detail=f"BOS PUT network error: {str(e) or e.__class__.__name__}")
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"BOS PUT failed: {str(e) or e.__class__.__name__}")
    finally:
        pass

    url = storage.object_url_for_display(storage_key, expires_in=900)

    return BosProxyUploadOut(
        slot_key=skey,
        md5=md5_hex,
        storage_key=storage_key,
        etag=etag or None,
        size=int(size or 0),
        content_type=content_type,
        original_name=original_name,
        url=url,
    )


def _dialect_name() -> str:
    try:
        return str(getattr(engine, "dialect", None).name or "").lower()
    except Exception:
        return ""


def _json_text(col, key: str):
    d = _dialect_name()
    k = (key or "").strip()
    if not k:
        return cast("", String)

    if "postgres" in d:
        try:
            return col[k].as_string()
        except Exception:
            try:
                return col[k].astext  # type: ignore
            except Exception:
                return cast(col, String)

    if "mysql" in d or "mariadb" in d:
        try:
            return func.json_unquote(func.json_extract(col, f"$.{k}"))
        except Exception:
            return cast(func.json_extract(col, f"$.{k}"), String)

    try:
        return cast(func.json_extract(col, f"$.{k}"), String)
    except Exception:
        return cast(col, String)


def _json_text_unquoted(col, key: str):
    try:
        expr = _json_text(col, key)
        expr = func.trim(expr)
        expr = func.replace(expr, '"', "")
        return expr
    except Exception:
        return _json_text(col, key)


def _digits8_expr(expr):
    e = func.replace(expr, "-", "")
    e = func.replace(e, "/", "")
    e = func.replace(e, ".", "")
    e = func.replace(e, " ", "")
    return func.substr(e, 1, 8)


def _add_json_fuzzy_any(clauses: list, *, keys: List[str], value: Optional[str]) -> None:
    v = (value or "").strip()
    if not v:
        return
    vv = f"%{v.lower()}%"
    terms = []
    for k in (keys or []):
        kk = (k or "").strip()
        if not kk:
            continue
        expr = func.lower(func.coalesce(_json_text_unquoted(Order.dynamic_data, kk), ""))
        terms.append(expr.like(vv))
    if terms:
        clauses.append(or_(*terms))


def _parse_bj_date_range(ymd: str) -> Optional[Tuple[datetime, datetime]]:
    s = (ymd or "").strip()
    if not s:
        return None
    try:
        bj_start = datetime.strptime(s, "%Y-%m-%d")
        bj_end = bj_start + timedelta(days=1)
        return bj_start, bj_end
    except Exception:
        return None


def _parse_bj_date_span(start_ymd: str, end_ymd: str) -> Optional[Tuple[datetime, datetime]]:
    s0 = (start_ymd or "").strip()
    e0 = (end_ymd or "").strip()
    if not s0 or not e0:
        return None
    try:
        bj_start = datetime.strptime(s0, "%Y-%m-%d")
        bj_end_inclusive = datetime.strptime(e0, "%Y-%m-%d")
        if bj_end_inclusive < bj_start:
            return None
        bj_end_exclusive = bj_end_inclusive + timedelta(days=1)
        return bj_start, bj_end_exclusive
    except Exception:
        return None


def _json_date_expr_mysql(col, key: str):
    raw = func.nullif(func.trim(_json_text_unquoted(col, key)), "")
    d1 = func.str_to_date(raw, "%Y-%m-%d")
    d2 = func.str_to_date(raw, "%Y%m%d")
    return func.coalesce(d1, d2)


def _add_json_date_range_any(
    clauses: list,
    *,
    keys: List[str],
    start_ymd: Optional[str],
    end_ymd: Optional[str],
):
    s = (start_ymd or "").strip()
    e = (end_ymd or "").strip()
    if not s and not e:
        return
    if not s or not e:
        raise HTTPException(status_code=400, detail="first_register_date_start and first_register_date_end are required")

    try:
        datetime.strptime(s, "%Y-%m-%d")
        datetime.strptime(e, "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="first_register_date_* must be YYYY-MM-DD")

    if e < s:
        raise HTTPException(status_code=400, detail="first_register_date_end must be >= first_register_date_start")

    d = _dialect_name()
    if "mysql" in d or "mariadb" in d:
        s_date = datetime.strptime(s, "%Y-%m-%d").date()
        e_date = datetime.strptime(e, "%Y-%m-%d").date()
        or_terms = []
        for k in keys:
            dt_expr = _json_date_expr_mysql(Order.dynamic_data, k)
            or_terms.append(and_(dt_expr.is_not(None), dt_expr >= s_date, dt_expr <= e_date))
        if or_terms:
            clauses.append(or_(*or_terms))
        return

    s8 = s.replace("-", "")
    e8 = e.replace("-", "")
    if len(s8) != 8 or len(e8) != 8:
        raise HTTPException(status_code=400, detail="first_register_date_* must be YYYY-MM-DD")

    or_terms = []
    for k in keys:
        txt = _json_text_unquoted(Order.dynamic_data, k)
        txt8 = _digits8_expr(txt)
        or_terms.append(and_(txt8 >= s8, txt8 <= e8))

    if or_terms:
        clauses.append(or_(*or_terms))


class OrderDraftIn(BaseModel):
    module: str = "order"
    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    salesperson_id: Optional[int] = None
    order_info: Optional[OrderInfoIn] = None


class OrderDraftOut(BaseModel):
    order_id: int


@router.post("/draft", response_model=OrderDraftOut)
async def create_order_draft(
    payload: OrderDraftIn,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name)
    _ensure_orders_write_access(role_name)
    _require_team_for_non_super_admin(role_name, tns)

    if role_name == ROLE_SALES:
        spid = int(user.id)
    else:
        spid = int(payload.salesperson_id or user.id)

    await _ensure_salesperson_exists(db, spid)
    await _ensure_order_write_acl_by_salesperson_id(
        db,
        salesperson_id=spid,
        current_user=user,
        role_name=role_name,
        team_names=tns,
    )

    o = Order(
        module=payload.module or "order",
        created_by=user.id,
        salesperson_id=spid,
        customer_group_id=payload.customer_group_id,
        channel_group_id=payload.channel_group_id,
        dynamic_data=payload.dynamic_data or {},
        ocr_raw_json={},
        status=0,
        audit_status=0,
        is_finished=False,
        is_rebate=False,
        is_paid=False,
    )
    db.add(o)
    await db.flush()

    info = OrderInfo(order_id=int(o.id))
    if payload.order_info is not None:
        _apply_order_info_patch(info, payload.order_info)
    else:
        _recalc_order_info(info)
    db.add(info)

    await db.commit()
    return OrderDraftOut(order_id=o.id)


class FinalizeImageIn(BaseModel):
    slot_key: str
    storage_key: str
    md5: str = ""
    size: int = 0
    content_type: Optional[str] = None
    etag: Optional[str] = None
    original_name: Optional[str] = None
    url: Optional[str] = None


class OrderFinalizeIn(BaseModel):
    order_id: int
    images: List[FinalizeImageIn] = Field(default_factory=list)
    clear_slots: List[str] = Field(default_factory=list)

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    salesperson_id: Optional[int] = None
    order_info: Optional[OrderInfoIn] = None


class OrderFinalizeOut(BaseModel):
    ok: bool = True
    order_id: int
    ocr_task_id: Optional[int] = None
    ocr_status: Optional[str] = None


def _validate_storage_key_by_config(*, slot_key: str, storage_key: str, md5_hex: str, require_md5: bool) -> None:
    sk = str(slot_key or "").strip()
    key = str(storage_key or "").strip().lstrip("/")
    if not sk or not key:
        raise HTTPException(status_code=400, detail="slot_key/storage_key invalid")
    if sk not in ALL_SLOTS:
        raise HTTPException(status_code=400, detail=f"非法 slot_key: {sk}")

    if getattr(storage, "enabled", False) and hasattr(storage, "validate_b1_key"):
        m = str(md5_hex or "").strip().lower()
        if require_md5 and not m:
            raise HTTPException(status_code=400, detail="md5 is required")
        if m:
            try:
                ok = storage.validate_b1_key(scene=sk, storage_key=key, md5_hex=m)
            except Exception:
                ok = False
            if not ok:
                raise HTTPException(status_code=400, detail="storage_key not valid for slot/md5")
            return
        return

    prefix_map = getattr(storage, "SLOT_PREFIX_MAP", {}) or {}
    try:
        prefix = str(prefix_map.get(sk, "") or "").strip()
    except Exception:
        prefix = ""
    if not prefix:
        raise HTTPException(status_code=400, detail="unknown slot_key")
    if not key.startswith(prefix + "/"):
        raise HTTPException(status_code=400, detail="storage_key does not belong to slot_key")


@router.post("/finalize", response_model=OrderFinalizeOut)
async def finalize_order_upload(
    payload: OrderFinalizeIn,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name, allow_finance=True)
    _require_team_for_non_super_admin(role_name, tns)

    if role_name == ROLE_MARKET:
        raise HTTPException(status_code=403, detail="No permission")

    if role_name == ROLE_FINANCE:
        _ensure_finance_finalize_payload_related_only(payload)
    else:
        _ensure_orders_write_access(role_name)

    order_id = int(payload.order_id)
    o = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    await _ensure_order_write_acl_by_salesperson_id(
        db,
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        current_user=user,
        role_name=role_name,
        team_names=tns,
    )

    if role_name == ROLE_FINANCE and not bool(getattr(o, "is_finished", False)):
        raise HTTPException(status_code=400, detail="Only finished orders can be updated in finance")

    if role_name != ROLE_FINANCE:
        if payload.salesperson_id is not None:
            spid = int(payload.salesperson_id)
            await _ensure_salesperson_exists(db, spid)
            await _ensure_order_write_acl_by_salesperson_id(
                db,
                salesperson_id=spid,
                current_user=user,
                role_name=role_name,
                team_names=tns,
            )
            o.salesperson_id = spid
        if payload.customer_group_id is not None:
            o.customer_group_id = int(payload.customer_group_id)
        if payload.channel_group_id is not None:
            o.channel_group_id = int(payload.channel_group_id)
        if payload.dynamic_data:
            o.dynamic_data = {**(o.dynamic_data or {}), **(payload.dynamic_data or {})}

    _ensure_required_customer_channel(customer_group_id=o.customer_group_id, channel_group_id=o.channel_group_id)

    clear_slots = [str(x or "").strip() for x in (payload.clear_slots or [])]
    clear_slots = [x for x in clear_slots if x]
    for sk in clear_slots:
        if sk not in ALL_SLOTS:
            raise HTTPException(status_code=400, detail=f"非法 clear_slots slot_key: {sk}")
        if sk not in MULTI_SLOTS:
            raise HTTPException(status_code=400, detail=f"暂不支持清空该slot: {sk}")

    by_slot: Dict[str, List[FinalizeImageIn]] = {}
    for im in payload.images or []:
        sk = (im.slot_key or "").strip()
        if sk not in ALL_SLOTS:
            raise HTTPException(status_code=400, detail=f"非法 slot_key: {sk}")
        by_slot.setdefault(sk, []).append(im)

    normalized_images: List[FinalizeImageIn] = []
    for sk, ims in by_slot.items():
        if sk in MULTI_SLOTS:
            normalized_images.extend(ims)
        else:
            normalized_images.append(ims[-1])

    touched_slots = set(by_slot.keys()) | set(clear_slots)
    for sk in touched_slots:
        desired_sks: List[str] = []
        if sk in by_slot:
            for im in by_slot.get(sk, []) or []:
                storage_key = (im.storage_key or "").strip().lstrip("/")
                if storage_key:
                    desired_sks.append(storage_key)
            if sk not in MULTI_SLOTS and desired_sks:
                desired_sks = [desired_sks[-1]]

        del_stmt = delete(OrderImage).where(and_(OrderImage.order_id == order_id, OrderImage.slot_key == sk))
        if desired_sks:
            del_stmt = del_stmt.where(~OrderImage.storage_key.in_(desired_sks))
        await db.execute(del_stmt)

    has_ocr_images = False
    for im in normalized_images:
        slot_key = (im.slot_key or "").strip()
        storage_key = (im.storage_key or "").strip().lstrip("/")
        if not storage_key:
            raise HTTPException(status_code=400, detail="storage_key 不能为空")

        _validate_storage_key_by_config(slot_key=slot_key, storage_key=storage_key, md5_hex=(im.md5 or "").strip(), require_md5=True)
        has_ocr_images = has_ocr_images or (slot_key in OCR_SLOTS)

        url = (im.url or "").strip()
        if not url and getattr(storage, "enabled", False):
            try:
                url = storage.object_public_url(storage_key)
            except Exception:
                url = ""

        imf = await _get_or_create_image_file(
            db,
            storage_key=storage_key,
            url=url,
            size=int(im.size or 0),
            original_name=im.original_name,
            content_type=im.content_type,
            etag=im.etag,
            md5=(im.md5 or "").strip(),
        )

        exists_stmt = select(OrderImage.id).where(
            and_(OrderImage.order_id == order_id, OrderImage.slot_key == slot_key, OrderImage.storage_key == storage_key)
        )
        exists_id = (await db.execute(exists_stmt)).scalar_one_or_none()
        if exists_id:
            continue

        db.add(
            OrderImage(
                order_id=order_id,
                slot_key=slot_key,
                storage_key=storage_key,
                image_url=url or "",
                image_file_id=imf.id,
            )
        )

    info = (await db.execute(select(OrderInfo).where(OrderInfo.order_id == order_id))).scalar_one_or_none()
    if not info:
        info = OrderInfo(order_id=order_id)
        db.add(info)

    if role_name != ROLE_FINANCE and payload.order_info is not None:
        _apply_order_info_patch(info, payload.order_info)
    else:
        _recalc_order_info(info)

    ocr_task_id: Optional[int] = None
    ocr_status: Optional[str] = None
    if has_ocr_images:
        try:
            async with db.begin_nested():
                task = OcrTask(
                    scope_type="order",
                    scope_id=order_id,
                    active_scope_id=order_id,
                    status="pending",
                    progress=0,
                )
                db.add(task)
                await db.flush()
                ocr_task_id = int(task.id)
                ocr_status = str(task.status)
        except IntegrityError:
            exist_stmt = (
                select(OcrTask)
                .where(and_(OcrTask.scope_type == "order", OcrTask.active_scope_id == order_id))
                .order_by(OcrTask.id.desc())
            )
            exist_task = (await db.execute(exist_stmt)).scalars().first()
            if exist_task:
                ocr_task_id = int(exist_task.id)
                ocr_status = str(exist_task.status)

    await db.commit()
    return OrderFinalizeOut(ok=True, order_id=order_id, ocr_task_id=ocr_task_id, ocr_status=ocr_status)


# ==========================
# ✅ 报价助手：上传后绑定 slot -> 写 OrderImage，并可触发 order OCR 任务
# ==========================
class AiBindImagesIn(BaseModel):
    images: List[FinalizeImageIn] = Field(default_factory=list)
    clear_slots: List[str] = Field(default_factory=list)
    trigger_ocr: bool = True


class AiBindImagesOut(BaseModel):
    ok: bool = True
    order_id: int
    bound_count: int = 0
    ocr_task_id: Optional[int] = None
    ocr_status: Optional[str] = None


@router.post("/{order_id:int}/images/bind", response_model=AiBindImagesOut)
async def ai_bind_order_images(
    order_id: int,
    payload: AiBindImagesIn,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name, allow_finance=True)
    _require_team_for_non_super_admin(role_name, tns)

    if role_name == ROLE_MARKET:
        raise HTTPException(status_code=403, detail="No permission")

    if role_name == ROLE_FINANCE:
        for im in payload.images or []:
            _ensure_finance_related_only_slot(getattr(im, "slot_key", ""))
        for sk in payload.clear_slots or []:
            _ensure_finance_related_only_slot(sk)
    else:
        _ensure_orders_write_access(role_name)

    o = (await db.execute(select(Order).where(Order.id == int(order_id)))).scalar_one_or_none()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    await _ensure_order_write_acl_by_salesperson_id(
        db,
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        current_user=user,
        role_name=role_name,
        team_names=tns,
    )

    if role_name == ROLE_FINANCE and not bool(getattr(o, "is_finished", False)):
        raise HTTPException(status_code=400, detail="Only finished orders can be updated in finance")

    clear_slots = [str(x or "").strip() for x in (payload.clear_slots or [])]
    clear_slots = [x for x in clear_slots if x]
    for sk in clear_slots:
        if sk not in ALL_SLOTS:
            raise HTTPException(status_code=400, detail=f"非法 clear_slots slot_key: {sk}")
        if sk not in MULTI_SLOTS:
            raise HTTPException(status_code=400, detail=f"暂不支持清空该slot: {sk}")

    by_slot: Dict[str, List[FinalizeImageIn]] = {}
    for im in payload.images or []:
        sk = (im.slot_key or "").strip()
        if sk not in ALL_SLOTS:
            raise HTTPException(status_code=400, detail=f"非法 slot_key: {sk}")
        storage_key = (im.storage_key or "").strip().lstrip("/")
        if not storage_key:
            raise HTTPException(status_code=400, detail="storage_key 不能为空")
        by_slot.setdefault(sk, []).append(im)

    normalized_images: List[FinalizeImageIn] = []
    for sk, ims in by_slot.items():
        if sk in MULTI_SLOTS:
            normalized_images.extend(ims)
        else:
            normalized_images.append(ims[-1])

    touched_slots = set(by_slot.keys()) | set(clear_slots)
    for sk in touched_slots:
        if sk in MULTI_SLOTS:
            if sk in clear_slots:
                await db.execute(delete(OrderImage).where(and_(OrderImage.order_id == int(order_id), OrderImage.slot_key == sk)))
        else:
            await db.execute(delete(OrderImage).where(and_(OrderImage.order_id == int(order_id), OrderImage.slot_key == sk)))

    bound_count = 0
    has_ocr_images = False

    for im in normalized_images:
        slot_key = (im.slot_key or "").strip()
        storage_key = (im.storage_key or "").strip().lstrip("/")
        md5_hex = (im.md5 or "").strip().lower()

        _validate_storage_key_by_config(slot_key=slot_key, storage_key=storage_key, md5_hex=md5_hex, require_md5=getattr(storage, "enabled", False))
        has_ocr_images = has_ocr_images or (slot_key in OCR_SLOTS)

        url = (im.url or "").strip()
        if not url and getattr(storage, "enabled", False):
            try:
                url = storage.object_public_url(storage_key)
            except Exception:
                url = ""

        imf = await _get_or_create_image_file(
            db,
            storage_key=storage_key,
            url=url,
            size=int(im.size or 0),
            original_name=im.original_name,
            content_type=im.content_type,
            etag=im.etag,
            md5=md5_hex,
        )

        exists_stmt = select(OrderImage.id).where(and_(OrderImage.order_id == int(order_id), OrderImage.slot_key == slot_key, OrderImage.storage_key == storage_key))
        exists_id = (await db.execute(exists_stmt)).scalar_one_or_none()
        if exists_id:
            continue

        db.add(OrderImage(order_id=int(order_id), slot_key=slot_key, storage_key=storage_key, image_url=url or "", image_file_id=imf.id))
        bound_count += 1

    ocr_task_id: Optional[int] = None
    ocr_status: Optional[str] = None
    if bool(getattr(payload, "trigger_ocr", True)) and has_ocr_images:
        try:
            async with db.begin_nested():
                task = OcrTask(scope_type="order", scope_id=int(order_id), active_scope_id=int(order_id), status="pending", progress=0)
                db.add(task)
                await db.flush()
                ocr_task_id = int(task.id)
                ocr_status = str(task.status)
        except IntegrityError:
            exist_stmt = select(OcrTask).where(and_(OcrTask.scope_type == "order", OcrTask.active_scope_id == int(order_id))).order_by(OcrTask.id.desc())
            exist_task = (await db.execute(exist_stmt)).scalars().first()
            if exist_task:
                ocr_task_id = int(exist_task.id)
                ocr_status = str(exist_task.status)

    await db.commit()
    return AiBindImagesOut(ok=True, order_id=int(order_id), bound_count=int(bound_count), ocr_task_id=ocr_task_id, ocr_status=ocr_status)


@router.get("", response_model=OrderListResponse)
async def list_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    is_finished: Optional[bool] = Query(None),
    salesperson_id: Optional[int] = Query(None),
    created_by: Optional[int] = Query(None),
    customer_group_id: Optional[int] = Query(None),
    channel_group_id: Optional[int] = Query(None),
    team_name: Optional[str] = Query(None, description="可选：按团队筛选（仅能筛选自己可见的团队）"),
    created_date: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 单日，兼容历史）"),
    created_date_start: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 起）"),
    created_date_end: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 止，包含当天）"),
    first_register_date: Optional[str] = Query(None, description="YYYY-MM-DD（初登日期单日，兼容旧参数）"),
    first_register_date_start: Optional[str] = Query(None, description="YYYY-MM-DD（初登日期起，包含）"),
    first_register_date_end: Optional[str] = Query(None, description="YYYY-MM-DD（初登日期止，包含）"),
    owner_name: Optional[str] = Query(None, description="车主姓名（身份证姓名 id_name）"),
    id_number: Optional[str] = Query(None, description="身份证号（id_number / dl_id_number）"),
    plate_no: Optional[str] = Query(None, description="车牌号（dl_plate_no / plate_no）"),
    engine_no: Optional[str] = Query(None, description="发动机号（engine_no / dl_engine_no）"),
    vehicle_name: Optional[str] = Query(None, description="车辆名称（vehicle_brand_name / vehicle_name）"),
    vehicle_model: Optional[str] = Query(None, description="车辆型号（vehicle_model / dl_vehicle_model）"),
    vin: Optional[str] = Query(None, description="车架号（vin / dl_vin）"),
    remark: Optional[str] = Query(None, description="订单备注（order_info.remark）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    current_user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name)
    _require_team_for_non_super_admin(role_name, tns)

    tf = str(team_name or "").strip()
    if tf:
        _require_team_filter_allowed(role_name=role_name, team_names=tns, team_filter=tf)

    stmt = (
        select(Order)
        .options(*_rm_preload_options())
    )
    count_stmt = select(func.count(Order.id))

    clauses: list = []
    await _apply_orders_list_acl(db, current_user=current_user, role_name=role_name, team_names=tns, clauses=clauses)

    if tf:
        team_user_ids = select(User.id).where(_user_team_match_expr((tf,)))
        clauses.append(Order.salesperson_id.in_(team_user_ids))

    if is_finished is not None:
        clauses.append(Order.is_finished.is_(is_finished))

    if salesperson_id is not None:
        clauses.append(Order.salesperson_id == int(salesperson_id))
    if created_by is not None:
        clauses.append(Order.created_by == int(created_by))
    if customer_group_id is not None:
        clauses.append(Order.customer_group_id == int(customer_group_id))
    if channel_group_id is not None:
        clauses.append(Order.channel_group_id == int(channel_group_id))

    if created_date_start or created_date_end:
        if not created_date_start or not created_date_end:
            raise HTTPException(status_code=400, detail="created_date_start and created_date_end are required")
        rng = _parse_bj_date_span(created_date_start, created_date_end)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date_* must be YYYY-MM-DD and end>=start")
        start_bj, end_bj = rng
        clauses.append(and_(Order.created_at >= start_bj, Order.created_at < end_bj))
    elif created_date:
        rng = _parse_bj_date_range(created_date)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date must be YYYY-MM-DD")
        start_bj, end_bj = rng
        clauses.append(and_(Order.created_at >= start_bj, Order.created_at < end_bj))

    if first_register_date_start or first_register_date_end:
        _add_json_date_range_any(
            clauses,
            keys=["dl_first_register_date", "dl_register_date", "register_date", "first_register_date"],
            start_ymd=first_register_date_start,
            end_ymd=first_register_date_end,
        )
    elif (first_register_date or "").strip():
        v = (first_register_date or "").strip()
        v2 = v.replace("-", "")
        _add_json_fuzzy_any(clauses, keys=["dl_first_register_date", "dl_register_date", "register_date", "first_register_date"], value=v)
        _add_json_fuzzy_any(clauses, keys=["dl_first_register_date", "dl_register_date", "register_date", "first_register_date"], value=v2)

    _add_json_fuzzy_any(clauses, keys=["id_name", "dl_owner", "owner_name"], value=owner_name)
    _add_json_fuzzy_any(clauses, keys=["id_number", "dl_id_number"], value=id_number)
    _add_json_fuzzy_any(clauses, keys=["vehicle_model", "dl_brand_model", "brand_model", "dl_vehicle_model"], value=vehicle_model)
    _add_json_fuzzy_any(clauses, keys=["dl_plate_no", "plate_no"], value=plate_no)
    _add_json_fuzzy_any(clauses, keys=["engine_no", "dl_engine_no"], value=engine_no)
    _add_json_fuzzy_any(clauses, keys=["vin", "dl_vin"], value=vin)
    _add_json_fuzzy_any(clauses, keys=["vehicle_brand_name", "vehicle_name"], value=vehicle_name)

    rmk = (remark or "").strip()
    if rmk:
        if not hasattr(OrderInfo, "remark"):
            raise HTTPException(status_code=500, detail="OrderInfo.remark column not found")
        stmt = stmt.join(OrderInfo, OrderInfo.order_id == Order.id)
        count_stmt = count_stmt.select_from(Order).join(OrderInfo, OrderInfo.order_id == Order.id)
        expr = func.lower(func.coalesce(cast(getattr(OrderInfo, "remark"), String), ""))
        clauses.append(expr.like(f"%{rmk.lower()}%"))

    if clauses:
        stmt = stmt.where(and_(*clauses))
        count_stmt = count_stmt.where(and_(*clauses))

    stmt = stmt.order_by(Order.id.desc()).offset((page - 1) * page_size).limit(page_size)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt)).scalars().all()

    items: List[OrderOut] = await _rm_orders_to_out_list(db, rows, storage=storage)

    return OrderListResponse(meta=_build_list_meta(role_name), total=total, items=items)


@router.get("/{order_id:int}", response_model=Dict[str, Any])
async def get_order_detail(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name)
    _require_team_for_non_super_admin(role_name, tns)

    return await load_order_detail_blocks(
        db,
        int(order_id),
        current_user=user,
        role_name=role_name,
        team_names=tns,
        storage=storage,
        enforce_read_acl=True,
    )


@router.post("", response_model=OrderOut)
async def create_order(
    payload: OrderCreate,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name)
    _ensure_orders_write_access(role_name)
    _require_team_for_non_super_admin(role_name, tns)

    _ensure_required_customer_channel(customer_group_id=payload.customer_group_id, channel_group_id=payload.channel_group_id)

    if role_name == ROLE_SALES:
        spid = int(user.id)
    else:
        spid = int(payload.salesperson_id or user.id)

    await _ensure_salesperson_exists(db, spid)
    await _ensure_order_write_acl_by_salesperson_id(db, salesperson_id=spid, current_user=user, role_name=role_name, team_names=tns)

    o = Order(
        module=payload.module or "order",
        created_by=user.id,
        salesperson_id=spid,
        customer_group_id=payload.customer_group_id,
        channel_group_id=payload.channel_group_id,
        dynamic_data=payload.dynamic_data or {},
        ocr_raw_json=payload.ocr_raw_json or {},
        status=payload.status or 0,
        audit_status=payload.audit_status or 0,
        is_finished=bool(payload.is_finished),
        is_rebate=False,
        is_paid=False,
    )
    db.add(o)
    await db.flush()

    info = OrderInfo(order_id=int(o.id))
    if payload.order_info is not None:
        _apply_order_info_patch(info, payload.order_info)
    else:
        _recalc_order_info(info)
    db.add(info)

    await db.commit()
    return await _load_order_out(db, o.id, current_user=user, role_name=role_name, team_names=tns)


@router.put("/{order_id:int}", response_model=OrderOut)
async def update_order_detail(
    order_id: int,
    payload: OrderUpdate,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name)
    _ensure_orders_write_access(role_name)
    _require_team_for_non_super_admin(role_name, tns)

    o = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    await _ensure_order_write_acl_by_salesperson_id(
        db,
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        current_user=user,
        role_name=role_name,
        team_names=tns,
    )

    if o.is_finished and role_name not in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        raise HTTPException(status_code=403, detail="Finished order cannot be edited")

    if payload.salesperson_id is not None:
        spid = int(payload.salesperson_id)
        await _ensure_salesperson_exists(db, spid)
        await _ensure_order_write_acl_by_salesperson_id(db, salesperson_id=spid, current_user=user, role_name=role_name, team_names=tns)
        o.salesperson_id = spid
    if payload.customer_group_id is not None:
        o.customer_group_id = payload.customer_group_id
    if payload.channel_group_id is not None:
        o.channel_group_id = payload.channel_group_id
    if payload.dynamic_data is not None:
        o.dynamic_data = {**(o.dynamic_data or {}), **(payload.dynamic_data or {})}

    if payload.order_info is not None:
        info = (await db.execute(select(OrderInfo).where(OrderInfo.order_id == int(order_id)))).scalar_one_or_none()
        if not info:
            info = OrderInfo(order_id=int(order_id))
            db.add(info)
        _apply_order_info_patch(info, payload.order_info)

    await db.commit()
    return await _load_order_out(db, order_id, current_user=user, role_name=role_name, team_names=tns)


@router.patch("/{order_id:int}/status")
async def update_order_status(
    order_id: int,
    payload: OrderStatusUpdate,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name)
    _ensure_orders_write_access(role_name)
    _require_team_for_non_super_admin(role_name, tns)

    o = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    await _ensure_order_write_acl_by_salesperson_id(
        db,
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        current_user=user,
        role_name=role_name,
        team_names=tns,
    )

    if payload.is_finished is not None:
        if o.is_finished and payload.is_finished is False and role_name not in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
            raise HTTPException(status_code=403, detail="Only manager/super_admin can reopen finished order")

        if bool(payload.is_finished) is True:
            _ensure_required_customer_channel(customer_group_id=o.customer_group_id, channel_group_id=o.channel_group_id)

        o.is_finished = bool(payload.is_finished)

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
_ensure_user_in_teams = _ac_ensure_user_in_teams
_ensure_order_read_acl_by_salesperson_id = _ac_ensure_order_read_acl_by_salesperson_id
_ensure_order_write_acl_by_salesperson_id = _ac_ensure_order_write_acl_by_salesperson_id
_apply_orders_list_acl = _ac_apply_orders_list_acl
