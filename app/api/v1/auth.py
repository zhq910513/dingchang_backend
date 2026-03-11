# app/api/v1/auth.py
# encoding: utf-8
from __future__ import annotations

"""
认证相关接口（API 薄壳）

原则：
- Schemas 为接口真源：app.schemas.auth.LoginIn / LoginOut
- 业务规则全部下沉到 services.auth_service
- 时间口径：北京时间 naive DATETIME
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_session
from app.core.db import get_db
from app.schemas.auth import LoginIn, LoginOut
from app.services.auth_service import login as _login, logout as _logout

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginOut)
async def login(data: LoginIn, db: AsyncSession = Depends(get_db)):
    try:
        user, token, sess, role_names, team_names, team_name = await _login(
            db=db,
            username=data.username,
            password=data.password,
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.exception("login failed: %s", e)
        raise HTTPException(status_code=500, detail="登录失败")

    # LoginOut 真源字段：role_name 必填（不做兼容字段 role）
    role_name = role_names[0] if role_names else ""

    return LoginOut(
        token=token,
        user_id=user.id,
        role_name=role_name,
        team_name=team_name,
        team_names=team_names,
    )


@router.post("/logout")
async def logout(session=Depends(get_current_session), db: AsyncSession = Depends(get_db)):
    # get_current_session 真源：session.session_token
    token = session.session_token
    await _logout(db=db, token=token)
    return {"ok": True}
