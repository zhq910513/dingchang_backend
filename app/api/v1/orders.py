# encoding: utf-8
from __future__ import annotations

import anyio
import hashlib
import inspect
import re
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy import func, select, and_, or_, cast, String, distinct, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from typing import Optional, Any, Dict, List, Tuple, Set

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
from app.core.db import get_db, engine
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
)
from app.services.ocr_cleaner import clean_dynamic_data_for_ocr
from app.services.order_owner_name import append_owner_name_fuzzy_clause as _append_owner_name_fuzzy_clause
from app.services.order_read_model import (
    to_order_out as _rm_to_order_out,
    orders_to_list_items as _rm_orders_to_list_items,
)
from app.services.storage import StorageService

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
MULTI_SLOTS = {"related"}

ORDER_INFO_NON_NULL_NUMERIC_FIELDS: List[str] = [
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
]


class OptionItem(BaseModel):
    id: int
    group_name: str
    customer_code: Optional[str] = None
    customer_name: Optional[str] = None
    channel_code: Optional[str] = None
    channel_name: Optional[str] = None


class OptionListOut(BaseModel):
    items: List[OptionItem] = Field(default_factory=list)


class TeamItem(BaseModel):
    team_name: str


class TeamListOut(BaseModel):
    items: List[TeamItem] = Field(default_factory=list)


class SalespersonItem(BaseModel):
    id: int
    username: str
    real_name: Optional[str] = None


class SalespersonListOut(BaseModel):
    items: List[SalespersonItem] = Field(default_factory=list)


class OrderDraftIn(BaseModel):
    module: str = "order"
    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    salesperson_id: Optional[int] = None
    order_info: Optional[OrderInfoIn] = None


class OrderDraftOut(BaseModel):
    order_id: int


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


class OcrTaskItemOut(BaseModel):
    id: int
    order_id: Optional[int] = None
    status: str
    progress: int = 0
    error_message: Optional[str] = None


class OcrTaskListOut(BaseModel):
    items: List[OcrTaskItemOut] = Field(default_factory=list)


class BosStsOut(BaseModel):
    accessKeyId: str
    secretAccessKey: str
    sessionToken: str
    expiration: str
    bosHost: str


class BosProxyUploadOut(BaseModel):
    slot_key: str
    md5: str
    storage_key: str
    etag: Optional[str] = None
    size: int = 0
    content_type: Optional[str] = None
    original_name: Optional[str] = None
    url: str


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


async def _maybe_await(v):
    if inspect.isawaitable(v):
        return await v
    return v


def _model_fields_set(m: Any) -> Set[str]:
    fs = getattr(m, "model_fields_set", None)
    if isinstance(fs, set):
        return {str(x) for x in fs}
    fs2 = getattr(m, "__fields_set__", None)
    if isinstance(fs2, set):
        return {str(x) for x in fs2}
    return set()


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


async def _ensure_salesperson_exists(db: AsyncSession, salesperson_id: int) -> None:
    try:
        sid = int(salesperson_id)
    except Exception:
        raise HTTPException(status_code=400, detail="salesperson_id 非法")
    q = select(User.id).where(User.id == sid)
    u = (await db.execute(q)).scalar_one_or_none()
    if u is None:
        raise HTTPException(status_code=400, detail="salesperson_id 不存在")


async def _ensure_customer_group_exists(db: AsyncSession, customer_group_id: int) -> None:
    try:
        gid = int(customer_group_id)
    except Exception:
        raise HTTPException(status_code=400, detail="customer_group_id 非法")
    q = select(CustomerGroup.id).where(CustomerGroup.id == gid)
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=400, detail="customer_group_id 不存在")


async def _ensure_channel_group_exists(db: AsyncSession, channel_group_id: int) -> None:
    from app.models.channel_group import ChannelGroup

    try:
        gid = int(channel_group_id)
    except Exception:
        raise HTTPException(status_code=400, detail="channel_group_id 非法")
    q = select(ChannelGroup.id).where(ChannelGroup.id == gid)
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=400, detail="channel_group_id 不存在")


def _ensure_orders_access(role_name: Optional[str]) -> None:
    rn = role_name or ""
    if rn not in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_FINANCE, ROLE_MARKET, ROLE_SALES):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_orders_write_access(role_name: Optional[str]) -> None:
    if role_name in (ROLE_FINANCE, ROLE_MARKET):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_finance_related_only_slot(slot_key: str) -> None:
    if str(slot_key or "").strip() != "related":
        raise HTTPException(status_code=403, detail="Finance can only operate related images")


def _ensure_finance_finalize_payload_related_only(payload: OrderFinalizeIn) -> None:
    if payload.salesperson_id is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update salesperson_id in orders.finalize")
    if payload.customer_group_id is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update customer_group_id in orders.finalize")
    if payload.channel_group_id is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update channel_group_id in orders.finalize")
    if payload.order_info is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update order_info in orders.finalize")

    dyn = payload.dynamic_data or {}
    if isinstance(dyn, dict) and len(dyn) > 0:
        raise HTTPException(status_code=403, detail="Finance cannot update dynamic_data in orders.finalize")

    for sk in payload.clear_slots or []:
        if str(sk or "").strip() != "related":
            raise HTTPException(status_code=403, detail="Finance can only clear related slot")

    for im in payload.images or []:
        if str(im.slot_key or "").strip() != "related":
            raise HTTPException(status_code=403, detail="Finance can only finalize related images")


def _ensure_required_customer_channel(*, customer_group_id: Optional[int], channel_group_id: Optional[int]) -> None:
    if customer_group_id is None:
        raise HTTPException(status_code=400, detail="customer_group_id is required")
    if channel_group_id is None:
        raise HTTPException(status_code=400, detail="channel_group_id is required")


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


def _add_json_fuzzy(clauses: list, key: str, value: Optional[str]):
    v = (value or "").strip()
    if not v:
        return
    expr = func.lower(_json_text_unquoted(Order.dynamic_data, key))
    clauses.append(expr.like(f"%{v.lower()}%"))


def _add_owner_name_fuzzy(clauses: list, value: Optional[str]):
    def _flat_text_getter(key: str):
        return _json_text_unquoted(Order.dynamic_data, key)

    def _path_text_getter(path: str):
        json_path = f"$.{path}"
        dialect_name = _dialect_name()
        if "mysql" in dialect_name or "mariadb" in dialect_name:
            return func.json_unquote(func.json_extract(Order.dynamic_data, json_path))
        return cast(func.json_extract(Order.dynamic_data, json_path), String)

    _append_owner_name_fuzzy_clause(
        clauses,
        value=value,
        flat_text_getter=_flat_text_getter,
        path_text_getter=_path_text_getter,
    )


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


def _parse_ymd(ymd: str):
    s = (ymd or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _parse_date_or_none(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()

    s = str(v).strip()
    if not s or s in ("-", "—", "null", "none", "None"):
        return None

    s2 = s.replace("/", "-").replace(".", "-")
    s2 = re.sub(r"\s+", "", s2)

    if len(s2) >= 10 and s2[4] == "-" and s2[7] == "-":
        try:
            return datetime.strptime(s2[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    if len(s2) == 8 and s2.isdigit():
        try:
            return datetime.strptime(s2, "%Y%m%d").date()
        except Exception:
            return None

    if len(s2) == 7 and s2[4] == "-":
        return None

    return None


def _float_or_zero(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except Exception:
            raise HTTPException(status_code=400, detail="order_info numeric field invalid")
    try:
        return float(v)
    except Exception:
        raise HTTPException(status_code=400, detail="order_info numeric field invalid")


def _trim_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _num_or_zero(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


def _recalc_order_info_derived(info: OrderInfo) -> None:
    commercial = max(0.0, _num_or_zero(getattr(info, "commercial_amount", 0.0)))
    compulsory = max(0.0, _num_or_zero(getattr(info, "compulsory_amount", 0.0)))
    vehicle_tax = max(0.0, _num_or_zero(getattr(info, "vehicle_tax_amount", 0.0)))
    non_vehicle = max(0.0, _num_or_zero(getattr(info, "non_vehicle_amount", 0.0)))

    info.commercial_amount = commercial
    info.compulsory_amount = compulsory
    info.vehicle_tax_amount = vehicle_tax
    info.non_vehicle_amount = non_vehicle

    premium_total = commercial + compulsory + vehicle_tax + non_vehicle
    info.premium_total = premium_total

    ch_commercial_point = _num_or_zero(getattr(info, "channel_commercial_point", 0.0))
    ch_commercial_supplement_point = _num_or_zero(getattr(info, "channel_commercial_supplement_point", 0.0))
    ch_compulsory_point = _num_or_zero(getattr(info, "channel_compulsory_point", 0.0))
    ch_vehicle_tax_point = _num_or_zero(getattr(info, "channel_vehicle_tax_point", 0.0))
    ch_non_vehicle_point = _num_or_zero(getattr(info, "channel_non_vehicle_point", 0.0))
    ch_reward = _num_or_zero(getattr(info, "channel_reward", 0.0))

    cu_commercial_point = _num_or_zero(getattr(info, "customer_commercial_point", 0.0))
    cu_commercial_supplement_point = _num_or_zero(getattr(info, "customer_commercial_supplement_point", 0.0))
    cu_compulsory_point = _num_or_zero(getattr(info, "customer_compulsory_point", 0.0))
    cu_vehicle_tax_point = _num_or_zero(getattr(info, "customer_vehicle_tax_point", 0.0))
    cu_non_vehicle_point = _num_or_zero(getattr(info, "customer_non_vehicle_point", 0.0))
    cu_reward = _num_or_zero(getattr(info, "customer_reward", 0.0))

    info.channel_commercial_point = ch_commercial_point
    info.channel_commercial_supplement_point = ch_commercial_supplement_point
    info.channel_compulsory_point = ch_compulsory_point
    info.channel_vehicle_tax_point = ch_vehicle_tax_point
    info.channel_non_vehicle_point = ch_non_vehicle_point
    info.channel_reward = ch_reward

    info.customer_commercial_point = cu_commercial_point
    info.customer_commercial_supplement_point = cu_commercial_supplement_point
    info.customer_compulsory_point = cu_compulsory_point
    info.customer_vehicle_tax_point = cu_vehicle_tax_point
    info.customer_non_vehicle_point = cu_non_vehicle_point
    info.customer_reward = cu_reward

    channel_total = (
        commercial * (ch_commercial_point / 100.0)
        + commercial * (ch_commercial_supplement_point / 100.0)
        + compulsory * (ch_compulsory_point / 100.0)
        + vehicle_tax * (ch_vehicle_tax_point / 100.0)
        + non_vehicle * (ch_non_vehicle_point / 100.0)
        + ch_reward
    )
    customer_total = (
        commercial * (cu_commercial_point / 100.0)
        + commercial * (cu_commercial_supplement_point / 100.0)
        + compulsory * (cu_compulsory_point / 100.0)
        + vehicle_tax * (cu_vehicle_tax_point / 100.0)
        + non_vehicle * (cu_non_vehicle_point / 100.0)
        + cu_reward
    )

    info.channel_total = channel_total
    info.customer_total = customer_total
    info.profit = channel_total - customer_total


def _apply_order_info_patch(info: OrderInfo, payload: OrderInfoIn) -> None:
    if payload is None:
        return

    fs = _model_fields_set(payload)

    for name in ("owner_phone", "remark"):
        if name in fs:
            setattr(info, name, _trim_or_none(getattr(payload, name, None)))

    if "insurance_expire_date" in fs:
        info.insurance_expire_date = _parse_date_or_none(getattr(payload, "insurance_expire_date", None))

    for name in ORDER_INFO_NON_NULL_NUMERIC_FIELDS:
        if name in fs:
            setattr(info, name, _float_or_zero(getattr(payload, name, None)))

    _recalc_order_info_derived(info)


def _add_json_date_range_any(
    clauses: list,
    *,
    keys: List[str],
    start_ymd: Optional[str],
    end_ymd: Optional[str],
    err_prefix: str,
):
    s = (start_ymd or "").strip()
    e = (end_ymd or "").strip()
    if not s and not e:
        return
    if not s or not e:
        raise HTTPException(status_code=400, detail=f"{err_prefix}_start and {err_prefix}_end are required")

    try:
        datetime.strptime(s, "%Y-%m-%d")
        datetime.strptime(e, "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail=f"{err_prefix}_* must be YYYY-MM-DD")

    if e < s:
        raise HTTPException(status_code=400, detail=f"{err_prefix}_end must be >= {err_prefix}_start")

    s8 = s.replace("-", "")
    e8 = e.replace("-", "")
    or_terms = []
    for k in keys:
        txt = _json_text_unquoted(Order.dynamic_data, k)
        txt8 = _digits8_expr(txt)
        or_terms.append(and_(txt8 >= s8, txt8 <= e8))

    if or_terms:
        clauses.append(or_(*or_terms))


def _clean_dynamic_data_for_write(dyn: Any) -> Dict[str, Any]:
    if not isinstance(dyn, dict):
        return {}
    return clean_dynamic_data_for_ocr(dict(dyn))


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
        original_name=original_name,
        content_type=content_type,
        storage_key=storage_key,
        url=url or "",
        etag=etag,
        size=int(size or 0),
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


def _validate_finalize_storage_key(*, slot_key: str, storage_key: str, md5_hex: str) -> None:
    sk = str(slot_key or "").strip()
    key = str(storage_key or "").strip().lstrip("/")
    m = str(md5_hex or "").strip().lower()

    if sk not in ALL_SLOTS:
        raise HTTPException(status_code=400, detail=f"非法 slot_key: {sk}")
    if not key:
        raise HTTPException(status_code=400, detail="storage_key 不能为空")
    if not m:
        raise HTTPException(status_code=400, detail="md5 is required for finalize")

    try:
        ok = storage.validate_b1_key(scene=sk, storage_key=key, md5_hex=m)
    except Exception:
        ok = False

    if not ok:
        raise HTTPException(status_code=400, detail="storage_key not valid for slot/md5")


async def _apply_ocr_task_acl(
    *,
    stmt,
    current_user: User,
    role_name: Optional[str],
    team_names: Tuple[str, ...],
):
    rn = role_name or ""
    tns = _ac_normalize_team_names(team_names)

    if rn == ROLE_MARKET:
        raise HTTPException(status_code=403, detail="No permission")

    if rn == ROLE_SUPER_ADMIN:
        return stmt

    _ac_require_team_for_non_super_admin(rn, tns)

    stmt = stmt.join(Order, and_(Order.id == OcrTask.scope_id, OcrTask.scope_type == "order"))

    if rn == ROLE_MANAGER:
        stmt = stmt.where(_ac_order_salesperson_in_teams_expr(tns))
        return stmt

    my_team = _ac_require_single_team_for_strict_roles(rn, tns)
    stmt = stmt.where(_ac_order_salesperson_in_teams_expr((my_team,)))

    if rn == ROLE_SALES:
        stmt = stmt.where(Order.salesperson_id == int(current_user.id))

    return stmt


def _build_order_detail_stmt(order_id: int):
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

    if hasattr(Order, "customer_group"):
        stmt = stmt.options(selectinload(Order.customer_group))
    if hasattr(Order, "channel_group"):
        stmt = stmt.options(selectinload(Order.channel_group))

    return stmt


async def _load_order_out(
    db: AsyncSession,
    order_id: int,
    *,
    current_user: User,
    role_name: Optional[str],
    team_names: Tuple[str, ...],
) -> OrderOut:
    stmt = _build_order_detail_stmt(order_id)
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


async def _build_order_list_clauses(
    *,
    ctx: CurrentUserContext,
    role_name: str,
    team_names: Tuple[str, ...],
    is_finished: Optional[bool],
    salesperson_id: Optional[int],
    created_by: Optional[int],
    customer_group_id: Optional[int],
    channel_group_id: Optional[int],
    team_name: Optional[str],
    created_date: Optional[str],
    created_date_start: Optional[str],
    created_date_end: Optional[str],
    first_register_date_start: Optional[str],
    first_register_date_end: Optional[str],
    owner_name: Optional[str],
    id_number: Optional[str],
    plate_no: Optional[str],
    engine_no: Optional[str],
    vehicle_model: Optional[str],
    vin: Optional[str],
    is_paid: Optional[bool],
    is_rebate: Optional[bool],
) -> List[Any]:
    clauses: List[Any] = []

    tf = str(team_name or "").strip()
    if tf:
        _ac_require_team_filter_allowed(role_name=role_name, team_names=team_names, team_filter=tf)

    await _ac_apply_orders_list_acl(
        current_user=ctx.user,
        role_name=role_name,
        team_names=team_names,
        clauses=clauses,
    )

    if tf:
        clauses.append(_ac_order_salesperson_in_teams_expr((tf,)))

    if is_finished is not None:
        clauses.append(Order.is_finished == bool(is_finished))
    if is_paid is not None:
        clauses.append(Order.is_paid == bool(is_paid))
    if is_rebate is not None:
        clauses.append(Order.is_rebate == bool(is_rebate))
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

    _add_json_date_range_any(
        clauses,
        keys=["first_register_date"],
        start_ymd=first_register_date_start,
        end_ymd=first_register_date_end,
        err_prefix="first_register_date",
    )

    _add_owner_name_fuzzy(clauses, owner_name)
    _add_json_fuzzy(clauses, "id_number", id_number)
    _add_json_fuzzy(clauses, "plate_no", plate_no)
    _add_json_fuzzy(clauses, "engine_no", engine_no)
    _add_json_fuzzy(clauses, "vehicle_model", vehicle_model)
    _add_json_fuzzy(clauses, "vin", vin)

    return clauses


def _need_join_customer(market: Optional[str]) -> bool:
    return bool((market or "").strip())


def _need_join_info(insurance_expire_date: Optional[str], remark: Optional[str]) -> bool:
    return bool((insurance_expire_date or "").strip() or (remark or "").strip())


def _apply_optional_joins_and_filters(
    *,
    stmt,
    count_stmt,
    clauses: List[Any],
    market: Optional[str],
    insurance_expire_date: Optional[str],
    remark: Optional[str],
):
    need_join_customer = _need_join_customer(market)
    need_join_info = _need_join_info(insurance_expire_date, remark)

    if need_join_customer:
        stmt = stmt.join(CustomerGroup, CustomerGroup.id == Order.customer_group_id, isouter=True)
        count_stmt = count_stmt.join(CustomerGroup, CustomerGroup.id == Order.customer_group_id, isouter=True)
        mk = (market or "").strip().lower()
        clauses.append(func.lower(CustomerGroup.market).like(f"%{mk}%"))

    if need_join_info:
        stmt = stmt.join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
        count_stmt = count_stmt.join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)

    if (insurance_expire_date or "").strip():
        d = _parse_ymd(insurance_expire_date)
        if not d:
            raise HTTPException(status_code=400, detail="insurance_expire_date must be YYYY-MM-DD")
        clauses.append(OrderInfo.insurance_expire_date == d)

    if (remark or "").strip():
        clauses.append(func.lower(func.coalesce(OrderInfo.remark, "")).like(f"%{remark.strip().lower()}%"))

    return stmt, count_stmt


def _build_order_list_entity_stmt(order_ids: List[int]):
    stmt = (
        select(Order)
        .where(Order.id.in_(order_ids))
        .options(
            selectinload(Order.salesperson).selectinload(User.parent),
            selectinload(Order.customer_group),
            selectinload(Order.channel_group),
            selectinload(Order.order_info),
        )
    )
    return stmt


@router.get("/customer-groups", response_model=OptionListOut)
async def list_customer_groups(
    status: Optional[int] = Query(None, description="可选：启用状态过滤"),
    db: AsyncSession = Depends(get_db),
    ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
) -> OptionListOut:
    role_name = ctx.primary_role or ""
    team_names = tuple(ctx.team_names or ())

    _ensure_orders_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, team_names)

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
    status: Optional[int] = Query(None, description="可选：启用状态过滤"),
    db: AsyncSession = Depends(get_db),
    ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
) -> OptionListOut:
    from app.models.channel_group import ChannelGroup

    role_name = ctx.primary_role or ""
    team_names = tuple(ctx.team_names or ())

    _ensure_orders_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, team_names)

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


@router.get("/teams", response_model=TeamListOut)
async def list_teams(
    ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
) -> TeamListOut:
    role_name = ctx.primary_role or ""
    team_names = tuple(ctx.team_names or ())

    _ensure_orders_access(role_name)
    allowed = _ac_allowed_teams_for_user(role_name, team_names)

    items = [TeamItem(team_name=str(t)) for t in allowed if str(t).strip()]
    return TeamListOut(items=items)


@router.get("/salespersons", response_model=SalespersonListOut)
async def list_salespersons(
    status: int = Query(1, description="默认仅返回启用账号"),
    team_name: Optional[str] = Query(None, description="可选：按团队过滤"),
    ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
    db: AsyncSession = Depends(get_db),
) -> SalespersonListOut:
    role_name = ctx.primary_role or ""
    team_names = tuple(ctx.team_names or ())

    _ensure_orders_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, team_names)

    tf = str(team_name or "").strip()
    if tf:
        _ac_require_team_filter_allowed(role_name=role_name, team_names=team_names, team_filter=tf)

    stmt = (
        select(distinct(User.id).label("id"), User.username, User.real_name)
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.role_name == ROLE_SALES)
        .where(User.status == int(status))
        .order_by(User.id.asc())
    )

    if role_name != ROLE_SUPER_ADMIN:
        if role_name == ROLE_MANAGER:
            stmt = stmt.where(_ac_user_team_match_expr(_ac_normalize_team_names(team_names)))
        else:
            my_team = _ac_require_single_team_for_strict_roles(role_name, _ac_normalize_team_names(team_names))
            stmt = stmt.where(_ac_user_team_match_expr((my_team,)))

    if role_name == ROLE_SALES:
        stmt = stmt.where(User.id == int(ctx.user.id))

    if tf:
        stmt = stmt.where(_ac_user_team_match_expr((tf,)))

    rows = (await db.execute(stmt)).all()
    items = [SalespersonItem(id=int(r.id), username=str(r.username), real_name=r.real_name) for r in rows]
    return SalespersonListOut(items=items)


@router.get("/bos-sts", response_model=BosStsOut)
async def get_bos_sts(
    ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
    db: AsyncSession = Depends(get_db),
) -> BosStsOut:
    _ = db
    role_name = ctx.primary_role or ""
    team_names = tuple(ctx.team_names or ())

    _ensure_orders_access(role_name)
    _ensure_orders_write_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, team_names)

    cred = storage.assume_role(duration_seconds=900)
    return BosStsOut(
        accessKeyId=cred.access_key_id,
        secretAccessKey=cred.secret_access_key,
        sessionToken=cred.session_token,
        expiration=cred.expiration,
        bosHost=storage.vhost,
    )


@router.post("/bos-upload", response_model=BosProxyUploadOut)
async def bos_upload_proxy(
    slot_key: str = Form(...),
    file: UploadFile = File(...),
    ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
    db: AsyncSession = Depends(get_db),
) -> BosProxyUploadOut:
    _ = db
    role_name = ctx.primary_role or ""
    team_names = tuple(ctx.team_names or ())

    _ensure_orders_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, team_names)

    if role_name == ROLE_MARKET:
        raise HTTPException(status_code=403, detail="No permission")

    sk = (slot_key or "").strip()
    if sk not in ALL_SLOTS:
        raise HTTPException(status_code=400, detail=f"非法 slot_key: {slot_key}")

    if role_name == ROLE_FINANCE:
        _ensure_finance_related_only_slot(sk)
    else:
        _ensure_orders_write_access(role_name)

    if not file:
        raise HTTPException(status_code=400, detail="file 不能为空")

    md5_hex, size = await _compute_md5_and_size(file)
    content_type = (file.content_type or "application/octet-stream").strip()
    original_name = (file.filename or "file").strip()

    ext = _guess_ext(original_name, content_type)
    storage_key = storage.build_key_by_md5(scene=sk, md5_hex=md5_hex, ext=ext).lstrip("/")

    if not storage.validate_b1_key(scene=sk, storage_key=storage_key, md5_hex=md5_hex):
        raise HTTPException(status_code=400, detail="storage_key 不符合B1规则或不属于该slot")

    try:
        exists, etag = await anyio.to_thread.run_sync(lambda: storage.head_object(storage_key))
        if not exists:
            await file.seek(0)
            etag = await anyio.to_thread.run_sync(
                lambda: storage.put_object(
                    storage_key,
                    data=file.file,
                    content_type=content_type,
                )
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"BOS upload failed: {str(e) or e.__class__.__name__}")

    url = storage.object_url_for_display(storage_key, signed=None, expires_in=900)

    return BosProxyUploadOut(
        slot_key=sk,
        md5=md5_hex,
        storage_key=storage_key,
        etag=(etag or None),
        size=int(size or 0),
        content_type=content_type,
        original_name=original_name,
        url=url,
    )


@router.post("/draft", response_model=OrderDraftOut)
async def create_order_draft(
    payload: OrderDraftIn,
    db: AsyncSession = Depends(get_db),
    ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
) -> OrderDraftOut:
    role_name = ctx.primary_role or ""
    tns = _ac_normalize_team_names(ctx.team_names)

    _ensure_orders_access(role_name)
    _ensure_orders_write_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, tns)

    if role_name == ROLE_SALES:
        spid = int(ctx.user.id)
    else:
        spid = int(payload.salesperson_id or ctx.user.id)

    await _ensure_salesperson_exists(db, spid)
    await _ac_ensure_order_write_acl_by_salesperson_id(
        db=db,
        salesperson_id=spid,
        current_user=ctx.user,
        role_name=role_name,
        team_names=tns,
    )

    dyn = _clean_dynamic_data_for_write(payload.dynamic_data)

    o = Order(
        module=payload.module or "order",
        created_by=ctx.user.id,
        salesperson_id=spid,
        customer_group_id=payload.customer_group_id,
        channel_group_id=payload.channel_group_id,
        dynamic_data=dyn,
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
    db.add(info)
    if payload.order_info is not None:
        _apply_order_info_patch(info, payload.order_info)

    await db.commit()
    return OrderDraftOut(order_id=int(o.id))


@router.get("/ocr-tasks", response_model=OcrTaskListOut)
async def list_order_ocr_tasks(
    limit: int = Query(50, ge=1, le=200),
    order_id: Optional[int] = Query(None),
    active_only: bool = Query(False),
    status: Optional[str] = Query(None),
    ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
    db: AsyncSession = Depends(get_db),
) -> OcrTaskListOut:
    role_name = ctx.primary_role or ""
    team_names = tuple(ctx.team_names or ())

    _ensure_orders_access(role_name)

    stmt = select(OcrTask).where(OcrTask.scope_type == "order").order_by(OcrTask.id.desc())

    if order_id is not None:
        stmt = stmt.where(OcrTask.scope_id == int(order_id))
    if active_only:
        stmt = stmt.where(OcrTask.active_scope_id.isnot(None))
    if status:
        stmt = stmt.where(OcrTask.status == str(status).strip())

    stmt = await _apply_ocr_task_acl(
        stmt=stmt,
        current_user=ctx.user,
        role_name=role_name,
        team_names=_ac_normalize_team_names(team_names),
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


@router.post("/finalize", response_model=OrderFinalizeOut)
async def finalize_order_upload(
    payload: OrderFinalizeIn,
    db: AsyncSession = Depends(get_db),
    ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
) -> OrderFinalizeOut:
    role_name = ctx.primary_role or ""
    tns = _ac_normalize_team_names(ctx.team_names)

    _ensure_orders_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, tns)

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

    await _ac_ensure_order_write_acl_by_salesperson_id(
        db=db,
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        current_user=ctx.user,
        role_name=role_name,
        team_names=tns,
    )

    if role_name == ROLE_FINANCE and not bool(getattr(o, "is_finished", False)):
        raise HTTPException(status_code=400, detail="Only finished orders can be updated in finance")

    if role_name != ROLE_FINANCE:
        if payload.salesperson_id is not None:
            spid = int(payload.salesperson_id)
            await _ensure_salesperson_exists(db, spid)
            await _ac_ensure_order_write_acl_by_salesperson_id(
                db=db,
                salesperson_id=spid,
                current_user=ctx.user,
                role_name=role_name,
                team_names=tns,
            )
            o.salesperson_id = spid

        if payload.customer_group_id is not None:
            raise HTTPException(status_code=400, detail="customer_group_id cannot be updated in orders.finalize")
        if payload.channel_group_id is not None:
            raise HTTPException(status_code=400, detail="channel_group_id cannot be updated in orders.finalize")

        dyn_in = _clean_dynamic_data_for_write(payload.dynamic_data)
        if dyn_in:
            merged = dict(getattr(o, "dynamic_data", None) or {})
            merged.update(dyn_in)
            o.dynamic_data = _clean_dynamic_data_for_write(merged)

    _ensure_required_customer_channel(
        customer_group_id=getattr(o, "customer_group_id", None),
        channel_group_id=getattr(o, "channel_group_id", None),
    )

    clear_slots = [str(x or "").strip() for x in (payload.clear_slots or [])]
    clear_slots = [x for x in clear_slots if x]
    for sk in clear_slots:
        if sk not in ALL_SLOTS:
            raise HTTPException(status_code=400, detail=f"非法 clear_slots slot_key: {sk}")
        if sk not in MULTI_SLOTS:
            raise HTTPException(status_code=400, detail=f"暂不支持清空该slot: {sk}")

    by_slot: Dict[str, List[FinalizeImageIn]] = {}
    for im in payload.images or []:
        sk = str(im.slot_key or "").strip()
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
                storage_key = str(im.storage_key or "").strip().lstrip("/")
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
        slot_key = str(im.slot_key or "").strip()
        storage_key = str(im.storage_key or "").strip().lstrip("/")
        md5_hex = str(im.md5 or "").strip().lower()

        if not storage_key:
            raise HTTPException(status_code=400, detail="storage_key 不能为空")

        _validate_finalize_storage_key(slot_key=slot_key, storage_key=storage_key, md5_hex=md5_hex)

        has_ocr_images = has_ocr_images or (slot_key in OCR_SLOTS)

        url = str(im.url or "").strip()
        if not url:
            url = storage.object_url_for_display(storage_key, signed=None, expires_in=900)

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

        exists_stmt = select(OrderImage.id).where(
            and_(
                OrderImage.order_id == order_id,
                OrderImage.slot_key == slot_key,
                OrderImage.storage_key == storage_key,
            )
        )
        exists_id = (await db.execute(exists_stmt)).scalar_one_or_none()
        if exists_id:
            continue

        oi = OrderImage(
            order_id=order_id,
            slot_key=slot_key,
            storage_key=storage_key,
            image_url=url or "",
            image_file_id=imf.id,
        )
        db.add(oi)

    info = (await db.execute(select(OrderInfo).where(OrderInfo.order_id == order_id))).scalar_one_or_none()
    if not info:
        info = OrderInfo(order_id=order_id)
        db.add(info)

    if role_name != ROLE_FINANCE and payload.order_info is not None:
        _apply_order_info_patch(info, payload.order_info)

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
                    error_message=None,
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


@router.get("", response_model=OrderListResponse)
async def list_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    is_finished: Optional[bool] = Query(None),
    salesperson_id: Optional[int] = Query(None),
    created_by: Optional[int] = Query(None),
    customer_group_id: Optional[int] = Query(None),
    channel_group_id: Optional[int] = Query(None),
    team_name: Optional[str] = Query(None, description="按团队筛选"),
    created_date: Optional[str] = Query(None, description="YYYY-MM-DD 单日"),
    created_date_start: Optional[str] = Query(None, description="YYYY-MM-DD 起"),
    created_date_end: Optional[str] = Query(None, description="YYYY-MM-DD 止，包含当天"),
    first_register_date_start: Optional[str] = Query(None, description="YYYY-MM-DD 起"),
    first_register_date_end: Optional[str] = Query(None, description="YYYY-MM-DD 止，包含当天"),
    owner_name: Optional[str] = Query(None),
    id_number: Optional[str] = Query(None),
    plate_no: Optional[str] = Query(None),
    engine_no: Optional[str] = Query(None),
    vehicle_model: Optional[str] = Query(None),
    vin: Optional[str] = Query(None),
    remark: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    insurance_expire_date: Optional[str] = Query(None),
    is_paid: Optional[bool] = Query(None),
    is_rebate: Optional[bool] = Query(None),
    ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
    db: AsyncSession = Depends(get_db),
) -> OrderListResponse:
    role_name = ctx.primary_role or ""
    team_names = tuple(ctx.team_names or ())

    _ensure_orders_access(role_name)
    _ac_require_team_for_non_super_admin(role_name, team_names)

    clauses = await _build_order_list_clauses(
        ctx=ctx,
        role_name=role_name,
        team_names=team_names,
        is_finished=is_finished,
        salesperson_id=salesperson_id,
        created_by=created_by,
        customer_group_id=customer_group_id,
        channel_group_id=channel_group_id,
        team_name=team_name,
        created_date=created_date,
        created_date_start=created_date_start,
        created_date_end=created_date_end,
        first_register_date_start=first_register_date_start,
        first_register_date_end=first_register_date_end,
        owner_name=owner_name,
        id_number=id_number,
        plate_no=plate_no,
        engine_no=engine_no,
        vehicle_model=vehicle_model,
        vin=vin,
        is_paid=is_paid,
        is_rebate=is_rebate,
    )

    id_stmt = select(Order.id).select_from(Order)
    count_stmt = select(func.count(Order.id)).select_from(Order)

    id_stmt, count_stmt = _apply_optional_joins_and_filters(
        stmt=id_stmt,
        count_stmt=count_stmt,
        clauses=clauses,
        market=market,
        insurance_expire_date=insurance_expire_date,
        remark=remark,
    )

    if clauses:
        condition = and_(*clauses)
        id_stmt = id_stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    id_stmt = id_stmt.order_by(Order.id.desc()).offset((page - 1) * page_size).limit(page_size)

    total = int((await db.execute(count_stmt)).scalar() or 0)
    if total <= 0:
        return OrderListResponse(total=0, items=[])

    id_rows = (await db.execute(id_stmt)).all()
    order_ids = [int(r[0]) for r in id_rows if r and r[0] is not None]
    if not order_ids:
        return OrderListResponse(total=total, items=[])

    entity_stmt = _build_order_list_entity_stmt(order_ids)
    rows = (await db.execute(entity_stmt)).scalars().all()

    row_map = {int(getattr(o, "id", 0) or 0): o for o in rows}
    ordered_rows = [row_map[oid] for oid in order_ids if oid in row_map]

    items = await _rm_orders_to_list_items(ordered_rows)
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

    if payload.customer_group_id is not None:
        await _ensure_customer_group_exists(db, payload.customer_group_id)
    if payload.channel_group_id is not None:
        await _ensure_channel_group_exists(db, payload.channel_group_id)

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

    dyn = _clean_dynamic_data_for_write(payload.dynamic_data)

    o = Order(
        module=payload.module or "order",
        created_by=ctx.user.id,
        salesperson_id=spid,
        customer_group_id=payload.customer_group_id,
        channel_group_id=payload.channel_group_id,
        dynamic_data=dyn,
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
    db.add(info)

    if payload.order_info is not None:
        _apply_order_info_patch(info, payload.order_info)

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

    can_edit_customer_channel = (
        role_name in (ROLE_SALES, ROLE_MANAGER, ROLE_SUPER_ADMIN)
        and bool(getattr(o, "is_finished", False)) is False
    )

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
        if not can_edit_customer_channel:
            raise HTTPException(status_code=400, detail="customer_group_id cannot be updated")
        await _ensure_customer_group_exists(db, payload.customer_group_id)
        o.customer_group_id = int(payload.customer_group_id)

    if payload.channel_group_id is not None:
        if not can_edit_customer_channel:
            raise HTTPException(status_code=400, detail="channel_group_id cannot be updated")
        await _ensure_channel_group_exists(db, payload.channel_group_id)
        o.channel_group_id = int(payload.channel_group_id)

    if payload.status is not None:
        o.status = int(payload.status)
    if payload.audit_status is not None:
        o.audit_status = int(payload.audit_status)
    if payload.ocr_raw_json is not None:
        o.ocr_raw_json = payload.ocr_raw_json or {}
    if payload.dynamic_data is not None:
        merged = dict(o.dynamic_data or {})
        merged.update(payload.dynamic_data or {})
        o.dynamic_data = _clean_dynamic_data_for_write(merged)

    _ensure_required_customer_channel(
        customer_group_id=getattr(o, "customer_group_id", None),
        channel_group_id=getattr(o, "channel_group_id", None),
    )

    if payload.order_info is not None:
        info = (await db.execute(select(OrderInfo).where(OrderInfo.order_id == int(order_id)))).scalar_one_or_none()
        if not info:
            info = OrderInfo(order_id=int(order_id))
            db.add(info)
        _apply_order_info_patch(info, payload.order_info)

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

    if payload.is_rebate is not None or payload.is_paid is not None:
        raise HTTPException(status_code=400, detail="Finance fields cannot be updated in orders module")

    await db.commit()
    return {"ok": True}


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
_require_team_filter_ALLOWED = _ac_require_team_filter_allowed
_ac_ensure_user_in_teams = _ac_ensure_user_in_teams
_ac_ensure_order_read_acl_by_salesperson_id = _ac_ensure_order_read_acl_by_salesperson_id
_ac_ensure_order_write_acl_by_salesperson_id = _ac_ensure_order_write_acl_by_salesperson_id
_apply_orders_list_acl = _ac_apply_orders_list_acl
