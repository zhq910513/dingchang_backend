# app/api/v1/users.py
# encoding: utf-8
"""
v1 - 用户 / 账号管理（API 薄壳）

原则：
- Schemas 为接口真源：app.schemas.user
- 业务规则全部下沉到 services.users_service

承重墙（2026-03-05）：
- deps 只返回 CurrentUserContext（不再解包 tuple）
- API 不自定义入参模型（schemas 约束 API）
- 不兼容旧字段：display_name/phone 等字段已在 service 层禁用，本层不再接收/透传
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.api.deps import CurrentUserContext, get_current_user_with_role
from app.core.db import get_db
from app.schemas.user import UserCreateIn, UserListOut, UserOut, UserUpdateIn
from app.services.users_service import (
    list_users as _list_users,
    create_user as _create_user,
    update_user as _update_user,
    delete_user as _delete_user,
)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=UserListOut)
async def list_users(
        keyword: Optional[str] = Query(None),
        role: Optional[str] = Query(None),
        db: AsyncSession = Depends(get_db),
):
    rows = await _list_users(db=db, keyword=keyword, role=role)
    items = [UserOut.from_orm(r) for r in rows]
    return UserListOut(total=len(items), items=items)


@router.post("", response_model=UserOut)
async def create_user(
        payload: UserCreateIn,
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role),
) -> UserOut:
    try:
            row = await _create_user(
        db=db,
        current_role=ctx.primary_role or "",
        username=payload.username,
        password=payload.password,
        role_name=payload.role_name,
        team_name=payload.team_name,
        team_names=payload.team_names,
    )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return UserOut.from_orm(row)


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
        user_id: int,
        payload: UserUpdateIn,
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role),
) -> UserOut:
    try:
            row = await _update_user(
        db=db,
        current_role=ctx.primary_role or "",
        user_id=int(user_id),
        password=payload.password,
        team_name=payload.team_name,
        team_names=payload.team_names,
    )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return UserOut.from_orm(row)


@router.delete("/{user_id}")
async def delete_user(
        user_id: int,
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role),
):
    try:
        await _delete_user(db=db, current_user=ctx.user, current_role=ctx.primary_role or "", user_id=user_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ok": True}

