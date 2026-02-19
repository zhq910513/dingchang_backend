# app/api/v1/finance.py
# encoding: utf-8
"""
财务管理（订单维度 / 去兼容版）

- BOS STS：GET /finance/bos-sts（兼容 /finance/orders/bos-sts）
- BOS 代传：POST /finance/bos-upload（兼容 /finance/orders/bos-upload）
- finalize：POST /finance/finalize（兼容 /finance/finalize-upload /finance/orders/finalize /finance/orders/finalize-upload）

补齐：
- /finance/orders 支持 created_date_start / created_date_end（按北京时间过滤 created_at，包含结束日）
- /finance/orders 支持 first_register_date_start / first_register_date_end（按 dynamic_data 常见字段范围过滤，包含结束日）
- 保留旧参数 created_date / first_register_date 兼容（单日）

本轮：
- 团队隔离（支持经理多团队）
- 财务端下拉筛选（只读）：customer-groups / channel-groups / salespersons
- 财务汇总：/finance/orders/summary（按搜索条件全量聚合）
- 稳定导出：/finance/orders/export（一次性导出全部符合条件）
- 修复搜索栏未生效：新增 salesperson_id / plate_no / vin / engine_no / id_number / vehicle_model 筛选（列表/汇总/导出一致）
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Set
from urllib.parse import quote

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
    false as sql_false,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from zoneinfo import ZoneInfo

from app.api.deps import get_current_user_with_role_and_teams
from app.core.constants import (
    ROLE_FINANCE,
    ROLE_MANAGER,
    ROLE_SUPER_ADMIN,
    ROLE_SALES,
    ROLE_MARKET,
    TEAM_NAMES,
)
from app.core.db import get_db, engine
from app.models.order import Order, OrderImage
from app.models.order_info import OrderInfo
from app.models.user import User
from app.models.customer_group import CustomerGroup
from app.models.channel_group import ChannelGroup
from app.models.image_file import ImageFile
from app.models.role import Role
from app.models.user_role import UserRole
from app.schemas.order import OrderOut, OrderInfoOut
from app.schemas.finance import (
    FinanceOrderOut,
    FinanceOrderListResponse,
    FinanceOrderStatusUpdate,
)
from app.services.storage import StorageService
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


def _normalize_team_names(team_names: Tuple[str, ...] | List[str] | None) -> Tuple[str, ...]:
    if not team_names:
        return tuple()
    if isinstance(team_names, tuple):
        arr = [str(x or "").strip() for x in team_names]
    else:
        arr = [str(x or "").strip() for x in team_names]
    arr = [x for x in arr if x]
    return tuple(sorted(set(arr)))


def _current_team_names_or_403(
    *,
    role_name: Optional[str],
    team_names: Tuple[str, ...],
) -> Optional[Tuple[str, ...]]:
    if role_name == ROLE_SUPER_ADMIN:
        return None

    tns = _normalize_team_names(team_names)
    if not tns:
        raise HTTPException(status_code=403, detail="当前账号未绑定团队（team_name/team_names）")

    invalid = [t for t in tns if t not in TEAM_NAMES]
    if invalid:
        raise HTTPException(status_code=403, detail="当前账号团队非法（team_name/team_names）")

    if role_name in (ROLE_FINANCE, ROLE_MARKET):
        if len(tns) != 1:
            raise HTTPException(status_code=403, detail="当前账号团队配置异常：该角色必须且只能属于 1 个团队")
        return (tns[0],)

    if role_name == ROLE_MANAGER:
        return tuple(tns)

    raise HTTPException(status_code=403, detail="No permission")


def _parse_query_team_names(team_name: Optional[str], team_names: Optional[Tuple[str, ...]]) -> Tuple[str, ...]:
    arr: List[str] = []
    s = (team_name or "").strip()
    if s:
        arr.append(s)
    if team_names:
        for x in team_names:
            sx = str(x or "").strip()
            if sx:
                arr.append(sx)
    return _normalize_team_names(arr)


def _effective_team_filter_for_query(
    *,
    role_name: Optional[str],
    current_team_names: Optional[Tuple[str, ...]],
    team_name: Optional[str],
    team_names: Optional[Tuple[str, ...]],
) -> Optional[Tuple[str, ...]]:
    requested = _parse_query_team_names(team_name, team_names)

    if requested:
        invalid = [t for t in requested if t not in TEAM_NAMES]
        if invalid:
            raise HTTPException(status_code=400, detail="team_name/team_names 非法")

    if role_name == ROLE_SUPER_ADMIN:
        return requested or None

    allowed = current_team_names or tuple()
    if not requested:
        return allowed

    eff = tuple(sorted(set(allowed) & set(requested)))
    if not eff:
        raise HTTPException(status_code=403, detail="跨团队筛选被拒绝")

    if role_name in (ROLE_FINANCE, ROLE_MARKET):
        if len(allowed) == 1 and eff != allowed:
            raise HTTPException(status_code=403, detail="跨团队筛选被拒绝")
        return allowed

    if role_name == ROLE_MANAGER:
        return eff

    return allowed


def _user_team_match_expr(teams: Tuple[str, ...]):
    tns = tuple([str(x or "").strip() for x in (teams or ()) if str(x or "").strip()])
    if not tns:
        return sql_false()

    terms = [User.team_name.in_(list(tns))]

    if hasattr(User, "team_names"):
        for t in tns:
            terms.append(User.team_names == t)
            terms.append(User.team_names.like(f"{t},%"))
            terms.append(User.team_names.like(f"%,{t},%"))
            terms.append(User.team_names.like(f"%,{t}"))

    return or_(*terms)


def _order_salesperson_in_teams_expr(team_names: Tuple[str, ...]):
    team_user_ids = select(User.id).where(_user_team_match_expr(team_names))
    return Order.salesperson_id.in_(team_user_ids)


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    """
    与 orders 域 schemas/order.py 的 _fmt_dt 同口径：
    - DB DATETIME 若为 naive：直接格式化输出（禁止无脑 +8）
    - 若被错误贴了 UTC tzinfo（offset=0）：去 tzinfo 再格式化（避免 +8）
    - 其它 aware：兜底转 Asia/Shanghai 再格式化（极少见）
    """
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


def _order_info_out(info: Optional[OrderInfo]) -> Optional[OrderInfoOut]:
    if not info:
        return None
    return OrderInfoOut.from_orm(info)


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
    """
    ✅ MySQL/MariaDB：把 JSON 字段尽量解析成 DATE
    - 兼容：YYYY-MM-DD / YYYY-M-D（STR_TO_DATE 允许）/ YYYYMMDD
    - 解析失败返回 NULL（不会抛错）
    """
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

    # ✅ 优先：MySQL/MariaDB 用 STR_TO_DATE 做稳健解析（兼容 YYYY-M-D / YYYYMMDD）
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

    # 其他方言：保持原有 digits8 字符串比较（默认数据为 YYYYMMDD / YYYY-MM-DD）
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


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


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
    current_team_names = _current_team_names_or_403(role_name=role_name, team_names=team_names)

    stmt = select(CustomerGroup).order_by(CustomerGroup.id.asc())

    if hasattr(CustomerGroup, "deleted_at"):
        stmt = stmt.where(getattr(CustomerGroup, "deleted_at").is_(None))
    if status is not None and hasattr(CustomerGroup, "status"):
        stmt = stmt.where(getattr(CustomerGroup, "status") == int(status))

    if hasattr(CustomerGroup, "team_name") and current_team_names is not None:
        stmt = stmt.where(getattr(CustomerGroup, "team_name").in_(list(current_team_names)))

    rows = (await db.execute(stmt)).scalars().all()
    return OptionListOut(items=[OptionItem(id=int(x.id), group_name=str(_group_code_name(x) or _group_display_name(x) or "")) for x in rows])


@router.get("/channel-groups", response_model=OptionListOut)
async def finance_list_channel_groups(
    status: Optional[int] = Query(None, description="可选：启用状态过滤（若模型有该字段）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    current_team_names = _current_team_names_or_403(role_name=role_name, team_names=team_names)

    stmt = select(ChannelGroup).order_by(ChannelGroup.id.asc())

    if hasattr(ChannelGroup, "deleted_at"):
        stmt = stmt.where(getattr(ChannelGroup, "deleted_at").is_(None))
    if status is not None and hasattr(ChannelGroup, "status"):
        stmt = stmt.where(getattr(ChannelGroup, "status") == int(status))

    if hasattr(ChannelGroup, "team_name") and current_team_names is not None:
        stmt = stmt.where(getattr(ChannelGroup, "team_name").in_(list(current_team_names)))

    rows = (await db.execute(stmt)).scalars().all()
    return OptionListOut(items=[OptionItem(id=int(x.id), group_name=str(_group_code_name(x) or _group_display_name(x) or "")) for x in rows])


@router.get("/salespersons", response_model=SalespersonListOut)
async def finance_list_salespersons(
    status: int = Query(1, description="默认仅返回启用账号；传 0 可查禁用"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    current_team_names = _current_team_names_or_403(role_name=role_name, team_names=team_names)

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
        stmt = stmt.where(_user_team_match_expr(current_team_names))

    rows = (await db.execute(stmt)).all()
    return SalespersonListOut(items=[SalespersonItem(id=int(r.id), username=str(r.username), real_name=r.real_name) for r in rows])


def _split_team_names_any(val) -> List[str]:
    if val is None:
        return []
    if isinstance(val, (list, tuple, set)):
        out = []
        for x in val:
            s = str(x or "").strip()
            if s:
                out.append(s)
        seen = set()
        uniq = []
        for x in out:
            if x in seen:
                continue
            seen.add(x)
            uniq.append(x)
        return uniq
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        for sep in [",", "，", "|", ";", "；", " "]:
            if sep in s:
                parts = [p.strip() for p in s.split(sep)]
                parts = [p for p in parts if p]
                if parts:
                    seen = set()
                    uniq = []
                    for x in parts:
                        if x in seen:
                            continue
                        seen.add(x)
                        uniq.append(x)
                    return uniq
        return [s]
    s2 = str(val or "").strip()
    return [s2] if s2 else []


async def _salesperson_in_current_teams_or_403(
    *,
    salesperson: Optional[User],
    current_team_names: Optional[Tuple[str, ...]],
) -> None:
    if current_team_names is None:
        return
    allowed = set(current_team_names)

    sp = salesperson
    team_name_val = (getattr(sp, "team_name", None) or "").strip() if sp else ""
    team_names_val = _split_team_names_any(getattr(sp, "team_names", None)) if sp else []

    if not team_names_val and team_name_val:
        team_names_val = [team_name_val]

    if not team_names_val:
        raise HTTPException(status_code=403, detail="跨团队访问被拒绝")

    if not (set(team_names_val) & allowed):
        raise HTTPException(status_code=403, detail="跨团队访问被拒绝")


def _pick_manager_id_from_salesperson(sp: Optional[User]) -> Optional[int]:
    if not sp:
        return None
    for key in ("manager_id", "leader_id", "supervisor_id", "parent_id"):
        try:
            v = getattr(sp, key, None)
        except Exception:
            v = None
        if v is None:
            continue
        try:
            iv = int(v)
            if iv > 0:
                return iv
        except Exception:
            continue
    return None


def _pick_manager_name_inline(sp: Optional[User]) -> Optional[str]:
    if not sp:
        return None
    for key in ("manager_name", "leader_name", "supervisor_name", "parent_name"):
        try:
            v = getattr(sp, key, None)
        except Exception:
            v = None
        s = str(v or "").strip()
        if s:
            return s
    return None


async def _load_finance_order_out(
    db: AsyncSession,
    order_id: int,
    *,
    current_team_names: Optional[Tuple[str, ...]],
) -> OrderOut:
    stmt = (
        select(Order)
        .where(Order.id == int(order_id))
        .options(
            selectinload(Order.creator),
            selectinload(Order.salesperson),
            selectinload(Order.customer_group),
            selectinload(Order.channel_group),
            selectinload(Order.order_info),
            selectinload(Order.images).selectinload(OrderImage.image_file),
        )
    )
    o = (await db.execute(stmt)).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    if not bool(getattr(o, "is_finished", False)):
        raise HTTPException(status_code=400, detail="Only finished orders can be viewed in finance")

    await _salesperson_in_current_teams_or_403(
        salesperson=getattr(o, "salesperson", None),
        current_team_names=current_team_names,
    )

    ensure_display_urls_for_order_images(getattr(o, "images", None) or [], storage)

    info = getattr(o, "order_info", None)

    return OrderOut(
        id=o.id,
        created_by=o.created_by,
        salesperson_id=o.salesperson_id,
        customer_group_id=o.customer_group_id,
        channel_group_id=o.channel_group_id,
        is_finished=bool(o.is_finished),
        is_rebate=bool(getattr(o, "is_rebate", False)),
        is_paid=bool(getattr(o, "is_paid", False)),
        dynamic_data=o.dynamic_data or {},
        image_urls=safe_image_urls(o, storage),
        images=getattr(o, "images", None) or [],
        created_at=getattr(o, "created_at", None),
        updated_at=getattr(o, "updated_at", None),
        customer_group_name=_group_code_name(getattr(o, "customer_group", None)),
        channel_group_name=_group_code_name(getattr(o, "channel_group", None)),
        salesperson_name=_user_display_name(getattr(o, "salesperson", None)),
        order_info=_order_info_out(info),
    )


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
    first_register_date: Optional[str] = Query(None, description="初登日期 YYYY-MM-DD（兼容旧参数：模糊/单日）"),
    first_register_date_start: Optional[str] = Query(None, description="初登日期起 YYYY-MM-DD（包含）"),
    first_register_date_end: Optional[str] = Query(None, description="初登日期止 YYYY-MM-DD（包含）"),
    is_paid: Optional[bool] = Query(None, description="是否回款"),
    is_rebate: Optional[bool] = Query(None, description="是否返点"),
    team_name: Optional[str] = Query(None, description="按团队筛选"),
    team_names: Optional[Tuple[str, ...]] = Query(None, description="按多团队筛选（可重复 team_names=xxx）"),
    # ✅ 新增：搜索栏常用字段
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

    current_team_names = _current_team_names_or_403(role_name=role_name, team_names=user_team_names)
    effective_team_names = _effective_team_filter_for_query(
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
            func.coalesce(func.sum(OrderInfo.customer_total), 0).label("receivable"),
            func.coalesce(func.sum(OrderInfo.channel_total), 0).label("payable"),
            func.coalesce(func.sum(OrderInfo.profit), 0).label("profit"),
            func.coalesce(func.sum(OrderInfo.channel_reward), 0).label("channel_reward"),
            func.coalesce(func.sum(OrderInfo.customer_reward), 0).label("customer_reward"),
        )
        .select_from(Order)
        .join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
        .where(Order.is_finished.is_(True))
    )

    if effective_team_names is not None:
        clauses.append(_order_salesperson_in_teams_expr(effective_team_names))

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
        _add_json_fuzzy(clauses, "id_name", owner_name)

    # ✅ 新增：搜索栏字段（dynamic_data 模糊）
    if (plate_no or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_plate_no", "plate_no"], plate_no)
    if (vin or "").strip():
        _add_json_fuzzy_any(clauses, ["vin", "dl_vin"], vin)
    if (engine_no or "").strip():
        _add_json_fuzzy_any(clauses, ["engine_no", "dl_engine_no"], engine_no)
    if (id_number or "").strip():
        _add_json_fuzzy_any(clauses, ["id_number", "dl_id_number"], id_number)
    if (vehicle_model or "").strip():
        _add_json_fuzzy_any(clauses, ["vehicle_model", "dl_vehicle_model"], vehicle_model)

    if first_register_date_start or first_register_date_end:
        _add_json_date_range_any(
            clauses,
            keys=["dl_register_date", "register_date", "first_register_date"],
            start_ymd=first_register_date_start,
            end_ymd=first_register_date_end,
            err_prefix="first_register_date",
        )
    elif (first_register_date or "").strip():
        v = (first_register_date or "").strip()
        v2 = v.replace("-", "")
        _add_json_fuzzy(clauses, "dl_register_date", v)
        _add_json_fuzzy(clauses, "dl_register_date", v2)
        _add_json_fuzzy(clauses, "register_date", v)
        _add_json_fuzzy(clauses, "first_register_date", v)

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


@router.get("/orders", response_model=FinanceOrderListResponse)
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
    first_register_date: Optional[str] = Query(None, description="初登日期 YYYY-MM-DD（兼容旧参数：模糊/单日）"),
    first_register_date_start: Optional[str] = Query(None, description="初登日期起 YYYY-MM-DD（包含）"),
    first_register_date_end: Optional[str] = Query(None, description="初登日期止 YYYY-MM-DD（包含）"),
    is_paid: Optional[bool] = Query(None, description="是否回款"),
    is_rebate: Optional[bool] = Query(None, description="是否返点"),
    team_name: Optional[str] = Query(None, description="按团队筛选"),
    team_names: Optional[Tuple[str, ...]] = Query(None, description="按多团队筛选（可重复 team_names=xxx）"),
    # ✅ 新增：搜索栏常用字段
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

    current_team_names = _current_team_names_or_403(role_name=role_name, team_names=user_team_names)
    effective_team_names = _effective_team_filter_for_query(
        role_name=role_name,
        current_team_names=current_team_names,
        team_name=team_name,
        team_names=team_names,
    )

    stmt = (
        select(Order)
        .where(Order.is_finished.is_(True))
        .options(
            selectinload(Order.salesperson),
            selectinload(Order.customer_group),
            selectinload(Order.channel_group),
            selectinload(Order.order_info),
        )
    )
    count_stmt = select(func.count(Order.id)).where(Order.is_finished.is_(True))

    clauses: list = []

    if effective_team_names is not None:
        clauses.append(_order_salesperson_in_teams_expr(effective_team_names))

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
        _add_json_fuzzy(clauses, "id_name", owner_name)

    # ✅ 新增：搜索栏字段（dynamic_data 模糊）
    if (plate_no or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_plate_no", "plate_no"], plate_no)
    if (vin or "").strip():
        _add_json_fuzzy_any(clauses, ["vin", "dl_vin"], vin)
    if (engine_no or "").strip():
        _add_json_fuzzy_any(clauses, ["engine_no", "dl_engine_no"], engine_no)
    if (id_number or "").strip():
        _add_json_fuzzy_any(clauses, ["id_number", "dl_id_number"], id_number)
    if (vehicle_model or "").strip():
        _add_json_fuzzy_any(clauses, ["vehicle_model", "dl_vehicle_model"], vehicle_model)

    if first_register_date_start or first_register_date_end:
        _add_json_date_range_any(
            clauses,
            keys=["dl_register_date", "register_date", "first_register_date"],
            start_ymd=first_register_date_start,
            end_ymd=first_register_date_end,
            err_prefix="first_register_date",
        )
    elif (first_register_date or "").strip():
        v = (first_register_date or "").strip()
        v2 = v.replace("-", "")
        _add_json_fuzzy(clauses, "dl_register_date", v)
        _add_json_fuzzy(clauses, "dl_register_date", v2)
        _add_json_fuzzy(clauses, "register_date", v)
        _add_json_fuzzy(clauses, "first_register_date", v)

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

        mid = _pick_manager_id_from_salesperson(sp)
        if mid:
            salesperson_to_manager_id[int(getattr(sp, "id", 0) or 0)] = mid
            manager_ids.add(mid)

    managers_by_id: Dict[int, User] = {}
    if manager_ids:
        mgr_rows = (await db.execute(select(User).where(User.id.in_(list(manager_ids))))).scalars().all()
        managers_by_id = {int(getattr(u, "id", 0) or 0): u for u in mgr_rows if getattr(u, "id", None) is not None}

    items: List[FinanceOrderOut] = []
    for o in rows:
        cg = getattr(o, "customer_group", None)
        ch = getattr(o, "channel_group", None)
        sp = getattr(o, "salesperson", None)
        info = getattr(o, "order_info", None)

        dd = getattr(o, "dynamic_data", None) or {}

        plate_no_val = (dd.get("dl_plate_no") or dd.get("plate_no") or "") if isinstance(dd, dict) else ""
        vin_val = (dd.get("vin") or dd.get("dl_vin") or "") if isinstance(dd, dict) else ""
        engine_no_val = (dd.get("engine_no") or dd.get("dl_engine_no") or "") if isinstance(dd, dict) else ""
        vehicle_model_val = (dd.get("vehicle_model") or "") if isinstance(dd, dict) else ""

        raw_id_number = dd.get("id_number") if isinstance(dd, dict) else ""
        if raw_id_number is None and isinstance(dd, dict):
            raw_id_number = dd.get("dl_id_number")
        id_number_val = (str(raw_id_number).strip() if raw_id_number is not None else "")

        owner = (dd.get("id_name") or "") if isinstance(dd, dict) else ""

        # ✅ 修复：初登日期展示与筛选/导出一致，按多个key兜底
        first_register_val = ""
        if isinstance(dd, dict):
            first_register_val = _extract_dd(dd, "dl_register_date", "register_date", "first_register_date")

        team_name_val = None
        team_names_val: List[str] = []
        if sp:
            team_name_val = (getattr(sp, "team_name", None) or None)
            team_names_val = _split_team_names_any(getattr(sp, "team_names", None))
            if not team_names_val and team_name_val:
                team_names_val = [str(team_name_val).strip()] if str(team_name_val).strip() else []

        manager_id_val = None
        manager_name_val = None
        if sp:
            inline_name = _pick_manager_name_inline(sp)
            if inline_name:
                manager_name_val = inline_name

            sp_id_int = int(getattr(sp, "id", 0) or 0)
            mid = salesperson_to_manager_id.get(sp_id_int) or _pick_manager_id_from_salesperson(sp)
            if mid:
                manager_id_val = int(mid)
                if not manager_name_val:
                    manager_name_val = _user_display_name(managers_by_id.get(int(mid)))

        # ✅ 新增：订单备注（order_info.remark），仅用于列表/详情展示；导出不包含
        remark_val = None
        if info is not None:
            rv = getattr(info, "remark", None)
            rs = str(rv or "").strip()
            remark_val = rs or None

        items.append(
            FinanceOrderOut(
                id=int(o.id),
                col_01_date=_fmt_dt(getattr(o, "created_at", None)),
                col_02_channel=_group_code_name(ch),
                col_03_customer=_group_code_name(cg),
                col_04_market=getattr(cg, "market", None) if cg else None,
                col_05_owner=owner or None,
                col_06_plate_no=plate_no_val or None,
                col_07_insurance_expire_date=(
                    info.insurance_expire_date.strftime("%Y-%m-%d")
                    if info and getattr(info, "insurance_expire_date", None)
                    else None
                ),
                col_08_vin=vin_val or None,
                col_09_engine_no=engine_no_val or None,
                col_10_vehicle_model=vehicle_model_val or None,
                col_11_first_register_date=(first_register_val or None),
                col_12_id_number=(id_number_val or None),
                col_13_owner_phone=(
                    str(getattr(info, "owner_phone", None))
                    if info and getattr(info, "owner_phone", None) is not None
                    else None
                ),
                col_14_commercial_amount=_to_float(getattr(info, "commercial_amount", None)) if info else None,
                col_15_compulsory_amount=_to_float(getattr(info, "compulsory_amount", None)) if info else None,
                col_16_tax_amount=_to_float(getattr(info, "vehicle_tax_amount", None)) if info else None,
                col_17_noncar_amount=_to_float(getattr(info, "non_vehicle_amount", None)) if info else None,
                col_18_ch_commercial_point=_to_float(getattr(info, "channel_commercial_point", None)) if info else None,
                col_19_ch_compulsory_point=_to_float(getattr(info, "channel_compulsory_point", None)) if info else None,
                col_20_ch_tax_point=_to_float(getattr(info, "channel_vehicle_tax_point", None)) if info else None,
                col_21_ch_noncar_point=_to_float(getattr(info, "channel_non_vehicle_point", None)) if info else None,
                col_22_cu_commercial_point=_to_float(getattr(info, "customer_commercial_point", None)) if info else None,
                col_23_cu_compulsory_point=_to_float(getattr(info, "customer_compulsory_point", None)) if info else None,
                col_24_cu_tax_point=_to_float(getattr(info, "customer_vehicle_tax_point", None)) if info else None,
                col_25_cu_noncar_point=_to_float(getattr(info, "customer_non_vehicle_point", None)) if info else None,
                # ✅ 修复：与 summary/export 统一口径：应收=customer_total，应付=channel_total
                col_26_receivable=_to_float(getattr(info, "customer_total", None)) if info else None,
                col_27_payable=_to_float(getattr(info, "channel_total", None)) if info else None,
                col_28_profit=_to_float(getattr(info, "profit", None)) if info else None,
                col_29_is_paid=bool(getattr(o, "is_paid", False)),
                col_30_is_rebate=bool(getattr(o, "is_rebate", False)),
                col_31_channel_reward=_to_float(getattr(info, "channel_reward", None)) if info else None,
                col_32_customer_reward=_to_float(getattr(info, "customer_reward", None)) if info else None,
                # ✅ 新增：备注（列表展示）
                remark=remark_val,
                customer_group_id=getattr(o, "customer_group_id", None),
                channel_group_id=getattr(o, "channel_group_id", None),
                salesperson_id=getattr(o, "salesperson_id", None),
                salesperson_name=_user_display_name(sp),
                manager_id=manager_id_val,
                manager_name=manager_name_val,
                team_name=(str(team_name_val).strip() if team_name_val is not None and str(team_name_val).strip() else None),
                team_names=team_names_val,
                dynamic_data=dd,
                created_at=_fmt_dt(getattr(o, "created_at", None)),
                updated_at=_fmt_dt(getattr(o, "updated_at", None)),
            )
        )

    return FinanceOrderListResponse(total=int(total or 0), items=items)


# ✅ 修复：FastAPI 路由参数不要写 {order_id:int}，用函数参数类型约束即可
@router.get("/orders/{order_id}", response_model=OrderOut)
async def get_finance_order_detail(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _current_user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    tns = _current_team_names_or_403(role_name=role_name, team_names=team_names)
    return await _load_finance_order_out(db, order_id, current_team_names=tns)


@router.patch("/orders/{order_id}/status")
async def update_finance_order_status(
    order_id: int,
    payload: FinanceOrderStatusUpdate,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _current_user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    _ensure_finance_write_access(role_name)
    tns = _current_team_names_or_403(role_name=role_name, team_names=team_names)

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

    await _salesperson_in_current_teams_or_403(
        salesperson=getattr(o, "salesperson", None),
        current_team_names=tns,
    )

    if payload.is_rebate is not None:
        o.is_rebate = bool(payload.is_rebate)
    if payload.is_paid is not None:
        o.is_paid = bool(payload.is_paid)

    await db.commit()
    return {"ok": True}


@router.post("/orders/{order_id}/return")
async def return_finance_order_to_unfinished(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _current_user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    _ensure_finance_write_access(role_name)
    tns = _current_team_names_or_403(role_name=role_name, team_names=team_names)

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

    await _salesperson_in_current_teams_or_403(
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

    await _salesperson_in_current_teams_or_403(
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

    current_team_names = _current_team_names_or_403(role_name=role_name, team_names=team_names)

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

    current_team_names = _current_team_names_or_403(role_name=role_name, team_names=team_names)

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
    first_register_date: Optional[str] = Query(None, description="初登日期 YYYY-MM-DD（兼容旧参数：模糊/单日）"),
    first_register_date_start: Optional[str] = Query(None, description="初登日期起 YYYY-MM-DD（包含）"),
    first_register_date_end: Optional[str] = Query(None, description="初登日期止 YYYY-MM-DD（包含）"),
    is_paid: Optional[bool] = Query(None, description="是否回款"),
    is_rebate: Optional[bool] = Query(None, description="是否返点"),
    team_name: Optional[str] = Query(None, description="按团队筛选"),
    team_names: Optional[Tuple[str, ...]] = Query(None, description="按多团队筛选（可重复 team_names=xxx）"),
    # ✅ 新增：搜索栏常用字段
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

    current_team_names = _current_team_names_or_403(role_name=role_name, team_names=user_team_names)
    effective_team_names = _effective_team_filter_for_query(
        role_name=role_name,
        current_team_names=current_team_names,
        team_name=team_name,
        team_names=team_names,
    )

    clauses: list = [Order.is_finished.is_(True)]

    if effective_team_names is not None:
        clauses.append(_order_salesperson_in_teams_expr(effective_team_names))

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
        _add_json_fuzzy(clauses, "id_name", owner_name)

    # ✅ 新增：搜索栏字段（dynamic_data 模糊）
    if (plate_no or "").strip():
        _add_json_fuzzy_any(clauses, ["dl_plate_no", "plate_no"], plate_no)
    if (vin or "").strip():
        _add_json_fuzzy_any(clauses, ["vin", "dl_vin"], vin)
    if (engine_no or "").strip():
        _add_json_fuzzy_any(clauses, ["engine_no", "dl_engine_no"], engine_no)
    if (id_number or "").strip():
        _add_json_fuzzy_any(clauses, ["id_number", "dl_id_number"], id_number)
    if (vehicle_model or "").strip():
        _add_json_fuzzy_any(clauses, ["vehicle_model", "dl_vehicle_model"], vehicle_model)

    if first_register_date_start or first_register_date_end:
        _add_json_date_range_any(
            clauses,
            keys=["dl_register_date", "register_date", "first_register_date"],
            start_ymd=first_register_date_start,
            end_ymd=first_register_date_end,
            err_prefix="first_register_date",
        )
    elif (first_register_date or "").strip():
        v = (first_register_date or "").strip()
        v2 = v.replace("-", "")
        _add_json_fuzzy(clauses, "dl_register_date", v)
        _add_json_fuzzy(clauses, "dl_register_date", v2)
        _add_json_fuzzy(clauses, "register_date", v)
        _add_json_fuzzy(clauses, "first_register_date", v)

    if (insurance_expire_date or "").strip():
        d = _parse_ymd(insurance_expire_date)
        if not d:
            raise HTTPException(status_code=400, detail="insurance_expire_date must be YYYY-MM-DD")
        clauses.append(OrderInfo.insurance_expire_date == d.date())
        need_join_info = True

    headers = [
        "日期",
        "渠道",
        "客户",
        "市场",
        "业务员",
        "车主",
        "车牌",
        "保险到期日",
        "车架号",
        "发动机号",
        "车型",
        "初登日期",
        "身份证号",
        "电话",
        "商业金额",
        "交强金额",
        "车船税金额",
        "非车金额",
        "渠道商业点位",
        "渠道商业后补点位",
        "渠道交强点位",
        "渠道车船税点位",
        "渠道非车点位",
        "渠道奖励",
        "客户商业点位",
        "客户商业后补点位",
        "客户交强点位",
        "客户车船税点位",
        "客户非车点位",
        "客户奖励",
        "应收",
        "应付",
        "利润",
        # ✅ 调整：所属团队在所属经理前
        "所属团队",
        "所属经理",
        "是否回款",
        "是否返点",
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
                mid = _pick_manager_id_from_salesperson(sp)
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

                created_at = _fmt_dt(getattr(o, "created_at", None)) or "-"
                channel_name = _group_code_name(ch) or "-"
                customer_name = _group_code_name(cg) or "-"
                market_val = (getattr(cg, "market", None) if cg else None) or "-"
                salesperson_name = _user_display_name(sp) or "-"

                owner = _extract_dd(dd, "id_name")
                plate = _extract_dd(dd, "dl_plate_no", "plate_no")
                vin_val = _extract_dd(dd, "vin", "dl_vin")
                engine_no_val = _extract_dd(dd, "engine_no", "dl_engine_no")
                vehicle_model_val = _extract_dd(dd, "vehicle_model", "dl_vehicle_model")
                first_register = _extract_dd(dd, "dl_register_date", "register_date", "first_register_date")
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

                # ✅ 与 summary/list/export 统一口径：应收=customer_total，应付=channel_total
                receivable = _fmt_money_export(getattr(info, "customer_total", None) if info else None)
                payable = _fmt_money_export(getattr(info, "channel_total", None) if info else None)
                profit = _fmt_money_export(getattr(info, "profit", None) if info else None)

                manager_name = "-"
                if sp:
                    inline_name = _pick_manager_name_inline(sp)
                    if inline_name:
                        manager_name = inline_name
                    else:
                        sid = int(getattr(sp, "id", 0) or 0)
                        mid = salesperson_to_manager_id.get(sid) or _pick_manager_id_from_salesperson(sp)
                        if mid:
                            manager_name = _user_display_name(managers_by_id.get(int(mid))) or "-"

                team_names_val = _split_team_names_any(getattr(sp, "team_names", None)) if sp else []
                team_name_val = str(getattr(sp, "team_name", None) or "").strip() if sp else ""
                team_display = _join_teams_export(team_names_val, team_name_val or None)

                paid = "是" if bool(getattr(o, "is_paid", False)) else "否"
                rebate = "是" if bool(getattr(o, "is_rebate", False)) else "否"

                cols = [
                    created_at,
                    channel_name,
                    customer_name,
                    market_val,
                    salesperson_name,
                    owner or "-",
                    plate or "-",
                    insurance_expire,
                    vin_val or "-",
                    engine_no_val or "-",
                    vehicle_model_val or "-",
                    first_register or "-",
                    id_number_val or "-",
                    phone or "-",
                    cm,
                    jq,
                    tax,
                    nc,
                    ch_cm_p,
                    ch_cm_sup_p,
                    ch_jq_p,
                    ch_tax_p,
                    ch_nc_p,
                    ch_reward,
                    cu_cm_p,
                    cu_cm_sup_p,
                    cu_jq_p,
                    cu_tax_p,
                    cu_nc_p,
                    cu_reward,
                    receivable,
                    payable,
                    profit,
                    # ✅ 调整：团队在经理前
                    team_display,
                    manager_name,
                    paid,
                    rebate,
                ]

                # ✅ Excel 文本保护列（避免科学计数/去前导0）
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
