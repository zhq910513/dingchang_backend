# app/api/v1/finance.py
# encoding: utf-8
"""
财务管理（订单维度 / 去兼容版）

✅ 新增：
- BOS STS：GET /finance/bos-sts（兼容 /finance/orders/bos-sts）
- BOS 代传：POST /finance/bos-upload（兼容 /finance/orders/bos-upload）
- finalize：POST /finance/finalize（兼容 /finance/finalize-upload /finance/orders/finalize /finance/orders/finalize-upload）

✅ 本次补齐：
- /finance/orders 支持 created_date_start / created_date_end（按北京时间过滤 created_at，包含结束日）
- /finance/orders 支持 first_register_date_start / first_register_date_end（按 dynamic_data 常见字段范围过滤，包含结束日）
- 保留旧参数 created_date / first_register_date 兼容（单日）

✅ 本轮新增：团队隔离（支持经理多团队）
- super_admin：可查看全部
- manager：可查看“自己多选团队”下所有数据（按订单 salesperson.team_name）
- finance：只能查看自己 team_name 下的数据（单团队）
- market：只能查看自己 team_name 下的数据（单团队，只读）

✅ 本轮继续补齐（链路打通）：
- 财务端下拉筛选（只读）：
  - GET /finance/customer-groups
  - GET /finance/channel-groups
  - GET /finance/salespersons

✅ 本轮新增（财务汇总）：
- GET /finance/orders/summary：按“搜索条件”在数据库全量聚合（不受分页影响）

⚠️ 权限与范围：
- finance/manager/super_admin：可读可写（写含：paid/rebate、备用图 related）
- market：只读（不可改 paid/rebate，不可上传/维护图片，不可拿 sts）
- 仅允许操作已完成订单（财务域）
- 仅允许操作 slot_key = related（备用图）
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple

import anyio
import requests
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy import func, select, and_, or_, cast, String, delete, distinct
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

# ✅ finance 只允许动备用图
FINANCE_ALLOWED_SLOTS = {"related"}
MULTI_SLOTS = {"related"}


def _ensure_finance_access(role_name: Optional[str]) -> None:
    """
    ✅ finance 域读权限：
    - super_admin / manager / finance / market：允许（market 只读）
    - sales：禁止
    - 其它：禁止
    """
    if role_name == ROLE_SALES:
        raise HTTPException(status_code=403, detail="Sales has no permission to access finance")
    if role_name not in (ROLE_FINANCE, ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_MARKET):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_finance_write_access(role_name: Optional[str]) -> None:
    """
    ✅ finance 域写权限（编辑权限保持原样）：
    - 仅 finance / manager / super_admin
    - market：只读，禁止
    """
    if role_name not in (ROLE_FINANCE, ROLE_MANAGER, ROLE_SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="No permission")


def _normalize_team_names(team_names: Tuple[str, ...] | List[str] | None) -> Tuple[str, ...]:
    if not team_names:
        return tuple()
    if isinstance(team_names, tuple):
        arr = [str(x or "").strip() for x in team_names]
    else:
        arr = [str(x or "").strip() for x in team_names]
    arr = [x for x in arr if x]
    # 去重+稳定排序
    return tuple(sorted(set(arr)))


def _current_team_names_or_403(
    *,
    role_name: Optional[str],
    team_names: Tuple[str, ...],
) -> Optional[Tuple[str, ...]]:
    """
    super_admin：None（表示不限制）
    manager：允许多团队（>=1），返回 tuple(team_names...）
    finance：必须单团队（==1），返回 tuple(team_name,）
    market：必须单团队（==1），返回 tuple(team_name,）  ✅ 本轮新增：market 只读
    """
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

    # 其它角色已在 _ensure_finance_access 拦截，这里防御
    raise HTTPException(status_code=403, detail="No permission")


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")


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
    """
    返回 dynamic_data[key] 的“文本表达式”。

    ✅ 关键修复：
    - MySQL/MariaDB：优先用 JSON_UNQUOTE(JSON_EXTRACT())，避免 cast 后带双引号
    - Postgres：优先走 json path 的 as_string / astext
    - 其它：尽量用 json_extract + cast
    """
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
    """
    统一把字符串表达式“去引号+去空白”。
    """
    try:
        expr = _json_text(col, key)
        expr = func.trim(expr)
        expr = func.replace(expr, '"', "")
        return expr
    except Exception:
        return _json_text(col, key)


def _digits8_expr(expr):
    """
    把日期字符串归一化为 YYYYMMDD 的前 8 位数字：
    - 2025-11-13 -> 20251113
    - 20251113 -> 20251113
    - 2025-11-13T00:00:00 -> 20251113
    """
    e = func.replace(expr, "-", "")
    e = func.replace(e, "/", "")
    e = func.replace(e, ".", "")
    e = func.replace(e, " ", "")
    return func.substr(e, 1, 8)


def _add_json_fuzzy(clauses: list, key: str, value: Optional[str]):
    """
    ✅ 修正点：
    - 用 _json_text_unquoted() 再 lower，避免部分方言/驱动返回带引号导致 like 命中不稳定
    """
    v = (value or "").strip()
    if not v:
        return
    expr = func.lower(_json_text_unquoted(Order.dynamic_data, key))
    clauses.append(expr.like(f"%{v.lower()}%"))


def _parse_bj_date_range(ymd: str) -> Optional[Tuple[datetime, datetime]]:
    s = (ymd or "").strip()
    if not s:
        return None
    try:
        bj_start = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
        bj_end = bj_start + timedelta(days=1)
        return bj_start.astimezone(timezone.utc), bj_end.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_bj_date_span(start_ymd: str, end_ymd: str) -> Optional[Tuple[datetime, datetime]]:
    """
    start/end: YYYY-MM-DD（北京时间）
    返回：[start_utc, end_utc_exclusive)，其中 end 为“包含 end 当天”
    """
    s0 = (start_ymd or "").strip()
    e0 = (end_ymd or "").strip()
    if not s0 or not e0:
        return None
    try:
        bj_start = datetime.strptime(s0, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
        bj_end_inclusive = datetime.strptime(e0, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
        if bj_end_inclusive < bj_start:
            return None
        bj_end_exclusive = bj_end_inclusive + timedelta(days=1)
        return bj_start.astimezone(timezone.utc), bj_end_exclusive.astimezone(timezone.utc)
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


def _add_json_date_range_any(
    clauses: list,
    *,
    keys: List[str],
    start_ymd: Optional[str],
    end_ymd: Optional[str],
    err_prefix: str,
):
    """
    在 Order.dynamic_data 中按多个 key 任选其一命中区间。
    ✅ 把两端都归一化成 YYYYMMDD（8位数字）再比较，兼容：
       - dl_register_date: 20251113
       - register_date / first_register_date: 2025-11-13
    注意：包含 end_ymd。
    """
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


# ===========================
# ✅ 财务端筛选下拉（只读）
# ===========================
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

    # ✅ 若 customer_group 具备 team_name 字段，则按团队隔离；否则不强行猜字段
    if hasattr(CustomerGroup, "team_name") and current_team_names is not None:
        stmt = stmt.where(getattr(CustomerGroup, "team_name").in_(list(current_team_names)))

    rows = (await db.execute(stmt)).scalars().all()
    return OptionListOut(items=[OptionItem(id=int(x.id), group_name=str(_group_display_name(x) or "")) for x in rows])


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

    # ✅ channel_group 一般有 team_name：严格按团队隔离
    if hasattr(ChannelGroup, "team_name") and current_team_names is not None:
        stmt = stmt.where(getattr(ChannelGroup, "team_name").in_(list(current_team_names)))

    rows = (await db.execute(stmt)).scalars().all()
    return OptionListOut(items=[OptionItem(id=int(x.id), group_name=str(_group_display_name(x) or "")) for x in rows])


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

    # ✅ 团队隔离：super_admin 全量；manager 多团队；finance/market 单团队
    if current_team_names is not None:
        stmt = stmt.where(User.team_name.in_(list(current_team_names)))

    rows = (await db.execute(stmt)).all()
    return SalespersonListOut(items=[SalespersonItem(id=int(r.id), username=str(r.username), real_name=r.real_name) for r in rows])


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

    # ✅ 团队隔离：按订单 salesperson.team_name（支持经理多团队）
    if current_team_names is not None:
        sp = getattr(o, "salesperson", None)
        sp_tn = (getattr(sp, "team_name", None) or "").strip() if sp else ""
        if not sp_tn or sp_tn not in set(current_team_names):
            raise HTTPException(status_code=403, detail="跨团队访问被拒绝")

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
        customer_group_name=_group_display_name(getattr(o, "customer_group", None)),
        channel_group_name=_group_display_name(getattr(o, "channel_group", None)),
        salesperson_name=_user_display_name(getattr(o, "salesperson", None)),
        order_info=_order_info_out(info),
    )


# ===========================
# ✅ 财务汇总（数据库全量聚合）
# ===========================
class FinanceOrdersSummaryOut(BaseModel):
    commercial_amount: float = 0.0
    compulsory_amount: float = 0.0
    vehicle_tax_amount: float = 0.0
    noncar_amount: float = 0.0
    receivable: float = 0.0
    payable: float = 0.0
    profit: float = 0.0


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
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _current_user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    current_team_names = _current_team_names_or_403(role_name=role_name, team_names=team_names)

    clauses: list = []

    # ✅ 基础范围：仅已完成订单
    stmt = (
        select(
            func.coalesce(func.sum(OrderInfo.commercial_amount), 0).label("commercial_amount"),
            func.coalesce(func.sum(OrderInfo.compulsory_amount), 0).label("compulsory_amount"),
            func.coalesce(func.sum(OrderInfo.vehicle_tax_amount), 0).label("vehicle_tax_amount"),
            func.coalesce(func.sum(OrderInfo.non_vehicle_amount), 0).label("noncar_amount"),
            func.coalesce(func.sum(OrderInfo.customer_total), 0).label("receivable"),
            func.coalesce(func.sum(OrderInfo.channel_total), 0).label("payable"),
            func.coalesce(func.sum(OrderInfo.profit), 0).label("profit"),
        )
        .select_from(Order)
        .join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
        .where(Order.is_finished.is_(True))
    )

    # ✅ 团队隔离：按 salesperson.team_name（支持经理多团队）
    if current_team_names is not None:
        stmt = stmt.join(User, User.id == Order.salesperson_id)
        clauses.append(User.team_name.in_(list(current_team_names)))

    if order_id is not None:
        clauses.append(Order.id == int(order_id))

    if channel_group_id is not None:
        clauses.append(Order.channel_group_id == int(channel_group_id))
    if customer_group_id is not None:
        clauses.append(Order.customer_group_id == int(customer_group_id))

    # ✅ created_at：支持 start/end（优先）；兼容 created_date 单日
    if created_date_start or created_date_end:
        if not created_date_start or not created_date_end:
            raise HTTPException(status_code=400, detail="created_date_start and created_date_end are required")
        rng = _parse_bj_date_span(created_date_start, created_date_end)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date_* must be YYYY-MM-DD and end>=start")
        start_utc, end_utc = rng
        clauses.append(Order.created_at >= start_utc)
        clauses.append(Order.created_at < end_utc)
    elif created_date:
        rng = _parse_bj_date_range(created_date)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date must be YYYY-MM-DD")
        start_utc, end_utc = rng
        clauses.append(Order.created_at >= start_utc)
        clauses.append(Order.created_at < end_utc)

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

    # ✅ 初登日期：first_register_date 对应 dl_register_date（主字段）
    if first_register_date_start or first_register_date_end:
        _add_json_date_range_any(
            clauses,
            keys=["dl_register_date", "register_date", "first_register_date"],
            start_ymd=first_register_date_start,
            end_ymd=first_register_date_end,
            err_prefix="first_register_date",
        )
    elif (first_register_date or "").strip():
        # ✅ 兼容旧逻辑：模糊（例如传 "2025-01"），也兼容传 "202501"
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
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    current_user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)

    current_team_names = _current_team_names_or_403(role_name=role_name, team_names=team_names)

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

    # ✅ 团队隔离：按 salesperson.team_name（支持经理多团队）
    if current_team_names is not None:
        stmt = stmt.join(User, User.id == Order.salesperson_id)
        count_stmt = count_stmt.join(User, User.id == Order.salesperson_id)
        clauses.append(User.team_name.in_(list(current_team_names)))

    if order_id is not None:
        clauses.append(Order.id == int(order_id))

    if channel_group_id is not None:
        clauses.append(Order.channel_group_id == int(channel_group_id))
    if customer_group_id is not None:
        clauses.append(Order.customer_group_id == int(customer_group_id))

    # ✅ created_at：支持 start/end（优先）；兼容 created_date 单日
    if created_date_start or created_date_end:
        if not created_date_start or not created_date_end:
            raise HTTPException(status_code=400, detail="created_date_start and created_date_end are required")
        rng = _parse_bj_date_span(created_date_start, created_date_end)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date_* must be YYYY-MM-DD and end>=start")
        start_utc, end_utc = rng
        clauses.append(Order.created_at >= start_utc)
        clauses.append(Order.created_at < end_utc)
    elif created_date:
        rng = _parse_bj_date_range(created_date)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date must be YYYY-MM-DD")
        start_utc, end_utc = rng
        clauses.append(Order.created_at >= start_utc)
        clauses.append(Order.created_at < end_utc)

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

    # ✅ 初登日期：first_register_date 对应 dl_register_date（主字段）
    if first_register_date_start or first_register_date_end:
        _add_json_date_range_any(
            clauses,
            keys=["dl_register_date", "register_date", "first_register_date"],
            start_ymd=first_register_date_start,
            end_ymd=first_register_date_end,
            err_prefix="first_register_date",
        )
    elif (first_register_date or "").strip():
        # ✅ 兼容旧逻辑：模糊（例如传 "2025-01"），也兼容传 "202501"
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

    items: List[FinanceOrderOut] = []
    for o in rows:
        cg = getattr(o, "customer_group", None)
        ch = getattr(o, "channel_group", None)
        sp = getattr(o, "salesperson", None)
        info = getattr(o, "order_info", None)

        dd = getattr(o, "dynamic_data", None) or {}

        plate_no = (dd.get("dl_plate_no") or dd.get("plate_no") or "") if isinstance(dd, dict) else ""
        vin = (dd.get("vin") or dd.get("dl_vin") or "") if isinstance(dd, dict) else ""
        engine_no = (dd.get("engine_no") or dd.get("dl_engine_no") or "") if isinstance(dd, dict) else ""
        vehicle_model = (dd.get("vehicle_model") or "") if isinstance(dd, dict) else ""
        id_number = (dd.get("id_number") or "") if isinstance(dd, dict) else ""
        owner = (dd.get("id_name") or "") if isinstance(dd, dict) else ""

        items.append(
            FinanceOrderOut(
                id=int(o.id),
                col_01_date=_fmt_dt(getattr(o, "created_at", None)),
                col_02_channel=_group_display_name(ch),
                col_03_customer=_group_display_name(cg),
                col_04_market=getattr(cg, "market", None) if cg else None,
                col_05_owner=owner or None,
                col_06_plate_no=plate_no or None,
                col_07_insurance_expire_date=(
                    info.insurance_expire_date.strftime("%Y-%m-%d")
                    if info and getattr(info, "insurance_expire_date", None)
                    else None
                ),
                col_08_vin=vin or None,
                col_09_engine_no=engine_no or None,
                col_10_vehicle_model=vehicle_model or None,
                col_11_first_register_date=(dd.get("dl_register_date") if isinstance(dd, dict) else None),
                col_12_id_number=id_number or None,
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
                col_26_receivable=_to_float(getattr(info, "customer_total", None)) if info else None,
                col_27_payable=_to_float(getattr(info, "channel_total", None)) if info else None,
                col_28_profit=_to_float(getattr(info, "profit", None)) if info else None,
                col_29_is_paid=bool(getattr(o, "is_paid", False)),
                col_30_is_rebate=bool(getattr(o, "is_rebate", False)),
                customer_group_id=getattr(o, "customer_group_id", None),
                channel_group_id=getattr(o, "channel_group_id", None),
                salesperson_id=getattr(o, "salesperson_id", None),
                salesperson_name=_user_display_name(sp),
                dynamic_data=dd,
                created_at=_fmt_dt(getattr(o, "created_at", None)),
                updated_at=_fmt_dt(getattr(o, "updated_at", None)),
            )
        )

    return FinanceOrderListResponse(total=int(total or 0), items=items)


@router.get("/orders/{order_id:int}", response_model=OrderOut)
async def get_finance_order_detail(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _current_user, role_name, team_names, _team_ids = user_with_role
    _ensure_finance_access(role_name)
    tns = _current_team_names_or_403(role_name=role_name, team_names=team_names)
    return await _load_finance_order_out(db, order_id, current_team_names=tns)


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

    if tns is not None:
        sp = getattr(o, "salesperson", None)
        sp_tn = (getattr(sp, "team_name", None) or "").strip() if sp else ""
        if not sp_tn or sp_tn not in set(tns):
            raise HTTPException(status_code=403, detail="跨团队访问被拒绝")

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

    if tns is not None:
        sp = getattr(o, "salesperson", None)
        sp_tn = (getattr(sp, "team_name", None) or "").strip() if sp else ""
        if not sp_tn or sp_tn not in set(tns):
            raise HTTPException(status_code=403, detail="跨团队访问被拒绝")

    o.is_finished = False
    o.is_rebate = False
    o.is_paid = False

    await db.commit()
    return {"ok": True}


# ===========================
# ✅ BOS STS + 代传 + finalize（finance 专用：仅 related）
# ===========================
class BosStsOut(BaseModel):
    accessKeyId: str
    secretAccessKey: str
    sessionToken: str
    expiration: str
    bosHost: str


@router.get("/bos-sts", response_model=BosStsOut)
@router.get("/orders/bos-sts", response_model=BosStsOut)  # 兼容旧前端
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

    # ✅ 团队隔离：按订单 salesperson.team_name（支持经理多团队）
    if current_team_names is not None:
        sp = getattr(o, "salesperson", None)
        sp_tn = (getattr(sp, "team_name", None) or "").strip() if sp else ""
        if not sp_tn or sp_tn not in set(current_team_names):
            raise HTTPException(status_code=403, detail="跨团队访问被拒绝")

    return o


@router.post("/bos-upload", response_model=BosProxyUploadOut)
@router.post("/orders/bos-upload", response_model=BosProxyUploadOut)  # 兼容旧前端
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
@router.post("/finalize-upload", response_model=FinanceFinalizeOut)  # 兼容旧前端
@router.post("/orders/finalize", response_model=FinanceFinalizeOut)  # 兼容旧前端
@router.post("/orders/finalize-upload", response_model=FinanceFinalizeOut)  # 兼容旧前端
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
