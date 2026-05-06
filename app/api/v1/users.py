# app/api/v1/users.py
# encoding: utf-8
"""
v1 - 用户 / 账号管理（API 薄壳）

原则：
- Schemas 为接口真源：app.schemas.user
- 业务规则全部下沉到 services.users_service
- 不做任何旧兼容：不接 role_id / manager_id / status 等旧字段
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserContext, get_current_user_with_role
from app.core.db import get_db
from app.schemas.user import UserCreateIn, UserListOut, UserOut, UserUpdateIn
from app.services.users_service import (
    create_user as _create_user,
    delete_user as _delete_user,
    get_user_projection_by_id as _get_user_projection_by_id,
    list_users as _list_users,
    update_user as _update_user,
)

router = APIRouter(prefix="/users", tags=["users"])


def _mapping_to_user_out(row: Mapping[str, Any]) -> UserOut:
    return UserOut(
        id=int(row.get("id") or 0),
        username=str(row.get("username") or ""),
        real_name=row.get("real_name"),
        role_name=row.get("role_name"),
        team_name=row.get("team_name"),
        team_names=row.get("team_names"),
        status=int(row.get("status") or 1),
        is_online=bool(int(row.get("is_online") or 0)),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        meta=row.get("meta") or {},
    )


def _model_dump_exclude_unset(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


@router.get("", response_model=UserListOut)
async def list_users(
    keyword: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    status: Optional[int] = Query(None, description="账号状态：1=启用，0=禁用"),
    is_online: Optional[bool] = Query(None, description="是否在线（最近心跳窗口内）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    ctx: CurrentUserContext = Depends(get_current_user_with_role),
):
    try:
        payload = await _list_users(
            db=db,
            current_user=ctx.user,
            current_role=ctx.primary_role or "",
            keyword=keyword,
            role=role,
            status=status,
            is_online=is_online,
            page=page,
            page_size=page_size,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    items = [_mapping_to_user_out(row) for row in payload.get("items") or []]
    return UserListOut(
        total=int(payload.get("total") or 0),
        items=items,
        meta=payload.get("meta") or {},
    )


@router.post("", response_model=UserOut)
async def create_user(
    payload: UserCreateIn,
    db: AsyncSession = Depends(get_db),
    ctx: CurrentUserContext = Depends(get_current_user_with_role),
) -> UserOut:
    try:
        row = await _create_user(
            db=db,
            current_user=ctx.user,
            current_role=ctx.primary_role or "",
            username=payload.username,
            password=payload.password,
            role_name=payload.role_name,
            real_name=payload.real_name,
            team_name=payload.team_name,
            team_names=payload.team_names,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    proj = await _get_user_projection_by_id(db=db, user_id=int(getattr(row, "id", 0) or 0))
    if not proj:
        raise HTTPException(status_code=500, detail="创建成功但读取用户失败")
    return _mapping_to_user_out(proj)


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    payload: UserUpdateIn,
    db: AsyncSession = Depends(get_db),
    ctx: CurrentUserContext = Depends(get_current_user_with_role),
) -> UserOut:
    update_fields = _model_dump_exclude_unset(payload)
    update_kwargs: dict[str, Any] = {}
    for field_name in ("password", "real_name", "team_name", "team_names"):
        if field_name in update_fields:
            update_kwargs[field_name] = update_fields.get(field_name)

    try:
        row = await _update_user(
            db=db,
            current_user=ctx.user,
            current_role=ctx.primary_role or "",
            user_id=int(user_id),
            **update_kwargs,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        msg = str(exc)
        if msg == "用户不存在":
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

    proj = await _get_user_projection_by_id(db=db, user_id=int(getattr(row, "id", 0) or 0))
    if not proj:
        raise HTTPException(status_code=500, detail="更新成功但读取用户失败")
    return _mapping_to_user_out(proj)


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: CurrentUserContext = Depends(get_current_user_with_role),
):
    try:
        await _delete_user(
            db=db,
            current_user=ctx.user,
            current_role=ctx.primary_role or "",
            user_id=user_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        msg = str(exc)
        if msg == "用户不存在":
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}
