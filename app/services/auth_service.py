# app/services/auth_service.py
# encoding: utf-8
from __future__ import annotations

"""Authentication service backed by frozen user/session/role models."""

import logging
import os
from datetime import datetime, timedelta
from typing import Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from app.core.config import settings
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
_INVALID_LOGIN_MESSAGE = "用户名或密码错误"
_DISABLED_LOGIN_MESSAGE = "账号已禁用"
_MISSING_ROLE_MESSAGE = "账号未配置角色"

# Generated once with current PASSWORD_HASH_ITERATIONS so padded failures follow
# the real password-hash cost after security tuning.
_DUMMY_PASSWORD_HASH = hash_password("__dingchang_invalid_login_dummy__")

_DEFAULT_MAX_ACTIVE_SESSIONS_PER_USER = 8


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


def _now_bj_naive() -> datetime:
    return datetime.now(_BJ).replace(tzinfo=None)


def _safe_session_ttl_seconds() -> int:
    try:
        ttl = int(getattr(settings, "SESSION_TIMEOUT_SECONDS", 7200) or 7200)
    except Exception:
        ttl = 7200
    return ttl if ttl > 0 else 7200


def _safe_max_active_sessions_per_user() -> int:
    try:
        value = int(os.getenv("AUTH_MAX_ACTIVE_SESSIONS_PER_USER", "") or _DEFAULT_MAX_ACTIVE_SESSIONS_PER_USER)
    except Exception:
        value = _DEFAULT_MAX_ACTIVE_SESSIONS_PER_USER
    return max(value, 1)


def _burn_failed_login_cost(password: str) -> None:
    try:
        verify_password(password or " ", _DUMMY_PASSWORD_HASH)
    except Exception:
        # Failure-cost padding must never alter the business error returned.
        pass


async def _cleanup_user_sessions(
    *,
    db: AsyncSession,
    user_id: int,
    keep_session_id: int,
    now: datetime,
) -> None:
    ttl = _safe_session_ttl_seconds()
    cutoff = now - timedelta(seconds=ttl)

    await db.execute(
        update(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.expired == 0,
            UserSession.id != keep_session_id,
            UserSession.last_active_at < cutoff,
        )
        .values(expired=1)
    )

    max_active = _safe_max_active_sessions_per_user()
    keep_other_count = max(max_active - 1, 0)
    old_session_ids = list(
        (
            await db.execute(
                select(UserSession.id)
                .where(
                    UserSession.user_id == user_id,
                    UserSession.expired == 0,
                    UserSession.id != keep_session_id,
                )
                .order_by(UserSession.last_active_at.desc(), UserSession.id.desc())
                .offset(keep_other_count)
            )
        )
        .scalars()
        .all()
    )
    if not old_session_ids:
        return

    await db.execute(
        update(UserSession)
        .where(UserSession.id.in_(old_session_ids))
        .values(expired=1)
    )


async def login(*, db: AsyncSession, username: str, password: str) -> Tuple[
    User, str, UserSession, list[str], list[str], str
]:
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")

    if not normalized_username or not normalized_password:
        raise ValueError(_INVALID_LOGIN_MESSAGE)

    q = (
        select(User)
        .options(lazyload("*"))
        .where(User.username == normalized_username)
        .limit(1)
    )
    user = (await db.execute(q)).scalars().first()
    if not user:
        _burn_failed_login_cost(normalized_password)
        raise ValueError(_INVALID_LOGIN_MESSAGE)

    if int(getattr(user, "status", 0) or 0) != 1:
        _burn_failed_login_cost(normalized_password)
        raise ValueError(_DISABLED_LOGIN_MESSAGE)

    password_hash = str(getattr(user, "password_hash", "") or "")
    if not verify_password(normalized_password, password_hash):
        raise ValueError(_INVALID_LOGIN_MESSAGE)

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
    if not role_names:
        raise ValueError(_MISSING_ROLE_MESSAGE)

    teams, team_name = _normalize_team_scope(user)

    now = _now_bj_naive()
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
        await db.flush()
        await _cleanup_user_sessions(
            db=db,
            user_id=int(getattr(user, "id", 0) or 0),
            keep_session_id=int(getattr(sess, "id", 0) or 0),
            now=now,
        )
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

    q = (
        select(UserSession)
        .options(lazyload("*"))
        .where(UserSession.session_token == normalized_token)
        .limit(1)
    )
    sess = (await db.execute(q)).scalars().first()
    if not sess:
        return

    now = _now_bj_naive()
    try:
        await db.execute(
            update(UserSession)
            .where(UserSession.id == sess.id)
            .values(expired=1, last_active_at=now)
        )
        await db.commit()
        try:
            sess.expired = 1
            sess.last_active_at = now
        except Exception:
            pass
    except Exception:
        await db.rollback()
        logger.exception("logout transaction commit failed")
        raise
