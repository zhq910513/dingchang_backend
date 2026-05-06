# app/api/deps.py
# encoding: utf-8
from __future__ import annotations

"""
依赖项（Dependencies）

严格口径：
- 会话只认 DB 的 UserSession + expired + last_active_at + settings.SESSION_TIMEOUT_SECONDS
- 鉴权 Header 只认：X-Session-Token
- Session 过期后：标记 expired=1，返回 401
- 心跳续命：按节流时间更新 last_active_at，避免每请求 commit 打爆 DB
- 主角色：按业务优先级选主角色，避免多角色时出现不稳定行为
- 时间口径统一：北京时间 naive DATETIME

当前版本策略（安全优先）：
- session 校验：DB-first，不使用 Redis 读缓存
- 用户状态 / 角色集合 / 主角色：每次回 DB 校验
- Redis 相关函数保留为 no-op 兼容位，避免外部调用炸裂
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple, List
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from app.core.config import settings
from app.core.constants import (
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_FINANCE,
    ROLE_MARKET,
    ROLE_SALES,
)
from app.core.db import get_db
from app.models.role import Role
from app.models.session import UserSession
from app.models.user import User
from app.models.user_role import UserRole

BJ_TZ = ZoneInfo("Asia/Shanghai")

# 心跳续命节流时间：默认 30 秒
SESSION_HEARTBEAT_INTERVAL_SECONDS = int(
    os.getenv("SESSION_HEARTBEAT_INTERVAL_SECONDS", "30") or "30"
)

_ROLE_PRIORITY = {
    ROLE_SUPER_ADMIN: 0,
    ROLE_MANAGER: 10,
    ROLE_FINANCE: 20,
    ROLE_MARKET: 30,
    ROLE_SALES: 40,
}


@dataclass(frozen=True)
class CurrentUserContext:
    """
    deps 的唯一输出结构：

    - user：当前登录用户
    - primary_role：主角色（按优先级选取）
    - role_names：全部角色集合
    - team_names：用户可访问团队集合
    - team_ids：当前体系暂不使用，保留扩展位
    """

    user: User
    primary_role: Optional[str]
    role_names: Tuple[str, ...]
    team_names: Tuple[str, ...]
    team_ids: Tuple[int, ...]


def _pick_primary_role(role_names: Tuple[str, ...]) -> Optional[str]:
    if not role_names:
        return None

    known = [role_name for role_name in role_names if role_name in _ROLE_PRIORITY]
    if known:
        return min(known, key=lambda role_name: _ROLE_PRIORITY.get(role_name, 999999))

    return role_names[0]


def _now_bj_naive() -> datetime:
    return datetime.now(BJ_TZ).replace(tzinfo=None)


def _to_bj_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    try:
        return dt.astimezone(BJ_TZ).replace(tzinfo=None)
    except Exception:
        return dt.replace(tzinfo=None)


def _safe_int(v: object, default: int) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except Exception:
        return default


def _get_redis():
    try:
        from app.core.db import redis  # type: ignore
        return redis
    except Exception:
        return None


def _as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, (tuple, set)):
        return list(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if "," in s:
            return [x.strip() for x in s.split(",") if x and x.strip()]
        return [s]
    return [v]


def _extract_teams_from_user(user: User) -> Tuple[Tuple[int, ...], Tuple[str, ...]]:
    """
    团队抽取（冻结字段口径）：
    - user.team_name：默认/落点团队（单值）
    - user.team_names：可访问团队集合（CSV 字符串）
    """
    team_names_set: set[str] = set()

    team_name = (getattr(user, "team_name", None) or "").strip()
    if team_name:
        team_names_set.add(team_name)

    raw_team_names = (getattr(user, "team_names", None) or "").strip()
    if raw_team_names:
        for team_name_item in raw_team_names.split(","):
            team_name_item = (team_name_item or "").strip()
            if team_name_item:
                team_names_set.add(team_name_item)

    team_ids: Tuple[int, ...] = tuple()
    team_names: Tuple[str, ...] = tuple(sorted(team_names_set))
    return team_ids, team_names


async def _delete_redis_session_cache(token: str) -> None:
    """
    兼容位：
    当前 session 不走 Redis 读缓存，但保留删除入口，避免外部调用方报错。
    """
    try:
        redis_client = _get_redis()
        if redis_client:
            await redis_client.delete(f"session:{token}")
    except Exception:
        pass


async def _cache_redis_session(token: str, user_id: int, ttl: int) -> None:
    """
    兼容位：
    当前版本不使用 Redis session 读缓存，为安全起见这里不写入任何可被信任的数据。
    """
    _ = token
    _ = user_id
    _ = ttl
    return None


async def _expire_session_row(db: AsyncSession, sess: UserSession) -> None:
    try:
        await db.execute(
            update(UserSession)
            .where(UserSession.id == sess.id)
            .values(expired=1)
        )
        await db.commit()
        try:
            sess.expired = 1
        except Exception:
            pass
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


async def get_current_session(
    db: AsyncSession = Depends(get_db),
    x_session_token: Optional[str] = Header(default=None, alias="X-Session-Token"),
) -> UserSession:
    token = (x_session_token or "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Session-Token",
        )

    stmt = (
        select(UserSession, User, Role.role_name)
        .select_from(UserSession)
        .join(User, User.id == UserSession.user_id)
        .outerjoin(UserRole, UserRole.user_id == User.id)
        .outerjoin(Role, Role.id == UserRole.role_id)
        .options(lazyload("*"))
        .where(UserSession.session_token == token)
        .order_by(Role.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    sess = rows[0][0] if rows else None
    if sess is not None:
        user = rows[0][1]
        role_names = tuple(
            str(role_name or "").strip()
            for _, _, role_name in rows
            if str(role_name or "").strip()
        )
        setattr(sess, "_auth_user", user)
        setattr(user, "_auth_role_names", role_names)

    if not sess or int(getattr(sess, "expired", 0) or 0) == 1:
        await _delete_redis_session_cache(token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session",
        )

    last_active_at = getattr(sess, "last_active_at", None)
    if not last_active_at:
        await _expire_session_row(db, sess)
        await _delete_redis_session_cache(token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session (no last_active_at)",
        )

    ttl = _safe_int(getattr(settings, "SESSION_TIMEOUT_SECONDS", 7200), 7200)
    if ttl <= 0:
        ttl = 7200

    now = _now_bj_naive()
    last_active_bj = _to_bj_naive(last_active_at)

    # 超时：标记过期 + 清缓存 + 返回 401
    if now - last_active_bj > timedelta(seconds=ttl):
        await _expire_session_row(db, sess)
        await _delete_redis_session_cache(token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired",
        )

    heartbeat_interval_seconds = SESSION_HEARTBEAT_INTERVAL_SECONDS
    if heartbeat_interval_seconds < 0:
        heartbeat_interval_seconds = 0

    try:
        need_touch = True
        if heartbeat_interval_seconds > 0:
            need_touch = (now - last_active_bj) > timedelta(seconds=heartbeat_interval_seconds)

        if need_touch:
            await db.execute(
                update(UserSession)
                .where(UserSession.id == sess.id)
                .values(last_active_at=now)
            )
            await db.commit()
            try:
                sess.last_active_at = now
            except Exception:
                pass
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass

    await _cache_redis_session(token, int(getattr(sess, "user_id", 0) or 0), ttl)
    return sess


async def get_current_user(
    sess: UserSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
) -> User:
    uid = int(getattr(sess, "user_id", 0) or 0)
    if uid <= 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session user_id",
        )

    user = getattr(sess, "_auth_user", None)
    if not isinstance(user, User):
        user = (
            await db.execute(
                select(User)
                .options(lazyload("*"))
                .where(User.id == uid)
                .limit(1)
            )
        ).scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if int(getattr(user, "status", 0) or 0) != 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User disabled",
        )

    return user


async def get_current_user_context(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUserContext:
    cached_role_names = getattr(user, "_auth_role_names", None)
    if cached_role_names is not None:
        role_names = tuple(
            str(role_name or "").strip()
            for role_name in cached_role_names
            if str(role_name or "").strip()
        )
    else:
        stmt = (
            select(Role.id, Role.role_name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
            .order_by(Role.id.asc())
        )
        rows = (await db.execute(stmt)).all()
        role_names = tuple([role_name for _, role_name in rows if role_name])

    primary_role = _pick_primary_role(role_names)
    team_ids, team_names = _extract_teams_from_user(user)

    return CurrentUserContext(
        user=user,
        primary_role=primary_role,
        role_names=role_names,
        team_names=team_names,
        team_ids=team_ids,
    )


# ---- 兼容导入名：统一返回 CurrentUserContext ----

async def get_current_user_with_roles(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUserContext:
    return await get_current_user_context(user=user, db=db)


async def get_current_user_with_role(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUserContext:
    return await get_current_user_context(user=user, db=db)


async def get_current_user_with_role_and_teams(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUserContext:
    return await get_current_user_context(user=user, db=db)
