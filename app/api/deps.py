# app/api/deps.py
# encoding: utf-8
"""
依赖项（Dependencies）

去兼容版原则：
- 会话只认 DB 的 UserSession + expired + last_active_at + settings.SESSION_TIMEOUT_SECONDS
- 心跳续命：节流更新 last_active_at（避免每请求 commit 打爆 DB）
- ✅ 主角色：按“业务优先级”选主角色（避免同时拥有 sales+manager 时被误判为 sales）

团队隔离（增强版，兼容旧字段）：
- 兼容旧：user.team_name（单团队）
- 兼容新：user.team_names / user.team_ids / user.teams(relationship) / user.team_id / user.team_id_list 等多种形态
- 本文件只做“抽取与归一化”，不在这里猜业务规则（业务规则在各 API/service 层落地）

✅ 关键修复：
- _extract_teams_from_user 不再“命中第一个字段就 break”，改为多来源 union 合并。
  避免：User 同时存在 team_names="" 与 team_name="A"，结果团队被抽成空的严重问题。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple, List

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select, update, inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.config import settings
from app.core.constants import (
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_FINANCE,
    ROLE_MARKET,
    ROLE_SALES,
)
from app.models.session import UserSession
from app.models.user import User
from app.models.role import Role
from app.models.user_role import UserRole

logger = logging.getLogger(__name__)

# ✅ 心跳写库节流：默认 30s 写一次（生产强烈建议开启）
# 设为 0 可恢复“每请求更新”
SESSION_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("SESSION_HEARTBEAT_INTERVAL_SECONDS", "30") or "30")

# ✅ 主角色优先级（数值越小优先级越高）
_ROLE_PRIORITY = {
    ROLE_SUPER_ADMIN: 0,
    ROLE_MANAGER: 10,
    ROLE_FINANCE: 20,
    ROLE_MARKET: 30,
    ROLE_SALES: 40,
}


def _pick_primary_role(role_names: Tuple[str, ...]) -> Optional[str]:
    """
    ✅ 选主角色：
    - 优先按业务优先级（避免 manager+sales 被当成 sales）
    - 若都不在优先级表中：回退到 role_names[0]（保持兼容旧逻辑的稳定性）
    """
    if not role_names:
        return None
    known = [r for r in role_names if r in _ROLE_PRIORITY]
    if known:
        return min(known, key=lambda r: _ROLE_PRIORITY.get(r, 999999))
    return role_names[0]


def _utcnow_naive() -> datetime:
    """
    统一返回 UTC naive datetime，避免 tzinfo 写入 MySQL DATETIME 带来不确定性。
    """
    return datetime.utcnow()


def _to_utc_naive(dt: datetime) -> datetime:
    """
    兼容 DB 返回 naive/aware 两种情况：
    - naive：按 UTC 解释（与当前项目其他地方的 datetime.utcnow() 存储一致）
    - aware：转成 UTC 后去 tzinfo
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _safe_int(v: object, default: int) -> int:
    try:
        x = int(v)  # type: ignore
        return x
    except Exception:
        return default


def _get_redis():
    """
    惰性获取 redis（若项目未启用，返回 None）
    """
    try:
        from app.core.db import redis  # type: ignore
        return redis
    except Exception:
        return None


def _as_list(v: Any) -> List[Any]:
    """
    将各种输入归一成 list：
    - None -> []
    - str: 若包含逗号，按逗号切；否则单元素
    - set/tuple/list -> list(...)
    - 其它 -> [v]
    """
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, tuple) or isinstance(v, set):
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
    # 去重 + 稳定排序（避免响应漂移）
    return tuple(sorted(set(out)))


def _normalize_team_ids(raw: Any) -> Tuple[int, ...]:
    items = _as_list(raw)
    out: List[int] = []
    for x in items:
        try:
            if isinstance(x, bool):
                continue
            if isinstance(x, int):
                out.append(int(x))
                continue
            s = str(x).strip()
            if not s:
                continue
            out.append(int(s))
        except Exception:
            continue
    # 去重 + 稳定排序
    return tuple(sorted(set(out)))


def _extract_teams_from_user(user: User) -> Tuple[Tuple[int, ...], Tuple[str, ...]]:
    """
    从 user 对象上“尽力抽取”团队信息（不做业务判断）：
    支持字段/形态（任一存在即可）：
    - team_ids / team_id_list / teams_ids / teamIds / teamIdList
    - team_names / team_name_list / teamNames / teamNameList
    - team_id / team_name（旧版单团队）
    - teams（relationship，元素可能有 id/name/team_id/team_name 属性）

    ✅ 关键优化：
    - 多来源 union 合并：不会因为某个字段存在但为空（如 team_names=""）而“提前 break”导致漏 team_name。
    - 对 relationship: teams 仅在“已加载”情况下读取，避免 Async SQLAlchemy 懒加载触发 greenlet 错误。
    """
    team_ids_set: set[int] = set()
    team_names_set: set[str] = set()

    # 1) ids：多来源合并
    id_candidates = [
        "team_ids",
        "team_id_list",
        "teams_ids",
        "teamIds",
        "teamIdList",
        "team_id",
    ]
    for k in id_candidates:
        if not hasattr(user, k):
            continue
        raw = getattr(user, k, None)
        if raw is None:
            continue
        for tid in _normalize_team_ids(raw):
            team_ids_set.add(int(tid))

    # 2) names：多来源合并（重点：team_names 为空也不会遮蔽 team_name）
    name_candidates = [
        "team_names",
        "team_name_list",
        "teamNames",
        "teamNameList",
        "team_name",
    ]
    for k in name_candidates:
        if not hasattr(user, k):
            continue
        raw = getattr(user, k, None)
        if raw is None:
            continue
        for tn in _normalize_team_names(raw):
            team_names_set.add(str(tn))

    # 3) relationship: teams（仅作为补充；且仅在已加载时读取）
    rel = None
    try:
        st = inspect(user)
        if hasattr(st, "unloaded") and "teams" not in st.unloaded:
            rel = getattr(user, "teams", None)
    except Exception:
        rel = None

    if rel:
        try:
            for t in list(rel):  # type: ignore
                tid = None
                for k in ("id", "team_id", "teamId"):
                    if hasattr(t, k):
                        tid = getattr(t, k, None)
                        if tid is not None:
                            break

                tname = None
                for k in ("name", "team_name", "teamName"):
                    if hasattr(t, k):
                        tname = getattr(t, k, None)
                        if tname is not None:
                            break

                for x in _normalize_team_ids(tid):
                    team_ids_set.add(int(x))
                for x in _normalize_team_names(tname):
                    team_names_set.add(str(x))
        except Exception:
            pass

    team_ids = tuple(sorted(team_ids_set))
    team_names = tuple(sorted(team_names_set))
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

    now = _utcnow_naive()
    last_active_utc = _to_utc_naive(last_active_at)

    if now - last_active_utc > timedelta(seconds=ttl):
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
            need_touch = (now - last_active_utc) > timedelta(seconds=hb)

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


async def get_current_user_with_roles(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tuple[User, Optional[str], Tuple[str, ...]]:
    """
    返回：
    - user
    - primary_role_name：✅ 按业务优先级选主角色
    - role_names：该用户的全部角色（按 Role.id 升序）
    """
    stmt = (
        select(Role.id, Role.role_name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id)
        .order_by(Role.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    role_names = tuple([rname for _, rname in rows if rname])

    primary = _pick_primary_role(role_names)
    return user, primary, role_names


async def get_current_user_with_role(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tuple[User, Optional[str]]:
    user, primary, _roles = await get_current_user_with_roles(user=user, db=db)
    return user, primary


async def get_current_user_with_role_and_team(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tuple[User, Optional[str], Optional[str]]:
    """
    ✅ 兼容旧：返回 user + 主角色 + team_name（单值）
    - 若 user 具备多团队信息：取稳定排序后的第一个 team_name（用于旧接口的“默认团队”语义）
    - 新接口请用 get_current_user_with_role_and_teams()
    """
    user, primary, _roles = await get_current_user_with_roles(user=user, db=db)
    _team_ids, team_names = _extract_teams_from_user(user)
    team_name = team_names[0] if team_names else None
    return user, primary, team_name


async def get_current_user_with_role_and_teams(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tuple[User, Optional[str], Tuple[str, ...], Tuple[int, ...]]:
    """
    ✅ 新增：返回 user + 主角色 + team_names + team_ids
    - 用于后续“按团队隔离数据”统一依赖
    - 本文件不判断谁是经理/谁能多选，只负责把 user 上的团队信息抽取出来
    """
    user, primary, _roles = await get_current_user_with_roles(user=user, db=db)
    team_ids, team_names = _extract_teams_from_user(user)
    return user, primary, team_names, team_ids


async def get_current_user_scope(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    ✅ 新增：统一返回“访问范围”字典，后续 API 可直接用它做过滤
    返回字段（稳定）：
    - user
    - primary_role
    - roles
    - team_names
    - team_ids
    """
    user, primary, roles = await get_current_user_with_roles(user=user, db=db)
    team_ids, team_names = _extract_teams_from_user(user)
    return {
        "user": user,
        "primary_role": primary,
        "roles": roles,
        "team_names": team_names,
        "team_ids": team_ids,
    }
