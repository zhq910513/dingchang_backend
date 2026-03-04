# app/api/v1/users.py
# encoding: utf-8
"""
v1 - 用户 / 账号管理（API 薄壳）

原则：
- Schemas 为接口真源：app.schemas.user
- 业务规则全部下沉到 services.users_service
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_with_role
from app.core.db import get_db
from app.schemas.user import UserOut, UserListOut
from app.services.users_service import list_users as _list_users, create_user as _create_user, \
    update_user as _update_user, delete_user as _delete_user

router = APIRouter(prefix="/users", tags=["users"])


class UserCreateIn(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=6)
    display_name: str = ""
    phone: str = ""
    role: str = Field(..., description="角色：super_admin/manager/sales/finance/market")
    team_name: Optional[str] = None
    team_names: Optional[str] = None


class UserUpdateIn(BaseModel):
    display_name: Optional[str] = None
    phone: Optional[str] = None
    password: Optional[str] = None
    team_name: Optional[str] = None
    team_names: Optional[str] = None


@router.get("", response_model=UserListOut)
async def list_users(
        keyword: Optional[str] = Query(None),
        role: Optional[str] = Query(None),
        db: AsyncSession = Depends(get_db),
        me=Depends(get_current_user_with_role),
):
    user, main_role, _teams = me
    rows = await _list_users(db=db, keyword=keyword, role=role)
    items = [UserOut.from_orm(r) for r in rows]
    return UserListOut(total=len(items), items=items)


@router.post("", response_model=UserOut)
async def create_user(data: UserCreateIn, db: AsyncSession = Depends(get_db), me=Depends(get_current_user_with_role)):
    user, main_role, _teams = me
    try:
        row = await _create_user(
            db=db,
            current_user=user,
            current_role=main_role,
            username=data.username,
            password=data.password,
            display_name=data.display_name or "",
            phone=data.phone or "",
            role_name=data.role,
            team_name=data.team_name,
            team_names=data.team_names,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return UserOut.from_orm(row)


@router.put("/{user_id:int}", response_model=UserOut)
async def update_user(user_id: int, data: UserUpdateIn, db: AsyncSession = Depends(get_db),
                      me=Depends(get_current_user_with_role)):
    user, main_role, _teams = me
    try:
        row = await _update_user(
            db=db,
            current_user=user,
            current_role=main_role,
            user_id=user_id,
            display_name=data.display_name,
            phone=data.phone,
            password=data.password,
            team_name=data.team_name,
            team_names=data.team_names,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return UserOut.from_orm(row)


@router.delete("/{user_id:int}")
async def delete_user(user_id: int, db: AsyncSession = Depends(get_db), me=Depends(get_current_user_with_role)):
    user, main_role, _teams = me
    try:
        await _delete_user(db=db, current_user=user, current_role=main_role, user_id=user_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ok": True}
