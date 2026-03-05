# app/services/users_service.py
# encoding: utf-8
from __future__ import annotations

"""
用户/账号管理服务（新表口径 / API 薄壳）

承重墙（2026-03-05）：
- 只允许使用冻结 models 中已确认存在的字段：
    User: id, username, password_hash, status, team_name, team_names, created_at, updated_at
    Role: id, role_name
    UserRole: user_id, role_id
- 不兼容旧字段/旧逻辑：display_name/phone/created_by 等若上游仍传入 -> 直接报错（强制上游对齐）
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Sequence, List

from sqlalchemy import select, func, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_SALES,
    ROLE_FINANCE,
    ROLE_MARKET,
)
from app.core.security import hash_password
from app.models.user import User
from app.models.user_role import UserRole
from app.models.role import Role

logger = logging.getLogger(__name__)
_BJ = ZoneInfo("Asia/Shanghai")


def _now_bj_naive() -> datetime:
    return datetime.now(_BJ).replace(tzinfo=None)


def _split_csv(v: Optional[str]) -> List[str]:
    s = (v or "").strip()
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x and x.strip()]


def _normalize_team_name(v: Optional[str]) -> Optional[str]:
    s = (v or "").strip()
    return s or None


def _normalize_team_names_csv(v: Optional[str]) -> str:
    # team_names 是 CSV 字符串存储（冻结口径）
    items = _split_csv(v)
    # 去重并稳定排序（审计友好）
    items = sorted(set(items))
    return ",".join(items)


async def _get_role_id(db: AsyncSession, role_name: str) -> int:
    # ✅ 冻结字段：Role.role_name
    rid = (await db.execute(select(Role.id).where(Role.role_name == role_name))).scalars().first()
    if not rid:
        raise ValueError(f"角色不存在: {role_name}")
    return int(rid)


async def _get_user_primary_role(db: AsyncSession, user_id: int) -> Optional[str]:
    # 不在此处猜“主角色优先级”，这里只取一个 role_name 用于少数校验
    r = (
        await db.execute(
            select(Role.role_name)
            .select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == int(user_id))
            .order_by(Role.id.asc())
        )
    ).scalars().first()
    return str(r) if r else None


async def list_users(
        *,
        db: AsyncSession,
        keyword: Optional[str] = None,
        role: Optional[str] = None,
) -> Sequence[User]:
    """
    列表查询（冻结口径）：
    - keyword 仅匹配 username（不再猜 display_name 等不存在列）
    - role 通过 Role.role_name 过滤
    """
    q = select(User)

    if keyword:
        like = f"%{keyword.strip()}%"
        q = q.where(User.username.like(like))

    if role:
        q = (
            q.join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.role_name == role)
        )

    q = q.order_by(User.id.desc())
    return list((await db.execute(q)).scalars().all())


async def create_user(
        *,
        db: AsyncSession,
        current_user: User,
        current_role: str,
        username: str,
        password: str,
        display_name: str = "",
        phone: str = "",
        role_name: str,
        team_name: Optional[str] = None,
        team_names: Optional[str] = None,
        created_by: Optional[int] = None,
) -> User:
    """
    创建用户（冻结口径）

    承重墙：
    - User model 不包含 display_name/phone/created_by（按你此前审查结论）
    - 若上游仍传入这些字段（非空），直接报错，强制 API/schemas 对齐 models
    """
    if (display_name or "").strip():
        raise ValueError("不支持字段: display_name（请对齐 models/schemas）")
    if (phone or "").strip():
        raise ValueError("不支持字段: phone（请对齐 models/schemas）")
    if created_by is not None:
        raise ValueError("不支持字段: created_by（请对齐 models/schemas）")

    now = _now_bj_naive()

    # 权限约束（不依赖不存在字段）
    if current_role == ROLE_MANAGER:
        if role_name not in {ROLE_SALES, ROLE_FINANCE, ROLE_MARKET}:
            raise PermissionError("manager 只能创建 sales/finance/market 子账号")
        # manager 创建子账号：必须指定 team_name 且在 manager 可见团队内
        mgr_team_name = _normalize_team_name(getattr(current_user, "team_name", None))
        mgr_team_names = _split_csv(getattr(current_user, "team_names", None))
        mgr_scope = set(mgr_team_names)
        if mgr_team_name:
            mgr_scope.add(mgr_team_name)

        child_team = _normalize_team_name(team_name)
        if not child_team:
            raise ValueError("子账号必须指定 team_name")
        if mgr_scope and child_team not in mgr_scope:
            raise PermissionError("子账号 team_name 不在经理团队范围内")

        team_name = child_team
        team_names = ""  # 子账号不设置 team_names（冻结语义）

    elif current_role == ROLE_SUPER_ADMIN:
        # super_admin 创建 manager：可指定 team_names，若 team_name 未给，取 team_names 第一个
        if role_name == ROLE_MANAGER:
            tn_csv = _normalize_team_names_csv(team_names)
            if tn_csv and not _normalize_team_name(team_name):
                first = _split_csv(tn_csv)[0]
                team_name = first
            team_names = tn_csv
        else:
            # 非 manager 必须有 team_name
            if not _normalize_team_name(team_name):
                raise ValueError("账号必须指定 team_name")

            team_name = _normalize_team_name(team_name)
            team_names = _normalize_team_names_csv(team_names)

    else:
        raise PermissionError("无权限创建用户")

    u = User(
        username=username.strip(),
        password_hash=hash_password(password),
        team_name=_normalize_team_name(team_name),
        team_names=_normalize_team_names_csv(team_names),
        status=1,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()

    rid = await _get_role_id(db, role_name)
    db.add(UserRole(user_id=int(u.id), role_id=int(rid)))

    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise ValueError(f"创建失败: {e}")

    await db.refresh(u)
    return u


async def update_user(
        *,
        db: AsyncSession,
        current_user: User,
        current_role: str,
        user_id: int,
        display_name: Optional[str] = None,
        phone: Optional[str] = None,
        password: Optional[str] = None,
        team_name: Optional[str] = None,
        team_names: Optional[str] = None,
) -> User:
    """
    更新用户（冻结口径）

    承重墙：
    - display_name/phone 不存在：若传入非空 -> 直接报错
    - manager 的“只能编辑自己创建的子账号”依赖 created_by（不存在），因此不实现该旧规则
      当前只保留：manager 不可编辑 manager/super_admin 账号（按角色判断）
    """
    if display_name is not None and display_name.strip():
        raise ValueError("不支持字段: display_name（请对齐 models/schemas）")
    if phone is not None and phone.strip():
        raise ValueError("不支持字段: phone（请对齐 models/schemas）")

    now = _now_bj_naive()

    u = (await db.execute(select(User).where(User.id == int(user_id)))).scalars().first()
    if not u:
        raise ValueError("用户不存在")

    # 权限约束
    if current_role == ROLE_SUPER_ADMIN:
        if int(getattr(u, "id", 0) or 0) == int(getattr(current_user, "id", 0) or 0):
            # 允许改自己密码/团队？这里不做额外限制，交给上游业务决定
            pass
    elif current_role == ROLE_MANAGER:
        # manager 不允许编辑 manager / super_admin 账号
        target_role = await _get_user_primary_role(db, int(u.id))
        if target_role in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
            raise PermissionError("无权限编辑该用户")
    else:
        raise PermissionError("无权限编辑用户")

    if password:
        u.password_hash = hash_password(password)

    if team_name is not None:
        u.team_name = _normalize_team_name(team_name)
    if team_names is not None:
        u.team_names = _normalize_team_names_csv(team_names)

    u.updated_at = now
    await db.commit()
    await db.refresh(u)
    return u


async def delete_user(
        *,
        db: AsyncSession,
        current_user: User,
        current_role: str,
        user_id: int,
) -> None:
    """
    删除用户（冻结口径）

    承重墙：
    - 不使用 created_by（不存在）
    - super_admin：不能删自己；不能删 super_admin；允许删 manager（不检查子账号归属关系，因为 created_by 不存在）
    - manager：默认无删除权限（避免误删）
    """
    u = (await db.execute(select(User).where(User.id == int(user_id)))).scalars().first()
    if not u:
        return

    if current_role == ROLE_SUPER_ADMIN:
        if int(getattr(u, "id", 0) or 0) == int(getattr(current_user, "id", 0) or 0):
            raise PermissionError("禁止删除自己")

        target_role = await _get_user_primary_role(db, int(u.id))
        if target_role == ROLE_SUPER_ADMIN:
            raise PermissionError("禁止删除 super_admin")

    else:
        raise PermissionError("无权限删除用户")

    await db.execute(delete(UserRole).where(UserRole.user_id == int(u.id)))
    await db.delete(u)
    await db.commit()
