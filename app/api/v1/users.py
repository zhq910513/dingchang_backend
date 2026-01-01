# app/api/v1/users.py
# encoding: utf-8
"""
v1 - 用户 / 账号管理（去兼容版）
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_with_role
from app.core.config import settings
from app.core.constants import (
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_SALES,
    ROLE_FINANCE,
    ROLE_MARKET,
    ROLE_CHILD_CREATABLE_MAP,
    TEAM_NAMES,
)
from app.core.db import get_db
from app.core.security import hash_password
from app.models.role import Role
from app.models.session import UserSession
from app.models.user import User
from app.models.user_role import UserRole
from app.schemas.user import UserCreate, UserSimple

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])


def _clean_str(v: Optional[str]) -> Optional[str]:
    s = (v or "").strip()
    return s or None


def _split_csv(v: Optional[str]) -> List[str]:
    s = (v or "").strip()
    if not s:
        return []
    parts = [x.strip() for x in s.split(",")]
    return [x for x in parts if x]


def _normalize_team_list(items: List[str]) -> List[str]:
    out: List[str] = []
    for x in items or []:
        s = (x or "").strip()
        if not s:
            continue
        out.append(s)
    # 去重+稳定排序
    return sorted(set(out))


def _ensure_team_in_whitelist(team: str) -> str:
    t = (team or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail="team_name 不能为空")
    if t not in TEAM_NAMES:
        raise HTTPException(status_code=400, detail=f"非法团队 team_name：{t}")
    return t


def _ensure_team_list_valid(team_list: List[str], *, err: str) -> List[str]:
    tl = _normalize_team_list(team_list)
    if not tl:
        raise HTTPException(status_code=400, detail=err)
    for t in tl:
        _ensure_team_in_whitelist(t)
    return tl


def _user_team_names(u: User) -> List[str]:
    """
    从 User 上抽取“团队集合”：
    - 优先 team_names（逗号分隔）
    - 兼容 team_name（单团队）
    """
    names = _split_csv(getattr(u, "team_names", None))
    tn = _clean_str(getattr(u, "team_name", None))
    if tn:
        names.append(tn)
    return _normalize_team_list(names)


def _choose_single_team_for_child(
    *,
    manager: User,
    requested_team_name: Optional[str],
) -> str:
    """
    下属账号最终只能落在一个 team_name：
    - 若经理有多团队：必须显式指定 team_name，且属于经理团队集合
    - 若经理仅一个团队：可不传 team_name，默认该唯一团队；若传了必须匹配
    """
    mgr_teams = _user_team_names(manager)
    if not mgr_teams:
        raise HTTPException(status_code=400, detail="manager has no team configured")

    req = _clean_str(requested_team_name)
    if len(mgr_teams) == 1:
        only = mgr_teams[0]
        if req and req != only:
            raise HTTPException(status_code=400, detail="team_name must match manager team")
        return only

    # 多团队
    if not req:
        raise HTTPException(status_code=400, detail="请指定 team_name（经理为多团队）")
    if req not in mgr_teams:
        raise HTTPException(status_code=400, detail="team_name not in manager team_names")
    return req


async def _get_user_primary_role_name(db: AsyncSession, user_id: int) -> Optional[str]:
    stmt = (
        select(Role.role_name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
        .order_by(Role.id.asc())
    )
    return (await db.execute(stmt)).scalars().first()


async def _ensure_user_is_manager(db: AsyncSession, user_id: int) -> User:
    u = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if not u:
        raise HTTPException(status_code=400, detail="manager_id not found")
    if int(getattr(u, "status", 0) or 0) != 1:
        raise HTTPException(status_code=400, detail="manager account is disabled")

    role_name = await _get_user_primary_role_name(db, u.id)
    if role_name != ROLE_MANAGER:
        raise HTTPException(status_code=400, detail="manager_id is not a manager account")

    mgr_teams = _user_team_names(u)
    if not mgr_teams:
        raise HTTPException(status_code=400, detail="manager has no team configured")
    for t in mgr_teams:
        if t not in TEAM_NAMES:
            raise HTTPException(status_code=400, detail="manager team invalid")
    return u


@router.get("/me", response_model=UserSimple)
async def get_me(
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    """
    ✅ 当前登录账号信息（给前端取 team_names / team_name 用）
    - 任意已登录账号可用
    - 返回 team_names（兼容 team_name）
    """
    _ = db
    u, role_name = user_role
    teams = _user_team_names(u)

    return UserSimple(
        id=int(u.id),
        username=str(u.username),
        real_name=getattr(u, "real_name", None),
        role_name=role_name,
        is_online=False,
        team_name=getattr(u, "team_name", None),
        team_names=teams or None,
    )


@router.get("/managers", response_model=List[UserSimple])
async def list_managers(
    status: int = Query(1, description="默认仅返回启用账号"),
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    """
    ✅ 给前端创建子账号时的“归属经理”下拉用
    - 仅 super_admin 可用
    - 返回 manager 的 team_names（用于前端在选中经理后展示团队勾选）
    """
    _user, role_name = user_role
    if role_name != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No permission")

    stmt = (
        select(User.id, User.username, User.real_name, User.team_name, User.team_names, Role.role_name)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.role_name == ROLE_MANAGER)
        .order_by(User.id.asc())
    )
    if status is not None:
        stmt = stmt.where(User.status == int(status))

    rows = (await db.execute(stmt)).all()
    out: List[UserSimple] = []
    for r in rows:
        teams = _normalize_team_list(
            _split_csv(getattr(r, "team_names", None))
            + ([getattr(r, "team_name", None)] if getattr(r, "team_name", None) else [])
        )
        out.append(
            UserSimple(
                id=int(r.id),
                username=str(r.username),
                real_name=r.real_name,
                role_name=r.role_name,
                is_online=False,
                team_name=getattr(r, "team_name", None),
                team_names=teams or None,
            )
        )
    return out


@router.post("", status_code=201)
async def create_user(
    payload: UserCreate,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    """
    创建子账号（去兼容版规则 + 多团队经理）
    """
    current_user, current_role_name = user_role

    allowed_roles = ROLE_CHILD_CREATABLE_MAP.get(current_role_name or "", ())
    if not allowed_roles:
        raise HTTPException(status_code=403, detail="No permission to create users")

    role = (await db.execute(select(Role).where(Role.id == payload.role_id))).scalars().first()
    if not role:
        raise HTTPException(status_code=400, detail="Role does not exist")
    if role.role_name not in allowed_roles:
        raise HTTPException(status_code=403, detail="Cannot create this role")

    username = (payload.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    pwd = (payload.password or "").strip()
    if not pwd:
        raise HTTPException(status_code=400, detail="password is required")

    exists = (await db.execute(select(User).where(User.username == username))).scalars().first()
    if exists:
        raise HTTPException(status_code=400, detail="Username already exists")

    parent_id: Optional[int] = None

    manager_team_names_csv: Optional[str] = None
    manager_default_team_name: Optional[str] = None

    child_team_name: Optional[str] = None

    if current_role_name == ROLE_MANAGER:
        if role.role_name not in (ROLE_SALES, ROLE_FINANCE, ROLE_MARKET):
            raise HTTPException(status_code=403, detail="Manager can only create sales/finance/market")
        parent_id = int(current_user.id)

        child_team_name = _choose_single_team_for_child(
            manager=current_user,
            requested_team_name=getattr(payload, "team_name", None),
        )

    elif current_role_name == ROLE_SUPER_ADMIN:
        if role.role_name == ROLE_MANAGER:
            parent_id = int(current_user.id)

            req_team_names: List[str] = []
            raw_team_names = getattr(payload, "team_names", None)
            if raw_team_names:
                req_team_names.extend(list(raw_team_names))
            if getattr(payload, "team_name", None):
                req_team_names.append(str(getattr(payload, "team_name", None)))

            teams = _ensure_team_list_valid(req_team_names, err="请为经理账号分配团队（team_names/team_name）")
            manager_team_names_csv = ",".join(teams)
            manager_default_team_name = teams[0]

        elif role.role_name in (ROLE_SALES, ROLE_FINANCE, ROLE_MARKET):
            if not getattr(payload, "manager_id", None):
                raise HTTPException(status_code=400, detail="请指定分配给哪个经理（manager_id）")
            mgr = await _ensure_user_is_manager(db, int(payload.manager_id))
            parent_id = int(mgr.id)

            child_team_name = _choose_single_team_for_child(
                manager=mgr,
                requested_team_name=getattr(payload, "team_name", None),
            )
        else:
            raise HTTPException(status_code=400, detail="Unsupported role")
    else:
        raise HTTPException(status_code=403, detail="No permission")

    new_user = User(
        username=username,
        real_name=_clean_str(getattr(payload, "real_name", None)),
        password_hash=hash_password(pwd),
        parent_id=parent_id,
        status=1,
    )

    if role.role_name == ROLE_MANAGER:
        new_user.team_names = manager_team_names_csv
        new_user.team_name = manager_default_team_name
    else:
        new_user.team_name = child_team_name

    db.add(new_user)
    await db.flush()

    db.add(UserRole(user_id=new_user.id, role_id=role.id))
    await db.commit()

    logger.info(
        "Create user: operator=%s new_user=%s role=%s parent_id=%s team=%s teams=%s",
        current_user.id,
        new_user.id,
        role.role_name,
        parent_id,
        getattr(new_user, "team_name", None),
        getattr(new_user, "team_names", None),
    )
    return {"id": int(new_user.id)}


@router.get("/children", response_model=List[UserSimple])
async def list_children_users(
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    """
    获取当前账号创建的子账号，并附带在线状态：
    - super_admin / manager 可查看
    - 在线：last_active_at 在 SESSION_TIMEOUT_SECONDS 内 且 session.expired=0
    """
    current_user, current_role_name = user_role
    if current_role_name not in (ROLE_MANAGER, ROLE_SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="No permission")

    stmt = (
        select(User.id, User.username, User.real_name, User.team_name, User.team_names, Role.role_name)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(User.parent_id == current_user.id)
        .order_by(User.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    user_ids = [int(r.id) for r in rows]
    if not user_ids:
        return []

    stmt_session = (
        select(
            UserSession.user_id,
            func.max(UserSession.last_active_at).label("last_active_at"),
            func.max(UserSession.expired).label("max_expired"),
        )
        .where(UserSession.user_id.in_(user_ids))
        .group_by(UserSession.user_id)
    )
    srows = (await db.execute(stmt_session)).all()
    session_map = {int(r.user_id): r for r in srows}

    now = datetime.utcnow()
    ttl = int(getattr(settings, "SESSION_TIMEOUT_SECONDS", 7200) or 7200)

    out: List[UserSimple] = []
    for r in rows:
        sess = session_map.get(int(r.id))
        online = False
        if sess and int(sess.max_expired or 0) == 0 and sess.last_active_at is not None:
            if (now - sess.last_active_at) <= timedelta(seconds=ttl):
                online = True

        teams = _normalize_team_list(
            _split_csv(getattr(r, "team_names", None))
            + ([getattr(r, "team_name", None)] if getattr(r, "team_name", None) else [])
        )

        out.append(
            UserSimple(
                id=int(r.id),
                username=str(r.username),
                real_name=r.real_name,
                role_name=r.role_name,
                is_online=online,
                team_name=getattr(r, "team_name", None),
                team_names=teams or None,
            )
        )

    return out
