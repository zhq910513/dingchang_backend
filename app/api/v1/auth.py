# app/api/v1/auth.py
# encoding: utf-8
from __future__ import annotations

"""
认证相关接口：
- /auth/login
- /auth/logout

原则：
- Schema 已冻结：app.schemas.auth.LoginIn / LoginOut
- DB 存北京时间 naive DATETIME（timezone=False）
"""

import hashlib
import hmac
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_session
from app.core.config import settings
from app.core.db import get_db
from app.core.security import generate_session_token, hash_password, verify_password
from app.models.role import Role
from app.models.session import UserSession
from app.models.user import User
from app.models.user_role import UserRole
from app.schemas.auth import LoginIn, LoginOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

BJ_TZ = ZoneInfo("Asia/Shanghai")


def _now_bj_naive() -> datetime:
    return datetime.now(BJ_TZ).replace(tzinfo=None)


def _split_team_names_csv(v: str | None) -> list[str]:
    s = (v or "").strip()
    if not s:
        return []
    parts = [x.strip() for x in s.split(",")]
    return [x for x in parts if x]


def _is_legacy_password_hash(hashed: str) -> bool:
    if not hashed or len(hashed) != 64:
        return False
    try:
        int(hashed, 16)
        return True
    except Exception:
        return False


def _legacy_sha256_hex(plain: str) -> str:
    return hashlib.sha256((plain or "").encode("utf-8")).hexdigest()


def _verify_password_compat(plain: str, stored_hash: str) -> bool:
    try:
        if _is_legacy_password_hash(stored_hash):
            return hmac.compare_digest(_legacy_sha256_hex(plain), (stored_hash or "").lower())
        return bool(verify_password(plain, stored_hash))
    except Exception:
        return False


async def _get_primary_role_name(db: AsyncSession, user_id: int) -> str:
    stmt = (
        select(Role.role_name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == int(user_id))
        .order_by(Role.id.asc())
    )
    rn = (await db.execute(stmt)).scalars().first()
    return str(rn or "")


async def _find_user_by_login_key(db: AsyncSession, login_key: str) -> User | None:
    key = (login_key or "").strip()
    if not key:
        return None

    # 1) username 精确匹配
    u = (await db.execute(select(User).where(User.username == key))).scalars().first()
    if u:
        return u

    # 2) real_name 精确匹配（排除空字符串）
    rows = (await db.execute(select(User).where(User.real_name == key))).scalars().all()
    rows = [x for x in rows if (x.real_name or "").strip()]
    if not rows:
        return None
    if len(rows) > 1:
        raise HTTPException(status_code=400, detail="该姓名存在多个账号，请使用登录账号（username）登录")
    return rows[0]


@router.post("/login", response_model=LoginOut)
async def login(payload: LoginIn, db: AsyncSession = Depends(get_db)) -> LoginOut:
    user = await _find_user_by_login_key(db, payload.username)

    # 统一错误信息，避免探测
    if not user or not _verify_password_compat(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码错误")

    if int(getattr(user, "status", 0) or 0) != 1:
        raise HTTPException(status_code=403, detail="账号已禁用")

    # 渐进升级：旧 sha256 hash 首次成功登录时升级
    try:
        if _is_legacy_password_hash(user.password_hash):
            user.password_hash = hash_password(payload.password)
            db.add(user)
            await db.commit()
            await db.refresh(user)
            logger.info("Password hash upgraded for user_id=%s", user.id)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass

    token = generate_session_token()
    now_bj = _now_bj_naive()
    sess = UserSession(
        user_id=int(user.id),
        session_token=token,
        last_active_at=now_bj,
        expired=0,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    # Redis（有就写，没有也不影响）
    try:
        from app.core.db import redis  # 依赖你现有实现（init_redis）

        if redis:
            await redis.set(f"session:{token}", str(user.id), ex=int(settings.SESSION_TIMEOUT_SECONDS))
    except Exception:
        pass

    role_name = await _get_primary_role_name(db, int(user.id))

    team_name = (getattr(user, "team_name", None) or "").strip() or None
    team_names = _split_team_names_csv(getattr(user, "team_names", None))
    if team_name and team_name not in team_names:
        team_names.append(team_name)

    return LoginOut(
        token=token,
        user_id=int(user.id),
        role_name=str(role_name or ""),
        team_names=team_names,
        team_name=team_name,
    )


@router.post("/logout")
async def logout(
    sess: UserSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    sess.expired = 1
    await db.commit()

    try:
        from app.core.db import redis

        if redis:
            await redis.delete(f"session:{sess.session_token}")
    except Exception:
        pass

    logger.info("User logout: user_id=%s", sess.user_id)
    return {"ok": True}
