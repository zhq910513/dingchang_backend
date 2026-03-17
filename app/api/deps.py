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

本阶段优化：
- Redis 改为 session 读缓存优先
- 命中缓存时避免每次先查 DB 的固定开销
- 保持现有返回结构与业务语义不变
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    ROLE_FINANCE,
    ROLE_MANAGER,
    ROLE_MARKET,
    ROLE_SALES,
    ROLE_SUPER_ADMIN,
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


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        return default


def _get_redis():
    try:
        from app.core.db import redis  # type: ignore
        return redis
    except Exception:
        return None


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if "," in stripped:
            return [item.strip() for item in stripped.split(",") if item and item.strip()]
        return [stripped]
    return [value]


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
        for team in raw_team_names.split(","):
            team = (team or "").strip()
            if team:
                team_names_set.add(team)

    team_ids: Tuple[int, ...] = tuple()
    team_names: Tuple[str, ...] = tuple(sorted(team_names_set))
    return team_ids, team_names


def _session_cache_key(token: str) -> str:
    return f"session:{token}"


def _session_ctx_cache_key(user_id: int) -> str:
    return f"session_ctx:{user_id}"


async def _delete_redis_session_cache(token: str) -> None:
    try:
        redis_client = _get_redis()
        if redis_client:
            await redis_client.delete(_session_cache_key(token))
    except Exception:
        pass


async def _delete_redis_user_context_cache(user_id: int) -> None:
    try:
        redis_client = _get_redis()
        if redis_client and user_id > 0:
            await redis_client.delete(_session_ctx_cache_key(user_id))
    except Exception:
        pass


async def _cache_redis_session_payload(
    token: str,
    *,
    session_id: int,
    user_id: int,
    last_active_at: datetime,
    ttl: int,
) -> None:
    try:
        redis_client = _get_redis()
        if not redis_client:
            return

        payload = {
            "session_id": int(session_id),
            "user_id": int(user_id),
            "last_active_at": last_active_at.isoformat(sep=" "),
        }
        await redis_client.set(
            _session_cache_key(token),
            json.dumps(payload, ensure_ascii=False),
            ex=ttl,
        )
    except Exception:
        pass


async def _read_redis_session_payload(token: str) -> Optional[Dict[str, Any]]:
    try:
        redis_client = _get_redis()
        if not redis_client:
            return None

        raw = await redis_client.get(_session_cache_key(token))
        if not raw:
            return None

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")

        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None

        session_id = _safe_int(payload.get("session_id"), 0)
        user_id = _safe_int(payload.get("user_id"), 0)
        last_active_at_raw = str(payload.get("last_active_at") or "").strip()

        if session_id <= 0 or user_id <= 0 or not last_active_at_raw:
            return None

        try:
            last_active_at = datetime.fromisoformat(last_active_at_raw)
        except Exception:
            return None

        return {
            "session_id": session_id,
            "user_id": user_id,
            "last_active_at": last_active_at,
        }
    except Exception:
        return None


async def _cache_redis_user_context_payload(
    user: User,
    *,
    primary_role: Optional[str],
    role_names: Tuple[str, ...],
    team_names: Tuple[str, ...],
    ttl: int,
) -> None:
    try:
        redis_client = _get_redis()
        if not redis_client:
            return

        user_id = int(getattr(user, "id", 0) or 0)
        if user_id <= 0:
            return

        payload = {
            "user": {
                "id": user_id,
                "username": str(getattr(user, "username", "") or ""),
                "real_name": getattr(user, "real_name", None),
                "team_name": getattr(user, "team_name", None),
                "team_names": getattr(user, "team_names", None),
                "status": int(getattr(user, "status", 0) or 0),
            },
            "primary_role": primary_role,
            "role_names": list(role_names),
            "team_names": list(team_names),
        }

        await redis_client.set(
            _session_ctx_cache_key(user_id),
            json.dumps(payload, ensure_ascii=False),
            ex=ttl,
        )
    except Exception:
        pass


async def _read_redis_user_context_payload(user_id: int) -> Optional[Dict[str, Any]]:
    try:
        redis_client = _get_redis()
        if not redis_client or user_id <= 0:
            return None

        raw = await redis_client.get(_session_ctx_cache_key(user_id))
        if not raw:
            return None

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")

        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None

        user_data = payload.get("user")
        if not isinstance(user_data, dict):
            return None

        cached_user_id = _safe_int(user_data.get("id"), 0)
        if cached_user_id != user_id:
            return None

        return payload
    except Exception:
        return None


def _build_detached_user_from_cache(user_data: Dict[str, Any]) -> User:
    user = User()
    user.id = _safe_int(user_data.get("id"), 0)
    user.username = str(user_data.get("username", "") or "")
    user.real_name = user_data.get("real_name")
    user.team_name = user_data.get("team_name")
    user.team_names = user_data.get("team_names")
    user.status = _safe_int(user_data.get("status"), 0)
    return user


async def _expire_session_row(db: AsyncSession, session_row: UserSession) -> None:
    try:
        await db.execute(
            update(UserSession)
            .where(UserSession.id == session_row.id)
            .values(expired=1)
        )
        await db.commit()
        try:
            session_row.expired = 1
        except Exception:
            pass
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


def _get_session_timeout_seconds() -> int:
    ttl = _safe_int(getattr(settings, "SESSION_TIMEOUT_SECONDS", 7200), 7200)
    if ttl <= 0:
        ttl = 7200
    return ttl


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

    ttl = _get_session_timeout_seconds()
    now = _now_bj_naive()
    heartbeat_interval_seconds = SESSION_HEARTBEAT_INTERVAL_SECONDS
    if heartbeat_interval_seconds < 0:
        heartbeat_interval_seconds = 0

    cached_session = await _read_redis_session_payload(token)
    if cached_session:
        cached_last_active_at = _to_bj_naive(cached_session["last_active_at"])

        if now - cached_last_active_at > timedelta(seconds=ttl):
            await _delete_redis_session_cache(token)
            await _delete_redis_user_context_cache(int(cached_session["user_id"]))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired",
            )

        session_row = UserSession()
        session_row.id = int(cached_session["session_id"])
        session_row.user_id = int(cached_session["user_id"])
        session_row.session_token = token
        session_row.expired = 0
        session_row.last_active_at = cached_last_active_at

        if heartbeat_interval_seconds == 0 or (
            now - cached_last_active_at > timedelta(seconds=heartbeat_interval_seconds)
        ):
            try:
                await db.execute(
                    update(UserSession)
                    .where(
                        UserSession.id == session_row.id,
                        UserSession.expired == 0,
                    )
                    .values(last_active_at=now)
                )
                await db.commit()
                session_row.last_active_at = now
                await _cache_redis_session_payload(
                    token,
                    session_id=session_row.id,
                    user_id=session_row.user_id,
                    last_active_at=now,
                    ttl=ttl,
                )
            except Exception:
                try:
                    await db.rollback()
                except Exception:
                    pass

        return session_row

    stmt = (
        select(UserSession)
        .where(UserSession.session_token == token)
        .limit(1)
    )
    session_row = (await db.execute(stmt)).scalars().first()

    if not session_row or int(getattr(session_row, "expired", 0) or 0) == 1:
        await _delete_redis_session_cache(token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session",
        )

    last_active_at = getattr(session_row, "last_active_at", None)
    if not last_active_at:
        await _expire_session_row(db, session_row)
        await _delete_redis_session_cache(token)
        await _delete_redis_user_context_cache(int(getattr(session_row, "user_id", 0) or 0))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session (no last_active_at)",
        )

    last_active_bj = _to_bj_naive(last_active_at)

    if now - last_active_bj > timedelta(seconds=ttl):
        await _expire_session_row(db, session_row)
        await _delete_redis_session_cache(token)
        await _delete_redis_user_context_cache(int(getattr(session_row, "user_id", 0) or 0))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired",
        )

    try:
        need_touch = True
        if heartbeat_interval_seconds > 0:
            need_touch = (now - last_active_bj) > timedelta(seconds=heartbeat_interval_seconds)

        if need_touch:
            await db.execute(
                update(UserSession)
                .where(UserSession.id == session_row.id)
                .values(last_active_at=now)
            )
            await db.commit()
            try:
                session_row.last_active_at = now
            except Exception:
                pass
        await _cache_redis_session_payload(
            token,
            session_id=int(getattr(session_row, "id", 0) or 0),
            user_id=int(getattr(session_row, "user_id", 0) or 0),
            last_active_at=_to_bj_naive(getattr(session_row, "last_active_at", now) or now),
            ttl=ttl,
        )
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass

    return session_row


async def get_current_user(
    sess: UserSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
) -> User:
    user_id = int(getattr(sess, "user_id", 0) or 0)
    if user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session user_id",
        )

    cached_context = await _read_redis_user_context_payload(user_id)
    if cached_context:
        user_data = cached_context.get("user")
        if isinstance(user_data, dict) and _safe_int(user_data.get("status"), 0) == 1:
            return _build_detached_user_from_cache(user_data)

    user = (
        await db.execute(
            select(User).where(User.id == user_id).limit(1)
        )
    ).scalars().first()
    if not user:
        await _delete_redis_user_context_cache(user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if int(getattr(user, "status", 0) or 0) != 1:
        await _delete_redis_user_context_cache(user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User disabled",
        )

    return user


async def get_current_user_context(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUserContext:
    user_id = int(getattr(user, "id", 0) or 0)
    ttl = _get_session_timeout_seconds()

    cached_context = await _read_redis_user_context_payload(user_id)
    if cached_context:
        user_data = cached_context.get("user")
        if isinstance(user_data, dict) and _safe_int(user_data.get("status"), 0) == 1:
            cached_user = _build_detached_user_from_cache(user_data)
            role_names = tuple(
                [
                    str(role_name).strip()
                    for role_name in _as_list(cached_context.get("role_names"))
                    if str(role_name).strip()
                ]
            )
            primary_role = str(cached_context.get("primary_role") or "").strip() or None
            team_names = tuple(
                [
                    str(team_name).strip()
                    for team_name in _as_list(cached_context.get("team_names"))
                    if str(team_name).strip()
                ]
            )
            return CurrentUserContext(
                user=cached_user,
                primary_role=primary_role,
                role_names=role_names,
                team_names=team_names,
                team_ids=tuple(),
            )

    stmt = (
        select(Role.id, Role.role_name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
        .order_by(Role.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    role_names = tuple([role_name for _, role_name in rows if role_name])

    primary_role = _pick_primary_role(role_names)
    team_ids, team_names = _extract_teams_from_user(user)

    await _cache_redis_user_context_payload(
        user,
        primary_role=primary_role,
        role_names=role_names,
        team_names=team_names,
        ttl=ttl,
    )

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