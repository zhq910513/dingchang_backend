# app/services/auth_service.py
# encoding: utf-8
from __future__ import annotations

"""
认证服务（新表口径 / 不做兼容）

原则：
- 只依赖冻结 Models：User/UserSession/UserRole/Role（均映射 *_new）
- 输出严格对齐 schemas：LoginOut
- 时间口径：北京时间 naive DATETIME
"""

import logging
from datetime import datetime
from typing import Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_MARKET, ROLE_SALES, ROLE_SUPER_ADMIN
from app.core.security import (
    generate_session_token,
    hash_password,
    needs_password_rehash,
    verify_password,
)
from app.models.role import Role
from app.models.session import UserSession
from app.models.user import User
from app.models.user_role import UserRole

logger = logging.getLogger(__name__)

_ROLE_PRIORITY = {
    ROLE_SUPER_ADMIN: 0,
    ROLE_MANAGER: 10,
    ROLE_FINANCE: 20,
    ROLE_MARKET: 30,
    ROLE_SALES: 40,
}

_BJ = ZoneInfo("Asia/Shanghai")


def _normalize_team_scope(user: User) -> tuple[list[str], str]:
    team_name = (getattr(user, "team_name", None) or "").strip()
    raw_team_names = (getattr(user, "team_names", None) or "").strip()

    teams: list[str] = []
    if raw_team_names:
        for item in raw_team_names.split(","):
            s = (item or "").strip()
            if s and s not in teams:
                teams.append(s)

    if team_name and team_name not in teams:
        teams.append(team_name)

    if not team_name and teams:
        team_name = teams[0]

    return teams, team_name


async def login(*, db: AsyncSession, username: str, password: str) -> Tuple[
    User, str, UserSession, list[str], list[str], str
]:
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")

    if not normalized_username or not normalized_password:
        raise ValueError("用户名或密码错误")

    q = (
        select(User)
        .where(User.username == normalized_username)
        .limit(1)
    )
    user = (await db.execute(q)).scalars().first()
    if not user:
        raise ValueError("用户名或密码错误")

    if int(getattr(user, "status", 0) or 0) != 1:
        raise ValueError("账号已禁用")

    password_hash = str(getattr(user, "password_hash", "") or "")
    if not verify_password(normalized_password, password_hash):
        raise ValueError("用户名或密码错误")

    if needs_password_rehash(password_hash):
        try:
            user.password_hash = hash_password(normalized_password)
        except Exception:
            logger.exception("password rehash failed for username=%s", normalized_username)

    role_q = (
        select(Role.role_name)
        .select_from(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user.id)
    )
    role_names = list((await db.execute(role_q)).scalars().all())
    role_names = [str(x or "").strip() for x in role_names if str(x or "").strip()]
    role_names.sort(key=lambda x: _ROLE_PRIORITY.get(x, 9999))

    teams, team_name = _normalize_team_scope(user)

    now = datetime.now(_BJ).replace(tzinfo=None)
    token = generate_session_token()

    sess = UserSession(
        user_id=user.id,
        session_token=token,
        created_at=now,
        last_active_at=now,
        expired=0,
    )
    db.add(sess)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("login transaction commit failed for username=%s", normalized_username)
        raise

    try:
        await db.refresh(sess)
    except Exception:
        logger.exception("refresh session failed after login username=%s", normalized_username)
        raise

    return user, token, sess, role_names, teams, team_name


async def logout(*, db: AsyncSession, token: str) -> None:
    normalized_token = str(token or "").strip()
    if not normalized_token:
        return

    q = select(UserSession).where(UserSession.session_token == normalized_token).limit(1)
    sess = (await db.execute(q)).scalars().first()
    if not sess:
        return

    await db.delete(sess)
    await db.commit()