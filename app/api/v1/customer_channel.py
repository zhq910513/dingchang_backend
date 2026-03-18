# app/api/v1/customer_channel.py
# encoding: utf-8
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserContext, get_current_user, get_current_user_with_role_and_teams
from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_MARKET, ROLE_SUPER_ADMIN
from app.core.db import get_db
from app.models.user import User
from app.schemas.customer_channel import (
    ChannelGroupCreateIn,
    ChannelGroupListItemOut,
    ChannelGroupListPageOut,
    ChannelGroupOptionOut,
    ChannelGroupOptionPageOut,
    ChannelGroupOut,
    CustomerChannelPageCapabilitiesOut,
    CustomerGroupCreateIn,
    CustomerGroupListItemOut,
    CustomerGroupListPageOut,
    CustomerGroupOptionOut,
    CustomerGroupOptionPageOut,
    CustomerGroupOut,
    CustomerGroupUpdateIn,
    ChannelGroupUpdateIn,
)
from app.services.customer_channel_service import (
    create_channel_group as _create_channel_group,
    create_customer_group as _create_customer_group,
    get_channel_group_by_id as _get_channel_group_by_id,
    get_customer_group_by_id as _get_customer_group_by_id,
    list_channel_groups as _list_channel_groups,
    list_channel_groups_manage as _list_channel_groups_manage,
    list_customer_groups as _list_customer_groups,
    list_customer_groups_manage as _list_customer_groups_manage,
    soft_delete_channel_group as _soft_delete_channel_group,
    soft_delete_customer_group as _soft_delete_customer_group,
    update_channel_group as _update_channel_group,
    update_customer_group as _update_customer_group,
)

router = APIRouter(prefix="/customer-channel", tags=["customer-channel"])


def _fmt_dt(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    s = str(v or "").strip()
    return s or None


def _display_name(user: Optional[User]) -> Optional[str]:
    if not user:
        return None
    real_name = str(getattr(user, "real_name", "") or "").strip()
    if real_name:
        return real_name
    username = str(getattr(user, "username", "") or "").strip()
    return username or None


def _capabilities_by_role(role_name: Optional[str]) -> CustomerChannelPageCapabilitiesOut:
    rn = str(role_name or "").strip()
    return CustomerChannelPageCapabilitiesOut(
        can_create=(rn != ROLE_FINANCE),
        can_edit=(rn in (ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_MARKET)),
        can_delete=(rn != ROLE_FINANCE),
        can_view_deleted=(rn == ROLE_SUPER_ADMIN),
    )


def _ensure_create_allowed(ctx: CurrentUserContext) -> None:
    if (ctx.primary_role or "").strip() == ROLE_FINANCE:
        raise HTTPException(status_code=403, detail="财务账号无新增权限")


def _ensure_edit_allowed(ctx: CurrentUserContext) -> None:
    if (ctx.primary_role or "").strip() not in (ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_MARKET):
        raise HTTPException(status_code=403, detail="仅经理/超级账号/市场账号可编辑")


def _ensure_delete_allowed(ctx: CurrentUserContext) -> None:
    if (ctx.primary_role or "").strip() == ROLE_FINANCE:
        raise HTTPException(status_code=403, detail="财务账号无删除权限")


def _ensure_valid_user_id(ctx: CurrentUserContext) -> int:
    user_id = getattr(ctx.user, "id", None)
    if user_id is None:
        raise HTTPException(status_code=401, detail="登录状态无效，请重新登录")
    return int(user_id)


def _raise_bad_request(exc: ValueError) -> None:
    raise HTTPException(status_code=400, detail=str(exc) or "请求处理失败") from exc


def _to_customer_group_option_out(row: Mapping[str, Any]) -> CustomerGroupOptionOut:
    return CustomerGroupOptionOut(
        id=int(row.get("id", 0) or 0),
        customer_code=str(row.get("customer_code", "") or ""),
        customer_name=str(row.get("customer_name", "") or ""),
    )


def _to_channel_group_option_out(row: Mapping[str, Any]) -> ChannelGroupOptionOut:
    return ChannelGroupOptionOut(
        id=int(row.get("id", 0) or 0),
        channel_code=str(row.get("channel_code", "") or ""),
        channel_name=str(row.get("channel_name", "") or ""),
    )


def _to_customer_group_list_item_out(row: Mapping[str, Any]) -> CustomerGroupListItemOut:
    return CustomerGroupListItemOut(
        id=int(row.get("id", 0) or 0),
        customer_code=str(row.get("customer_code", "") or ""),
        customer_name=str(row.get("customer_name", "") or ""),
        market=(str(row.get("market", "") or "").strip() or None),
        region=(str(row.get("region", "") or "").strip() or None),
        contacts=row.get("contacts", None),
        created_by=(int(row.get("created_by")) if row.get("created_by") is not None else None),
        created_by_name=(str(row.get("created_by_name", "") or "").strip() or None),
        created_at=_fmt_dt(row.get("created_at")),
        updated_at=_fmt_dt(row.get("updated_at")),
        deleted_at=_fmt_dt(row.get("deleted_at")),
        is_deleted=int(row.get("is_deleted", 0) or 0),
        meta=row.get("meta") or {"capabilities": {}},
    )


def _to_channel_group_list_item_out(row: Mapping[str, Any]) -> ChannelGroupListItemOut:
    return ChannelGroupListItemOut(
        id=int(row.get("id", 0) or 0),
        channel_code=str(row.get("channel_code", "") or ""),
        channel_name=str(row.get("channel_name", "") or ""),
        region=(str(row.get("region", "") or "").strip() or None),
        contacts=row.get("contacts", None),
        created_by=(int(row.get("created_by")) if row.get("created_by") is not None else None),
        created_by_name=(str(row.get("created_by_name", "") or "").strip() or None),
        created_at=_fmt_dt(row.get("created_at")),
        updated_at=_fmt_dt(row.get("updated_at")),
        deleted_at=_fmt_dt(row.get("deleted_at")),
        is_deleted=int(row.get("is_deleted", 0) or 0),
        meta=row.get("meta") or {"capabilities": {}},
    )


def _to_customer_group_out(row) -> CustomerGroupOut:
    creator = getattr(row, "creator", None)
    return CustomerGroupOut(
        id=int(getattr(row, "id", 0) or 0),
        customer_code=str(getattr(row, "customer_code", "") or ""),
        customer_name=str(getattr(row, "customer_name", "") or ""),
        market=(str(getattr(row, "market", "") or "").strip() or None),
        region=(str(getattr(row, "region", "") or "").strip() or None),
        contacts=getattr(row, "contacts", None),
        created_by=(int(getattr(row, "created_by", 0)) if getattr(row, "created_by", None) is not None else None),
        created_by_name=_display_name(creator),
        created_at=_fmt_dt(getattr(row, "created_at", None)),
        updated_at=_fmt_dt(getattr(row, "updated_at", None)),
        deleted_at=_fmt_dt(getattr(row, "deleted_at", None)),
        is_deleted=int(getattr(row, "is_deleted", 0) or 0),
    )


def _to_channel_group_out(row) -> ChannelGroupOut:
    creator = getattr(row, "creator", None)
    return ChannelGroupOut(
        id=int(getattr(row, "id", 0) or 0),
        channel_code=str(getattr(row, "channel_code", "") or ""),
        channel_name=str(getattr(row, "channel_name", "") or ""),
        region=(str(getattr(row, "region", "") or "").strip() or None),
        contacts=getattr(row, "contacts", None),
        created_by=(int(getattr(row, "created_by", 0)) if getattr(row, "created_by", None) is not None else None),
        created_by_name=_display_name(creator),
        created_at=_fmt_dt(getattr(row, "created_at", None)),
        updated_at=_fmt_dt(getattr(row, "updated_at", None)),
        deleted_at=_fmt_dt(getattr(row, "deleted_at", None)),
        is_deleted=int(getattr(row, "is_deleted", 0) or 0),
    )


@router.get("/customers", response_model=CustomerGroupOptionPageOut)
async def list_customers(
        keyword: Optional[str] = Query(None),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        db: AsyncSession = Depends(get_db),
        _: Any = Depends(get_current_user),
):
    result = await _list_customer_groups(
        db=db,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    items = [_to_customer_group_option_out(r) for r in result["items"]]
    return CustomerGroupOptionPageOut(
        items=items,
        page=result["page"],
        page_size=result["page_size"],
        total=result["total"],
        has_more=result["has_more"],
    )


@router.get("/channels", response_model=ChannelGroupOptionPageOut)
async def list_channels(
        keyword: Optional[str] = Query(None),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        db: AsyncSession = Depends(get_db),
        _: Any = Depends(get_current_user),
):
    result = await _list_channel_groups(
        db=db,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    items = [_to_channel_group_option_out(r) for r in result["items"]]
    return ChannelGroupOptionPageOut(
        items=items,
        page=result["page"],
        page_size=result["page_size"],
        total=result["total"],
        has_more=result["has_more"],
    )


@router.get("/customer-groups", response_model=CustomerGroupListPageOut)
async def list_customer_groups_manage(
        customer_code: Optional[str] = Query(None),
        customer_name: Optional[str] = Query(None),
        market: Optional[str] = Query(None),
        region: Optional[str] = Query(None),
        created_by_name: Optional[str] = Query(None),
        include_deleted: int = Query(0),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    result = await _list_customer_groups_manage(
        db=db,
        role_name=ctx.primary_role,
        customer_code=customer_code,
        customer_name=customer_name,
        market=market,
        region=region,
        created_by_name=created_by_name,
        include_deleted=bool(include_deleted),
        page=page,
        page_size=page_size,
    )
    items = [_to_customer_group_list_item_out(r) for r in result["items"]]
    return CustomerGroupListPageOut(
        total=int(result.get("total", 0) or 0),
        items=items,
        meta=result.get("meta") or {},
    )


@router.get("/channel-groups", response_model=ChannelGroupListPageOut)
async def list_channel_groups_manage(
        channel_code: Optional[str] = Query(None),
        channel_name: Optional[str] = Query(None),
        region: Optional[str] = Query(None),
        created_by_name: Optional[str] = Query(None),
        include_deleted: int = Query(0),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    result = await _list_channel_groups_manage(
        db=db,
        role_name=ctx.primary_role,
        channel_code=channel_code,
        channel_name=channel_name,
        region=region,
        created_by_name=created_by_name,
        include_deleted=bool(include_deleted),
        page=page,
        page_size=page_size,
    )
    items = [_to_channel_group_list_item_out(r) for r in result["items"]]
    return ChannelGroupListPageOut(
        total=int(result.get("total", 0) or 0),
        items=items,
        meta=result.get("meta") or {},
    )


@router.post("/customer-groups", response_model=CustomerGroupOut)
async def create_customer_group_manage(
        payload: CustomerGroupCreateIn = Body(...),
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    _ensure_create_allowed(ctx)
    created_by = _ensure_valid_user_id(ctx)

    try:
        row = await _create_customer_group(
            db=db,
            customer_code=payload.customer_code,
            customer_name=payload.customer_name,
            team_name=None,
            market=payload.market,
            region=payload.region,
            contacts=[x.model_dump() for x in payload.contacts],
            created_by=created_by,
        )
    except ValueError as exc:
        _raise_bad_request(exc)

    return _to_customer_group_out(row)


@router.post("/channel-groups", response_model=ChannelGroupOut)
async def create_channel_group_manage(
        payload: ChannelGroupCreateIn = Body(...),
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    _ensure_create_allowed(ctx)
    created_by = _ensure_valid_user_id(ctx)

    try:
        row = await _create_channel_group(
            db=db,
            channel_code=payload.channel_code,
            channel_name=payload.channel_name,
            team_name=None,
            region=payload.region,
            contacts=[x.model_dump() for x in payload.contacts],
            created_by=created_by,
        )
    except ValueError as exc:
        _raise_bad_request(exc)

    return _to_channel_group_out(row)


@router.put("/customer-groups/{group_id}", response_model=CustomerGroupOut)
async def update_customer_group_manage(
        group_id: int,
        payload: CustomerGroupUpdateIn = Body(...),
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    _ensure_edit_allowed(ctx)

    row = await _get_customer_group_by_id(db=db, group_id=group_id, with_creator=False)
    if not row:
        raise HTTPException(status_code=404, detail="客户不存在")
    if int(getattr(row, "is_deleted", 0) or 0) == 1:
        raise HTTPException(status_code=400, detail="客户已删除，无法编辑")

    try:
        row = await _update_customer_group(
            db=db,
            row=row,
            customer_code=payload.customer_code,
            customer_name=payload.customer_name,
            market=payload.market,
            region=payload.region,
            contacts=[x.model_dump() for x in payload.contacts],
        )
    except ValueError as exc:
        _raise_bad_request(exc)

    return _to_customer_group_out(row)


@router.put("/channel-groups/{group_id}", response_model=ChannelGroupOut)
async def update_channel_group_manage(
        group_id: int,
        payload: ChannelGroupUpdateIn = Body(...),
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    _ensure_edit_allowed(ctx)

    row = await _get_channel_group_by_id(db=db, group_id=group_id, with_creator=False)
    if not row:
        raise HTTPException(status_code=404, detail="渠道不存在")
    if int(getattr(row, "is_deleted", 0) or 0) == 1:
        raise HTTPException(status_code=400, detail="渠道已删除，无法编辑")

    try:
        row = await _update_channel_group(
            db=db,
            row=row,
            channel_code=payload.channel_code,
            channel_name=payload.channel_name,
            region=payload.region,
            contacts=[x.model_dump() for x in payload.contacts],
        )
    except ValueError as exc:
        _raise_bad_request(exc)

    return _to_channel_group_out(row)


@router.delete("/customer-groups/{group_id}", response_model=CustomerGroupOut)
async def delete_customer_group_manage(
        group_id: int,
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    _ensure_delete_allowed(ctx)

    row = await _get_customer_group_by_id(db=db, group_id=group_id, with_creator=False)
    if not row:
        raise HTTPException(status_code=404, detail="客户不存在")
    if int(getattr(row, "is_deleted", 0) or 0) == 1:
        raise HTTPException(status_code=400, detail="客户已删除")

    try:
        row = await _soft_delete_customer_group(db=db, row=row)
    except ValueError as exc:
        _raise_bad_request(exc)

    return _to_customer_group_out(row)


@router.delete("/channel-groups/{group_id}", response_model=ChannelGroupOut)
async def delete_channel_group_manage(
        group_id: int,
        db: AsyncSession = Depends(get_db),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    _ensure_delete_allowed(ctx)

    row = await _get_channel_group_by_id(db=db, group_id=group_id, with_creator=False)
    if not row:
        raise HTTPException(status_code=404, detail="渠道不存在")
    if int(getattr(row, "is_deleted", 0) or 0) == 1:
        raise HTTPException(status_code=400, detail="渠道已删除")

    try:
        row = await _soft_delete_channel_group(db=db, row=row)
    except ValueError as exc:
        _raise_bad_request(exc)

    return _to_channel_group_out(row)
