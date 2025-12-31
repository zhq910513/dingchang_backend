# app/api/v1/users.py
# encoding: utf-8
"""
v1 - 用户 / 账号管理（去兼容版）
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.api.deps import get_current_user_with_role
from app.core.db import get_db
from app.core.config import settings
from app.core.security import hash_password
from app.core.constants import (
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_SALES,
    ROLE_FINANCE,
    ROLE_MARKET,
    ROLE_CHILD_CREATABLE_MAP,
)
from app.models.user import User
from app.models.role import Role
from app.models.user_role import UserRole
from app.models.session import UserSession
from app.schemas.user import UserCreate, UserSimple

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])


async def _get_user_primary_role_name(db: AsyncSession, user_id: int) -> Optional[str]:
    stmt = (
        select(Role.role_name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
        .order_by(Role.id.asc())
    )
    return (await db.execute(stmt)).scalars().first()


async def _ensure_user_is_manager(db: AsyncSession, user_id: int) -> User:
    u = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if not u:
        raise HTTPException(status_code=400, detail="manager_id not found")
    if int(getattr(u, "status", 0) or 0) != 1:
        raise HTTPException(status_code=400, detail="manager account is disabled")

    role_name = await _get_user_primary_role_name(db, u.id)
    if role_name != ROLE_MANAGER:
        raise HTTPException(status_code=400, detail="manager_id is not a manager account")
    return u


@router.get("/managers", response_model=List[UserSimple])
async def list_managers(
    status: int = Query(1, description="默认仅返回启用账号"),
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    """
    ✅ 给前端创建子账号时的“归属经理”下拉用
    - 仅 super_admin 可用
    """
    _user, role_name = user_role
    if role_name != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No permission")

    stmt = (
        select(User.id, User.username, User.real_name, Role.role_name)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.role_name == ROLE_MANAGER)
        .order_by(User.id.asc())
    )
    if status is not None:
        stmt = stmt.where(User.status == int(status))

    rows = (await db.execute(stmt)).all()
    return [
        UserSimple(
            id=r.id,
            username=r.username,
            real_name=r.real_name,
            role_name=r.role_name,
            is_online=False,
        )
        for r in rows
    ]


@router.post("", status_code=201)
async def create_user(
    payload: UserCreate,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    """
    创建子账号（去兼容版规则）：

    - super_admin 可创建：manager / sales / finance / market
      * 创建 sales/finance/market：必须指定 manager_id，且 parent_id = manager_id
      * 创建 manager：parent_id = super_admin.id（便于追溯）
    - manager 可创建：sales / finance / market
      * parent_id = manager.id
    """
    current_user, current_role_name = user_role

    allowed_roles = ROLE_CHILD_CREATABLE_MAP.get(current_role_name or "", ())
    if not allowed_roles:
        raise HTTPException(status_code=403, detail="No permission to create users")

    # role_id -> role
    role = (await db.execute(select(Role).where(Role.id == payload.role_id))).scalars().first()
    if not role:
        raise HTTPException(status_code=400, detail="Role does not exist")

    if role.role_name not in allowed_roles:
        raise HTTPException(status_code=403, detail="Cannot create this role")

    # username 唯一
    username = (payload.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    exists = (await db.execute(select(User).where(User.username == username))).scalars().first()
    if exists:
        raise HTTPException(status_code=400, detail="Username already exists")

    # 决定 parent_id
    parent_id: Optional[int]

    if current_role_name == ROLE_MANAGER:
        if role.role_name not in (ROLE_SALES, ROLE_FINANCE, ROLE_MARKET):
            raise HTTPException(status_code=403, detail="Manager can only create sales/finance/market")
        parent_id = current_user.id

    elif current_role_name == ROLE_SUPER_ADMIN:
        if role.role_name == ROLE_MANAGER:
            parent_id = current_user.id
        elif role.role_name in (ROLE_SALES, ROLE_FINANCE, ROLE_MARKET):
            if not payload.manager_id:
                raise HTTPException(status_code=400, detail="请指定分配给哪个经理（manager_id）")
            mgr = await _ensure_user_is_manager(db, int(payload.manager_id))
            parent_id = mgr.id
        else:
            raise HTTPException(status_code=400, detail="Unsupported role")
    else:
        raise HTTPException(status_code=403, detail="No permission")

    new_user = User(
        username=username,
        real_name=(payload.real_name or "").strip(),
        password_hash=hash_password(payload.password),
        parent_id=parent_id,
        status=1,
    )
    db.add(new_user)
    await db.flush()

    db.add(UserRole(user_id=new_user.id, role_id=role.id))
    await db.commit()

    logger.info(
        "Create user: operator=%s new_user=%s role=%s parent_id=%s",
        current_user.id,
        new_user.id,
        role.role_name,
        parent_id,
    )
    return {"id": new_user.id}


@router.get("/children", response_model=List[UserSimple])
async def list_children_users(
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    """
    获取当前账号创建的子账号，并附带在线状态：
    - super_admin / manager 可查看
    - 在线：last_active_at 在 SESSION_TIMEOUT_SECONDS 内 且 session.expired=0
    """
    current_user, current_role_name = user_role
    if current_role_name not in (ROLE_MANAGER, ROLE_SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="No permission")

    stmt = (
        select(User.id, User.username, User.real_name, Role.role_name)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(User.parent_id == current_user.id)
        .order_by(User.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    user_ids = [r.id for r in rows]
    if not user_ids:
        return []

    stmt_session = (
        select(
            UserSession.user_id,
            func.max(UserSession.last_active_at).label("last_active_at"),
            func.max(UserSession.expired).label("max_expired"),
        )
        .where(UserSession.user_id.in_(user_ids))
        .group_by(UserSession.user_id)
    )
    srows = (await db.execute(stmt_session)).all()
    session_map = {r.user_id: r for r in srows}

    now = datetime.utcnow()
    ttl = int(getattr(settings, "SESSION_TIMEOUT_SECONDS", 7200) or 7200)

    out: List[UserSimple] = []
    for r in rows:
        sess = session_map.get(r.id)
        online = False
        if sess and int(sess.max_expired or 0) == 0 and sess.last_active_at is not None:
            if (now - sess.last_active_at) <= timedelta(seconds=ttl):
                online = True

        out.append(
            UserSimple(
                id=r.id,
                username=r.username,
                real_name=r.real_name,
                role_name=r.role_name,
                is_online=online,
            )
        )

    return out
