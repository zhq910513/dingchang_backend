# app/api/v1/finance.py
# encoding: utf-8

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select, and_, or_, cast, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user_with_role_and_teams, CurrentUserContext
from app.core.access_control import (
    current_team_names_or_403 as _ac_current_team_names_or_403,
    effective_team_filter_for_query as _ac_effective_team_filter_for_query,
    order_salesperson_in_teams_expr as _ac_order_salesperson_in_teams_expr,
    salesperson_in_current_teams_or_403 as _ac_salesperson_in_current_teams_or_403,
)
from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_MARKET, ROLE_SALES
from app.core.db import get_db, engine
from app.models.customer_group import CustomerGroup
from app.models.order import Order, OrderImage
from app.models.order_info import OrderInfo
from app.models.user import User
from app.schemas.finance import FinanceOrderStatusUpdate
from app.schemas.order import OrderOut
from app.services.order_read_model import to_order_out as _rm_to_order_out
from app.services.storage import StorageService

router = APIRouter(prefix="/finance", tags=["finance"])
storage = StorageService()


class FinanceOrdersSummaryOut(BaseModel):
    commercial_amount: float = 0.0
    compulsory_amount: float = 0.0
    vehicle_tax_amount: float = 0.0
    non_vehicle_amount: float = 0.0
    channel_reward: float = 0.0
    customer_reward: float = 0.0
    receivable: float = 0.0
    payable: float = 0.0
    profit: float = 0.0


def _ensure_finance_access(role_name: Optional[str]) -> None:
    if role_name == ROLE_SALES:
        raise HTTPException(status_code=403, detail="Sales has no permission to access finance")
    if role_name not in (ROLE_FINANCE, ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_MARKET):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_finance_write_access(role_name: Optional[str]) -> None:
    if role_name not in (ROLE_FINANCE, ROLE_MANAGER, ROLE_SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="No permission")


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


def _parse_bj_date_range(ymd: str):
    s = (ymd or "").strip()
    if not s:
        return None
    try:
        bj_start = datetime.strptime(s, "%Y-%m-%d")
        bj_end = bj_start + timedelta(days=1)
        return bj_start, bj_end
    except Exception:
        return None


def _parse_bj_date_span(start_ymd: str, end_ymd: str):
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


async def _load_finance_order(
        db: AsyncSession,
        order_id: int,
        *,
        current_team_names: Optional[Tuple[str, ...]],
) -> Order:
    stmt = (
        select(Order)
        .where(Order.id == int(order_id))
        .options(
            selectinload(Order.salesperson).selectinload(User.parent),
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
        raise HTTPException(status_code=400, detail="Only finished orders can be accessed in finance")

    await _ac_salesperson_in_current_teams_or_403(
        salesperson=getattr(o, "salesperson", None),
        current_team_names=current_team_names,
    )
    return o


def _build_finance_filter_clauses(
        *,
        team_name: Optional[str],
        team_names: Optional[Tuple[str, ...]],
        current_team_names: Optional[Tuple[str, ...]],
        role_name: Optional[str],
        created_date: Optional[str],
        created_date_start: Optional[str],
        created_date_end: Optional[str],
        channel_group_id: Optional[int],
        customer_group_id: Optional[int],
        market: Optional[str],
        owner_name: Optional[str],
        insurance_expire_date: Optional[str],
        first_register_date_start: Optional[str],
        first_register_date_end: Optional[str],
        is_paid: Optional[bool],
        is_rebate: Optional[bool],
) -> Tuple[list, bool, bool]:
    effective_team_names = _ac_effective_team_filter_for_query(
        role_name=role_name,
        current_team_names=current_team_names,
        team_name=team_name,
        team_names=team_names,
    )

    clauses: list = [Order.is_finished.is_(True)]
    need_join_customer = False
    need_join_info = False

    if effective_team_names is not None:
        clauses.append(_ac_order_salesperson_in_teams_expr(effective_team_names))

    if channel_group_id is not None:
        clauses.append(Order.channel_group_id == int(channel_group_id))
    if customer_group_id is not None:
        clauses.append(Order.customer_group_id == int(customer_group_id))
    if is_paid is not None:
        clauses.append(Order.is_paid.is_(bool(is_paid)))
    if is_rebate is not None:
        clauses.append(Order.is_rebate.is_(bool(is_rebate)))

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

    if (market or "").strip():
        need_join_customer = True
        mk = (market or "").strip().lower()
        clauses.append(func.lower(CustomerGroup.market).like(f"%{mk}%"))

    if (owner_name or "").strip():
        _add_json_fuzzy(clauses, "owner_name", owner_name)

    _add_json_date_range_any(
        clauses,
        keys=["first_register_date"],
        start_ymd=first_register_date_start,
        end_ymd=first_register_date_end,
        err_prefix="first_register_date",
    )

    if (insurance_expire_date or "").strip():
        d = _parse_ymd(insurance_expire_date)
        if not d:
            raise HTTPException(status_code=400, detail="insurance_expire_date must be YYYY-MM-DD")
        need_join_info = True
        clauses.append(OrderInfo.insurance_expire_date == d)

    return clauses, need_join_customer, need_join_info


@router.get("/orders/summary", response_model=FinanceOrdersSummaryOut)
async def finance_orders_summary(
        created_date: Optional[str] = Query(None),
        created_date_start: Optional[str] = Query(None),
        created_date_end: Optional[str] = Query(None),
        channel_group_id: Optional[int] = Query(None),
        customer_group_id: Optional[int] = Query(None),
        market: Optional[str] = Query(None),
        owner_name: Optional[str] = Query(None),
        insurance_expire_date: Optional[str] = Query(None),
        first_register_date_start: Optional[str] = Query(None),
        first_register_date_end: Optional[str] = Query(None),
        is_paid: Optional[bool] = Query(None),
        is_rebate: Optional[bool] = Query(None),
        team_name: Optional[str] = Query(None),
        team_names: Optional[Tuple[str, ...]] = Query(None),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
) -> FinanceOrdersSummaryOut:
    role_name = ctx.primary_role or ""
    _ensure_finance_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=tuple(ctx.team_names or ()))
    clauses, need_join_customer, need_join_info = _build_finance_filter_clauses(
        team_name=team_name,
        team_names=team_names,
        current_team_names=current_team_names,
        role_name=role_name,
        created_date=created_date,
        created_date_start=created_date_start,
        created_date_end=created_date_end,
        channel_group_id=channel_group_id,
        customer_group_id=customer_group_id,
        market=market,
        owner_name=owner_name,
        insurance_expire_date=insurance_expire_date,
        first_register_date_start=first_register_date_start,
        first_register_date_end=first_register_date_end,
        is_paid=is_paid,
        is_rebate=is_rebate,
    )

    stmt = (
        select(
            func.coalesce(func.sum(OrderInfo.commercial_amount), 0).label("commercial_amount"),
            func.coalesce(func.sum(OrderInfo.compulsory_amount), 0).label("compulsory_amount"),
            func.coalesce(func.sum(OrderInfo.vehicle_tax_amount), 0).label("vehicle_tax_amount"),
            func.coalesce(func.sum(OrderInfo.non_vehicle_amount), 0).label("non_vehicle_amount"),
            func.coalesce(func.sum(OrderInfo.channel_reward), 0).label("channel_reward"),
            func.coalesce(func.sum(OrderInfo.customer_reward), 0).label("customer_reward"),
            func.coalesce(func.sum(OrderInfo.channel_total), 0).label("receivable"),
            func.coalesce(func.sum(OrderInfo.customer_total), 0).label("payable"),
            func.coalesce(func.sum(OrderInfo.profit), 0).label("profit"),
        )
        .select_from(Order)
        .join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
    )

    if need_join_customer:
        stmt = stmt.join(CustomerGroup, CustomerGroup.id == Order.customer_group_id, isouter=True)

    if clauses:
        stmt = stmt.where(and_(*clauses))

    row = (await db.execute(stmt)).mappings().first() or {}
    return FinanceOrdersSummaryOut(
        commercial_amount=float(row.get("commercial_amount") or 0),
        compulsory_amount=float(row.get("compulsory_amount") or 0),
        vehicle_tax_amount=float(row.get("vehicle_tax_amount") or 0),
        non_vehicle_amount=float(row.get("non_vehicle_amount") or 0),
        channel_reward=float(row.get("channel_reward") or 0),
        customer_reward=float(row.get("customer_reward") or 0),
        receivable=float(row.get("receivable") or 0),
        payable=float(row.get("payable") or 0),
        profit=float(row.get("profit") or 0),
    )


@router.get("/orders/export")
async def export_finance_orders(
        created_date: Optional[str] = Query(None),
        created_date_start: Optional[str] = Query(None),
        created_date_end: Optional[str] = Query(None),
        channel_group_id: Optional[int] = Query(None),
        customer_group_id: Optional[int] = Query(None),
        market: Optional[str] = Query(None),
        owner_name: Optional[str] = Query(None),
        insurance_expire_date: Optional[str] = Query(None),
        first_register_date_start: Optional[str] = Query(None),
        first_register_date_end: Optional[str] = Query(None),
        is_paid: Optional[bool] = Query(None),
        is_rebate: Optional[bool] = Query(None),
        team_name: Optional[str] = Query(None),
        team_names: Optional[Tuple[str, ...]] = Query(None),
        ids: Optional[Tuple[int, ...]] = Query(None),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    role_name = ctx.primary_role or ""
    _ensure_finance_access(role_name)
    _ensure_finance_write_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=tuple(ctx.team_names or ()))
    clauses, need_join_customer, need_join_info = _build_finance_filter_clauses(
        team_name=team_name,
        team_names=team_names,
        current_team_names=current_team_names,
        role_name=role_name,
        created_date=created_date,
        created_date_start=created_date_start,
        created_date_end=created_date_end,
        channel_group_id=channel_group_id,
        customer_group_id=customer_group_id,
        market=market,
        owner_name=owner_name,
        insurance_expire_date=insurance_expire_date,
        first_register_date_start=first_register_date_start,
        first_register_date_end=first_register_date_end,
        is_paid=is_paid,
        is_rebate=is_rebate,
    )

    if ids:
        valid_ids = [int(x) for x in ids if int(x) > 0]
        if valid_ids:
            clauses.append(Order.id.in_(valid_ids))

    stmt = (
        select(Order)
        .options(
            selectinload(Order.salesperson).selectinload(User.parent),
            selectinload(Order.customer_group),
            selectinload(Order.channel_group),
            selectinload(Order.order_info),
        )
    )
    if need_join_customer:
        stmt = stmt.join(CustomerGroup, CustomerGroup.id == Order.customer_group_id, isouter=True)
    if need_join_info:
        stmt = stmt.join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)

    stmt = stmt.where(and_(*clauses)).order_by(Order.id.desc())
    rows = (await db.execute(stmt)).scalars().all()

    def esc(s: str) -> str:
        return (
            str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    def money(v) -> str:
        try:
            return f"{float(v):.2f}"
        except Exception:
            return "-"

    def group_code_name(g, kind: str) -> str:
        if not g:
            return "-"
        if kind == "channel":
            code = str(getattr(g, "channel_code", "") or "").strip()
            name = str(getattr(g, "channel_name", "") or "").strip()
        else:
            code = str(getattr(g, "customer_code", "") or "").strip()
            name = str(getattr(g, "customer_name", "") or "").strip()

        if code and name:
            return f"{code} - {name}"
        if name:
            return name
        if code:
            return code
        return "-"

    def td_html(value, *, force_text: bool = False) -> str:
        text = esc(str(value if value is not None else ""))
        if force_text:
            return f"<td style=\"mso-number-format:'\\@';\">{text}</td>"
        return f"<td>{text}</td>"

    def build_row_html(cells: List[str]) -> str:
        # 强制按文本展示，避免 Excel 自动科学计数/精度丢失
        # 0 日期
        # 6 车牌
        # 7 保险到期日
        # 8 车架号
        # 9 发动机号
        # 11 初登日期
        # 12 身份证号
        # 13 电话
        text_indexes = {0, 6, 7, 8, 9, 11, 12, 13}
        tds = []
        for idx, val in enumerate(cells):
            tds.append(td_html(val, force_text=idx in text_indexes))
        return "<tr>" + "".join(tds) + "</tr>"

    headers = [
        "日期", "渠道", "客户", "市场", "业务员", "车主", "车牌", "保险到期日", "车架号", "发动机号",
        "车型", "初登日期", "身份证号", "电话", "商业金额", "交强金额", "车船税金额", "非车金额",
        "渠道商业点位", "渠道商业后补点位", "渠道交强点位", "渠道车船税点位", "渠道非车点位", "渠道奖励",
        "客户商业点位", "客户商业后补点位", "客户交强点位", "客户车船税点位", "客户非车点位", "客户奖励",
        "应收", "应付", "利润", "所属经理", "所属团队", "是否回款", "是否返点",
    ]

    html = [
        "<!DOCTYPE html><html><head><meta charset='utf-8' /></head><body><table border='1'><thead><tr>",
        *[f"<th>{esc(h)}</th>" for h in headers],
        "</tr></thead><tbody>",
    ]

    for o in rows:
        dd = getattr(o, "dynamic_data", None) or {}
        info = getattr(o, "order_info", None)
        sp = getattr(o, "salesperson", None)
        cg = getattr(o, "customer_group", None)
        ch = getattr(o, "channel_group", None)

        manager_name = None
        if sp and getattr(sp, "parent", None):
            manager_name = getattr(sp.parent, "real_name", None) or getattr(sp.parent, "username", None)

        team_names_val = []
        raw_teams = str(getattr(sp, "team_names", "") or "").strip()
        if raw_teams:
            team_names_val = [x.strip() for x in raw_teams.split(",") if x.strip()]
        if not team_names_val and getattr(sp, "team_name", None):
            team_names_val = [str(getattr(sp, "team_name")).strip()]
        team_display = "、".join(team_names_val) if team_names_val else "-"

        row = [
            str(getattr(o, "created_at", None) or "-")[:10],
            group_code_name(ch, "channel"),
            group_code_name(cg, "customer"),
            getattr(cg, "market", None) or "-",
            (getattr(sp, "real_name", None) or getattr(sp, "username", None) or "-"),
            dd.get("owner_name") or "-",
            dd.get("plate_no") or "-",
            str(getattr(info, "insurance_expire_date", None) or "-"),
            dd.get("vin") or "-",
            dd.get("engine_no") or "-",
            dd.get("vehicle_model") or "-",
            dd.get("first_register_date") or "-",
            dd.get("id_number") or "-",
            getattr(info, "owner_phone", None) or "-",
            money(getattr(info, "commercial_amount", None)),
            money(getattr(info, "compulsory_amount", None)),
            money(getattr(info, "vehicle_tax_amount", None)),
            money(getattr(info, "non_vehicle_amount", None)),
            str(getattr(info, "channel_commercial_point", None) or "-"),
            str(getattr(info, "channel_commercial_supplement_point", None) or "-"),
            str(getattr(info, "channel_compulsory_point", None) or "-"),
            str(getattr(info, "channel_vehicle_tax_point", None) or "-"),
            str(getattr(info, "channel_non_vehicle_point", None) or "-"),
            money(getattr(info, "channel_reward", None)),
            str(getattr(info, "customer_commercial_point", None) or "-"),
            str(getattr(info, "customer_commercial_supplement_point", None) or "-"),
            str(getattr(info, "customer_compulsory_point", None) or "-"),
            str(getattr(info, "customer_vehicle_tax_point", None) or "-"),
            str(getattr(info, "customer_non_vehicle_point", None) or "-"),
            money(getattr(info, "customer_reward", None)),
            money(getattr(info, "channel_total", None)),
            money(getattr(info, "customer_total", None)),
            money(getattr(info, "profit", None)),
            manager_name or "-",
            team_display,
            "是" if bool(getattr(o, "is_paid", False)) else "否",
            "是" if bool(getattr(o, "is_rebate", False)) else "否",
        ]
        html.append(build_row_html(row))

    html.append("</tbody></table></body></html>")
    body = "".join(html).encode("utf-8")
    filename = f"finance_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xls"
    return StreamingResponse(
        iter([body]),
        media_type="application/vnd.ms-excel; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/orders/{order_id}", response_model=OrderOut)
async def get_finance_order_detail(
        order_id: int,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
) -> OrderOut:
    role_name = ctx.primary_role or ""
    _ensure_finance_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=tuple(ctx.team_names or ()))
    o = await _load_finance_order(db, int(order_id), current_team_names=current_team_names)
    images_by_order_id: Dict[int, List[OrderImage]] = {int(o.id): list(getattr(o, "images", None) or [])}
    return _rm_to_order_out(o, storage=storage, images_by_order_id=images_by_order_id)


@router.patch("/orders/{order_id}/status")
async def update_finance_order_status(
        order_id: int,
        payload: FinanceOrderStatusUpdate,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    role_name = ctx.primary_role or ""
    _ensure_finance_access(role_name)
    _ensure_finance_write_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=tuple(ctx.team_names or ()))
    o = await _load_finance_order(db, int(order_id), current_team_names=current_team_names)

    if payload.is_paid is not None:
        o.is_paid = bool(payload.is_paid)
    if payload.is_rebate is not None:
        o.is_rebate = bool(payload.is_rebate)

    await db.commit()
    return {"ok": True}


@router.post("/orders/{order_id}/return")
async def return_finance_order_to_unfinished(
        order_id: int,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    role_name = ctx.primary_role or ""
    _ensure_finance_access(role_name)
    _ensure_finance_write_access(role_name)

    current_team_names = _ac_current_team_names_or_403(role_name=role_name, team_names=tuple(ctx.team_names or ()))
    o = await _load_finance_order(db, int(order_id), current_team_names=current_team_names)

    o.is_finished = False
    o.is_paid = False
    o.is_rebate = False

    await db.commit()
    return {"ok": True}
