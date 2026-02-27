# app/api/v1/finance.py
# encoding: utf-8
"""
财务管理（订单维度 / 去兼容版）

- BOS STS：GET /finance/bos-sts（兼容 /finance/orders/bos-sts）
- BOS 代传：POST /finance/bos-upload（兼容 /finance/orders/bos-upload）
- finalize：POST /finance/finalize（兼容 /finance/finalize-upload /finance/orders/finalize /finance/orders/finalize-upload）

补齐：
- /finance/orders 支持 created_date_start / created_date_end（按北京时间过滤 created_at，包含结束日）
- /finance/orders 支持 first_register_date_start / first_register_date_end（按 dynamic_data 当前字段 dl_register_date 范围过滤，包含结束日）
- 保留旧参数 created_date / first_register_date 兼容（单日/模糊），但字段口径仅使用当前字段

本轮：
- 团队隔离（支持经理多团队）
- 财务端下拉筛选（只读）：customer-groups / channel-groups / salespersons
- 财务汇总：/finance/orders/summary（按搜索条件全量聚合）
- 稳定导出：/finance/orders/export（一次性导出全部符合条件；不带参数时导出全部“已完成订单”）
- 修复搜索栏未生效：新增 salesperson_id / plate_no / vin / engine_no / id_number / vehicle_model 筛选（列表/汇总/导出一致）

重要修复（2026-02-19）：
- 路由匹配顺序：/orders/export 必须在 /orders/{order_id} 之前，否则 export 会被当成 order_id 导致 422
- 进一步加固：/orders/{order_id:int} 仅匹配整数，避免静态子路由被吞

导出修复（2026-02-19）：
- 日期列仅输出 YYYY-MM-DD（不再带时分秒）
- “应收/应付”与列表口径一致：应收=channel_total，应付=customer_total

本次清理（2026-02-24）：
- 清理财务侧旧字段兜底，统一只使用当前 OCR 字段口径：
  dl_owner / dl_plate_no / dl_vin / dl_engine_no / dl_id_number / dl_vehicle_model / dl_register_date

本次修复（2026-02-28）：
- /finance/orders 返回结构改为与 /orders 列表一致（OrderListResponse / OrderOut）
  财务仅权限/操作不同，列表字段口径一致，避免前端出现“财务列表字段为空”问题。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Set, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import anyio
import requests
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    func,
    select,
    and_,
    or_,
    cast,
    String,
    delete,
    distinct,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
from app.core.constants import (
    ROLE_FINANCE,
    ROLE_MANAGER,
    ROLE_SUPER_ADMIN,
    ROLE_SALES,
    ROLE_MARKET,
)
from app.core.db import get_db, engine
from app.models.channel_group import ChannelGroup
from app.models.customer_group import CustomerGroup
from app.models.image_file import ImageFile
from app.models.order import Order, OrderImage
from app.models.order_info import OrderInfo
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole
from app.schemas.finance import (
    FinanceOrderStatusUpdate,
)
from app.schemas.order import (
    OrderOut,
    OrderListResponse,
    OrderInfoOut,
)
from app.services.storage import StorageService
from app.services.order_detail_builder import load_order_detail_blocks
from app.utils.order_image_urls import ensure_display_urls_for_order_images, safe_image_urls

router = APIRouter(prefix="/finance", tags=["finance"])

BJ_TZ = ZoneInfo("Asia/Shanghai")
storage = StorageService()

FINANCE_ALLOWED_SLOTS = {"related"}
MULTI_SLOTS = {"related"}


def _ensure_finance_access(role_name: Optional[str]) -> None:
    if role_name == ROLE_SALES:
        raise HTTPException(status_code=403, detail="Sales has no permission to access finance")
    if role_name not in (ROLE_FINANCE, ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_MARKET):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_finance_write_access(role_name: Optional[str]) -> None:
    if role_name not in (ROLE_FINANCE, ROLE_MANAGER, ROLE_SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_finance_export_access(role_name: Optional[str]) -> None:
    _ensure_finance_write_access(role_name)


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


def _fmt_date_only(dt: Optional[datetime]) -> Optional[str]:
    s = _fmt_dt(dt)
    if not s:
        return None
    return s.split(" ")[0]


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


def _add_json_fuzzy_any(clauses: list, keys: List[str], value: Optional[str]):
    v = (value or "").strip()
    if not v:
        return
    vv = v.lower()
    terms = []
    for k in keys:
        if not (k or "").strip():
            continue
        expr = func.lower(_json_text_unquoted(Order.dynamic_data, k))
        terms.append(expr.like(f"%{vv}%"))
    if terms:
        clauses.append(or_(*terms))


def _add_first_register_date_legacy_filter(clauses: list, value: Optional[str]):
    v = (value or "").strip()
    if not v:
        return

    v_dash = v.lower()
    v_digits = v.replace("-", "").lower()

    expr_dl = func.lower(_json_text_unquoted(Order.dynamic_data, "dl_register_date"))
    terms = [expr_dl.like(f"%{v_dash}%")]
    if v_digits and v_digits != v_dash:
        terms.append(expr_dl.like(f"%{v_digits}%"))

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


def _parse_ymd(ymd: str) -> Optional[datetime]:
    s = (ymd or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
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
        raise HTTPException(status_code=400, detail=f"{err_prefix}_* must be YYYY-MM-DD")

    or_terms = []
    for k in keys:
        txt = _json_text_unquoted(Order.dynamic_data, k)
        txt8 = _digits8_expr(txt)
        or_terms.append(and_(txt8 >= s8, txt8 <= e8))

    if or_terms:
        clauses.append(or_(*or_terms))


def _extract_dd(dd: dict, *keys: str) -> str:
    for k in keys:
        if not k:
            continue
        v = dd.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


class OptionItem(BaseModel):
    id: int
    group_name: str


class OptionListOut(BaseModel):
    items: List[OptionItem] = Field(default_factory=list)


class SalespersonItem(BaseModel):
    id: int
    username: str
    real_name: Optional[str] = None


class SalespersonListOut(BaseModel):
    items: List[SalespersonItem] = Field(default_factory=list)


@router.get("/customer-groups", response_model=OptionListOut)
async def finance_list_customer_groups(
    status: Optional[int] = Query(None, description="可选：启用状态过滤（若模型有该字段）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=team_names)

    stmt = select(CustomerGroup).order_by(CustomerGroup.id.asc())

    if hasattr(CustomerGroup, "deleted_at"):
        stmt = stmt.where(getattr(CustomerGroup, "deleted_at").is_(None))
    if status is not None and hasattr(CustomerGroup, "status"):
        stmt = stmt.where(getattr(CustomerGroup, "status") == int(status))

    if hasattr(CustomerGroup, "team_name") and current_team_names is not None:
        stmt = stmt.where(getattr(CustomerGroup, "team_name").in_(list(current_team_names)))

    rows = (await db.execute(stmt)).scalars().all()
    return OptionListOut(
        items=[OptionItem(id=int(x.id), group_name=str(_group_code_name(x) or _group_display_name(x) or "")) for x in rows]
    )


@router.get("/channel-groups", response_model=OptionListOut)
async def finance_list_channel_groups(
    status: Optional[int] = Query(None, description="可选：启用状态过滤（若模型有该字段）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=team_names)

    stmt = select(ChannelGroup).order_by(ChannelGroup.id.asc())

    if hasattr(ChannelGroup, "deleted_at"):
        stmt = stmt.where(getattr(ChannelGroup, "deleted_at").is_(None))
    if status is not None and hasattr(ChannelGroup, "status"):
        stmt = stmt.where(getattr(ChannelGroup, "status") == int(status))

    if hasattr(ChannelGroup, "team_name") and current_team_names is not None:
        stmt = stmt.where(getattr(ChannelGroup, "team_name").in_(list(current_team_names)))

    rows = (await db.execute(stmt)).scalars().all()
    return OptionListOut(
        items=[OptionItem(id=int(x.id), group_name=str(_group_code_name(x) or _group_display_name(x) or "")) for x in rows]
    )


@router.get("/salespersons", response_model=SalespersonListOut)
async def finance_list_salespersons(
    status: int = Query(1, description="默认仅返回启用账号；传 0 可查禁用"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=team_names)

    stmt = (
        select(distinct(User.id).label("id"), User.username, User.real_name)
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.role_name == ROLE_SALES)
        .where(User.status == int(status))
        .order_by(User.id.asc())
    )

    if current_team_names is not None:
        stmt = stmt.where(_ac_user_team_match_expr(current_team_names))

    rows = (await db.execute(stmt)).all()
    return SalespersonListOut(items=[SalespersonItem(id=int(r.id), username=str(r.username), real_name=r.real_name) for r in rows])


async def _ensure_finance_order_viewable(
    db: AsyncSession,
    order_id: int,
    *,
    current_team_names: Optional[Tuple[str, ...]],
) -> Order:
    o = (
        await db.execute(
            select(Order)
            .where(Order.id == int(order_id))
            .options(selectinload(Order.salesperson))
        )
    ).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    if not bool(getattr(o, "is_finished", False)):
        raise HTTPException(status_code=400, detail="Only finished orders can be viewed in finance")

    await _ac_salesperson_in_current_teams_or_403(
        salesperson=getattr(o, "salesperson", None),
        current_team_names=current_team_names,
    )
    return o


class FinanceOrdersSummaryOut(BaseModel):
    commercial_amount: float = 0.0
    compulsory_amount: float = 0.0
    vehicle_tax_amount: float = 0.0
    noncar_amount: float = 0.0
    receivable: float = 0.0
    payable: float = 0.0
    profit: float = 0.0

    channel_reward: float = 0.0
    customer_reward: float = 0.0


@router.get("/orders/summary", response_model=FinanceOrdersSummaryOut)
async def finance_orders_summary(
    order_id: Optional[int] = Query(None, description="精确订单ID"),
    created_date: Optional[str] = Query(None, description="日期 YYYY-MM-DD（兼容旧参数：按北京时间过滤 created_at 单日）"),
    created_date_start: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 起）"),
    created_date_end: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 止，包含当天）"),
    channel_group_id: Optional[int] = Query(None, description="渠道"),
    customer_group_id: Optional[int] = Query(None, description="客户"),
    market: Optional[str] = Query(None, description="市场（模糊）"),
    owner_name: Optional[str] = Query(None, description="车主（模糊）"),
    insurance_expire_date: Optional[str] = Query(None, description="保险到期日 YYYY-MM-DD"),
    first_register_date: Optional[str] = Query(None, description="初登日期 YYYY-MM-DD（兼容旧参数名，字段口径仅 dl_register_date）"),
    first_register_date_start: Optional[str] = Query(None, description="初登日期起 YYYY-MM-DD（包含）"),
    first_register_date_end: Optional[str] = Query(None, description="初登日期止 YYYY-MM-DD（包含）"),
    is_paid: Optional[bool] = Query(None, description="是否回款"),
    is_rebate: Optional[bool] = Query(None, description="是否返点"),
    team_name: Optional[str] = Query(None, description="按团队筛选"),
    team_names: Optional[Tuple[str, ...]] = Query(None, description="按多团队筛选（可重复 team_names=xxx）"),
    salesperson_id: Optional[int] = Query(None, description="业务员ID（精确）"),
    plate_no: Optional[str] = Query(None, description="车牌号（模糊）"),
    vin: Optional[str] = Query(None, description="车架号VIN（模糊）"),
    engine_no: Optional[str] = Query(None, description="发动机号（模糊）"),
    id_number: Optional[str] = Query(None, description="身份证号（模糊）"),
    vehicle_model: Optional[str] = Query(None, description="车型（模糊）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _current_user, role_name, user_team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=user_team_names)
    effective_team_names = _ac_effective_team_filter_for_query(
        role_name=role_name,
        current_team_names=current_team_names,
        team_name=team_name,
        team_names=team_names,
    )

    clauses: list = []

    stmt = (
        select(
            func.coalesce(func.sum(OrderInfo.commercial_amount), 0).label("commercial_amount"),
            func.coalesce(func.sum(OrderInfo.compulsory_amount), 0).label("compulsory_amount"),
            func.coalesce(func.sum(OrderInfo.vehicle_tax_amount), 0).label("vehicle_tax_amount"),
            func.coalesce(func.sum(OrderInfo.non_vehicle_amount), 0).label("noncar_amount"),
            func.coalesce(func.sum(OrderInfo.channel_total), 0).label("receivable"),
            func.coalesce(func.sum(OrderInfo.customer_total), 0).label("payable"),
            func.coalesce(func.sum(OrderInfo.profit), 0).label("profit"),
            func.coalesce(func.sum(OrderInfo.channel_reward), 0).label("channel_reward"),
            func.coalesce(func.sum(OrderInfo.customer_reward), 0).label("customer_reward"),
        )
        .select_from(Order)
        .join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
        .where(Order.is_finished.is_(True))
    )

    if effective_team_names is not None:
        clauses.append(_ac_order_salesperson_in_teams_expr(effective_team_names))

    if order_id is not None:
        clauses.append(Order.id == int(order_id))

    if salesperson_id is not None:
        clauses.append(Order.salesperson_id == int(salesperson_id))

    if channel_group_id is not None:
        clauses.append(Order.channel_group_id == int(channel_group_id))
    if customer_group_id is not None:
        clauses.append(Order.customer_group_id == int(customer_group_id))

    if created_date_start or created_date_end:
        if not created_date_start or not created_date_end:
            raise HTTPException(status_code=400, detail="created_date_start and created_date_end are required")
        rng = _parse_bj_date_span(created_date_start, created_date_end)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date_* must be YYYY-MM-DD and end>=start")
        start_bj, end_bj = rng
        clauses.append(Order.created_at >= start_bj)
        clauses.append(Order.created_at < end_bj)
    elif created_date:
        rng = _parse_bj_date_range(created_date)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date must be YYYY-MM-DD")
        start_bj, end_bj = rng
        clauses.append(Order.created_at >= start_bj)
        clauses.append(Order.created_at < end_bj)

    if is_paid is not None:
        clauses.append(Order.is_paid.is_(bool(is_paid)))
    if is_rebate is not None:
        clauses.append(Order.is_rebate.is_(bool(is_rebate)))

    if (market or "").strip():
        stmt = stmt.join(CustomerGroup, CustomerGroup.id == Order.customer_group_id, isouter=True)
        mk = (market or "").strip().lower()
        clauses.append(func.lower(CustomerGroup.market).like(f"%{mk}%"))

    if (owner_name or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_owner"], owner_name)

    if (plate_no or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_plate_no"], plate_no)
    if (vin or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_vin"], vin)
    if (engine_no or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_engine_no"], engine_no)
    if (id_number or "").strip():
        _add_json_fuzzy_any(clauses, ["id_number", "dl_id_number"], id_number)
    if (vehicle_model or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_vehicle_model", "dl_brand_model", "vehicle_model"], vehicle_model)

    if first_register_date_start or first_register_date_end:
        _add_json_date_range_any(
            clauses,
            keys=["dl_register_date"],
            start_ymd=first_register_date_start,
            end_ymd=first_register_date_end,
            err_prefix="first_register_date",
        )
    elif (first_register_date or "").strip():
        _add_first_register_date_legacy_filter(clauses, first_register_date)

    if (insurance_expire_date or "").strip():
        d = _parse_ymd(insurance_expire_date)
        if not d:
            raise HTTPException(status_code=400, detail="insurance_expire_date must be YYYY-MM-DD")
        clauses.append(OrderInfo.insurance_expire_date == d.date())

    if clauses:
        stmt = stmt.where(and_(*clauses))

    row = (await db.execute(stmt)).mappings().first() or {}

    return FinanceOrdersSummaryOut(
        commercial_amount=float(row.get("commercial_amount") or 0),
        compulsory_amount=float(row.get("compulsory_amount") or 0),
        vehicle_tax_amount=float(row.get("vehicle_tax_amount") or 0),
        noncar_amount=float(row.get("noncar_amount") or 0),
        receivable=float(row.get("receivable") or 0),
        payable=float(row.get("payable") or 0),
        profit=float(row.get("profit") or 0),
        channel_reward=float(row.get("channel_reward") or 0),
        customer_reward=float(row.get("customer_reward") or 0),
    )


@router.get("/orders", response_model=OrderListResponse)
async def list_finance_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    order_id: Optional[int] = Query(None, description="精确订单ID"),
    created_date: Optional[str] = Query(None, description="日期 YYYY-MM-DD（兼容旧参数：按北京时间过滤 created_at 单日）"),
    created_date_start: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 起）"),
    created_date_end: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 止，包含当天）"),
    channel_group_id: Optional[int] = Query(None, description="渠道"),
    customer_group_id: Optional[int] = Query(None, description="客户"),
    market: Optional[str] = Query(None, description="市场（模糊）"),
    owner_name: Optional[str] = Query(None, description="车主（模糊）"),
    insurance_expire_date: Optional[str] = Query(None, description="保险到期日 YYYY-MM-DD"),
    first_register_date: Optional[str] = Query(None, description="初登日期 YYYY-MM-DD（兼容旧参数名，字段口径仅 dl_register_date）"),
    first_register_date_start: Optional[str] = Query(None, description="初登日期起 YYYY-MM-DD（包含）"),
    first_register_date_end: Optional[str] = Query(None, description="初登日期止 YYYY-MM-DD（包含）"),
    is_paid: Optional[bool] = Query(None, description="是否回款"),
    is_rebate: Optional[bool] = Query(None, description="是否返点"),
    team_name: Optional[str] = Query(None, description="按团队筛选"),
    team_names: Optional[Tuple[str, ...]] = Query(None, description="按多团队筛选（可重复 team_names=xxx）"),
    salesperson_id: Optional[int] = Query(None, description="业务员ID（精确）"),
    plate_no: Optional[str] = Query(None, description="车牌号（模糊）"),
    vin: Optional[str] = Query(None, description="车架号VIN（模糊）"),
    engine_no: Optional[str] = Query(None, description="发动机号（模糊）"),
    id_number: Optional[str] = Query(None, description="身份证号（模糊）"),
    vehicle_model: Optional[str] = Query(None, description="车型（模糊）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _current_user, role_name, user_team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=user_team_names)
    effective_team_names = _ac_effective_team_filter_for_query(
        role_name=role_name,
        current_team_names=current_team_names,
        team_name=team_name,
        team_names=team_names,
    )

    stmt = (
        select(Order)
        .where(Order.is_finished.is_(True))
        .options(
            selectinload(Order.creator),
            selectinload(Order.salesperson),
            selectinload(Order.customer_group),
            selectinload(Order.channel_group),
            selectinload(Order.order_info),
            selectinload(Order.images).selectinload(OrderImage.image_file),
        )
    )
    count_stmt = select(func.count(Order.id)).where(Order.is_finished.is_(True))

    clauses: list = []

    if effective_team_names is not None:
        clauses.append(_ac_order_salesperson_in_teams_expr(effective_team_names))

    if order_id is not None:
        clauses.append(Order.id == int(order_id))

    if salesperson_id is not None:
        clauses.append(Order.salesperson_id == int(salesperson_id))

    if channel_group_id is not None:
        clauses.append(Order.channel_group_id == int(channel_group_id))
    if customer_group_id is not None:
        clauses.append(Order.customer_group_id == int(customer_group_id))

    if created_date_start or created_date_end:
        if not created_date_start or not created_date_end:
            raise HTTPException(status_code=400, detail="created_date_start and created_date_end are required")
        rng = _parse_bj_date_span(created_date_start, created_date_end)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date_* must be YYYY-MM-DD and end>=start")
        start_bj, end_bj = rng
        clauses.append(Order.created_at >= start_bj)
        clauses.append(Order.created_at < end_bj)
    elif created_date:
        rng = _parse_bj_date_range(created_date)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date must be YYYY-MM-DD")
        start_bj, end_bj = rng
        clauses.append(Order.created_at >= start_bj)
        clauses.append(Order.created_at < end_bj)

    if is_paid is not None:
        clauses.append(Order.is_paid.is_(bool(is_paid)))
    if is_rebate is not None:
        clauses.append(Order.is_rebate.is_(bool(is_rebate)))

    if (market or "").strip():
        stmt = stmt.join(CustomerGroup, CustomerGroup.id == Order.customer_group_id, isouter=True)
        count_stmt = count_stmt.join(CustomerGroup, CustomerGroup.id == Order.customer_group_id, isouter=True)
        mk = (market or "").strip().lower()
        clauses.append(func.lower(CustomerGroup.market).like(f"%{mk}%"))

    if (owner_name or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_owner"], owner_name)

    if (plate_no or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_plate_no"], plate_no)
    if (vin or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_vin"], vin)
    if (engine_no or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_engine_no"], engine_no)
    if (id_number or "").strip():
        _add_json_fuzzy_any(clauses, ["id_number", "dl_id_number"], id_number)
    if (vehicle_model or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_vehicle_model", "dl_brand_model", "vehicle_model"], vehicle_model)

    if first_register_date_start or first_register_date_end:
        _add_json_date_range_any(
            clauses,
            keys=["dl_register_date"],
            start_ymd=first_register_date_start,
            end_ymd=first_register_date_end,
            err_prefix="first_register_date",
        )
    elif (first_register_date or "").strip():
        _add_first_register_date_legacy_filter(clauses, first_register_date)

    if (insurance_expire_date or "").strip():
        d = _parse_ymd(insurance_expire_date)
        if not d:
            raise HTTPException(status_code=400, detail="insurance_expire_date must be YYYY-MM-DD")
        stmt = stmt.join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
        count_stmt = count_stmt.join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
        clauses.append(OrderInfo.insurance_expire_date == d.date())

    if clauses:
        stmt = stmt.where(and_(*clauses))
        count_stmt = count_stmt.where(and_(*clauses))

    stmt = stmt.order_by(Order.id.desc()).offset((page - 1) * page_size).limit(page_size)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt)).scalars().all()

    manager_ids: Set[int] = set()
    salesperson_to_manager_id: Dict[int, int] = {}

    for o in rows:
        sp = getattr(o, "salesperson", None)
        if not sp:
            continue

        mid = _ac_pick_manager_id_from_salesperson(sp)
        if mid:
            salesperson_to_manager_id[int(getattr(sp, "id", 0) or 0)] = mid
            manager_ids.add(mid)

    managers_by_id: Dict[int, User] = {}
    if manager_ids:
        mgr_rows = (await db.execute(select(User).where(User.id.in_(list(manager_ids))))).scalars().all()
        managers_by_id = {int(getattr(u, "id", 0) or 0): u for u in mgr_rows if getattr(u, "id", None) is not None}

    items: List[OrderOut] = []
    for o in rows:
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

        items.append(
            OrderOut(
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
                order_info=OrderInfoOut.from_orm(getattr(o, "order_info", None)) if getattr(o, "order_info", None) else None,
            )
        )

    return OrderListResponse(total=int(total or 0), items=items)


def _esc_html(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _fmt_money_export(v) -> str:
    try:
        if v is None:
            return "-"
        x = float(v)
        return f"{x:.2f}"
    except Exception:
        return "-"


def _fmt_point_export(v) -> str:
    if v is None:
        return "-"
    s = str(v).strip()
    return s if s else "-"


def _fmt_text_export(v) -> str:
    s = str(v or "").strip()
    return s if s else "-"


def _join_teams_export(team_names: List[str], team_name: Optional[str]) -> str:
    arr: List[str] = []
    for x in (team_names or []):
        sx = str(x or "").strip()
        if sx:
            arr.append(sx)
    tn = str(team_name or "").strip()
    if tn and tn not in arr:
        arr.append(tn)
    seen = set()
    out = []
    for x in arr:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return "、".join(out) if out else "-"


def _now_shanghai_stamp() -> str:
    try:
        return datetime.now(BJ_TZ).strftime("%Y-%m-%d_%H%M%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d_%H%M%S")


@router.get("/orders/export")
async def finance_orders_export(
    order_id: Optional[int] = Query(None, description="精确订单ID"),
    created_date: Optional[str] = Query(None, description="日期 YYYY-MM-DD（兼容旧参数：按北京时间过滤 created_at 单日）"),
    created_date_start: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 起）"),
    created_date_end: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 止，包含当天）"),
    channel_group_id: Optional[int] = Query(None, description="渠道"),
    customer_group_id: Optional[int] = Query(None, description="客户"),
    market: Optional[str] = Query(None, description="市场（模糊）"),
    owner_name: Optional[str] = Query(None, description="车主（模糊）"),
    insurance_expire_date: Optional[str] = Query(None, description="保险到期日 YYYY-MM-DD"),
    first_register_date: Optional[str] = Query(None, description="初登日期 YYYY-MM-DD（兼容旧参数名，字段口径仅 dl_register_date）"),
    first_register_date_start: Optional[str] = Query(None, description="初登日期起 YYYY-MM-DD（包含）"),
    first_register_date_end: Optional[str] = Query(None, description="初登日期止 YYYY-MM-DD（包含）"),
    is_paid: Optional[bool] = Query(None, description="是否回款"),
    is_rebate: Optional[bool] = Query(None, description="是否返点"),
    team_name: Optional[str] = Query(None, description="按团队筛选"),
    team_names: Optional[Tuple[str, ...]] = Query(None, description="按多团队筛选（可重复 team_names=xxx）"),
    salesperson_id: Optional[int] = Query(None, description="业务员ID（精确）"),
    plate_no: Optional[str] = Query(None, description="车牌号（模糊）"),
    vin: Optional[str] = Query(None, description="车架号VIN（模糊）"),
    engine_no: Optional[str] = Query(None, description="发动机号（模糊）"),
    id_number: Optional[str] = Query(None, description="身份证号（模糊）"),
    vehicle_model: Optional[str] = Query(None, description="车型（模糊）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _current_user, role_name, user_team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    _ensure_finance_export_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=user_team_names)
    effective_team_names = _ac_effective_team_filter_for_query(
        role_name=role_name,
        current_team_names=current_team_names,
        team_name=team_name,
        team_names=team_names,
    )

    clauses: list = [Order.is_finished.is_(True)]

    if effective_team_names is not None:
        clauses.append(_ac_order_salesperson_in_teams_expr(effective_team_names))

    if order_id is not None:
        clauses.append(Order.id == int(order_id))

    if salesperson_id is not None:
        clauses.append(Order.salesperson_id == int(salesperson_id))

    if channel_group_id is not None:
        clauses.append(Order.channel_group_id == int(channel_group_id))
    if customer_group_id is not None:
        clauses.append(Order.customer_group_id == int(customer_group_id))

    if created_date_start or created_date_end:
        if not created_date_start or not created_date_end:
            raise HTTPException(status_code=400, detail="created_date_start and created_date_end are required")
        rng = _parse_bj_date_span(created_date_start, created_date_end)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date_* must be YYYY-MM-DD and end>=start")
        start_bj, end_bj = rng
        clauses.append(Order.created_at >= start_bj)
        clauses.append(Order.created_at < end_bj)
    elif created_date:
        rng = _parse_bj_date_range(created_date)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date must be YYYY-MM-DD")
        start_bj, end_bj = rng
        clauses.append(Order.created_at >= start_bj)
        clauses.append(Order.created_at < end_bj)

    if is_paid is not None:
        clauses.append(Order.is_paid.is_(bool(is_paid)))
    if is_rebate is not None:
        clauses.append(Order.is_rebate.is_(bool(is_rebate)))

    need_join_customer = bool((market or "").strip())
    need_join_info = bool((insurance_expire_date or "").strip())

    if need_join_customer:
        mk = (market or "").strip().lower()
        clauses.append(func.lower(CustomerGroup.market).like(f"%{mk}%"))

    if (owner_name or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_owner"], owner_name)

    if (plate_no or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_plate_no"], plate_no)
    if (vin or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_vin"], vin)
    if (engine_no or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_engine_no"], engine_no)
    if (id_number or "").strip():
        _add_json_fuzzy_any(clauses, ["id_number", "dl_id_number"], id_number)
    if (vehicle_model or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_vehicle_model", "dl_brand_model", "vehicle_model"], vehicle_model)

    if first_register_date_start or first_register_date_end:
        _add_json_date_range_any(
            clauses,
            keys=["dl_register_date"],
            start_ymd=first_register_date_start,
            end_ymd=first_register_date_end,
            err_prefix="first_register_date",
        )
    elif (first_register_date or "").strip():
        _add_first_register_date_legacy_filter(clauses, first_register_date)

    if (insurance_expire_date or "").strip():
        d = _parse_ymd(insurance_expire_date)
        if not d:
            raise HTTPException(status_code=400, detail="insurance_expire_date must be YYYY-MM-DD")
        clauses.append(OrderInfo.insurance_expire_date == d.date())
        need_join_info = True

    headers = [
        "日期", "渠道", "客户", "市场", "业务员", "车主", "车牌", "保险到期日", "车架号", "发动机号", "车型", "初登日期", "身份证号", "电话",
        "商业金额", "交强金额", "车船税金额", "非车金额",
        "渠道商业点位", "渠道商业后补点位", "渠道交强点位", "渠道车船税点位", "渠道非车点位", "渠道奖励",
        "客户商业点位", "客户商业后补点位", "客户交强点位", "客户车船税点位", "客户非车点位", "客户奖励",
        "应收", "应付", "利润", "所属团队", "所属经理", "是否回款", "是否返点",
    ]

    base_stmt = (
        select(Order)
        .select_from(Order)
        .options(
            selectinload(Order.salesperson),
            selectinload(Order.customer_group),
            selectinload(Order.channel_group),
            selectinload(Order.order_info),
        )
    )

    if need_join_customer:
        base_stmt = base_stmt.join(CustomerGroup, CustomerGroup.id == Order.customer_group_id, isouter=True)
    if need_join_info:
        base_stmt = base_stmt.join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)

    base_stmt = base_stmt.where(and_(*clauses)).order_by(Order.id.desc())

    filename = f"财务管理_订单_{_now_shanghai_stamp()}.xls"
    disp = f"attachment; filename*=UTF-8''{quote(filename)}"

    async def gen():
        yield (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\" /></head><body>"
            "<table border=\"1\"><thead><tr>"
            + "".join([f"<th>{_esc_html(h)}</th>" for h in headers])
            + "</tr></thead><tbody>"
        ).encode("utf-8")

        batch = 500
        last_id = None

        while True:
            stmt = base_stmt
            if last_id is not None:
                stmt = stmt.where(Order.id < int(last_id))
            stmt = stmt.limit(batch)

            rows = (await db.execute(stmt)).scalars().all()
            if not rows:
                break

            manager_ids: Set[int] = set()
            salesperson_to_manager_id: Dict[int, int] = {}
            for o in rows:
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

            for o in rows:
                cg = getattr(o, "customer_group", None)
                ch = getattr(o, "channel_group", None)
                sp = getattr(o, "salesperson", None)
                info = getattr(o, "order_info", None)
                dd = getattr(o, "dynamic_data", None) or {}
                dd = dd if isinstance(dd, dict) else {}

                created_at = _fmt_date_only(getattr(o, "created_at", None)) or "-"
                channel_name = _group_code_name(ch) or "-"
                customer_name = _group_code_name(cg) or "-"
                market_val = (getattr(cg, "market", None) if cg else None) or "-"
                salesperson_name = _user_display_name(sp) or "-"

                owner = _extract_dd(dd, "dl_owner", "id_name", "owner_name")
                plate = _extract_dd(dd, "dl_plate_no", "plate_no")
                vin_val = _extract_dd(dd, "dl_vin", "vin")
                engine_no_val = _extract_dd(dd, "dl_engine_no", "engine_no")
                vehicle_model_val = _extract_dd(dd, "dl_vehicle_model", "dl_brand_model", "vehicle_model")
                first_register = _extract_dd(dd, "dl_register_date", "first_register_date", "register_date")
                id_number_val = _extract_dd(dd, "id_number", "dl_id_number")
                phone = str(getattr(info, "owner_phone", None) or "").strip()

                insurance_expire = "-"
                try:
                    if info and getattr(info, "insurance_expire_date", None):
                        insurance_expire = getattr(info, "insurance_expire_date").strftime("%Y-%m-%d")
                except Exception:
                    insurance_expire = "-"

                cm = _fmt_money_export(getattr(info, "commercial_amount", None) if info else None)
                jq = _fmt_money_export(getattr(info, "compulsory_amount", None) if info else None)
                tax = _fmt_money_export(getattr(info, "vehicle_tax_amount", None) if info else None)
                nc = _fmt_money_export(getattr(info, "non_vehicle_amount", None) if info else None)

                ch_cm_p = _fmt_point_export(getattr(info, "channel_commercial_point", None) if info else None)
                ch_cm_sup_p = _fmt_point_export(getattr(info, "channel_commercial_supplement_point", None) if info else None)
                ch_jq_p = _fmt_point_export(getattr(info, "channel_compulsory_point", None) if info else None)
                ch_tax_p = _fmt_point_export(getattr(info, "channel_vehicle_tax_point", None) if info else None)
                ch_nc_p = _fmt_point_export(getattr(info, "channel_non_vehicle_point", None) if info else None)
                ch_reward = _fmt_money_export(getattr(info, "channel_reward", None) if info else None)

                cu_cm_p = _fmt_point_export(getattr(info, "customer_commercial_point", None) if info else None)
                cu_cm_sup_p = _fmt_point_export(getattr(info, "customer_commercial_supplement_point", None) if info else None)
                cu_jq_p = _fmt_point_export(getattr(info, "customer_compulsory_point", None) if info else None)
                cu_tax_p = _fmt_point_export(getattr(info, "customer_vehicle_tax_point", None) if info else None)
                cu_nc_p = _fmt_point_export(getattr(info, "customer_non_vehicle_point", None) if info else None)
                cu_reward = _fmt_money_export(getattr(info, "customer_reward", None) if info else None)

                receivable = _fmt_money_export(getattr(info, "channel_total", None) if info else None)
                payable = _fmt_money_export(getattr(info, "customer_total", None) if info else None)
                profit = _fmt_money_export(getattr(info, "profit", None) if info else None)

                manager_name = "-"
                if sp:
                    inline_name = _ac_pick_manager_name_inline(sp)
                    if inline_name:
                        manager_name = inline_name
                    else:
                        sid = int(getattr(sp, "id", 0) or 0)
                        mid = salesperson_to_manager_id.get(sid) or _ac_pick_manager_id_from_salesperson(sp)
                        if mid:
                            manager_name = _user_display_name(managers_by_id.get(int(mid))) or "-"

                team_names_val = _ac_split_team_names_any(getattr(sp, "team_names", None)) if sp else []
                team_name_val = str(getattr(sp, "team_name", None) or "").strip() if sp else ""
                team_display = _join_teams_export(team_names_val, team_name_val or None)

                paid = "是" if bool(getattr(o, "is_paid", False)) else "否"
                rebate = "是" if bool(getattr(o, "is_rebate", False)) else "否"

                cols = [
                    created_at, channel_name, customer_name, market_val, salesperson_name,
                    owner or "-", plate or "-", insurance_expire, vin_val or "-", engine_no_val or "-",
                    vehicle_model_val or "-", first_register or "-", id_number_val or "-", phone or "-",
                    cm, jq, tax, nc,
                    ch_cm_p, ch_cm_sup_p, ch_jq_p, ch_tax_p, ch_nc_p, ch_reward,
                    cu_cm_p, cu_cm_sup_p, cu_jq_p, cu_tax_p, cu_nc_p, cu_reward,
                    receivable, payable, profit, team_display, manager_name, paid, rebate,
                ]

                text_cols = {6, 8, 9, 11, 12, 13}

                tds = []
                for idx, c in enumerate(cols):
                    cc = _esc_html(_fmt_text_export(c))
                    if idx in text_cols:
                        tds.append(f"<td style=\"mso-number-format:'\\@';\">{cc}</td>")
                    else:
                        tds.append(f"<td>{cc}</td>")

                yield ("<tr>" + "".join(tds) + "</tr>").encode("utf-8")

            last_id = int(getattr(rows[-1], "id", 0) or 0)
            if last_id <= 0:
                break

        yield "</tbody></table></body></html>".encode("utf-8")

    return StreamingResponse(
        gen(),
        media_type="application/vnd.ms-excel; charset=utf-8",
        headers={"Content-Disposition": disp},
    )


@router.get("/orders/{order_id:int}", response_model=Dict[str, Any])
async def get_finance_order_detail(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    current_user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=team_names)
    _ = await _ensure_finance_order_viewable(db, int(order_id), current_team_names=current_team_names)

    return await load_order_detail_blocks(
        db,
        int(order_id),
        current_user=current_user,
        role_name=role_name,
        team_names=_ac_normalize_team_names(team_names),
        storage=storage,
        enforce_read_acl=False,
    )


@router.patch("/orders/{order_id:int}/status")
async def update_finance_order_status(
    order_id: int,
    payload: FinanceOrderStatusUpdate,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _current_user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    _ensure_finance_write_access(role_name)
    tns = _ac_current_team_names_or_403(role_name=role_name, team_names=team_names)

    o = (
        await db.execute(
            select(Order)
            .where(Order.id == int(order_id))
            .options(selectinload(Order.salesperson))
        )
    ).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    if not bool(getattr(o, "is_finished", False)):
        raise HTTPException(status_code=400, detail="Only finished orders can be updated in finance")

    await _ac_salesperson_in_current_teams_or_403(
        salesperson=getattr(o, "salesperson", None),
        current_team_names=tns,
    )

    if payload.is_rebate is not None:
        o.is_rebate = bool(payload.is_rebate)
    if payload.is_paid is not None:
        o.is_paid = bool(payload.is_paid)

    await db.commit()
    return {"ok": True}


@router.post("/orders/{order_id:int}/return")
async def return_finance_order_to_unfinished(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _current_user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    _ensure_finance_write_access(role_name)
    tns = _ac_current_team_names_or_403(role_name=role_name, team_names=team_names)

    o = (
        await db.execute(
            select(Order)
            .where(Order.id == int(order_id))
            .options(selectinload(Order.salesperson))
        )
    ).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    if not bool(getattr(o, "is_finished", False)):
        raise HTTPException(status_code=400, detail="Order is already unfinished")

    await _ac_salesperson_in_current_teams_or_403(
        salesperson=getattr(o, "salesperson", None),
        current_team_names=tns,
    )

    o.is_finished = False
    o.is_rebate = False
    o.is_paid = False

    await db.commit()
    return {"ok": True}


class BosStsOut(BaseModel):
    accessKeyId: str
    secretAccessKey: str
    sessionToken: str
    expiration: str
    bosHost: str


@router.get("/bos-sts", response_model=BosStsOut)
@router.get("/orders/bos-sts", response_model=BosStsOut)
async def finance_get_bos_sts(
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, _team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    _ensure_finance_write_access(role_name)
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


async def _ensure_order_finished_for_finance(
    db: AsyncSession,
    order_id: int,
    *,
    current_team_names: Optional[Tuple[str, ...]],
) -> Order:
    o = (
        await db.execute(
            select(Order)
            .where(Order.id == int(order_id))
            .options(selectinload(Order.salesperson))
        )
    ).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")
    if not bool(getattr(o, "is_finished", False)):
        raise HTTPException(status_code=400, detail="Only finished orders can be updated in finance")

    await _ac_salesperson_in_current_teams_or_403(
        salesperson=getattr(o, "salesperson", None),
        current_team_names=current_team_names,
    )

    return o


@router.post("/bos-upload", response_model=BosProxyUploadOut)
@router.post("/orders/bos-upload", response_model=BosProxyUploadOut)
async def finance_bos_upload_proxy(
    order_id: int = Form(...),
    slot_key: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    _ensure_finance_write_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=team_names)

    if not storage.enabled:
        raise HTTPException(status_code=400, detail="BOS 未启用（BOS_ENABLED=false）")

    _ = await _ensure_order_finished_for_finance(db, int(order_id), current_team_names=current_team_names)

    skey = (slot_key or "").strip()
    if skey not in FINANCE_ALLOWED_SLOTS:
        raise HTTPException(status_code=400, detail=f"非法 slot_key（finance 仅允许 related）: {slot_key}")

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


class FinalizeImageIn(BaseModel):
    slot_key: str
    storage_key: str
    md5: str = ""
    size: int = 0
    content_type: Optional[str] = None
    etag: Optional[str] = None
    original_name: Optional[str] = None
    url: Optional[str] = None


class FinanceFinalizeIn(BaseModel):
    order_id: int
    images: List[FinalizeImageIn] = Field(default_factory=list)
    clear_slots: List[str] = Field(default_factory=list)


class FinanceFinalizeOut(BaseModel):
    ok: bool = True
    order_id: int


@router.post("/finalize", response_model=FinanceFinalizeOut)
@router.post("/finalize-upload", response_model=FinanceFinalizeOut)
@router.post("/orders/finalize", response_model=FinanceFinalizeOut)
@router.post("/orders/finalize-upload", response_model=FinanceFinalizeOut)
async def finance_finalize_images(
    payload: FinanceFinalizeIn,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    _ensure_finance_write_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=team_names)

    order_id = int(payload.order_id)
    _ = await _ensure_order_finished_for_finance(db, order_id, current_team_names=current_team_names)

    clear_slots = [str(x or "").strip() for x in (payload.clear_slots or [])]
    clear_slots = [x for x in clear_slots if x]
    for sk in clear_slots:
        if sk not in FINANCE_ALLOWED_SLOTS:
            raise HTTPException(status_code=400, detail=f"非法 clear_slots（finance 仅允许 related）: {sk}")

    by_slot: Dict[str, List[FinalizeImageIn]] = {}
    for im in payload.images or []:
        sk = (im.slot_key or "").strip()
        if sk not in FINANCE_ALLOWED_SLOTS:
            raise HTTPException(status_code=400, detail=f"非法 slot_key（finance 仅允许 related）: {sk}")
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

    for im in normalized_images:
        slot_key = (im.slot_key or "").strip()
        storage_key = (im.storage_key or "").strip().lstrip("/")
        if not storage_key:
            raise HTTPException(status_code=400, detail="storage_key 不能为空")

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

    await db.commit()
    return FinanceFinalizeOut(ok=True, order_id=order_id)


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
