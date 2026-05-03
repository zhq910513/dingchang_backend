# app/api/v1/auth.py
# encoding: utf-8
from __future__ import annotations

"""Authentication API routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_session
from app.core.db import get_db
from app.schemas.auth import LoginIn, LoginOut
from app.services.auth_service import login as _login, logout as _logout

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_FORBIDDEN_LOGIN_MESSAGES = {"账号已禁用", "账号未配置角色"}


@router.post("/login", response_model=LoginOut)
async def login(data: LoginIn, db: AsyncSession = Depends(get_db)):
    try:
        user, token, sess, role_names, team_names, team_name = await _login(
            db=db,
            username=data.username,
            password=data.password,
        )
    except ValueError as e:
        message = str(e) or "登录失败"
        status_code = 403 if message in _FORBIDDEN_LOGIN_MESSAGES else 401
        raise HTTPException(status_code=status_code, detail=message)
    except Exception as e:
        logger.exception("login failed: %s", e)
        raise HTTPException(status_code=500, detail="登录失败")

    role_name = role_names[0] if role_names else ""

    username = str(getattr(user, "username", "") or "").strip()
    real_name = (str(getattr(user, "real_name", "") or "").strip() or None)
    full_name = real_name or username or None

    return LoginOut(
        token=token,
        user_id=user.id,
        username=username,
        real_name=real_name,
        full_name=full_name,
        role_name=role_name,
        team_name=team_name or None,
        team_names=team_names,
    )


@router.post("/logout")
async def logout(session=Depends(get_current_session), db: AsyncSession = Depends(get_db)):
    token = session.session_token
    await _logout(db=db, token=token)
    return {"ok": True}
