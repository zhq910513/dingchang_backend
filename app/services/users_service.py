# app/services/users_service.py
# encoding: utf-8
from __future__ import annotations

"""用户/账号管理服务（新表口径 / API 薄壳）

承重墙（冻结口径）：
- 只允许使用已确认存在的字段：
    User: id, username, password_hash, status, team_name, team_names, created_at, updated_at
    Role: id, role_name
    UserRole: user_id, role_id
- 不兼容旧字段/旧逻辑：display_name/phone/created_by 等一律禁止出现在入参中
"""

import logging
from datetime import datetime
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ROLE_FINANCE,
    ROLE_MANAGER,
    ROLE_MARKET,
    ROLE_SALES,
    ROLE_SUPER_ADMIN,
    TEAM_NAMES,
)
from app.core.security import hash_password
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole

logger = logging.getLogger(__name__)

_BJ = ZoneInfo("Asia/Shanghai")
_ALLOWED_ROLES = {
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_SALES,
    ROLE_FINANCE,
    ROLE_MARKET,
}


def _now_bj_naive() -> datetime:
    return datetime.now(_BJ).replace(tzinfo=None)


def _normalize_team_names_csv(team_names: Optional[str]) -> str:
    """把 team_names 输入标准化为稳定 CSV：去空、去重、排序。"""
    if team_names is None:
        return ""
    s = str(team_names).strip()
    if not s:
        return ""
    parts = [x.strip() for x in s.split(",") if x and x.strip()]
    return ",".join(sorted(set(parts)))


def _validate_team_names(team_name: Optional[str], team_names_csv: str) -> None:
    """校验 team_name + team_names_csv 都在白名单内。"""
    names: list[str] = [x.strip() for x in team_names_csv.split(",") if x and x.strip()]
    if team_name:
        names.append(team_name.strip())

    for n in names:
        if n and n not in TEAM_NAMES:
            raise ValueError(f"非法团队：{n}")


def _require_super_admin(current_role: str) -> None:
    if current_role != ROLE_SUPER_ADMIN:
        raise PermissionError("仅超级管理员可操作用户管理")


def _validate_password(password: str) -> str:
    p = str(password)
    if len(p) < 6:
        raise ValueError("password 长度至少 6")
    return p


async def list_users(
    *,
    db: AsyncSession,
    keyword: Optional[str] = None,
    role: Optional[str] = None,
) -> Sequence[User]:
    stmt = select(User)

    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(User.username.like(like))

    if role:
        r = str(role).strip()
        stmt = (
            stmt.join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.role_name == r)
        )

    stmt = stmt.order_by(User.id.desc())
    return (await db.execute(stmt)).scalars().all()


async def create_user(
    *,
    db: AsyncSession,
    current_role: str,
    username: str,
    password: str,
    role_name: str,
    team_name: Optional[str] = None,
    team_names: Optional[str] = None,
) -> User:
    _require_super_admin(current_role)

    uname = str(username).strip()
    if not uname:
        raise ValueError("username is required")

    p = _validate_password(password)

    rname = str(role_name).strip()
    if rname not in _ALLOWED_ROLES:
        raise ValueError("role_name 不合法")

    tn = (str(team_name).strip() if team_name else None) or None
    tns_csv = _normalize_team_names_csv(team_names)
    _validate_team_names(tn, tns_csv)

    now = _now_bj_naive()
    user = User(
        username=uname,
        password_hash=hash_password(p),
        status=1,
        team_name=tn,
        team_names=tns_csv,
        created_at=now,
        updated_at=now,
    )
    db.add(user)

    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        logger.exception("create_user flush failed: %s", e)
        raise ValueError("用户名已存在") from e

    role_row = (
        (await db.execute(select(Role).where(Role.role_name == rname)))
        .scalars()
        .first()
    )
    if not role_row:
        await db.rollback()
        raise ValueError("角色不存在（请先初始化 seed）")

    db.add(UserRole(user_id=user.id, role_id=role_row.id))

    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        logger.exception("create_user commit failed: %s", e)
        raise ValueError("创建用户失败（请检查角色/唯一约束）") from e

    await db.refresh(user)
    return user


async def update_user(
    *,
    db: AsyncSession,
    current_role: str,
    user_id: int,
    password: Optional[str] = None,
    team_name: Optional[str] = None,
    team_names: Optional[str] = None,
) -> User:
    _require_super_admin(current_role)

    uid = int(user_id)
    user = (await db.execute(select(User).where(User.id == uid))).scalars().first()
    if not user:
        raise ValueError("用户不存在")

    changed = False

    if password is not None:
        p = _validate_password(password)
        user.password_hash = hash_password(p)
        changed = True

    tn = (str(team_name).strip() if team_name is not None else None) or None
    tns_csv = _normalize_team_names_csv(team_names)
    _validate_team_names(tn, tns_csv)

    if user.team_name != tn:
        user.team_name = tn
        changed = True
    if (user.team_names or "") != tns_csv:
        user.team_names = tns_csv
        changed = True

    if changed:
        user.updated_at = _now_bj_naive()
        await db.commit()
        await db.refresh(user)

    return user


async def delete_user(
    *,
    db: AsyncSession,
    current_user: User,
    current_role: str,
    user_id: int,
) -> None:
    _require_super_admin(current_role)

    uid = int(user_id)
    current_uid = int(getattr(current_user, "id", 0) or 0)
    if uid == current_uid:
        raise ValueError("不能删除自己")

    await db.execute(delete(UserRole).where(UserRole.user_id == uid))
    await db.execute(delete(User).where(User.id == uid))
    await db.commit()
