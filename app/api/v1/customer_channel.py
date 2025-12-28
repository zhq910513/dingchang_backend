# app/api/v1/customer_channel.py
# encoding: utf-8
"""
客户 / 渠道 分组管理接口（去兼容版 / 共享数据版）

新权限规则（按强哥最新需求）：
1) 客户管理、渠道管理：不再做 created_by 范围限制，所有人共享（都能查看未删除数据）
2) 财务账号：只能看，不能新增/删除/恢复
3) include_deleted：仅 super_admin 生效（其他角色传 true 也会被忽略）
4) restore：仅 super_admin 可恢复（与“仅超管可看已删除”匹配）

✅ 新增搜索（后端模糊）：
- 渠道：channel_code / channel_name / created_by
- 客户：customer_code / customer_name / market / created_by
"""

from __future__ import annotations

from datetime import datetime
from typing import Tuple, Optional, Dict, List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_with_role
from app.core.constants import (
    ROLE_SUPER_ADMIN,
    ROLE_FINANCE,
)
from app.core.db import get_db
from app.models.user import User
from app.models.customer_group import CustomerGroup
from app.models.channel_group import ChannelGroup
from app.schemas.customer_channel import (
    CustomerGroupCreate,
    CustomerGroupOut,
    CustomerGroupListResponse,
    ChannelGroupCreate,
    ChannelGroupOut,
    ChannelGroupListResponse,
    ContactItem,
)

router = APIRouter()


def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _user_display_name(u: Optional[User]) -> Optional[str]:
    if not u:
        return None
    return getattr(u, "full_name", None) or getattr(u, "real_name", None) or getattr(u, "username", None)


def _normalize_include_deleted(role_name: Optional[str], include_deleted: bool) -> bool:
    if role_name != ROLE_SUPER_ADMIN:
        return False
    return bool(include_deleted)


def _ensure_can_modify(role_name: Optional[str]) -> None:
    if role_name == ROLE_FINANCE:
        raise HTTPException(status_code=403, detail="Finance cannot modify customer/channel groups")


async def _ensure_can_restore(role_name: Optional[str]) -> None:
    if role_name != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only super_admin can restore deleted groups")


async def _load_user_map(db: AsyncSession, user_ids: list[int]) -> Dict[int, User]:
    if not user_ids:
        return {}
    stmt = select(User).where(User.id.in_(list(set(user_ids))))
    rows = (await db.execute(stmt)).scalars().all()
    return {u.id: u for u in rows}


def _like_ci(col, v: Optional[str]):
    s = (v or "").strip()
    if not s:
        return None
    return func.lower(col).like(f"%{s.lower()}%")


def _creator_like_expr(v: Optional[str]):
    s = (v or "").strip()
    if not s:
        return None
    kw = f"%{s.lower()}%"
    return or_(
        func.lower(getattr(User, "username")).like(kw),
        func.lower(getattr(User, "real_name")).like(kw),
        func.lower(getattr(User, "full_name")).like(kw),
    )


def _normalize_contacts(contacts: Optional[List[ContactItem]]) -> List[dict]:
    """
    ✅ 入库前统一规整：
    - 去空（value 空的不入库）
    - 去重（type+value）
    - value/type 做 strip（Pydantic 已做校验与部分归一，这里再兜底）
    """
    items = contacts or []
    out: List[dict] = []
    seen = set()
    for c in items:
        t = (getattr(c, "type", "") or "").strip()
        v = (getattr(c, "value", "") or "").strip()
        if not v:
            continue
        key = (t, v)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": t, "value": v})
    return out


# ----------------- 客户分组 -----------------

customer_router = APIRouter(prefix="/customer-groups", tags=["customer-groups"])


@customer_router.get("", response_model=CustomerGroupListResponse)
async def list_customer_groups(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    include_deleted: bool = Query(False, description="仅 super_admin 生效：是否包含已删除数据"),
    customer_code: Optional[str] = Query(None, description="客户代码（模糊）"),
    customer_name: Optional[str] = Query(None, description="客户名称（模糊）"),
    market: Optional[str] = Query(None, description="市场（模糊）"),
    created_by: Optional[str] = Query(None, description="创建人（模糊：用户名/姓名）"),
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    _current_user, role_name = user_role
    include_deleted = _normalize_include_deleted(role_name, include_deleted)

    stmt = select(CustomerGroup).order_by(CustomerGroup.id.desc())
    if not include_deleted:
        stmt = stmt.where(CustomerGroup.deleted_at.is_(None))

    clauses = []

    ex = _like_ci(CustomerGroup.customer_code, customer_code)
    if ex is not None:
        clauses.append(ex)

    ex = _like_ci(CustomerGroup.customer_name, customer_name)
    if ex is not None:
        clauses.append(ex)

    ex = _like_ci(CustomerGroup.market, market)
    if ex is not None:
        clauses.append(ex)

    creator_expr = _creator_like_expr(created_by)
    if creator_expr is not None:
        stmt = stmt.join(User, User.id == CustomerGroup.created_by, isouter=True)
        clauses.append(creator_expr)

    if clauses:
        stmt = stmt.where(and_(*clauses))

    count_stmt = stmt.with_only_columns(func.count()).order_by(None)
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    user_ids = [int(getattr(o, "created_by", 0) or 0) for o in rows if getattr(o, "created_by", None)]
    user_map = await _load_user_map(db, user_ids)

    items = []
    for o in rows:
        cu = user_map.get(getattr(o, "created_by", None))
        items.append(
            CustomerGroupOut(
                id=o.id,
                customer_code=o.customer_code,
                customer_name=o.customer_name,
                market=getattr(o, "market", None),
                group_name=o.customer_name,
                region=o.region or "",
                contacts=o.contacts or [],
                created_by=getattr(o, "created_by", None),
                created_by_name=_user_display_name(cu),
                created_at=getattr(o, "created_at", None),
                deleted_at=getattr(o, "deleted_at", None),
                is_deleted=bool(getattr(o, "deleted_at", None)),
            )
        )

    return CustomerGroupListResponse(total=int(total or 0), items=items)


@customer_router.post("", response_model=CustomerGroupOut, status_code=201)
async def create_customer_group(
    payload: CustomerGroupCreate,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    current_user, role_name = user_role
    _ensure_can_modify(role_name)

    code = (payload.customer_code or "").strip()
    name = (payload.customer_name or "").strip()
    mk = (payload.market or "").strip()

    if not code:
        raise HTTPException(status_code=400, detail="customer_code is required")
    if not name:
        raise HTTPException(status_code=400, detail="customer_name is required")

    stmt = select(CustomerGroup).where(
        and_(
            CustomerGroup.customer_code == code,
            CustomerGroup.customer_name == name,
        )
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        if getattr(existing, "deleted_at", None) is None:
            raise HTTPException(status_code=400, detail="customer_code + customer_name already exists")
        raise HTTPException(status_code=400, detail="customer_code + customer_name exists but deleted; restore it instead")

    obj = CustomerGroup(
        customer_code=code,
        customer_name=name,
        market=mk or None,
        region=(payload.region or "").strip(),
        contacts=_normalize_contacts(payload.contacts),
        created_by=current_user.id,
        deleted_at=None,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)

    return CustomerGroupOut(
        id=obj.id,
        customer_code=obj.customer_code,
        customer_name=obj.customer_name,
        market=getattr(obj, "market", None),
        group_name=obj.customer_name,
        region=obj.region or "",
        contacts=obj.contacts or [],
        created_by=obj.created_by,
        created_by_name=_user_display_name(current_user),
        created_at=getattr(obj, "created_at", None),
        deleted_at=getattr(obj, "deleted_at", None),
        is_deleted=False,
    )


@customer_router.delete("/{group_id}")
async def delete_customer_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    _current_user, role_name = user_role
    _ensure_can_modify(role_name)

    stmt = select(CustomerGroup).where(and_(CustomerGroup.id == group_id, CustomerGroup.deleted_at.is_(None)))
    obj = (await db.execute(stmt)).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Customer group not found")

    obj.deleted_at = _now()
    await db.commit()
    return {"id": group_id}


@customer_router.post("/{group_id}/restore")
async def restore_customer_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    _, role_name = user_role
    await _ensure_can_restore(role_name)

    obj = (await db.execute(select(CustomerGroup).where(CustomerGroup.id == group_id))).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Customer group not found")
    if obj.deleted_at is None:
        raise HTTPException(status_code=400, detail="Customer group is not deleted")

    stmt = select(CustomerGroup).where(
        and_(
            CustomerGroup.customer_code == obj.customer_code,
            CustomerGroup.customer_name == obj.customer_name,
            CustomerGroup.deleted_at.is_(None),
        )
    )
    conflict = (await db.execute(stmt)).scalar_one_or_none()
    if conflict:
        raise HTTPException(status_code=400, detail="Cannot restore: same customer_code + customer_name already exists")

    obj.deleted_at = None
    await db.commit()
    return {"id": group_id}


# ----------------- 渠道分组 -----------------

channel_router = APIRouter(prefix="/channel-groups", tags=["channel-groups"])


@channel_router.get("", response_model=ChannelGroupListResponse)
async def list_channel_groups(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    include_deleted: bool = Query(False, description="仅 super_admin 生效：是否包含已删除数据"),
    channel_code: Optional[str] = Query(None, description="渠道代码（模糊）"),
    channel_name: Optional[str] = Query(None, description="渠道名称（模糊）"),
    created_by: Optional[str] = Query(None, description="创建人（模糊：用户名/姓名）"),
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    _current_user, role_name = user_role
    include_deleted = _normalize_include_deleted(role_name, include_deleted)

    stmt = select(ChannelGroup).order_by(ChannelGroup.id.desc())
    if not include_deleted:
        stmt = stmt.where(ChannelGroup.deleted_at.is_(None))

    clauses = []

    ex = _like_ci(ChannelGroup.channel_code, channel_code)
    if ex is not None:
        clauses.append(ex)

    ex = _like_ci(ChannelGroup.channel_name, channel_name)
    if ex is not None:
        clauses.append(ex)

    creator_expr = _creator_like_expr(created_by)
    if creator_expr is not None:
        stmt = stmt.join(User, User.id == ChannelGroup.created_by, isouter=True)
        clauses.append(creator_expr)

    if clauses:
        stmt = stmt.where(and_(*clauses))

    count_stmt = stmt.with_only_columns(func.count()).order_by(None)
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    user_ids = [int(getattr(o, "created_by", 0) or 0) for o in rows if getattr(o, "created_by", None)]
    user_map = await _load_user_map(db, user_ids)

    items = []
    for o in rows:
        cu = user_map.get(getattr(o, "created_by", None))
        items.append(
            ChannelGroupOut(
                id=o.id,
                channel_code=o.channel_code,
                channel_name=o.channel_name,
                group_name=o.channel_name,
                region=o.region or "",
                contacts=o.contacts or [],
                created_by=getattr(o, "created_by", None),
                created_by_name=_user_display_name(cu),
                created_at=getattr(o, "created_at", None),
                deleted_at=getattr(o, "deleted_at", None),
                is_deleted=bool(getattr(o, "deleted_at", None)),
            )
        )

    return ChannelGroupListResponse(total=int(total or 0), items=items)


@channel_router.post("", response_model=ChannelGroupOut, status_code=201)
async def create_channel_group(
    payload: ChannelGroupCreate,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    current_user, role_name = user_role
    _ensure_can_modify(role_name)

    code = (payload.channel_code or "").strip()
    name = (payload.channel_name or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="channel_code is required")
    if not name:
        raise HTTPException(status_code=400, detail="channel_name is required")

    stmt = select(ChannelGroup).where(
        and_(
            ChannelGroup.channel_code == code,
            ChannelGroup.channel_name == name,
        )
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        if getattr(existing, "deleted_at", None) is None:
            raise HTTPException(status_code=400, detail="channel_code + channel_name already exists")
        raise HTTPException(status_code=400, detail="channel_code + channel_name exists but deleted; restore it instead")

    obj = ChannelGroup(
        channel_code=code,
        channel_name=name,
        region=(payload.region or "").strip(),
        contacts=_normalize_contacts(payload.contacts),
        created_by=current_user.id,
        deleted_at=None,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)

    return ChannelGroupOut(
        id=obj.id,
        channel_code=obj.channel_code,
        channel_name=obj.channel_name,
        group_name=obj.channel_name,
        region=obj.region or "",
        contacts=obj.contacts or [],
        created_by=obj.created_by,
        created_by_name=_user_display_name(current_user),
        created_at=getattr(obj, "created_at", None),
        deleted_at=getattr(obj, "deleted_at", None),
        is_deleted=False,
    )


@channel_router.delete("/{group_id}")
async def delete_channel_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    _current_user, role_name = user_role
    _ensure_can_modify(role_name)

    stmt = select(ChannelGroup).where(and_(ChannelGroup.id == group_id, ChannelGroup.deleted_at.is_(None)))
    obj = (await db.execute(stmt)).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Channel group not found")

    obj.deleted_at = _now()
    await db.commit()
    return {"id": group_id}


@channel_router.post("/{group_id}/restore")
async def restore_channel_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    _, role_name = user_role
    await _ensure_can_restore(role_name)

    obj = (await db.execute(select(ChannelGroup).where(ChannelGroup.id == group_id))).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Channel group not found")
    if obj.deleted_at is None:
        raise HTTPException(status_code=400, detail="Channel group is not deleted")

    stmt = select(ChannelGroup).where(
        and_(
            ChannelGroup.channel_code == obj.channel_code,
            ChannelGroup.channel_name == obj.channel_name,
            ChannelGroup.deleted_at.is_(None),
        )
    )
    conflict = (await db.execute(stmt)).scalar_one_or_none()
    if conflict:
        raise HTTPException(status_code=400, detail="Cannot restore: same channel_code + channel_name already exists")

    obj.deleted_at = None
    await db.commit()
    return {"id": group_id}


router.include_router(customer_router)
router.include_router(channel_router)
