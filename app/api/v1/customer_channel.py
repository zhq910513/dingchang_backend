# app/api/v1/customer_channel.py
# encoding: utf-8
"""
客户 / 渠道 分组管理接口（共享数据版）

⚠️ 本轮改造为【团队隔离版】：
- super_admin：可查看全部；创建/编辑若要落到某团队，需传 team_name（query）
- 非 super_admin：只能查看/操作自己 team_name 下的数据
"""

from __future__ import annotations

from datetime import datetime
from typing import Tuple, Optional, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_with_role
from app.core.constants import (
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_FINANCE,
    ROLE_MARKET,
    TEAM_NAMES,
)
from app.core.db import get_db
from app.models.channel_group import ChannelGroup
from app.models.customer_group import CustomerGroup
from app.models.user import User
from app.schemas.customer_channel import (
    CustomerGroupCreate,
    CustomerGroupUpdate,
    CustomerGroupOut,
    CustomerGroupListResponse,
    ChannelGroupCreate,
    ChannelGroupUpdate,
    ChannelGroupOut,
    ChannelGroupListResponse,
    ContactItem,
)

router = APIRouter()


def _now() -> datetime:
    # 统一以容器/服务端时区（Asia/Shanghai）返回“本地时间（naive）”，避免 MySQL 适配 tz-aware datetime 出错
    return datetime.now()


def _clean_team_name(v: Optional[str]) -> Optional[str]:
    s = (v or "").strip()
    return s or None


def _current_team_name_or_403(user: User, role_name: Optional[str]) -> str:
    if role_name == ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=400, detail="super_admin must specify team_name explicitly")
    tn = _clean_team_name(getattr(user, "team_name", None))
    if not tn:
        raise HTTPException(status_code=403, detail="当前账号未绑定团队（team_name）")
    if tn not in TEAM_NAMES:
        raise HTTPException(status_code=403, detail="当前账号团队非法（team_name）")
    return tn


def _resolve_team_name(
    *,
    current_user: User,
    role_name: Optional[str],
    team_name: Optional[str],
) -> Optional[str]:
    """
    - super_admin：可选 team_name；不传则表示“全量”（保留）
    - 其他角色：强制使用自身 team_name（忽略传入）
    """
    if role_name == ROLE_SUPER_ADMIN:
        t = _clean_team_name(team_name)
        if t is None:
            return None
        if t not in TEAM_NAMES:
            raise HTTPException(status_code=400, detail=f"非法团队 team_name：{t}")
        return t

    return _current_team_name_or_403(current_user, role_name)


def _user_display_name(u: Optional[User]) -> Optional[str]:
    if not u:
        return None
    return getattr(u, "full_name", None) or getattr(u, "real_name", None) or getattr(u, "username", None)


async def _created_by_name(db: AsyncSession, created_by: Optional[int], current_user: User) -> Optional[str]:
    if not created_by:
        return None
    try:
        if int(created_by) == int(getattr(current_user, "id", 0) or 0):
            return _user_display_name(current_user)
    except Exception:
        pass

    u = (await db.execute(select(User).where(User.id == int(created_by)))).scalars().first()
    return _user_display_name(u)


def _normalize_include_deleted(role_name: Optional[str], include_deleted: bool) -> bool:
    if role_name != ROLE_SUPER_ADMIN:
        return False
    return bool(include_deleted)


def _ensure_can_modify(role_name: Optional[str]) -> None:
    # ✅ 只有 super_admin / manager / market 可操作；finance 与其它角色一律禁止
    if role_name == ROLE_FINANCE:
        raise HTTPException(status_code=403, detail="财务账号无操作权限")
    if role_name not in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_MARKET):
        raise HTTPException(status_code=403, detail="No permission")


def _ensure_can_edit(role_name: Optional[str]) -> None:
    if role_name not in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_MARKET):
        raise HTTPException(status_code=403, detail="仅经理账号/超级账号/市场账号可编辑")


async def _ensure_can_restore(role_name: Optional[str]) -> None:
    if role_name != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="仅超级账号可恢复已删除数据")


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
    入库前统一规整：
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
    team_name: Optional[str] = Query(None, description="仅 super_admin：按团队过滤"),
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    current_user, role_name = user_role
    include_deleted = _normalize_include_deleted(role_name, include_deleted)

    tn = _resolve_team_name(current_user=current_user, role_name=role_name, team_name=team_name)

    stmt = select(CustomerGroup).order_by(CustomerGroup.id.desc())
    if not include_deleted:
        stmt = stmt.where(CustomerGroup.deleted_at.is_(None))

    # ✅ 团队隔离：非 super_admin 必有 tn；super_admin tn 可为 None（全量）
    if tn is not None:
        stmt = stmt.where(CustomerGroup.team_name == tn)

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
                team_name=getattr(o, "team_name", None),
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
    team_name: Optional[str] = Query(None, description="仅 super_admin：创建到指定团队"),
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    current_user, role_name = user_role
    _ensure_can_modify(role_name)

    tn = _resolve_team_name(current_user=current_user, role_name=role_name, team_name=team_name)
    if role_name == ROLE_SUPER_ADMIN and tn is None:
        raise HTTPException(status_code=400, detail="super_admin 创建客户必须指定 team_name")

    code = (payload.customer_code or "").strip()
    name = (payload.customer_name or "").strip()
    mk = (payload.market or "").strip()

    if not code:
        raise HTTPException(status_code=400, detail="customer_code is required")
    if not name:
        raise HTTPException(status_code=400, detail="customer_name is required")

    stmt = select(CustomerGroup).where(
        and_(
            CustomerGroup.team_name == tn,
            CustomerGroup.customer_code == code,
            CustomerGroup.customer_name == name,
        )
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    if existing:
        if getattr(existing, "deleted_at", None) is None:
            raise HTTPException(status_code=400, detail="该客户信息已存在，请勿重复创建")

        existing.market = mk or None
        existing.region = (payload.region or "").strip()
        existing.contacts = _normalize_contacts(payload.contacts)
        existing.deleted_at = None
        await db.commit()
        await db.refresh(existing)

        return CustomerGroupOut(
            id=existing.id,
            customer_code=existing.customer_code,
            customer_name=existing.customer_name,
            market=getattr(existing, "market", None),
            team_name=getattr(existing, "team_name", None),
            group_name=existing.customer_name,
            region=existing.region or "",
            contacts=existing.contacts or [],
            created_by=getattr(existing, "created_by", None),
            created_by_name=await _created_by_name(db, getattr(existing, "created_by", None), current_user),
            created_at=getattr(existing, "created_at", None),
            deleted_at=getattr(existing, "deleted_at", None),
            is_deleted=False,
        )

    obj = CustomerGroup(
        team_name=tn,
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
        team_name=getattr(obj, "team_name", None),
        group_name=obj.customer_name,
        region=obj.region or "",
        contacts=obj.contacts or [],
        created_by=obj.created_by,
        created_by_name=_user_display_name(current_user),
        created_at=getattr(obj, "created_at", None),
        deleted_at=getattr(obj, "deleted_at", None),
        is_deleted=False,
    )


@customer_router.put("/{group_id}", response_model=CustomerGroupOut)
async def update_customer_group(
    group_id: int,
    payload: CustomerGroupUpdate,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    current_user, role_name = user_role
    _ensure_can_modify(role_name)
    _ensure_can_edit(role_name)

    # ✅ 先按 team 限制读取
    if role_name == ROLE_SUPER_ADMIN:
        obj = (await db.execute(select(CustomerGroup).where(CustomerGroup.id == group_id))).scalar_one_or_none()
    else:
        tn = _current_team_name_or_403(current_user, role_name)
        obj = (
            await db.execute(
                select(CustomerGroup).where(and_(CustomerGroup.id == group_id, CustomerGroup.team_name == tn))
            )
        ).scalar_one_or_none()

    if not obj:
        raise HTTPException(status_code=404, detail="Customer group not found")
    if getattr(obj, "deleted_at", None) is not None:
        raise HTTPException(status_code=400, detail="该客户已删除，请先恢复后再编辑")

    code = (payload.customer_code or "").strip()
    name = (payload.customer_name or "").strip()
    mk = (payload.market or "").strip()

    if not code:
        raise HTTPException(status_code=400, detail="customer_code is required")
    if not name:
        raise HTTPException(status_code=400, detail="customer_name is required")

    stmt = select(CustomerGroup).where(
        and_(
            CustomerGroup.team_name == getattr(obj, "team_name", None),
            CustomerGroup.customer_code == code,
            CustomerGroup.customer_name == name,
            CustomerGroup.id != group_id,
        )
    )
    conflict = (await db.execute(stmt)).scalar_one_or_none()
    if conflict:
        raise HTTPException(status_code=400, detail="该客户信息已存在，请勿重复创建")

    obj.customer_code = code
    obj.customer_name = name
    obj.market = mk or None
    obj.region = (payload.region or "").strip()
    obj.contacts = _normalize_contacts(payload.contacts)

    await db.commit()
    await db.refresh(obj)

    return CustomerGroupOut(
        id=obj.id,
        customer_code=obj.customer_code,
        customer_name=obj.customer_name,
        market=getattr(obj, "market", None),
        team_name=getattr(obj, "team_name", None),
        group_name=obj.customer_name,
        region=obj.region or "",
        contacts=obj.contacts or [],
        created_by=getattr(obj, "created_by", None),
        created_by_name=await _created_by_name(db, getattr(obj, "created_by", None), current_user),
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
    current_user, role_name = user_role
    _ensure_can_modify(role_name)

    if role_name == ROLE_SUPER_ADMIN:
        stmt = select(CustomerGroup).where(and_(CustomerGroup.id == group_id, CustomerGroup.deleted_at.is_(None)))
    else:
        tn = _current_team_name_or_403(current_user, role_name)
        stmt = select(CustomerGroup).where(
            and_(CustomerGroup.id == group_id, CustomerGroup.team_name == tn, CustomerGroup.deleted_at.is_(None))
        )

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
            CustomerGroup.team_name == getattr(obj, "team_name", None),
            CustomerGroup.customer_code == obj.customer_code,
            CustomerGroup.customer_name == obj.customer_name,
            CustomerGroup.id != group_id,
        )
    )
    conflict = (await db.execute(stmt)).scalar_one_or_none()
    if conflict:
        raise HTTPException(
            status_code=400,
            detail="Cannot restore: same team_name + customer_code + customer_name already exists",
        )

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
    team_name: Optional[str] = Query(None, description="仅 super_admin：按团队过滤"),
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    current_user, role_name = user_role
    include_deleted = _normalize_include_deleted(role_name, include_deleted)

    tn = _resolve_team_name(current_user=current_user, role_name=role_name, team_name=team_name)

    stmt = select(ChannelGroup).order_by(ChannelGroup.id.desc())
    if not include_deleted:
        stmt = stmt.where(ChannelGroup.deleted_at.is_(None))

    if tn is not None:
        stmt = stmt.where(ChannelGroup.team_name == tn)

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
                team_name=getattr(o, "team_name", None),
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
    team_name: Optional[str] = Query(None, description="仅 super_admin：创建到指定团队"),
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    current_user, role_name = user_role
    _ensure_can_modify(role_name)

    tn = _resolve_team_name(current_user=current_user, role_name=role_name, team_name=team_name)
    if role_name == ROLE_SUPER_ADMIN and tn is None:
        raise HTTPException(status_code=400, detail="super_admin 创建渠道必须指定 team_name")

    code = (payload.channel_code or "").strip()
    name = (payload.channel_name or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="channel_code is required")
    if not name:
        raise HTTPException(status_code=400, detail="channel_name is required")

    stmt = select(ChannelGroup).where(
        and_(
            ChannelGroup.team_name == tn,
            ChannelGroup.channel_code == code,
            ChannelGroup.channel_name == name,
        )
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    if existing:
        if getattr(existing, "deleted_at", None) is None:
            raise HTTPException(status_code=400, detail="该渠道信息已存在，请勿重复创建")

        existing.region = (payload.region or "").strip()
        existing.contacts = _normalize_contacts(payload.contacts)
        existing.deleted_at = None
        await db.commit()
        await db.refresh(existing)

        return ChannelGroupOut(
            id=existing.id,
            channel_code=existing.channel_code,
            channel_name=existing.channel_name,
            team_name=getattr(existing, "team_name", None),
            group_name=existing.channel_name,
            region=existing.region or "",
            contacts=existing.contacts or [],
            created_by=getattr(existing, "created_by", None),
            created_by_name=await _created_by_name(db, getattr(existing, "created_by", None), current_user),
            created_at=getattr(existing, "created_at", None),
            deleted_at=getattr(existing, "deleted_at", None),
            is_deleted=False,
        )

    obj = ChannelGroup(
        team_name=tn,
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
        team_name=getattr(obj, "team_name", None),
        group_name=obj.channel_name,
        region=obj.region or "",
        contacts=obj.contacts or [],
        created_by=obj.created_by,
        created_by_name=_user_display_name(current_user),
        created_at=getattr(obj, "created_at", None),
        deleted_at=getattr(obj, "deleted_at", None),
        is_deleted=False,
    )


@channel_router.put("/{group_id}", response_model=ChannelGroupOut)
async def update_channel_group(
    group_id: int,
    payload: ChannelGroupUpdate,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    current_user, role_name = user_role
    _ensure_can_modify(role_name)
    _ensure_can_edit(role_name)

    if role_name == ROLE_SUPER_ADMIN:
        obj = (await db.execute(select(ChannelGroup).where(ChannelGroup.id == group_id))).scalar_one_or_none()
    else:
        tn = _current_team_name_or_403(current_user, role_name)
        obj = (
            await db.execute(select(ChannelGroup).where(and_(ChannelGroup.id == group_id, ChannelGroup.team_name == tn)))
        ).scalar_one_or_none()

    if not obj:
        raise HTTPException(status_code=404, detail="Channel group not found")
    if getattr(obj, "deleted_at", None) is not None:
        raise HTTPException(status_code=400, detail="该渠道已删除，请先恢复后再编辑")

    code = (payload.channel_code or "").strip()
    name = (payload.channel_name or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="channel_code is required")
    if not name:
        raise HTTPException(status_code=400, detail="channel_name is required")

    stmt = select(ChannelGroup).where(
        and_(
            ChannelGroup.team_name == getattr(obj, "team_name", None),
            ChannelGroup.channel_code == code,
            ChannelGroup.channel_name == name,
            ChannelGroup.id != group_id,
        )
    )
    conflict = (await db.execute(stmt)).scalar_one_or_none()
    if conflict:
        raise HTTPException(status_code=400, detail="该渠道信息已存在，请勿重复创建")

    obj.channel_code = code
    obj.channel_name = name
    obj.region = (payload.region or "").strip()
    obj.contacts = _normalize_contacts(payload.contacts)

    await db.commit()
    await db.refresh(obj)

    return ChannelGroupOut(
        id=obj.id,
        channel_code=obj.channel_code,
        channel_name=obj.channel_name,
        team_name=getattr(obj, "team_name", None),
        group_name=obj.channel_name,
        region=obj.region or "",
        contacts=obj.contacts or [],
        created_by=getattr(obj, "created_by", None),
        created_by_name=await _created_by_name(db, getattr(obj, "created_by", None), current_user),
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
    current_user, role_name = user_role
    _ensure_can_modify(role_name)

    if role_name == ROLE_SUPER_ADMIN:
        stmt = select(ChannelGroup).where(and_(ChannelGroup.id == group_id, ChannelGroup.deleted_at.is_(None)))
    else:
        tn = _current_team_name_or_403(current_user, role_name)
        stmt = select(ChannelGroup).where(
            and_(ChannelGroup.id == group_id, ChannelGroup.team_name == tn, ChannelGroup.deleted_at.is_(None))
        )

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
            ChannelGroup.team_name == getattr(obj, "team_name", None),
            ChannelGroup.channel_code == obj.channel_code,
            ChannelGroup.channel_name == obj.channel_name,
            ChannelGroup.id != group_id,
        )
    )
    conflict = (await db.execute(stmt)).scalar_one_or_none()
    if conflict:
        raise HTTPException(
            status_code=400,
            detail="Cannot restore: same team_name + channel_code + channel_name already exists",
        )

    obj.deleted_at = None
    await db.commit()
    return {"id": group_id}


router.include_router(customer_router)
router.include_router(channel_router)
