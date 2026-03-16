# app/services/users_service.py
# encoding: utf-8
from __future__ import annotations

"""用户/账号管理服务（新表口径 / API 薄壳）

冻结原则：
- 只允许使用已确认存在的字段：
    User: id, username, real_name, password_hash, parent_id, status, team_name, team_names, created_at, updated_at
    Role: id, role_name
    UserRole: user_id, role_id
- 不兼容旧字段/旧逻辑：display_name/phone/created_by/role_id/manager_id 等一律禁止出现在接口真源

本文件只负责业务写入与读取；
权限规则统一收口到 app.core.access_control。
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access_control import (
    apply_users_list_acl as _ac_apply_users_list_acl,
    ensure_user_manage_target_allowed as _ac_ensure_user_manage_target_allowed,
    require_user_manage_access as _ac_require_user_manage_access,
    validate_user_role_name_for_create as _ac_validate_user_role_name_for_create,
)
from app.core.constants import ROLE_MANAGER, ROLE_SUPER_ADMIN, TEAM_NAMES
from app.core.security import hash_password
from app.models.role import Role
from app.models.session import UserSession
from app.models.user import User
from app.models.user_role import UserRole

logger = logging.getLogger(__name__)

_BJ = ZoneInfo("Asia/Shanghai")
_ONLINE_WINDOW_MINUTES = 5


def _now_bj_naive() -> datetime:
    return datetime.now(_BJ).replace(tzinfo=None)


def _normalize_team_names_csv(team_names: Optional[str]) -> str:
    if team_names is None:
        return ""
    s = str(team_names).strip()
    if not s:
        return ""
    parts = [x.strip() for x in s.split(",") if x and x.strip()]
    return ",".join(sorted(set(parts)))


def _validate_team_names(team_name: Optional[str], team_names_csv: str) -> None:
    names: list[str] = [x.strip() for x in team_names_csv.split(",") if x and x.strip()]
    if team_name:
        names.append(team_name.strip())

    for n in names:
        if n and n not in TEAM_NAMES:
            raise ValueError(f"非法团队：{n}")


def _validate_password(password: str) -> str:
    p = str(password or "")
    if len(p) < 6:
        raise ValueError("password 长度至少 6")
    return p


def _users_projection_stmt():
    """
    用户列表/详情的轻量投影查询：
    直接查询最终需要的列，不把整颗 ORM 实体树扛回来。
    """
    role_min_sq = (
        select(
            UserRole.user_id.label("user_id"),
            func.min(UserRole.role_id).label("role_id"),
        )
        .group_by(UserRole.user_id)
        .subquery("user_role_min_sq")
    )

    session_last_active_sq = (
        select(
            UserSession.user_id.label("user_id"),
            func.max(UserSession.last_active_at).label("last_active_at"),
        )
        .where(UserSession.expired == 0)
        .group_by(UserSession.user_id)
        .subquery("session_last_active_sq")
    )

    online_cutoff = _now_bj_naive() - timedelta(minutes=_ONLINE_WINDOW_MINUTES)

    stmt = (
        select(
            User.id.label("id"),
            User.username.label("username"),
            User.real_name.label("real_name"),
            User.team_name.label("team_name"),
            User.team_names.label("team_names"),
            User.status.label("status"),
            User.parent_id.label("parent_id"),
            User.created_at.label("created_at"),
            User.updated_at.label("updated_at"),
            Role.role_name.label("role_name"),
            case(
                (session_last_active_sq.c.last_active_at >= online_cutoff, 1),
                else_=0,
            ).label("is_online"),
        )
        .select_from(User)
        .outerjoin(role_min_sq, role_min_sq.c.user_id == User.id)
        .outerjoin(Role, Role.id == role_min_sq.c.role_id)
        .outerjoin(session_last_active_sq, session_last_active_sq.c.user_id == User.id)
    )
    return stmt


async def list_users(
    *,
    db: AsyncSession,
    current_user: User,
    current_role: str,
    keyword: Optional[str] = None,
    role: Optional[str] = None,
) -> Sequence:
    _ac_require_user_manage_access(role_name=current_role)

    stmt = _users_projection_stmt()
    stmt = _ac_apply_users_list_acl(
        current_user=current_user,
        role_name=current_role,
        stmt=stmt,
    )

    keyword_s = str(keyword or "").strip()
    if keyword_s:
        like = f"%{keyword_s}%"
        stmt = stmt.where(User.username.like(like))

    role_s = str(role or "").strip()
    if role_s:
        stmt = stmt.where(Role.role_name == role_s)

    # 需求：列表按更新时间倒叙；同更新时间时按 id 倒叙兜底
    stmt = stmt.order_by(User.updated_at.desc(), User.id.desc())

    return (await db.execute(stmt)).mappings().all()


async def get_user_projection_by_id(
    *,
    db: AsyncSession,
    user_id: int,
) -> Optional[dict]:
    stmt = _users_projection_stmt().where(User.id == int(user_id))
    row = (await db.execute(stmt)).mappings().first()
    return dict(row) if row else None


async def create_user(
    *,
    db: AsyncSession,
    current_user: User,
    current_role: str,
    username: str,
    password: str,
    role_name: str,
    team_name: Optional[str] = None,
    team_names: Optional[str] = None,
) -> User:
    _ac_require_user_manage_access(role_name=current_role)

    uname = str(username or "").strip()
    if not uname:
        raise ValueError("username is required")

    p = _validate_password(password)

    rname = str(role_name or "").strip()
    _ac_validate_user_role_name_for_create(
        current_role=current_role,
        target_role_name=rname,
    )

    # 先查角色，避免 user flush 成功后才发现 role 不存在，白跑一趟数据库
    role_row = (
        (await db.execute(select(Role).where(Role.role_name == rname)))
        .scalars()
        .first()
    )
    if not role_row:
        raise ValueError("角色不存在（请先初始化 seed）")

    tn = (str(team_name).strip() if team_name else None) or None
    tns_csv = _normalize_team_names_csv(team_names)
    _validate_team_names(tn, tns_csv)

    now = _now_bj_naive()

    parent_id = None
    if current_role in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        parent_id = int(getattr(current_user, "id", 0) or 0)

    user = User(
        username=uname,
        real_name=None,
        password_hash=hash_password(p),
        status=1,
        team_name=tn,
        team_names=tns_csv,
        parent_id=parent_id,
        created_at=now,
        updated_at=now,
    )
    db.add(user)

    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        logger.exception("create_user flush failed: %s", e)
        raise ValueError("用户名已存在") from e

    db.add(UserRole(user_id=user.id, role_id=role_row.id))

    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        logger.exception("create_user commit failed: %s", e)
        raise ValueError("创建用户失败（请检查角色/唯一约束）") from e

    # 不 refresh：API 层会调用 get_user_projection_by_id 重新读取最终态
    return user


async def update_user(
    *,
    db: AsyncSession,
    current_user: User,
    current_role: str,
    user_id: int,
    password: Optional[str] = None,
    team_name: Optional[str] = None,
    team_names: Optional[str] = None,
) -> User:
    _ac_require_user_manage_access(role_name=current_role)

    uid = int(user_id)
    user = (await db.execute(select(User).where(User.id == uid))).scalars().first()
    if not user:
        raise ValueError("用户不存在")

    await _ac_ensure_user_manage_target_allowed(
        db=db,
        current_user=current_user,
        current_role=current_role,
        target_user=user,
    )

    changed = False

    if password is not None:
        p = _validate_password(password)
        user.password_hash = hash_password(p)
        changed = True

    tn = (str(team_name).strip() if team_name is not None else None) or None
    tns_csv = _normalize_team_names_csv(team_names)
    _validate_team_names(tn, tns_csv)

    if user.team_name != tn:
        user.team_name = tn
        changed = True

    if (user.team_names or "") != tns_csv:
        user.team_names = tns_csv
        changed = True

    if changed:
        user.updated_at = _now_bj_naive()
        await db.commit()
        # 不 refresh：API 层会调用 get_user_projection_by_id 重新读取最终态

    return user


async def delete_user(
    *,
    db: AsyncSession,
    current_user: User,
    current_role: str,
    user_id: int,
) -> None:
    _ac_require_user_manage_access(role_name=current_role)

    uid = int(user_id)
    current_uid = int(getattr(current_user, "id", 0) or 0)
    if uid == current_uid:
        raise ValueError("不能删除自己")

    user = (await db.execute(select(User).where(User.id == uid))).scalars().first()
    if not user:
        raise ValueError("用户不存在")

    await _ac_ensure_user_manage_target_allowed(
        db=db,
        current_user=current_user,
        current_role=current_role,
        target_user=user,
    )

    await db.execute(delete(UserRole).where(UserRole.user_id == uid))
    await db.execute(delete(User).where(User.id == uid))
    await db.commit()