# app/services/auth_service.py
# encoding: utf-8
from __future__ import annotations

"""
认证服务（新表口径 / 不做兼容）

原则：
- 只依赖冻结 Models：User/UserSession/UserRole/Role（均映射 *_new）
- 输出严格对齐 schemas：LoginOut
- 时间口径：北京时间 naive DATETIME
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_session_token, verify_password
from app.models.user import User
from app.models.session import UserSession
from app.models.user_role import UserRole
from app.models.role import Role

logger = logging.getLogger(__name__)
_BJ = ZoneInfo("Asia/Shanghai")


async def login(*, db: AsyncSession, username: str, password: str) -> Tuple[
    User, str, UserSession, list[str], list[str], str]:
    """
    返回：(user, token, session_row, team_names(list), team_name(default))
    team_names/team_name 语义：
    - team_names：可访问团队集合（权限范围）
    - team_name：默认/落点团队（单值）
    """
    q = select(User).where(User.username == username)
    user = (await db.execute(q)).scalars().first()
    if not user:
        raise ValueError("用户名或密码错误")

    if not verify_password(password, user.password_hash):
        raise ValueError("用户名或密码错误")

    now = datetime.now(_BJ).replace(tzinfo=None)
    token = generate_session_token()

    # upsert session（同一用户允许多 session：按你项目原逻辑，这里创建新 session）
    sess = UserSession(
        user_id=user.id,
        token=token,
        created_at=now,
        last_active_at=now,
        expired_at=None,
    )
    db.add(sess)

    # role names
    role_q = (
        select(Role.name)
        .select_from(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user.id)
    )
    role_names = list((await db.execute(role_q)).scalars().all())

    # teams normalize: only team_name + team_names (CSV) per hard rule
    team_name = (getattr(user, "team_name", None) or "").strip() or None
    raw_team_names = (getattr(user, "team_names", None) or "").strip()
    teams = [t.strip() for t in raw_team_names.split(",") if t.strip()] if raw_team_names else []
    if team_name and team_name not in teams:
        teams.append(team_name)

    # ensure default team_name if empty but teams exist
    if (not team_name) and teams:
        team_name = teams[0]

    await db.commit()
    await db.refresh(sess)
    return user, token, sess, role_names, teams, team_name or ""


async def logout(*, db: AsyncSession, token: str) -> None:
    if not token:
        return
    q = select(UserSession).where(UserSession.token == token)
    sess = (await db.execute(q)).scalars().first()
    if not sess:
        return
    await db.delete(sess)
    await db.commit()
