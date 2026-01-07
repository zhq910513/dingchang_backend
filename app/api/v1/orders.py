# app/api/v1/orders.py
# encoding: utf-8
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Any, Dict, List, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import anyio
import requests
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy import func, select, and_, or_, cast, String, distinct, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user_with_role_and_teams
from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_SALES, ROLE_MARKET, TEAM_NAMES
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
)
from app.services.storage import StorageService
from app.utils.order_image_urls import ensure_display_urls_for_order_images, safe_image_urls

router = APIRouter(prefix="/orders", tags=["orders"])
storage = StorageService()

BJ_TZ = ZoneInfo("Asia/Shanghai")

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


def _ensure_orders_access(role_name: Optional[str], *, allow_finance: bool = False) -> None:
    """
    ✅ orders 模块访问控制（读）：
    - super_admin / manager / finance / market / sales：允许访问（读）
    - 其它角色：禁止
    说明：
    - “编辑权限保持原样”由 _ensure_orders_write_access + 各写入 ACL 负责
    - allow_finance 参数保留兼容历史调用（不再用于限制 finance 读）
    """
    rn = role_name or ""
    if rn not in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_FINANCE, ROLE_MARKET, ROLE_SALES):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_orders_write_access(role_name: Optional[str]) -> None:
    """
    订单写入口（创建/编辑/状态/上传流程等）：
    - finance：禁止（仅 related 图片维护例外，由单独接口/校验放行）
    - market：只读，禁止
    """
    if role_name in (ROLE_FINANCE, ROLE_MARKET):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_finance_related_only_slot(slot_key: str) -> None:
    if str(slot_key or "").strip() != "related":
        raise HTTPException(status_code=403, detail="Finance can only operate related images")


def _ensure_finance_finalize_payload_related_only(payload) -> None:
    """
    finance 通过 /orders/finalize 仅允许维护 related 图片：
    - 禁止修改任何订单字段（dynamic_data / customer/channel/salesperson / order_info）
    - images/clear_slots 仅允许 related
    """
    # 禁止携带订单字段修改
    if getattr(payload, "salesperson_id", None) is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update salesperson_id in orders.finalize")
    if getattr(payload, "customer_group_id", None) is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update customer_group_id in orders.finalize")
    if getattr(payload, "channel_group_id", None) is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update channel_group_id in orders.finalize")

    # ✅ finance 禁止携带 order_info
    if getattr(payload, "order_info", None) is not None:
        raise HTTPException(status_code=403, detail="Finance cannot update order_info in orders.finalize")

    dyn = getattr(payload, "dynamic_data", None) or {}
    if isinstance(dyn, dict) and len(dyn) > 0:
        raise HTTPException(status_code=403, detail="Finance cannot update dynamic_data in orders.finalize")

    # clear_slots 仅允许 related
    clear_slots = [str(x or "").strip() for x in (getattr(payload, "clear_slots", None) or [])]
    clear_slots = [x for x in clear_slots if x]
    for sk in clear_slots:
        if sk != "related":
            raise HTTPException(status_code=403, detail="Finance can only clear related slot")

    # images 仅允许 related
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


def _to_decimal(v: Any) -> Decimal:
    if v is None or v == "":
        return Decimal("0")
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _ensure_non_negative(v: Decimal, field: str) -> None:
    if v < 0:
        raise HTTPException(status_code=400, detail=f"{field} must be >= 0")


def _recalc_order_info(info: OrderInfo) -> None:
    """
    ✅ 对齐前端 OrderDetail.vue 规则：
    - premium_total = commercial_amount + compulsory_amount + vehicle_tax_amount + non_vehicle_amount
    - 渠道合计/客户合计：商业部分 = commercial_amount * (商业点位% + 商业后补%)/100
    - commercial_after_amount 若历史存在：不再参与合计计算（避免与“点位后补”双口径冲突）
    """
    cm = _to_decimal(getattr(info, "commercial_amount", 0))
    ca = _to_decimal(getattr(info, "compulsory_amount", 0))
    vta = _to_decimal(getattr(info, "vehicle_tax_amount", 0))
    nva = _to_decimal(getattr(info, "non_vehicle_amount", 0))

    _ensure_non_negative(cm, "commercial_amount")
    _ensure_non_negative(ca, "compulsory_amount")
    _ensure_non_negative(vta, "vehicle_tax_amount")
    _ensure_non_negative(nva, "non_vehicle_amount")

    premium_total = cm + ca + vta + nva
    info.premium_total = premium_total

    ch_cm_p = _to_decimal(getattr(info, "channel_commercial_point", 0))
    ch_cm_supp_p = _to_decimal(getattr(info, "channel_commercial_supplement_point", 0))  # ✅ 新增
    ch_ca_p = _to_decimal(getattr(info, "channel_compulsory_point", 0))
    ch_vta_p = _to_decimal(getattr(info, "channel_vehicle_tax_point", 0))
    ch_nva_p = _to_decimal(getattr(info, "channel_non_vehicle_point", 0))
    ch_reward = _to_decimal(getattr(info, "channel_reward", 0))

    channel_total = (
        (cm * ch_cm_p / Decimal("100"))
        + (cm * ch_cm_supp_p / Decimal("100"))  # ✅ 新增：商业后补点位
        + (ca * ch_ca_p / Decimal("100"))
        + (vta * ch_vta_p / Decimal("100"))
        + (nva * ch_nva_p / Decimal("100"))
        + ch_reward
    )
    info.channel_total = channel_total

    cu_cm_p = _to_decimal(getattr(info, "customer_commercial_point", 0))
    cu_cm_supp_p = _to_decimal(getattr(info, "customer_commercial_supplement_point", 0))  # ✅ 新增
    cu_ca_p = _to_decimal(getattr(info, "customer_compulsory_point", 0))
    cu_vta_p = _to_decimal(getattr(info, "customer_vehicle_tax_point", 0))
    cu_nva_p = _to_decimal(getattr(info, "customer_non_vehicle_point", 0))
    cu_reward = _to_decimal(getattr(info, "customer_reward", 0))

    customer_total = (
        (cm * cu_cm_p / Decimal("100"))
        + (cm * cu_cm_supp_p / Decimal("100"))  # ✅ 新增：商业后补点位
        + (ca * cu_ca_p / Decimal("100"))
        + (vta * cu_vta_p / Decimal("100"))
        + (nva * cu_nva_p / Decimal("100"))
        + cu_reward
    )
    info.customer_total = customer_total

    info.profit = channel_total - customer_total


def _apply_order_info_patch(info: OrderInfo, payload: OrderInfoIn) -> None:
    if payload is None:
        return

    if payload.insurance_expire_date is not None:
        info.insurance_expire_date = payload.insurance_expire_date

    if payload.owner_phone is not None:
        info.owner_phone = str(payload.owner_phone or "").strip()

    if payload.commercial_amount is not None:
        info.commercial_amount = _to_decimal(payload.commercial_amount)

    # ✅ 兼容旧字段（若 schema/model 仍存在），但不再参与合计计算
    if getattr(payload, "commercial_after_amount", None) is not None:
        info.commercial_after_amount = _to_decimal(getattr(payload, "commercial_after_amount"))

    if payload.compulsory_amount is not None:
        info.compulsory_amount = _to_decimal(payload.compulsory_amount)
    if payload.vehicle_tax_amount is not None:
        info.vehicle_tax_amount = _to_decimal(payload.vehicle_tax_amount)
    if payload.non_vehicle_amount is not None:
        info.non_vehicle_amount = _to_decimal(payload.non_vehicle_amount)

    if payload.channel_commercial_point is not None:
        info.channel_commercial_point = _to_decimal(payload.channel_commercial_point)

    # ✅ 新增：渠道-商业后补点位
    if getattr(payload, "channel_commercial_supplement_point", None) is not None:
        info.channel_commercial_supplement_point = _to_decimal(getattr(payload, "channel_commercial_supplement_point"))

    if payload.channel_compulsory_point is not None:
        info.channel_compulsory_point = _to_decimal(payload.channel_compulsory_point)
    if payload.channel_vehicle_tax_point is not None:
        info.channel_vehicle_tax_point = _to_decimal(payload.channel_vehicle_tax_point)
    if payload.channel_non_vehicle_point is not None:
        info.channel_non_vehicle_point = _to_decimal(payload.channel_non_vehicle_point)
    if payload.channel_reward is not None:
        info.channel_reward = _to_decimal(payload.channel_reward)

    if payload.customer_commercial_point is not None:
        info.customer_commercial_point = _to_decimal(payload.customer_commercial_point)

    # ✅ 新增：客户-商业后补点位
    if getattr(payload, "customer_commercial_supplement_point", None) is not None:
        info.customer_commercial_supplement_point = _to_decimal(getattr(payload, "customer_commercial_supplement_point"))

    if payload.customer_compulsory_point is not None:
        info.customer_compulsory_point = _to_decimal(payload.customer_compulsory_point)
    if payload.customer_vehicle_tax_point is not None:
        info.customer_vehicle_tax_point = _to_decimal(payload.customer_vehicle_tax_point)
    if payload.customer_non_vehicle_point is not None:
        info.customer_non_vehicle_point = _to_decimal(payload.customer_non_vehicle_point)
    if payload.customer_reward is not None:
        info.customer_reward = _to_decimal(payload.customer_reward)

    _recalc_order_info(info)


def _order_info_out(info: Optional[OrderInfo]) -> Optional[OrderInfoOut]:
    if not info:
        return None
    return OrderInfoOut.from_orm(info)


# ===========================
# ✅ 团队/角色 ACL（orders 域）
# ===========================
def _normalize_team_names(team_names: Optional[Tuple[str, ...] | List[str]]) -> Tuple[str, ...]:
    if not team_names:
        return tuple()
    if isinstance(team_names, tuple):
        return tuple([str(x or "").strip() for x in team_names if str(x or "").strip()])
    return tuple([str(x or "").strip() for x in (team_names or []) if str(x or "").strip()])


def _require_team_for_non_super_admin(role_name: Optional[str], team_names: Tuple[str, ...]) -> None:
    if role_name == ROLE_SUPER_ADMIN:
        return
    tns = _normalize_team_names(team_names)
    if not tns:
        raise HTTPException(status_code=400, detail="当前账号未配置团队，无法访问该模块")
    invalid = [t for t in tns if t not in TEAM_NAMES]
    if invalid:
        raise HTTPException(status_code=403, detail="当前账号团队非法（team_name）")


def _require_single_team_for_strict_roles(role_name: Optional[str], team_names: Tuple[str, ...]) -> str:
    """
    ✅ 严格单团队角色：业务/财务/市场
    - sales：本轮调整为“只能看自己的数据”，但仍要求账号团队配置为 1 个（数据治理一致性）
    - finance/market：单团队（按 team_name 共享查看）
    经理：允许多团队
    """
    _require_team_for_non_super_admin(role_name, team_names)
    rn = role_name or ""
    tns = _normalize_team_names(team_names)
    if rn in (ROLE_SALES, ROLE_FINANCE, ROLE_MARKET):
        if len(tns) != 1:
            raise HTTPException(status_code=400, detail="当前账号团队配置异常：该角色必须且只能属于 1 个团队")
        return tns[0]
    # manager/super_admin：不强制单团队，这里返回稳定的第一个仅供需要字符串的地方
    return tns[0] if tns else ""


async def _get_user_team_name(db: AsyncSession, user_id: int) -> str:
    tn = (await db.execute(select(User.team_name).where(User.id == int(user_id)))).scalars().first()
    return str(tn or "").strip()


async def _ensure_order_read_acl_by_salesperson_id(
    db: AsyncSession,
    *,
    salesperson_id: int,
    current_user: User,
    role_name: Optional[str],
    team_names: Tuple[str, ...],
) -> None:
    """
    ✅ 读权限（列表/详情）：
    - super_admin：全量
    - manager：可读自己 team_names 范围内全部订单（按 salesperson.team_name）
    - finance/market：可读自己 team_name 下全部订单
    - sales：只能读自己的订单（本轮调整）
    """
    rn = role_name or ""
    if rn == ROLE_SUPER_ADMIN:
        return

    _require_team_for_non_super_admin(role_name, team_names)
    tns = _normalize_team_names(team_names)

    if rn == ROLE_SALES:
        if int(salesperson_id) != int(current_user.id):
            raise HTTPException(status_code=403, detail="No permission")
        return

    # 允许的团队集合
    if rn == ROLE_MANAGER:
        allowed_teams = set(tns)
    elif rn in (ROLE_MARKET, ROLE_FINANCE):
        my_team = _require_single_team_for_strict_roles(role_name, tns)
        allowed_teams = {my_team}
    else:
        raise HTTPException(status_code=403, detail="No permission")

    sp_team = await _get_user_team_name(db, int(salesperson_id))
    if not sp_team or sp_team not in allowed_teams:
        raise HTTPException(status_code=403, detail="No permission")


async def _ensure_order_write_acl_by_salesperson_id(
    db: AsyncSession,
    *,
    salesperson_id: int,
    current_user: User,
    role_name: Optional[str],
    team_names: Tuple[str, ...],
) -> None:
    """
    ✅ 写权限（创建/编辑/状态/上传等）：在“读权限团队共享”的基础上收紧
    - sales：只能写自己的订单
    - manager：可写自己 team_names 范围内订单（不看 parent_id）
    - market/finance：写入口外层已拦截；这里保留防御
    """
    rn = role_name or ""
    if rn == ROLE_SUPER_ADMIN:
        return

    _require_team_for_non_super_admin(role_name, team_names)
    tns = _normalize_team_names(team_names)

    if rn == ROLE_SALES:
        if int(salesperson_id) != int(current_user.id):
            raise HTTPException(status_code=403, detail="No permission")
        return

    if rn == ROLE_MANAGER:
        sp_team = await _get_user_team_name(db, int(salesperson_id))
        if not sp_team or sp_team not in set(tns):
            raise HTTPException(status_code=403, detail="No permission")
        return

    if rn in (ROLE_MARKET, ROLE_FINANCE):
        my_team = _require_single_team_for_strict_roles(role_name, tns)
        sp_team = await _get_user_team_name(db, int(salesperson_id))
        if not sp_team or sp_team != my_team:
            raise HTTPException(status_code=403, detail="No permission")
        return

    raise HTTPException(status_code=403, detail="No permission")


async def _apply_orders_list_acl(
    db: AsyncSession,
    *,
    current_user: User,
    role_name: Optional[str],
    team_names: Tuple[str, ...],
    clauses: List,
) -> None:
    """
    ✅ 列表 ACL（团队共享 / sales 自己）：
    - super_admin：全量
    - manager：team_names 范围（按 salesperson.team_name）
    - finance/market：单团队范围
    - sales：只能看自己（本轮调整）
    """
    rn = role_name or ""
    if rn == ROLE_SUPER_ADMIN:
        return

    _require_team_for_non_super_admin(role_name, team_names)
    tns = _normalize_team_names(team_names)

    if rn == ROLE_SALES:
        clauses.append(Order.salesperson_id == int(current_user.id))
        return

    if rn == ROLE_MANAGER:
        team_user_ids = select(User.id).where(User.team_name.in_(list(tns)))
        clauses.append(Order.salesperson_id.in_(team_user_ids))
        return

    if rn in (ROLE_MARKET, ROLE_FINANCE):
        my_team = _require_single_team_for_strict_roles(role_name, tns)
        team_user_ids = select(User.id).where(User.team_name == my_team)
        clauses.append(Order.salesperson_id.in_(team_user_ids))
        return

    raise HTTPException(status_code=403, detail="No permission")


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

    # ✅ 读 ACL：sales 自己；manager/finance/market 按 team_name
    await _ensure_order_read_acl_by_salesperson_id(
        db,
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        current_user=current_user,
        role_name=role_name,
        team_names=team_names,
    )

    ensure_display_urls_for_order_images(getattr(o, "images", None) or [], storage)

    cg = getattr(o, "customer_group", None)
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
        customer_group_name=_group_display_name(cg),
        channel_group_name=_group_display_name(getattr(o, "channel_group", None)),
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

    # ✅ savepoint：避免 md5 unique / storage_key unique 冲突把外层事务一起回滚
    try:
        async with db.begin_nested():
            await db.flush()
        return obj
    except IntegrityError:
        # nested 已回滚到 savepoint，可继续查询
        obj2 = (await db.execute(select(ImageFile).where(ImageFile.storage_key == storage_key))).scalar_one_or_none()
        if obj2:
            return obj2

        if md5:
            obj3 = (await db.execute(select(ImageFile).where(ImageFile.md5 == md5))).scalar_one_or_none()
            if obj3:
                # 兜底合并（避免空字段丢失）
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


# ===========================
# 下拉：客户群 / 渠道群 / 业务员
# ===========================
class OptionItem(BaseModel):
    id: int
    group_name: str


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
    # ✅ customer/channel 是否需要团队过滤，取决于模型字段，当前未提供，不能猜
    _require_team_for_non_super_admin(role_name, _normalize_team_names(team_names))

    stmt = select(CustomerGroup).order_by(CustomerGroup.id.asc())
    if hasattr(CustomerGroup, "deleted_at"):
        stmt = stmt.where(getattr(CustomerGroup, "deleted_at").is_(None))
    if status is not None and hasattr(CustomerGroup, "status"):
        stmt = stmt.where(getattr(CustomerGroup, "status") == int(status))

    rows = (await db.execute(stmt)).scalars().all()
    return OptionListOut(items=[OptionItem(id=int(x.id), group_name=str(_group_display_name(x) or "")) for x in rows])


@router.get("/channel-groups", response_model=OptionListOut)
async def list_channel_groups(
    status: Optional[int] = Query(None, description="可选：启用状态过滤（若模型有该字段）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _user, role_name, team_names, _team_ids = user_with_role
    _ensure_orders_access(role_name)
    _require_team_for_non_super_admin(role_name, _normalize_team_names(team_names))

    tns = _normalize_team_names(team_names)

    stmt = select(ChannelGroup).order_by(ChannelGroup.id.asc())
    if hasattr(ChannelGroup, "deleted_at"):
        stmt = stmt.where(getattr(ChannelGroup, "deleted_at").is_(None))
    if status is not None and hasattr(ChannelGroup, "status"):
        stmt = stmt.where(getattr(ChannelGroup, "status") == int(status))

    # ✅ 团队隔离：ChannelGroup 已有 team_name 字段，必须过滤
    if hasattr(ChannelGroup, "team_name"):
        if role_name == ROLE_SUPER_ADMIN:
            pass
        elif role_name == ROLE_MANAGER:
            stmt = stmt.where(ChannelGroup.team_name.in_(list(tns)))
        else:
            my_team = _require_single_team_for_strict_roles(role_name, tns)
            stmt = stmt.where(ChannelGroup.team_name == my_team)

    rows = (await db.execute(stmt)).scalars().all()
    return OptionListOut(items=[OptionItem(id=int(x.id), group_name=str(_group_display_name(x) or "")) for x in rows])


class SalespersonItem(BaseModel):
    id: int
    username: str
    real_name: Optional[str] = None


class SalespersonListOut(BaseModel):
    items: List[SalespersonItem] = Field(default_factory=list)


@router.get("/salespersons", response_model=SalespersonListOut)
async def list_salespersons(
    status: int = Query(1, description="默认仅返回启用账号；传 0 可查禁用"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    current_user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name)
    _require_team_for_non_super_admin(role_name, tns)

    # ✅ 只读角色（market/finance）允许查看业务员列表（用于筛选/展示），但仍不允许写订单
    if role_name not in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_SALES, ROLE_MARKET, ROLE_FINANCE):
        raise HTTPException(status_code=403, detail="No permission")

    stmt = (
        select(distinct(User.id).label("id"), User.username, User.real_name)
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.role_name == ROLE_SALES)
        .order_by(User.id.asc())
    )
    stmt = stmt.where(User.status == int(status))

    # ✅ 团队隔离：
    # - super_admin：全量
    # - manager：多团队范围（不看 parent_id）
    # - market/finance：单团队范围
    # - sales：只看自己（本轮仍保持）
    if role_name != ROLE_SUPER_ADMIN:
        if role_name == ROLE_MANAGER:
            stmt = stmt.where(User.team_name.in_(list(tns)))
        else:
            my_team = _require_single_team_for_strict_roles(role_name, tns)
            stmt = stmt.where(User.team_name == my_team)

    # ✅ sales 仍只看自己
    if role_name == ROLE_SALES:
        stmt = stmt.where(User.id == current_user.id)

    rows = (await db.execute(stmt)).all()
    return SalespersonListOut(items=[SalespersonItem(id=int(r.id), username=str(r.username), real_name=r.real_name) for r in rows])


# ===========================
# ✅ OCR Tasks（OrderImport.vue 依赖）
# ===========================
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
    rn = role_name or ""

    # market 禁止走导入/OCR 相关链路
    if rn == ROLE_MARKET:
        raise HTTPException(status_code=403, detail="No permission")

    if rn == ROLE_SUPER_ADMIN:
        return stmt

    _require_team_for_non_super_admin(role_name, team_names)
    tns = _normalize_team_names(team_names)

    stmt = stmt.join(Order, and_(Order.id == OcrTask.scope_id, OcrTask.scope_type == "order"))

    # ✅ 团队隔离（团队共享）：manager 也只按团队，不看 parent_id
    if rn == ROLE_MANAGER:
        team_user_ids = select(User.id).where(User.team_name.in_(list(tns)))
    else:
        my_team = _require_single_team_for_strict_roles(role_name, tns)
        team_user_ids = select(User.id).where(User.team_name == my_team)

    stmt = stmt.where(Order.salesperson_id.in_(team_user_ids))

    # ✅ sales：只看自己任务
    if rn == ROLE_SALES:
        return stmt.where(Order.salesperson_id == int(current_user.id))

    if rn == ROLE_MANAGER:
        return stmt

    # ✅ finance：允许查看本团队任务（但本文件 OCR 链路仅在 orders 域被 sales/manager 使用；这里保持兼容）
    if rn == ROLE_FINANCE:
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


# ===========================
# BOS STS
# ===========================
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
    # ✅ market/finance 只读：禁止拿 sts（属于上传写入口）
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


# ===========================
# ✅ 稳定模式：后端代传 BOS
# ===========================
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
    user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    # ✅ finance 允许走该接口（但仅 related），其余写入口仍被禁止
    _ensure_orders_access(role_name, allow_finance=True)
    _require_team_for_non_super_admin(role_name, tns)
    _ = db

    # ✅ market 禁止上传
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

    # ✅ 关键修复：复用 StorageService 的签名与请求实现，避免 orders.py 手写签名导致 403
    def _head_obj() -> Tuple[bool, str]:
        return storage.head_object(storage_key)

    def _put_obj() -> str:
        # file.file 是 SpooledTemporaryFile/临时文件句柄，requests 可直接流式读取
        return storage.put_object(storage_key, data=file.file, content_type=content_type)

    try:
        try:
            exists, etag = await anyio.to_thread.run_sync(_head_obj)
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"BOS HEAD network error: {str(e) or e.__class__.__name__}")
        except Exception as e:
            # StorageService 会把 request_id/debug_id 带在异常文本里
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


# -------------------------
# JSON 动态字段过滤（仅保留一份，避免重复定义覆盖）
# -------------------------
def _dialect_name() -> str:
    try:
        return str(getattr(engine, "dialect", None).name or "").lower()
    except Exception:
        return ""


def _json_text(col, key: str):
    """
    返回 dynamic_data[key] 的“文本表达式”。

    ✅ 关键修复（与 finance.py 对齐）：
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
    统一把字符串表达式“去引号+去空白”，兼容 MySQL JSON_EXTRACT cast 后可能带引号： "2025-01-01"
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
    v = (value or "").strip()
    if not v:
        return
    expr = func.lower(_json_text(Order.dynamic_data, key))
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


def _add_json_date_range_any(
    clauses: list,
    *,
    keys: List[str],
    start_ymd: Optional[str],
    end_ymd: Optional[str],
):
    """
    在 Order.dynamic_data 中按多个 key 任选其一命中区间。
    ✅ 关键修复：把两端都归一化成 YYYYMMDD（8位数字）再比较，兼容：
       - dl_register_date: 20251113
       - register_date / first_register_date: 2025-11-13
    注意：包含 end_ymd。
    """
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


# ===========================
# draft / finalize
# ===========================
class OrderDraftIn(BaseModel):
    module: str = "order"
    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    salesperson_id: Optional[int] = None

    # ✅ 对齐前端：草稿阶段允许携带 order_info
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

    # ✅ role 范围控制 + 团队一致性
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

    # ✅ 新增：显式清空 slot（目前仅允许 multi-slot: related）
    clear_slots: List[str] = Field(default_factory=list)

    dynamic_data: Dict[str, Any] = Field(default_factory=dict)
    customer_group_id: Optional[int] = None
    channel_group_id: Optional[int] = None
    salesperson_id: Optional[int] = None

    # ✅ 对齐前端：finalize 阶段允许携带 order_info（finance 会被禁止）
    order_info: Optional[OrderInfoIn] = None


class OrderFinalizeOut(BaseModel):
    ok: bool = True
    order_id: int
    ocr_task_id: Optional[int] = None
    ocr_status: Optional[str] = None


@router.post("/finalize", response_model=OrderFinalizeOut)
async def finalize_order_upload(
    payload: OrderFinalizeIn,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    # ✅ finance 允许进来（仅 related 图维护）
    _ensure_orders_access(role_name, allow_finance=True)
    _require_team_for_non_super_admin(role_name, tns)

    # ✅ market 禁止走 finalize（上传写入口）
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

    # ✅ 写 ACL：sales 只能写自己；manager 按团队；finance/market 按团队
    await _ensure_order_write_acl_by_salesperson_id(
        db,
        salesperson_id=int(getattr(o, "salesperson_id", 0) or 0),
        current_user=user,
        role_name=role_name,
        team_names=tns,
    )

    # ✅ finance 仅允许操作“已完成订单”（与 finance 模块约束对齐）
    if role_name == ROLE_FINANCE and not bool(getattr(o, "is_finished", False)):
        raise HTTPException(status_code=400, detail="Only finished orders can be updated in finance")

    # finance 不允许改任何订单字段（上面已校验），这里保留原逻辑给非 finance
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

    # ✅ 1) 校验 clear_slots（仅允许 related）
    clear_slots = [str(x or "").strip() for x in (payload.clear_slots or [])]
    clear_slots = [x for x in clear_slots if x]
    for sk in clear_slots:
        if sk not in ALL_SLOTS:
            raise HTTPException(status_code=400, detail=f"非法 clear_slots slot_key: {sk}")
        if sk not in MULTI_SLOTS:
            raise HTTPException(status_code=400, detail=f"暂不支持清空该slot: {sk}")

    # ✅ 2) 按 slot 分组（客户端传入的 images 就代表“该 slot 的期望最终集合”）
    by_slot: Dict[str, List[FinalizeImageIn]] = {}
    for im in payload.images or []:
        sk = (im.slot_key or "").strip()
        if sk not in ALL_SLOTS:
            raise HTTPException(status_code=400, detail=f"非法 slot_key: {sk}")
        by_slot.setdefault(sk, []).append(im)

    # ✅ 3) 归一化（single-slot 取最后一张；multi-slot 全量保留）
    normalized_images: List[FinalizeImageIn] = []
    for sk, ims in by_slot.items():
        if sk in MULTI_SLOTS:
            normalized_images.extend(ims)
        else:
            normalized_images.append(ims[-1])

    # ✅ 4) 关键：对“被触达的 slot”做覆盖更新（包含：multi-slot 删除缺失项 + clear_slots 清空）
    touched_slots = set(by_slot.keys()) | set(clear_slots)

    for sk in touched_slots:
        desired_sks: List[str] = []
        if sk in by_slot:
            for im in by_slot.get(sk, []) or []:
                storage_key = (im.storage_key or "").strip().lstrip("/")
                if storage_key:
                    desired_sks.append(storage_key)

            # single-slot 只保留最后一张的 storage_key
            if sk not in MULTI_SLOTS and desired_sks:
                desired_sks = [desired_sks[-1]]

        # 清空：desired_sks 为空 => 删除该 slot 全部
        del_stmt = delete(OrderImage).where(and_(OrderImage.order_id == order_id, OrderImage.slot_key == sk))
        if desired_sks:
            del_stmt = del_stmt.where(~OrderImage.storage_key.in_(desired_sks))
        await db.execute(del_stmt)

    # ✅ 5) 插入/补齐新的图片记录
    has_ocr_images = False
    for im in normalized_images:
        slot_key = (im.slot_key or "").strip()
        storage_key = (im.storage_key or "").strip().lstrip("/")
        if not storage_key:
            raise HTTPException(status_code=400, detail="storage_key 不能为空")

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

    # ✅ 对齐前端：finalize 阶段允许同步更新 order_info（finance 已被拦截）
    if role_name != ROLE_FINANCE and payload.order_info is not None:
        _apply_order_info_patch(info, payload.order_info)
    else:
        # 没传且是新建：补齐计算字段
        if getattr(info, "premium_total", None) is None:
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


@router.get("", response_model=OrderListResponse)
async def list_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    is_finished: Optional[bool] = Query(None),
    salesperson_id: Optional[int] = Query(None),
    created_by: Optional[int] = Query(None),
    customer_group_id: Optional[int] = Query(None),
    channel_group_id: Optional[int] = Query(None),
    # ✅ 日期筛选：created_date_* 只按 created_at（按北京时间过滤）
    created_date: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 单日，兼容历史）"),
    created_date_start: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 起）"),
    created_date_end: Optional[str] = Query(None, description="YYYY-MM-DD（按北京时间过滤 created_at 止，包含当天）"),
    first_register_date_start: Optional[str] = Query(None, description="YYYY-MM-DD（初登日期起，包含）"),
    first_register_date_end: Optional[str] = Query(None, description="YYYY-MM-DD（初登日期止，包含）"),
    owner_name: Optional[str] = Query(None, description="车主姓名（身份证姓名 id_name）"),
    id_number: Optional[str] = Query(None, description="身份证号（id_number）"),
    plate_no: Optional[str] = Query(None, description="车牌号（dl_plate_no / plate_no）"),
    engine_no: Optional[str] = Query(None, description="发动机号（engine_no / dl_engine_no）"),
    vehicle_name: Optional[str] = Query(None, description="车辆名称（vehicle_brand_name / vehicle_name）"),
    vehicle_model: Optional[str] = Query(None, description="车辆型号（vehicle_model）"),
    vin: Optional[str] = Query(None, description="车架号（vin / dl_vin）"),
    remark: Optional[str] = Query(None, description="备注（remark / dla_remark）"),
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    current_user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name)
    _require_team_for_non_super_admin(role_name, tns)

    stmt = (
        select(Order)
        .options(
            selectinload(Order.creator),
            selectinload(Order.salesperson),
            selectinload(Order.customer_group),
            selectinload(Order.channel_group),
            selectinload(Order.order_info),
            selectinload(Order.images).selectinload(OrderImage.image_file),
        )
    )
    count_stmt = select(func.count(Order.id))

    clauses: list = []

    # ✅ ACL（sales 自己；manager 多团队；finance/market 单团队）
    await _apply_orders_list_acl(db, current_user=current_user, role_name=role_name, team_names=tns, clauses=clauses)

    if is_finished is not None:
        clauses.append(Order.is_finished.is_(is_finished))

    # 注意：下面这些“显式筛选条件”仍生效，但最终结果仍会被 ACL 限制在可见范围内
    if salesperson_id is not None:
        clauses.append(Order.salesperson_id == int(salesperson_id))
    if created_by is not None:
        clauses.append(Order.created_by == int(created_by))
    if customer_group_id is not None:
        clauses.append(Order.customer_group_id == int(customer_group_id))
    if channel_group_id is not None:
        clauses.append(Order.channel_group_id == int(channel_group_id))

    # ✅ 日期：支持 created_date_start/end（优先）；兼容旧 created_date（单日）
    # ✅ 约定：只按 created_at（前端“创建日期”语义）
    if created_date_start or created_date_end:
        if not created_date_start or not created_date_end:
            raise HTTPException(status_code=400, detail="created_date_start and created_date_end are required")
        rng = _parse_bj_date_span(created_date_start, created_date_end)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date_* must be YYYY-MM-DD and end>=start")
        start_utc, end_utc = rng
        clauses.append(and_(Order.created_at >= start_utc, Order.created_at < end_utc))
    elif created_date:
        rng = _parse_bj_date_range(created_date)
        if not rng:
            raise HTTPException(status_code=400, detail="created_date must be YYYY-MM-DD")
        start_utc, end_utc = rng
        clauses.append(and_(Order.created_at >= start_utc, Order.created_at < end_utc))

    # ✅ 初登日期：时间段（从 dynamic_data 常见字段取值）
    if first_register_date_start or first_register_date_end:
        _add_json_date_range_any(
            clauses,
            keys=["dl_register_date", "register_date", "first_register_date"],
            start_ymd=first_register_date_start,
            end_ymd=first_register_date_end,
        )

    _add_json_fuzzy(clauses, "id_name", owner_name)
    _add_json_fuzzy(clauses, "id_number", id_number)

    _add_json_fuzzy(clauses, "dl_plate_no", plate_no)
    _add_json_fuzzy(clauses, "plate_no", plate_no)

    _add_json_fuzzy(clauses, "engine_no", engine_no)
    _add_json_fuzzy(clauses, "dl_engine_no", engine_no)

    _add_json_fuzzy(clauses, "vehicle_brand_name", vehicle_name)
    _add_json_fuzzy(clauses, "vehicle_name", vehicle_name)

    _add_json_fuzzy(clauses, "vehicle_model", vehicle_model)

    _add_json_fuzzy(clauses, "vin", vin)
    _add_json_fuzzy(clauses, "dl_vin", vin)

    _add_json_fuzzy(clauses, "remark", remark)
    _add_json_fuzzy(clauses, "dla_remark", remark)

    if clauses:
        stmt = stmt.where(and_(*clauses))
        count_stmt = count_stmt.where(and_(*clauses))

    stmt = stmt.order_by(Order.id.desc()).offset((page - 1) * page_size).limit(page_size)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt)).scalars().all()

    items: list[OrderOut] = []
    for o in rows:
        ensure_display_urls_for_order_images(getattr(o, "images", None) or [], storage)
        cg = getattr(o, "customer_group", None)
        items.append(
            OrderOut(
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
                customer_group_name=_group_display_name(cg),
                channel_group_name=_group_display_name(getattr(o, "channel_group", None)),
                salesperson_name=_user_display_name(getattr(o, "salesperson", None)),
                customer_group_market=getattr(cg, "market", None) if cg else None,
                order_info=_order_info_out(getattr(o, "order_info", None)),
            )
        )

    return OrderListResponse(total=total, items=items)


@router.get("/{order_id:int}", response_model=OrderOut)
async def get_order_detail(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    user, role_name, team_names, _team_ids = user_with_role
    tns = _normalize_team_names(team_names)

    _ensure_orders_access(role_name)
    _require_team_for_non_super_admin(role_name, tns)
    return await _load_order_out(db, order_id, current_user=user, role_name=role_name, team_names=tns)


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

    _ensure_required_customer_channel(
        customer_group_id=payload.customer_group_id,
        channel_group_id=payload.channel_group_id,
    )

    # ✅ sales 只能给自己创建；manager/super_admin 允许指定，但仍需通过写 ACL（团队共享不看经理）
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
        ocr_raw_json=payload.ocr_raw_json or {},
        status=payload.status or 0,
        audit_status=payload.audit_status or 0,
        is_finished=bool(payload.is_finished),
        is_rebate=bool(payload.is_rebate),
        is_paid=bool(payload.is_paid),
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

    # ✅ 写 ACL：sales 只能改自己；manager 按团队共享不看经理
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
        await _ensure_order_write_acl_by_salesperson_id(
            db,
            salesperson_id=spid,
            current_user=user,
            role_name=role_name,
            team_names=tns,
        )
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

    # ✅ 写 ACL
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
