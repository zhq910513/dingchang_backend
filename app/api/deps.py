# app/api/deps.py
# encoding: utf-8
"""
依赖项（Dependencies）

去兼容版原则：
- 会话只认 DB 的 UserSession + expired + last_active_at + settings.SESSION_TIMEOUT_SECONDS
- 心跳续命：节流更新 last_active_at（避免每请求 commit 打爆 DB）
- ✅ 主角色：按“业务优先级”选主角色（避免同时拥有 sales+manager 时被误判为 sales）

团队抽取（冻结字段口径）：
- 只认 models 冻结字段：
    * user.team_name：默认/落点团队（单值）
    * user.team_names：可访问团队集合（CSV 字符串）
- 本文件只做“抽取与归一化”，不在这里猜业务规则（业务规则在各 API/service 层落地）

✅ 承重墙修复（2026-03-05）：
- deps 只允许一种返回签名：CurrentUserContext
- 不再返回 2/3/4 元组，彻底消灭“错误解包/错误取属性”的隐患

✅ 时间口径（2026-03-01）：
- DB 全局约定：北京时间 naive DATETIME
- session 的 last_active_at / timeout 计算：统一按北京时间 naive 处理（不再用 UTC naive）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple, List
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_FINANCE,
    ROLE_MARKET,
    ROLE_SALES,
)
from app.core.db import get_db
from app.models.role import Role
from app.models.session import UserSession
from app.models.user import User
from app.models.user_role import UserRole

BJ_TZ = ZoneInfo("Asia/Shanghai")

SESSION_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("SESSION_HEARTBEAT_INTERVAL_SECONDS", "30") or "30")

_ROLE_PRIORITY = {
    ROLE_SUPER_ADMIN: 0,
    ROLE_MANAGER: 10,
    ROLE_FINANCE: 20,
    ROLE_MARKET: 30,
    ROLE_SALES: 40,
}


@dataclass(frozen=True)
class CurrentUserContext:
    """
    deps 的唯一输出结构（承重墙）：

    - user：当前登录用户
    - primary_role：主角色（按优先级选取）
    - role_names：全部角色集合（用于少数场景，例如 UI 展示/调试；权限判断应以 primary_role 为主）
    - team_names：用户可访问团队集合（冻结字段归一化）
    - team_ids：当前体系暂不使用（保持空元组，保留扩展位）
    """
    user: User
    primary_role: Optional[str]
    role_names: Tuple[str, ...]
    team_names: Tuple[str, ...]
    team_ids: Tuple[int, ...]


def _pick_primary_role(role_names: Tuple[str, ...]) -> Optional[str]:
    if not role_names:
        return None
    known = [r for r in role_names if r in _ROLE_PRIORITY]
    if known:
        return min(known, key=lambda r: _ROLE_PRIORITY.get(r, 999999))
    return role_names[0]


def _now_bj_naive() -> datetime:
    # ✅ 北京时间 naive DATETIME
    return datetime.now(BJ_TZ).replace(tzinfo=None)


def _to_bj_naive(dt: datetime) -> datetime:
    # ✅ 兼容 DB 返回 naive/aware 两种情况：
    # - naive：按北京时间解释（项目约定）
    # - aware：转到 Asia/Shanghai 后去 tzinfo
    if dt.tzinfo is None:
        return dt
    try:
        return dt.astimezone(BJ_TZ).replace(tzinfo=None)
    except Exception:
        return dt.replace(tzinfo=None)


def _safe_int(v: object, default: int) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except Exception:
        return default


def _get_redis():
    try:
        from app.core.db import redis  # type: ignore
        return redis
    except Exception:
        return None


def _as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, (tuple, set)):
        return list(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if "," in s:
            return [x.strip() for x in s.split(",") if x and x.strip()]
        return [s]
    return [v]


def _normalize_team_names(raw: Any) -> Tuple[str, ...]:
    items = _as_list(raw)
    out: List[str] = []
    for x in items:
        try:
            s = str(x).strip()
        except Exception:
            continue
        if not s:
            continue
        out.append(s)
    return tuple(sorted(set(out)))


def _extract_teams_from_user(user: User) -> Tuple[Tuple[int, ...], Tuple[str, ...]]:
    """
    团队抽取（新口径）：

    - 只认 models 冻结字段：
        * user.team_name：默认/落点团队（单值）
        * user.team_names：可访问团队集合（CSV 字符串）
    - 不再兼容其它历史字段形态（team_ids/team_id_list/...）
    """
    team_names_set: set[str] = set()

    team_name = (getattr(user, "team_name", None) or "").strip()
    if team_name:
        team_names_set.add(team_name)

    raw_team_names = (getattr(user, "team_names", None) or "").strip()
    if raw_team_names:
        for t in raw_team_names.split(","):
            t = (t or "").strip()
            if t:
                team_names_set.add(t)

    # 当前体系 team_ids 暂不使用（保持空元组）
    team_ids: Tuple[int, ...] = tuple()
    team_names: Tuple[str, ...] = tuple(sorted(team_names_set))
    return team_ids, team_names


async def get_current_session(
        db: AsyncSession = Depends(get_db),
        x_session_token: Optional[str] = Header(default=None, alias="X-Session-Token"),
) -> UserSession:
    token = (x_session_token or "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Session-Token")

    stmt = select(UserSession).where(UserSession.session_token == token)
    sess = (await db.execute(stmt)).scalars().first()

    if not sess or int(getattr(sess, "expired", 0) or 0) == 1:
        try:
            rds = _get_redis()
            if rds:
                await rds.delete(f"session:{token}")
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    last_active_at = getattr(sess, "last_active_at", None)
    if not last_active_at:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session (no last_active_at)")

    ttl = _safe_int(getattr(settings, "SESSION_TIMEOUT_SECONDS", 7200), 7200)
    if ttl <= 0:
        ttl = 7200

    now = _now_bj_naive()
    last_active_bj = _to_bj_naive(last_active_at)

    if now - last_active_bj > timedelta(seconds=ttl):
        try:
            await db.execute(
                update(UserSession)
                .where(UserSession.id == sess.id)
                .values(expired=1)
            )
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                pass

        try:
            rds = _get_redis()
            if rds:
                await rds.delete(f"session:{token}")
        except Exception:
            pass

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")

    hb = SESSION_HEARTBEAT_INTERVAL_SECONDS
    if hb < 0:
        hb = 0

    try:
        need_touch = True
        if hb > 0:
            need_touch = (now - last_active_bj) > timedelta(seconds=hb)

        if need_touch:
            await db.execute(
                update(UserSession)
                .where(UserSession.id == sess.id)
                .values(last_active_at=now)
            )
            await db.commit()
            try:
                sess.last_active_at = now
            except Exception:
                pass
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass

    try:
        rds = _get_redis()
        if rds:
            await rds.set(f"session:{token}", str(sess.user_id), ex=ttl)
    except Exception:
        pass

    return sess


async def get_current_user(
        sess: UserSession = Depends(get_current_session),
        db: AsyncSession = Depends(get_db),
) -> User:
    uid = int(getattr(sess, "user_id", 0) or 0)
    if uid <= 0:
        raise HTTPException(status_code=401, detail="Invalid session user_id")

    user = (await db.execute(select(User).where(User.id == uid))).scalars().first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if int(getattr(user, "status", 0) or 0) != 1:
        raise HTTPException(status_code=403, detail="User disabled")

    return user


async def get_current_user_context(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
) -> CurrentUserContext:
    stmt = (
        select(Role.id, Role.role_name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id)
        .order_by(Role.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    role_names = tuple([rname for _, rname in rows if rname])

    primary = _pick_primary_role(role_names)
    team_ids, team_names = _extract_teams_from_user(user)

    return CurrentUserContext(
        user=user,
        primary_role=primary,
        role_names=role_names,
        team_names=team_names,
        team_ids=team_ids,
    )


# ---- 兼容导入名：保留函数名，但返回结构统一为 CurrentUserContext（不再返回 tuple） ----

async def get_current_user_with_roles(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
) -> CurrentUserContext:
    return await get_current_user_context(user=user, db=db)


async def get_current_user_with_role(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
) -> CurrentUserContext:
    return await get_current_user_context(user=user, db=db)


async def get_current_user_with_role_and_teams(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
) -> CurrentUserContext:
    return await get_current_user_context(user=user, db=db)
