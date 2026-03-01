# app/api/v1/users.py
# encoding: utf-8
"""
v1 - 用户 / 账号管理（去兼容版）

✅ 本次新增：
- 编辑用户：PUT /users/{user_id}
- 真删除用户（硬删除）：DELETE /users/{user_id}

权限规则：
- super_admin：可编辑/删除（但禁止删除自己；禁止删除 super_admin；删除经理需先清理其子账号）
- manager：只能编辑/删除自己创建的子账号（sales/finance/market）；不能操作经理账号
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, delete
from sqlalchemy.exc import IntegrityError
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
from app.schemas.user import UserOut, UserListOut

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


def _ensure_manage_permission(
    *,
    operator: User,
    operator_role: Optional[str],
    target: User,
    target_role: Optional[str],
    action: str,
) -> None:
    """
    action: "edit" | "delete"
    """
    rn = operator_role or ""
    tr = target_role or ""

    if rn not in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        raise HTTPException(status_code=403, detail="No permission")

    if int(operator.id) == int(target.id) and action == "delete":
        raise HTTPException(status_code=400, detail="不能删除自己")

    if rn == ROLE_MANAGER:
        # manager 只能管自己创建的子账号
        if int(getattr(target, "parent_id", 0) or 0) != int(operator.id):
            raise HTTPException(status_code=403, detail="No permission")
        if tr not in (ROLE_SALES, ROLE_FINANCE, ROLE_MARKET):
            raise HTTPException(status_code=403, detail="No permission")
        return

    # super_admin
    if tr == ROLE_SUPER_ADMIN and action == "delete":
        raise HTTPException(status_code=400, detail="禁止删除 super_admin 账号")


class UserCreateIn(BaseModel):
    username: str
    password: str
    role_id: int
    real_name: Optional[str] = None
    team_name: Optional[str] = None
    team_names: Optional[str] = None


class UserUpdateIn(BaseModel):
    """
    编辑用户入参（不允许改 role_id；如需改角色请走单独策略，不在本轮范围）
    """
    username: Optional[str] = None
    real_name: Optional[str] = None
    password: Optional[str] = None
    status: Optional[int] = Field(default=None, description="1启用 0禁用")
    # 下属账号（sales/finance/market）单团队
    team_name: Optional[str] = None
    # 经理账号多团队（仅 super_admin 可改）
    team_names: Optional[List[str]] = None
    # super_admin 可调整下属归属经理
    manager_id: Optional[int] = None


@router.get("/me", response_model=UserOut)
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

    return UserOut(
        id=int(u.id),
        username=str(u.username),
        real_name=getattr(u, "real_name", None),
        role_name=role_name,
        is_online=False,
        team_name=getattr(u, "team_name", None),
        team_names=teams or None,
    )


@router.get("/managers", response_model=List[UserOut])
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
    out: List[UserOut] = []
    for r in rows:
        teams = _normalize_team_list(
            _split_csv(getattr(r, "team_names", None))
            + ([getattr(r, "team_name", None)] if getattr(r, "team_name", None) else [])
        )
        out.append(
            UserOut(
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
    payload: UserCreateIn,
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

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # 并发下仍可能撞唯一约束
        raise HTTPException(status_code=400, detail="Username already exists")

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


@router.get("/children", response_model=List[UserOut])
async def list_children_users(
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    """
    获取当前账号创建的子账号，并附带在线状态：
    - super_admin / manager 可查看
    - 在线：last_active_at 在 SESSION_TIMEOUT_SECONDS 内 且存在 expired=0 的 session
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

    # ✅ 修复：只统计 expired=0 的 session，避免历史过期 session 把在线状态误判为离线
    stmt_session = (
        select(
            UserSession.user_id,
            func.max(UserSession.last_active_at).label("last_active_at"),
        )
        .where(UserSession.user_id.in_(user_ids), UserSession.expired == 0)
        .group_by(UserSession.user_id)
    )
    srows = (await db.execute(stmt_session)).all()
    session_map = {int(r.user_id): r for r in srows}

    # ✅ 方案 A / 北京时间 naive：用 datetime.now()（容器 TZ=Asia/Shanghai 时为北京时间）
    now = datetime.now()
    ttl = int(getattr(settings, "SESSION_TIMEOUT_SECONDS", 7200) or 7200)

    out: List[UserOut] = []
    for r in rows:
        sess = session_map.get(int(r.id))
        online = False
        if sess and sess.last_active_at is not None:
            try:
                if (now - sess.last_active_at) <= timedelta(seconds=ttl):
                    online = True
            except Exception:
                online = False

        teams = _normalize_team_list(
            _split_csv(getattr(r, "team_names", None))
            + ([getattr(r, "team_name", None)] if getattr(r, "team_name", None) else [])
        )

        out.append(
            UserOut(
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


@router.put("/{user_id:int}")
async def update_user(
    user_id: int,
    payload: UserUpdateIn,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    """
    ✅ 编辑用户
    - super_admin / manager 可用
    - manager 仅可编辑自己创建的子账号（sales/finance/market）
    - 不允许改角色（role_id）
    """
    operator, operator_role = user_role

    target = (await db.execute(select(User).where(User.id == int(user_id)))).scalars().first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    target_role = await _get_user_primary_role_name(db, int(target.id))
    _ensure_manage_permission(
        operator=operator,
        operator_role=operator_role,
        target=target,
        target_role=target_role,
        action="edit",
    )

    # username：仅 super_admin 允许改（避免经理把登录体系搞乱）
    if payload.username is not None:
        if operator_role != ROLE_SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="No permission to update username")
        username = (payload.username or "").strip()
        if not username:
            raise HTTPException(status_code=400, detail="username is required")
        exists = (
            await db.execute(select(User.id).where(User.username == username, User.id != int(target.id)))
        ).scalars().first()
        if exists:
            raise HTTPException(status_code=400, detail="Username already exists")
        target.username = username

    if payload.real_name is not None:
        target.real_name = _clean_str(payload.real_name)

    if payload.password is not None:
        pwd = (payload.password or "").strip()
        if not pwd:
            raise HTTPException(status_code=400, detail="password is required")
        target.password_hash = hash_password(pwd)

    if payload.status is not None:
        st = int(payload.status)
        if st not in (0, 1):
            raise HTTPException(status_code=400, detail="status must be 0 or 1")
        target.status = st

    # team/team_names 管理：
    if operator_role == ROLE_MANAGER:
        # manager 只能改子账号 team_name，且必须属于自己团队集合
        if payload.team_names is not None:
            raise HTTPException(status_code=403, detail="Manager cannot update team_names")
        if payload.manager_id is not None:
            raise HTTPException(status_code=403, detail="Manager cannot update manager_id")

        if payload.team_name is not None:
            # 目标必须是下属（前面已校验），这里用 operator 作为 manager 进行校验
            target.team_name = _choose_single_team_for_child(manager=operator, requested_team_name=payload.team_name)

        # manager 不允许编辑经理账号（前面已限制 target_role），这里不需要额外处理
    else:
        # super_admin
        if target_role == ROLE_MANAGER:
            # 允许改经理的 team_names（至少1个），并同步 team_name 为默认第一个
            if payload.team_names is not None or payload.team_name is not None:
                req_team_names: List[str] = []
                if payload.team_names:
                    req_team_names.extend(list(payload.team_names))
                if payload.team_name:
                    req_team_names.append(str(payload.team_name))

                teams = _ensure_team_list_valid(req_team_names, err="请为经理账号分配团队（team_names/team_name）")
                target.team_names = ",".join(teams)
                target.team_name = teams[0]
        else:
            # 目标是 sales/finance/market：允许变更归属经理与 team_name
            if payload.manager_id is not None:
                mgr = await _ensure_user_is_manager(db, int(payload.manager_id))
                target.parent_id = int(mgr.id)
                target.team_name = _choose_single_team_for_child(manager=mgr, requested_team_name=payload.team_name)
            elif payload.team_name is not None:
                # 不改 manager_id 但改 team_name：需要找到当前 parent manager 进行校验
                if not getattr(target, "parent_id", None):
                    raise HTTPException(status_code=400, detail="target user has no manager (parent_id)")
                mgr = await _ensure_user_is_manager(db, int(target.parent_id))
                target.team_name = _choose_single_team_for_child(manager=mgr, requested_team_name=payload.team_name)

            if payload.team_names is not None:
                raise HTTPException(status_code=400, detail="Only manager accounts support team_names")

    await db.commit()

    logger.info(
        "Update user: operator=%s target=%s target_role=%s",
        operator.id,
        target.id,
        target_role,
    )
    return {"ok": True, "id": int(target.id)}


@router.delete("/{user_id:int}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user_role: Tuple[User, Optional[str]] = Depends(get_current_user_with_role),
):
    """
    ✅ 真删除用户（硬删除）
    - super_admin / manager 可用
    - manager 仅可删除自己创建的子账号（sales/finance/market）
    - 禁止删除自己
    - 禁止删除 super_admin
    - 删除经理前必须先删除其子账号（否则拦截）
    - 若被业务表外键引用（如订单等），会 IntegrityError：返回明确错误
    """
    operator, operator_role = user_role

    target = (await db.execute(select(User).where(User.id == int(user_id)))).scalars().first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    target_role = await _get_user_primary_role_name(db, int(target.id))
    _ensure_manage_permission(
        operator=operator,
        operator_role=operator_role,
        target=target,
        target_role=target_role,
        action="delete",
    )

    # 删除经理：必须先清理子账号（避免一锅端误删）
    if target_role == ROLE_MANAGER:
        child_exists = (
            await db.execute(select(func.count(User.id)).where(User.parent_id == int(target.id)))
        ).scalar_one()
        if int(child_exists or 0) > 0:
            raise HTTPException(status_code=400, detail="该经理账号下仍有子账号，请先删除/迁移子账号")

    # 真删除：先删关联表，再删 user
    try:
        await db.execute(delete(UserSession).where(UserSession.user_id == int(target.id)))
        await db.execute(delete(UserRole).where(UserRole.user_id == int(target.id)))
        await db.delete(target)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # 说明：被其它业务表引用（订单、创建人、审批人等），无法真删
        raise HTTPException(status_code=400, detail="该账号已被业务数据引用，无法真删除；请先清理引用数据或改为停用")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"delete failed: {str(e) or e.__class__.__name__}")

    logger.info("Delete user: operator=%s target=%s target_role=%s", operator.id, target.id, target_role)
    return {"ok": True, "id": int(user_id)}
