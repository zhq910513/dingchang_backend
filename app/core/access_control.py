# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, or_, false as sql_false, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ROLE_FINANCE, ROLE_MANAGER, ROLE_MARKET, ROLE_SALES, ROLE_SUPER_ADMIN, TEAM_NAMES,
)
from app.models.order import Order
from app.models.user import User


def split_team_names_any(val) -> List[str]:
    if val is None:
        return []
    if isinstance(val, (list, tuple, set)):
        out = []
        for x in val:
            s = str(x or "").strip()
            if s:
                out.append(s)
        seen = set()
        uniq = []
        for x in out:
            if x in seen:
                continue
            seen.add(x)
            uniq.append(x)
        return uniq
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        for sep in [",", "，", "|", ";", "；", " "]:
            if sep in s:
                parts = [p.strip() for p in s.split(sep)]
                parts = [p for p in parts if p]
                if parts:
                    seen = set()
                    uniq = []
                    for x in parts:
                        if x in seen:
                            continue
                        seen.add(x)
                        uniq.append(x)
                    return uniq
        return [s]
    s2 = str(val or "").strip()
    return [s2] if s2 else []


def pick_manager_id_from_salesperson(sp: Optional[User]) -> Optional[int]:
    if not sp:
        return None
    for key in ("manager_id", "leader_id", "supervisor_id", "parent_id"):
        try:
            v = getattr(sp, key, None)
        except Exception:
            v = None
        if v is None:
            continue
        try:
            iv = int(v)
            if iv > 0:
                return iv
        except Exception:
            continue
    return None


def pick_manager_name_inline(sp: Optional[User]) -> Optional[str]:
    if not sp:
        return None
    for key in ("manager_name", "leader_name", "supervisor_name", "parent_name"):
        try:
            v = getattr(sp, key, None)
        except Exception:
            v = None
        s = str(v or "").strip()
        if s:
            return s
    return None


def normalize_team_names(team_names: Optional[Tuple[str, ...] | List[str]]) -> Tuple[str, ...]:
    if not team_names:
        return tuple()
    arr = [str(x or "").strip() for x in (team_names if not isinstance(team_names, tuple) else list(team_names))]
    arr = [x for x in arr if x]
    return tuple(sorted(set(arr)))


# =========================
# 报价助手（AI Assistant / 报价助手）统一权限阀门
# =========================
def require_ai_assistant_access(
        *,
        role_name: Optional[str],
        team_names: Tuple[str, ...] = tuple(),
) -> None:
    """
    报价助手统一访问权限（当前阶段收口策略）：
    - 仅超级管理员可访问（试用/灰度阶段）
    - 后续若放开 manager/market 等角色，只改这里，不改 API/service

    说明：
    - 当前规则不强制校验 team_names（超级管理员通常可无团队）
    - 若未来放开非 super_admin，再按角色要求接入团队校验逻辑
    """
    rn = (role_name or "").strip()
    if rn == ROLE_SUPER_ADMIN:
        return
    raise HTTPException(status_code=403, detail="报价助手当前仅超级管理员可用")


def require_team_for_non_super_admin(role_name: Optional[str], team_names: Tuple[str, ...]) -> None:
    if role_name == ROLE_SUPER_ADMIN:
        return
    tns = normalize_team_names(team_names)
    if not tns:
        raise HTTPException(status_code=400, detail="当前账号未配置团队，无法访问该模块")
    invalid = [t for t in tns if t not in TEAM_NAMES]
    if invalid:
        raise HTTPException(status_code=403, detail="当前账号团队非法（team_name/team_names）")


def require_single_team_for_strict_roles(role_name: Optional[str], team_names: Tuple[str, ...]) -> str:
    require_team_for_non_super_admin(role_name, team_names)
    tns = normalize_team_names(team_names)
    if role_name in (ROLE_SALES, ROLE_FINANCE, ROLE_MARKET):
        if len(tns) != 1:
            raise HTTPException(status_code=400, detail="当前账号团队配置异常：该角色必须且只能属于 1 个团队")
        return tns[0]
    return tns[0] if tns else ""


def allowed_teams_for_user(role_name: Optional[str], team_names: Tuple[str, ...]) -> Tuple[str, ...]:
    if role_name == ROLE_SUPER_ADMIN:
        return tuple(str(x) for x in TEAM_NAMES)
    tns = normalize_team_names(team_names)
    require_team_for_non_super_admin(role_name, tns)
    if role_name == ROLE_MANAGER:
        return tns
    if role_name in (ROLE_FINANCE, ROLE_MARKET, ROLE_SALES):
        return require_single_team_for_strict_roles(role_name, tns),
    return tns


def require_team_filter_allowed(*, role_name: Optional[str], team_names: Tuple[str, ...], team_filter: str) -> None:
    tf = str(team_filter or "").strip()
    if not tf:
        return
    if tf not in TEAM_NAMES:
        raise HTTPException(status_code=400, detail="team_name invalid")
    if role_name == ROLE_SUPER_ADMIN:
        return
    allowed = set(allowed_teams_for_user(role_name, team_names))
    if tf not in allowed:
        raise HTTPException(status_code=403, detail="No permission")


def user_team_match_expr(teams: Tuple[str, ...]):
    tns = tuple(str(x or "").strip() for x in (teams or ()) if str(x or "").strip())
    if not tns:
        return sql_false()
    terms = [User.team_name.in_(list(tns))]
    if hasattr(User, "team_names"):
        for t in tns:
            terms.extend([User.team_names == t, User.team_names.like(f"{t},%"), User.team_names.like(f"%,{t},%"),
                          User.team_names.like(f"%,{t}")])
    return or_(*terms)


def order_salesperson_in_teams_expr(team_names: Tuple[str, ...]):
    team_user_ids = select(User.id).where(user_team_match_expr(team_names))
    return Order.salesperson_id.in_(team_user_ids)


async def ensure_user_in_teams(db: AsyncSession, user_id: int, teams: Tuple[str, ...]) -> None:
    stmt = select(User.id).where(and_(User.id == int(user_id), user_team_match_expr(teams)))
    ok = (await db.execute(stmt)).scalar_one_or_none()
    if not ok:
        raise HTTPException(status_code=403, detail="No permission")


async def ensure_order_read_acl_by_salesperson_id(db: AsyncSession, *, salesperson_id: int, current_user: User,
                                                  role_name: Optional[str], team_names: Tuple[str, ...]) -> None:
    rn = role_name or ""
    if rn == ROLE_SUPER_ADMIN:
        return
    require_team_for_non_super_admin(role_name, team_names)
    tns = normalize_team_names(team_names)
    if rn == ROLE_SALES:
        if int(salesperson_id) != int(current_user.id):
            raise HTTPException(status_code=403, detail="No permission")
        return
    if rn == ROLE_MANAGER:
        await ensure_user_in_teams(db, int(salesperson_id), tns)
        return
    if rn in (ROLE_MARKET, ROLE_FINANCE):
        my_team = require_single_team_for_strict_roles(role_name, tns)
        await ensure_user_in_teams(db, int(salesperson_id), (my_team,))
        return
    raise HTTPException(status_code=403, detail="No permission")


async def ensure_order_write_acl_by_salesperson_id(db: AsyncSession, *, salesperson_id: int, current_user: User,
                                                   role_name: Optional[str], team_names: Tuple[str, ...]) -> None:
    # 与 read ACL 一致，写入口可额外在业务层限制角色
    await ensure_order_read_acl_by_salesperson_id(db, salesperson_id=salesperson_id, current_user=current_user,
                                                  role_name=role_name, team_names=team_names)


async def apply_orders_list_acl(db: AsyncSession, *, current_user: User, role_name: Optional[str],
                                team_names: Tuple[str, ...], clauses: List) -> None:
    rn = role_name or ""
    if rn == ROLE_SUPER_ADMIN:
        return
    require_team_for_non_super_admin(role_name, team_names)
    tns = normalize_team_names(team_names)
    if rn == ROLE_SALES:
        clauses.append(Order.salesperson_id == int(current_user.id))
        return
    if rn == ROLE_MANAGER:
        clauses.append(order_salesperson_in_teams_expr(tns))
        return
    if rn in (ROLE_MARKET, ROLE_FINANCE):
        my_team = require_single_team_for_strict_roles(role_name, tns)
        clauses.append(order_salesperson_in_teams_expr((my_team,)))
        return
    raise HTTPException(status_code=403, detail="No permission")


def current_team_names_or_403(*, role_name: Optional[str], team_names: Tuple[str, ...]) -> Optional[Tuple[str, ...]]:
    if role_name == ROLE_SUPER_ADMIN:
        return None
    tns = normalize_team_names(team_names)
    if not tns:
        raise HTTPException(status_code=403, detail="当前账号未绑定团队（team_name/team_names）")
    invalid = [t for t in tns if t not in TEAM_NAMES]
    if invalid:
        raise HTTPException(status_code=403, detail="当前账号团队非法（team_name/team_names）")
    if role_name in (ROLE_FINANCE, ROLE_MARKET):
        if len(tns) != 1:
            raise HTTPException(status_code=403, detail="当前账号团队配置异常：该角色必须且只能属于 1 个团队")
        return (tns[0],)
    if role_name == ROLE_MANAGER:
        return tns
    raise HTTPException(status_code=403, detail="No permission")


def parse_query_team_names(team_name: Optional[str], team_names: Optional[Tuple[str, ...]]) -> Tuple[str, ...]:
    arr: List[str] = []
    s = (team_name or "").strip()
    if s:
        arr.append(s)
    if team_names:
        for x in team_names:
            sx = str(x or "").strip()
            if sx:
                arr.append(sx)
    return normalize_team_names(arr)


def effective_team_filter_for_query(*, role_name: Optional[str], current_team_names: Optional[Tuple[str, ...]],
                                    team_name: Optional[str], team_names: Optional[Tuple[str, ...]]) -> Optional[
    Tuple[str, ...]]:
    requested = parse_query_team_names(team_name, team_names)
    if requested:
        invalid = [t for t in requested if t not in TEAM_NAMES]
        if invalid:
            raise HTTPException(status_code=400, detail="team_name/team_names 非法")
    if role_name == ROLE_SUPER_ADMIN:
        return requested or None
    allowed = current_team_names or tuple()
    if not requested:
        return allowed
    eff = tuple(sorted(set(allowed) & set(requested)))
    if not eff:
        raise HTTPException(status_code=403, detail="跨团队筛选被拒绝")
    if role_name in (ROLE_FINANCE, ROLE_MARKET):
        if len(allowed) == 1 and eff != allowed:
            raise HTTPException(status_code=403, detail="跨团队筛选被拒绝")
        return allowed
    if role_name == ROLE_MANAGER:
        return eff
    return allowed


async def salesperson_in_current_teams_or_403(*, salesperson: Optional[User],
                                              current_team_names: Optional[Tuple[str, ...]]) -> None:
    if current_team_names is None:
        return
    allowed = set(current_team_names)
    sp = salesperson
    team_name_val = (getattr(sp, "team_name", None) or "").strip() if sp else ""
    team_names_val = split_team_names_any(getattr(sp, "team_names", None)) if sp else []
    if not team_names_val and team_name_val:
        team_names_val = [team_name_val]
    if not team_names_val or not (set(team_names_val) & allowed):
        raise HTTPException(status_code=403, detail="跨团队访问被拒绝")
