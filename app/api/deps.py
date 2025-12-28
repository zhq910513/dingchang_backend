# app/api/deps.py
# encoding: utf-8
"""
依赖项（Dependencies）

去兼容版原则：
- 会话只认 DB 的 UserSession + expired + last_active_at + settings.SESSION_TIMEOUT_SECONDS
- 心跳续命：节流更新 last_active_at（避免每请求 commit 打爆 DB）
- 主角色取 Role.id 最小值（稳定）
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.config import settings
from app.models.session import UserSession
from app.models.user import User
from app.models.role import Role
from app.models.user_role import UserRole

logger = logging.getLogger(__name__)

# ✅ 心跳写库节流：默认 30s 写一次（生产强烈建议开启）
# 设为 0 可恢复“每请求更新”
SESSION_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("SESSION_HEARTBEAT_INTERVAL_SECONDS", "30") or "30")


def _utcnow_naive() -> datetime:
    """
    统一返回 UTC naive datetime，避免 tzinfo 写入 MySQL DATETIME 带来不确定性。
    """
    return datetime.utcnow()


def _to_utc_naive(dt: datetime) -> datetime:
    """
    兼容 DB 返回 naive/aware 两种情况：
    - naive：按 UTC 解释（与当前项目其他地方的 datetime.utcnow() 存储一致）
    - aware：转成 UTC 后去 tzinfo
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


async def get_current_session(
    db: AsyncSession = Depends(get_db),
    x_session_token: Optional[str] = Header(default=None, alias="X-Session-Token"),
) -> UserSession:
    if not x_session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Session-Token")

    stmt = select(UserSession).where(UserSession.session_token == x_session_token)
    sess = (await db.execute(stmt)).scalars().first()
    if not sess or int(getattr(sess, "expired", 0) or 0) == 1:
        # 若启用了 Redis，顺手清理（失败不影响）
        try:
            from app.core.db import redis
            if redis:
                await redis.delete(f"session:{x_session_token}")
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    last_active_at = getattr(sess, "last_active_at", None)
    if not last_active_at:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session (no last_active_at)")

    ttl = int(getattr(settings, "SESSION_TIMEOUT_SECONDS", 7200) or 7200)

    now = _utcnow_naive()
    last_active_utc = _to_utc_naive(last_active_at)

    if now - last_active_utc > timedelta(seconds=ttl):
        # 过期则顺手标记 expired=1（失败不影响返回）
        try:
            sess.expired = 1
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                pass

        # Redis 删除（有就删）
        try:
            from app.core.db import redis
            if redis:
                await redis.delete(f"session:{x_session_token}")
        except Exception:
            pass

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")

    # ✅ 心跳续命：节流写 DB（默认 30 秒最多写一次）
    try:
        need_touch = True
        if SESSION_HEARTBEAT_INTERVAL_SECONDS > 0:
            need_touch = (now - last_active_utc) > timedelta(seconds=SESSION_HEARTBEAT_INTERVAL_SECONDS)

        if need_touch:
            sess.last_active_at = now
            await db.commit()
        # 否则不写库，降低 DB 压力
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass

    # 若启用了 Redis，则刷新 TTL（失败不影响）
    try:
        from app.core.db import redis
        if redis:
            await redis.set(f"session:{x_session_token}", str(sess.user_id), ex=ttl)
    except Exception:
        pass

    return sess


async def get_current_user(
    sess: UserSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
) -> User:
    uid = int(getattr(sess, "user_id", 0) or 0)
    if uid <= 0:
        raise HTTPException(status_code=401, detail="Invalid session user_id")

    user = (await db.execute(select(User).where(User.id == uid))).scalars().first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if int(getattr(user, "status", 0) or 0) != 1:
        raise HTTPException(status_code=403, detail="User disabled")

    return user


async def get_current_user_with_role(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tuple[User, Optional[str]]:
    stmt = (
        select(Role.role_name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id)
        .order_by(Role.id.asc())
    )
    role_name = (await db.execute(stmt)).scalars().first()
    return user, role_name
