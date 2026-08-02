# app/core/access_control.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, or_, false as sql_false, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ROLE_ALL,
    ROLE_FINANCE,
    ROLE_MANAGER,
    ROLE_MARKET,
    ROLE_SALES,
    ROLE_SUPER_ADMIN,
    TEAM_NAMES,
)
from app.models.order import Order
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole


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
QUOTE_ASSISTANT_ACCESS_ROLES = set(ROLE_ALL)
QUOTE_ASSISTANT_QUOTE_USE_ROLES = set(ROLE_ALL)
QUOTE_PLATFORM_ACCOUNT_MANAGE_ROLES = {ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_SALES}
QUOTE_DEFAULT_CONFIG_MANAGE_ROLES = {ROLE_SUPER_ADMIN}


def require_ai_assistant_access(
        *,
        role_name: Optional[str],
) -> None:
    """报价助手统一访问权限：允许系统已知角色进入，数据读取继续按各业务域 ACL 收口。"""
    rn = (role_name or "").strip()
    if rn in QUOTE_ASSISTANT_ACCESS_ROLES:
        return
    raise HTTPException(status_code=403, detail="当前账号无权访问报价助手")


def require_quote_assistant_quote_use_access(
        *,
        role_name: Optional[str],
) -> None:
    """报价/上传材料会消耗平台资源，仅允许业务链路角色使用。"""
    rn = (role_name or "").strip()
    if rn in QUOTE_ASSISTANT_QUOTE_USE_ROLES:
        return
    raise HTTPException(status_code=403, detail="当前账号无权发起报价或上传报价材料")


def require_quote_platform_account_manage_access(
        *,
        role_name: Optional[str],
) -> None:
    """平台账号会触发登录、保活和额度扣减，仅允许业务链路角色维护。"""
    rn = (role_name or "").strip()
    if rn in QUOTE_PLATFORM_ACCOUNT_MANAGE_ROLES:
        return
    raise HTTPException(status_code=403, detail="当前账号无权维护报价平台账号")


def require_quote_default_config_manage_access(
        *,
        role_name: Optional[str],
) -> None:
    """默认报价参数影响报价请求体，仅允许超级账号维护和查看管理明细。"""
    rn = (role_name or "").strip()
    if rn in QUOTE_DEFAULT_CONFIG_MANAGE_ROLES:
        return
    raise HTTPException(status_code=403, detail="只有超级账号可以维护默认报价参数")


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
            terms.extend([
                User.team_names == t,
                User.team_names.like(f"{t},%"),
                User.team_names.like(f"%,{t},%"),
                User.team_names.like(f"%,{t}"),
            ])
    return or_(*terms)


def order_salesperson_in_teams_expr(team_names: Tuple[str, ...]):
    team_user_ids = select(User.id).where(user_team_match_expr(team_names))
    return Order.salesperson_id.in_(team_user_ids)


async def ensure_user_in_teams(db: AsyncSession, user_id: int, teams: Tuple[str, ...]) -> None:
    stmt = select(User.id).where(and_(User.id == int(user_id), user_team_match_expr(teams)))
    ok = (await db.execute(stmt)).scalar_one_or_none()
    if not ok:
        raise HTTPException(status_code=403, detail="No permission")


async def ensure_order_read_acl_by_salesperson_id(
        db: AsyncSession,
        *,
        salesperson_id: int,
        current_user: User,
        role_name: Optional[str],
        team_names: Tuple[str, ...],
) -> None:
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


async def ensure_order_write_acl_by_salesperson_id(
        db: AsyncSession,
        *,
        salesperson_id: int,
        current_user: User,
        role_name: Optional[str],
        team_names: Tuple[str, ...],
) -> None:
    await ensure_order_read_acl_by_salesperson_id(
        db=db,
        salesperson_id=salesperson_id,
        current_user=current_user,
        role_name=role_name,
        team_names=team_names,
    )


async def apply_orders_list_acl(*, current_user: User, role_name: Optional[str],
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
        return tns[0],
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


# =========================
# 用户管理（Users）统一权限阀门
# =========================

USER_MANAGE_ALLOWED_ROLES = {ROLE_SUPER_ADMIN, ROLE_MANAGER}
USER_MANAGER_CREATABLE_ROLES = {ROLE_SALES, ROLE_FINANCE, ROLE_MARKET}
USER_SUPER_ADMIN_CREATABLE_ROLES = {ROLE_MANAGER, ROLE_SALES, ROLE_FINANCE, ROLE_MARKET}
USER_MANAGE_ALL_ALLOWED_ROLES = {
    ROLE_SUPER_ADMIN,
    ROLE_MANAGER,
    ROLE_SALES,
    ROLE_FINANCE,
    ROLE_MARKET,
}


def require_user_manage_access(*, role_name: Optional[str]) -> None:
    rn = (role_name or "").strip()
    if rn in USER_MANAGE_ALLOWED_ROLES:
        return
    raise HTTPException(status_code=403, detail="无权限管理用户")


def allowed_user_create_roles(*, role_name: Optional[str]) -> Tuple[str, ...]:
    rn = (role_name or "").strip()
    if rn == ROLE_SUPER_ADMIN:
        return tuple(sorted(USER_SUPER_ADMIN_CREATABLE_ROLES))
    if rn == ROLE_MANAGER:
        return tuple(sorted(USER_MANAGER_CREATABLE_ROLES))
    raise HTTPException(status_code=403, detail="无权限管理用户")


async def get_user_primary_role_name(*, db: AsyncSession, user_id: int) -> Optional[str]:
    stmt = (
        select(Role.role_name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == int(user_id))
        .order_by(Role.id.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def validate_user_role_name_for_create(*, current_role: Optional[str], target_role_name: str) -> None:
    tr = str(target_role_name or "").strip()
    if tr not in USER_MANAGE_ALL_ALLOWED_ROLES:
        raise HTTPException(status_code=400, detail="role_name 不合法")

    allowed = set(allowed_user_create_roles(role_name=current_role))
    if tr not in allowed:
        raise HTTPException(status_code=403, detail="当前角色无权限创建该类型账号")


async def ensure_user_manage_target_allowed(
        *,
        db: AsyncSession,
        current_user: User,
        current_role: Optional[str],
        target_user: User,
) -> None:
    """
    用户管理目标约束（按“自己创建的账号”收口）：
    - super_admin：仅可管理自己创建的账号，且不能管理自己
    - manager：仅可管理自己创建的账号，且目标角色只能是 sales/finance/market，且不能管理自己
    """
    rn = (current_role or "").strip()
    require_user_manage_access(role_name=rn)

    current_uid = int(getattr(current_user, "id", 0) or 0)
    target_uid = int(getattr(target_user, "id", 0) or 0)
    target_parent_id = int(getattr(target_user, "parent_id", 0) or 0)

    if target_uid == current_uid:
        raise HTTPException(status_code=403, detail="不能管理自己")

    if target_parent_id != current_uid:
        raise HTTPException(status_code=403, detail="仅可管理自己创建的账号")

    if rn == ROLE_SUPER_ADMIN:
        return

    target_role = await get_user_primary_role_name(db=db, user_id=target_uid)
    if target_role not in USER_MANAGER_CREATABLE_ROLES:
        raise HTTPException(status_code=403, detail="经理仅可管理业务/财务/市场账号")


def apply_users_list_acl(*, current_user: User, role_name: Optional[str], stmt):
    """
    用户列表权限（按“自己创建的账号”收口）：
    - super_admin：仅查看自己创建的账号（不包含自己）
    - manager：仅查看自己创建的账号（不包含自己）
    """
    rn = (role_name or "").strip()
    require_user_manage_access(role_name=rn)

    current_uid = int(getattr(current_user, "id", 0) or 0)

    if rn in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        return stmt.where(
            and_(
                User.parent_id == current_uid,
                User.id != current_uid,
            )
        )

    raise HTTPException(status_code=403, detail="无权限管理用户")
