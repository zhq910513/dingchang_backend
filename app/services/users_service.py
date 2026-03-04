# app/services/users_service.py
# encoding: utf-8
from __future__ import annotations

"""
用户/账号管理服务（新表口径 / API 薄壳）

职责：
- 账号 CRUD 的业务规则全部在此处落地
- API 仅做入参解析 + 调用本服务 + 按 schemas 输出

注意：
- 保留 team_name + team_names 双字段（不同语义）
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Sequence

from sqlalchemy import select, func, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_SALES,
    ROLE_FINANCE,
    ROLE_MARKET,
)
from app.core.security import hash_password
from app.models.user import User
from app.models.user_role import UserRole
from app.models.role import Role

logger = logging.getLogger(__name__)
_BJ = ZoneInfo("Asia/Shanghai")


async def _get_role_id(db: AsyncSession, role_name: str) -> int:
    rid = (await db.execute(select(Role.id).where(Role.name == role_name))).scalars().first()
    if not rid:
        raise ValueError(f"角色不存在: {role_name}")
    return int(rid)


async def list_users(*, db: AsyncSession, keyword: Optional[str] = None, role: Optional[str] = None) -> Sequence[User]:
    q = select(User)
    if keyword:
        like = f"%{keyword.strip()}%"
        q = q.where((User.username.like(like)) | (User.display_name.like(like)))
    if role:
        q = q.join(UserRole, UserRole.user_id == User.id).join(Role, Role.id == UserRole.role_id).where(
            Role.name == role)
    q = q.order_by(User.id.desc())
    return list((await db.execute(q)).scalars().all())


async def create_user(
        *,
        db: AsyncSession,
        current_user: User,
        current_role: str,
        username: str,
        password: str,
        display_name: str = "",
        phone: str = "",
        role_name: str,
        team_name: Optional[str] = None,
        team_names: Optional[str] = None,
        created_by: Optional[int] = None,
) -> User:
    """
    约束（与你早期代码一致的核心规则）：
    - manager 创建子账号：子账号必须落在 manager 的团队范围内，且 role 只能是 sales/finance/market
    - super_admin 可创建 manager/sales/finance/market
    """
    now = datetime.now(_BJ).replace(tzinfo=None)

    if current_role == ROLE_MANAGER:
        if role_name not in {ROLE_SALES, ROLE_FINANCE, ROLE_MARKET}:
            raise PermissionError("manager 只能创建 sales/finance/market 子账号")
        # manager must choose child team_name within its accessible teams
        mgr_team_name = (getattr(current_user, "team_name", None) or "").strip()
        mgr_team_names = (getattr(current_user, "team_names", None) or "").strip()
        mgr_teams = [t.strip() for t in mgr_team_names.split(",") if t.strip()] if mgr_team_names else []
        if mgr_team_name and mgr_team_name not in mgr_teams:
            mgr_teams.append(mgr_team_name)
        child_team = (team_name or "").strip()
        if not child_team:
            raise ValueError("子账号必须指定 team_name")
        if child_team not in mgr_teams:
            raise PermissionError("子账号 team_name 不在经理团队范围内")
        team_name = child_team
        team_names = ""  # 子账号不设置 team_names
        created_by = current_user.id

    if current_role == ROLE_SUPER_ADMIN:
        # super_admin can create manager (team_names optional)
        if role_name == ROLE_MANAGER:
            # if team_names provided set default team_name from first
            tn = (team_names or "").strip()
            teams = [t.strip() for t in tn.split(",") if t.strip()] if tn else []
            if teams and not (team_name or "").strip():
                team_name = teams[0]
        else:
            # non-manager should have a team_name
            if not (team_name or "").strip():
                raise ValueError("账号必须指定 team_name")

    u = User(
        username=username.strip(),
        display_name=display_name.strip(),
        phone=(phone or "").strip(),
        password_hash=hash_password(password),
        team_name=(team_name or "").strip() or None,
        team_names=(team_names or "").strip() or "",
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()

    rid = await _get_role_id(db, role_name)
    db.add(UserRole(user_id=u.id, role_id=rid, created_at=now, updated_at=now))

    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise ValueError(f"创建失败: {e}")

    await db.refresh(u)
    return u


async def update_user(
        *,
        db: AsyncSession,
        current_user: User,
        current_role: str,
        user_id: int,
        display_name: Optional[str] = None,
        phone: Optional[str] = None,
        password: Optional[str] = None,
        team_name: Optional[str] = None,
        team_names: Optional[str] = None,
) -> User:
    now = datetime.now(_BJ).replace(tzinfo=None)
    u = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if not u:
        raise ValueError("用户不存在")

    if current_role == ROLE_MANAGER:
        # manager can only edit its created children
        if getattr(u, "created_by", None) != current_user.id:
            raise PermissionError("无权限编辑该用户")
        # forbid editing manager accounts
        # check target role
        rname = (await db.execute(
            select(Role.name).select_from(UserRole).join(Role, Role.id == UserRole.role_id).where(
                UserRole.user_id == u.id)
        )).scalars().first()
        if rname == ROLE_MANAGER:
            raise PermissionError("manager 不能编辑经理账号")

    if display_name is not None:
        u.display_name = display_name.strip()
    if phone is not None:
        u.phone = phone.strip()
    if password:
        u.password_hash = hash_password(password)
    # team fields
    if team_name is not None:
        u.team_name = team_name.strip() or None
    if team_names is not None:
        u.team_names = team_names.strip()

    u.updated_at = now
    await db.commit()
    await db.refresh(u)
    return u


async def delete_user(
        *,
        db: AsyncSession,
        current_user: User,
        current_role: str,
        user_id: int,
) -> None:
    u = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if not u:
        return

    if current_role == ROLE_SUPER_ADMIN:
        if u.id == current_user.id:
            raise PermissionError("禁止删除自己")
        # forbid delete super_admin
        rname = (await db.execute(
            select(Role.name).select_from(UserRole).join(Role, Role.id == UserRole.role_id).where(
                UserRole.user_id == u.id)
        )).scalars().first()
        if rname == ROLE_SUPER_ADMIN:
            raise PermissionError("禁止删除 super_admin")
        if rname == ROLE_MANAGER:
            # must have no children
            cnt = (await db.execute(
                select(func.count()).select_from(User).where(User.created_by == u.id))).scalars().first() or 0
            if cnt:
                raise PermissionError("删除经理前必须先删除其子账号")
    elif current_role == ROLE_MANAGER:
        if getattr(u, "created_by", None) != current_user.id:
            raise PermissionError("无权限删除该用户")
    else:
        raise PermissionError("无权限删除用户")

    # hard delete roles then user
    await db.execute(delete(UserRole).where(UserRole.user_id == u.id))
    await db.delete(u)
    await db.commit()
