# app/api/v1/auth.py
# encoding: utf-8
"""
认证相关接口：
- /auth/login
- /auth/logout

去兼容版原则：
- 登录入参字段仍叫 username（前端不改），但允许传 username 或 real_name
- real_name 同名多账号：拒绝登录，避免登录错人
"""

import logging
import hashlib
import hmac
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.db import get_db
from app.core.config import settings
from app.core.security import verify_password, generate_session_token, hash_password
from app.models.user import User
from app.models.session import UserSession
from app.models.role import Role
from app.models.user_role import UserRole
from app.api.deps import get_current_session
from app.schemas.auth import LoginRequest, LoginResponse, LoginUserOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _is_legacy_password_hash(hashed: str) -> bool:
    """
    判断是否为旧版 sha256(plain) hex：
    - 64位 hex 串
    """
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
    """
    兼容校验：
    - 新格式：走 verify_password（PBKDF2 等）
    - 旧格式：sha256(plain) hex 直比
    """
    try:
        if _is_legacy_password_hash(stored_hash):
            return hmac.compare_digest(_legacy_sha256_hex(plain), (stored_hash or "").lower())
        return bool(verify_password(plain, stored_hash))
    except Exception:
        return False


async def _get_primary_role(db: AsyncSession, user_id: int) -> tuple[str, str]:
    """
    主角色：
    - 允许一个用户多角色时也不炸
    - 取 Role.id 最小的那个作为主角色（id 越小权限越高）
    """
    stmt = (
        select(Role)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
        .order_by(Role.id.asc())
    )
    role = (await db.execute(stmt)).scalars().first()
    if not role:
        return "", ""
    return (role.role_name or ""), (role.description or "")


async def _find_user_by_login_key(db: AsyncSession, login_key: str) -> User | None:
    """
    登录支持：
    1) username 精确匹配优先
    2) real_name 精确匹配兜底（real_name 为空字符串不参与）
    3) real_name 命中多条：400，要求用 username 登录
    """
    key = (login_key or "").strip()
    if not key:
        return None

    # 1) username
    u = (await db.execute(select(User).where(User.username == key))).scalars().first()
    if u:
        return u

    # 2) real_name
    rows = (await db.execute(select(User).where(User.real_name == key))).scalars().all()
    rows = [x for x in rows if (x.real_name or "").strip()]  # 排除空字符串 real_name

    if not rows:
        return None
    if len(rows) > 1:
        raise HTTPException(
            status_code=400,
            detail="该姓名存在多个账号，请使用登录账号（username）登录",
        )
    return rows[0]


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> LoginResponse:
    user = await _find_user_by_login_key(db, payload.username)

    # 统一错误信息（避免泄露“用户名存在/不存在”）
    if not user or not _verify_password_compat(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码错误")

    if user.status != 1:
        raise HTTPException(status_code=403, detail="账号已禁用")

    # ✅ 渐进升级：旧 sha256 hash 在首次成功登录时升级为新 PBKDF2 格式
    try:
        if _is_legacy_password_hash(user.password_hash):
            user.password_hash = hash_password(payload.password)
            db.add(user)
            await db.commit()
            await db.refresh(user)
            logger.info("Password hash upgraded for user_id=%s", user.id)
    except Exception:
        # 升级失败不影响登录（只记录即可）
        try:
            await db.rollback()
        except Exception:
            pass

    # 创建 session
    token = generate_session_token()
    sess = UserSession(
        user_id=user.id,
        session_token=token,
        last_active_at=datetime.utcnow(),
        expired=0,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    # Redis（有就写；没有也不影响登录）
    try:
        from app.core.db import redis  # 依赖你现有实现（init_redis）

        if redis:
            await redis.set(f"session:{token}", str(user.id), ex=int(settings.SESSION_TIMEOUT_SECONDS))
    except Exception:
        pass

    role_name, role_label = await _get_primary_role(db, user.id)

    return LoginResponse(
        session_token=token,
        user=LoginUserOut(
            id=user.id,
            username=user.username,
            real_name=(user.real_name or None),
            status=user.status,
            role_name=role_name,
            role_label=role_label,
        ),
    )


@router.post("/logout")
async def logout(
    sess: UserSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    # DB 标记过期
    sess.expired = 1
    await db.commit()

    # Redis 删除（有就删）
    try:
        from app.core.db import redis

        if redis:
            await redis.delete(f"session:{sess.session_token}")
    except Exception:
        pass

    logger.info("User logout: user_id=%s", sess.user_id)
    return {"ok": True}
