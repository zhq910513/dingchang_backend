# app/services/users_service.py
# encoding: utf-8
from __future__ import annotations

"""用户/账号管理服务（新表口径 / API 薄壳）

冻结原则：
- 只允许使用已确认存在的字段：
    User: id, username, real_name, password_hash, parent_id, status, team_name,
          team_names, created_at, updated_at
    Role: id, role_name
    UserRole: user_id, role_id
- 不兼容旧字段/旧逻辑：display_name/phone/created_by/role_id/manager_id 等
  一律禁止出现在接口真源

本文件职责：
- 用户管理域的读写服务
- 用户管理域内的权限 bundle 编译
- 用户主角色投影（统一口径：业务优先级）
- 列表范围 SQL 编译
- 页面级 / 行级 meta 生成
- 目标用户可管理性纯逻辑校验
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.constants import (
    ROLE_FINANCE,
    ROLE_MANAGER,
    ROLE_MARKET,
    ROLE_SALES,
    ROLE_SUPER_ADMIN,
    TEAM_NAMES,
)
from app.core.security import hash_password
from app.models.role import Role
from app.models.session import UserSession
from app.models.user import User
from app.models.user_role import UserRole

logger = logging.getLogger(__name__)

_BJ = ZoneInfo("Asia/Shanghai")
_ONLINE_WINDOW_MINUTES = 5
_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100

_ROLE_PRIORITY: Dict[str, int] = {
    ROLE_SUPER_ADMIN: 0,
    ROLE_MANAGER: 10,
    ROLE_FINANCE: 20,
    ROLE_MARKET: 30,
    ROLE_SALES: 40,
}

_ALL_MANAGEABLE_ROLE_NAMES: List[str] = [
    ROLE_MANAGER,
    ROLE_SALES,
    ROLE_FINANCE,
    ROLE_MARKET,
]

_MANAGER_MANAGEABLE_ROLE_NAMES: List[str] = [
    ROLE_SALES,
    ROLE_FINANCE,
    ROLE_MARKET,
]


def _now_bj_naive() -> datetime:
    return datetime.now(_BJ).replace(tzinfo=None)


def _normalize_team_names_csv(team_names: Optional[str]) -> str:
    if team_names is None:
        return ""
    team_names_str = str(team_names).strip()
    if not team_names_str:
        return ""
    parts = [x.strip() for x in team_names_str.split(",") if x and x.strip()]
    return ",".join(sorted(set(parts)))


def _validate_team_names(team_name: Optional[str], team_names_csv: str) -> None:
    names: List[str] = [x.strip() for x in team_names_csv.split(",") if x and x.strip()]
    if team_name:
        names.append(team_name.strip())

    for name in names:
        if name and name not in TEAM_NAMES:
            raise ValueError(f"非法团队：{name}")


def _validate_password(password: str) -> str:
    password_str = str(password or "")
    if len(password_str) < 6:
        raise ValueError("password 长度至少 6")
    return password_str


def _normalize_pagination(page: Optional[int], page_size: Optional[int]) -> Dict[str, int]:
    page_int = int(page or _DEFAULT_PAGE)
    page_size_int = int(page_size or _DEFAULT_PAGE_SIZE)

    if page_int <= 0:
        page_int = _DEFAULT_PAGE
    if page_size_int <= 0:
        page_size_int = _DEFAULT_PAGE_SIZE
    if page_size_int > _MAX_PAGE_SIZE:
        page_size_int = _MAX_PAGE_SIZE

    offset = (page_int - 1) * page_size_int
    return {
        "page": page_int,
        "page_size": page_size_int,
        "offset": offset,
        "limit": page_size_int,
    }


def _role_priority_expr():
    return case(
        (Role.role_name == ROLE_SUPER_ADMIN, _ROLE_PRIORITY[ROLE_SUPER_ADMIN]),
        (Role.role_name == ROLE_MANAGER, _ROLE_PRIORITY[ROLE_MANAGER]),
        (Role.role_name == ROLE_FINANCE, _ROLE_PRIORITY[ROLE_FINANCE]),
        (Role.role_name == ROLE_MARKET, _ROLE_PRIORITY[ROLE_MARKET]),
        (Role.role_name == ROLE_SALES, _ROLE_PRIORITY[ROLE_SALES]),
        else_=999999,
    )


def _build_primary_role_sq():
    """
    统一主角色口径（冻结）：
    - 按业务优先级选主角色，而不是按最小 role_id
    - 若优先级相同，则按 Role.id 升序兜底

    输出列：
    - user_id
    - role_name
    """
    role_priority_expr = _role_priority_expr()

    ranked_sq = (
        select(
            UserRole.user_id.label("user_id"),
            Role.role_name.label("role_name"),
            func.row_number()
            .over(
                partition_by=UserRole.user_id,
                order_by=(role_priority_expr.asc(), Role.id.asc()),
            )
            .label("rn"),
        )
        .select_from(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .subquery("user_primary_role_ranked_sq")
    )

    primary_role_sq = (
        select(
            ranked_sq.c.user_id.label("user_id"),
            ranked_sq.c.role_name.label("role_name"),
        )
        .where(ranked_sq.c.rn == 1)
        .subquery("user_primary_role_sq")
    )
    return primary_role_sq


def _users_projection_stmt() -> Select:
    """
    用户列表/详情的轻量投影查询：
    - 直接查询最终需要的列，不把 ORM 关系树扛回来
    - 主角色口径统一走业务优先级
    """
    primary_role_sq = _build_primary_role_sq()

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
            primary_role_sq.c.role_name.label("role_name"),
            case(
                (session_last_active_sq.c.last_active_at >= online_cutoff, 1),
                else_=0,
            ).label("is_online"),
        )
        .select_from(User)
        .outerjoin(primary_role_sq, primary_role_sq.c.user_id == User.id)
        .outerjoin(session_last_active_sq, session_last_active_sq.c.user_id == User.id)
    )
    return stmt


def _compile_user_access_bundle(*, current_user: User, current_role: str) -> Dict[str, Any]:
    current_user_id = int(getattr(current_user, "id", 0) or 0)
    role_name = str(current_role or "").strip()

    if role_name == ROLE_SUPER_ADMIN:
        creatable_role_names = list(_ALL_MANAGEABLE_ROLE_NAMES)
        return {
            "current_user_id": current_user_id,
            "primary_role": role_name,
            "capabilities": {
                "user.manage.access": True,
                "user.list.view": True,
                "user.create": True,
                "user.update": True,
                "user.delete": True,
            },
            "scopes": {
                "user.visible_scope": "created_by_me",
                "user.manage_scope": "created_by_me",
                "user.creatable_role_names": creatable_role_names,
                "user.exclude_self": True,
            },
        }

    if role_name == ROLE_MANAGER:
        creatable_role_names = list(_MANAGER_MANAGEABLE_ROLE_NAMES)
        return {
            "current_user_id": current_user_id,
            "primary_role": role_name,
            "capabilities": {
                "user.manage.access": True,
                "user.list.view": True,
                "user.create": True,
                "user.update": True,
                "user.delete": True,
            },
            "scopes": {
                "user.visible_scope": "created_by_me",
                "user.manage_scope": "created_by_me",
                "user.creatable_role_names": creatable_role_names,
                "user.exclude_self": True,
            },
        }

    return {
        "current_user_id": current_user_id,
        "primary_role": role_name,
        "capabilities": {
            "user.manage.access": False,
            "user.list.view": False,
            "user.create": False,
            "user.update": False,
            "user.delete": False,
        },
        "scopes": {
            "user.visible_scope": "none",
            "user.manage_scope": "none",
            "user.creatable_role_names": [],
            "user.exclude_self": True,
        },
    }


def _require_bundle_capability(bundle: Mapping[str, Any], capability_key: str, detail: str) -> None:
    capabilities = dict(bundle.get("capabilities") or {})
    if not bool(capabilities.get(capability_key)):
        raise PermissionError(detail)


def _build_user_list_scope_clauses(bundle: Mapping[str, Any]) -> List[Any]:
    _require_bundle_capability(bundle, "user.manage.access", "无权限管理用户")
    _require_bundle_capability(bundle, "user.list.view", "无权限查看用户列表")

    scopes = dict(bundle.get("scopes") or {})
    visible_scope = str(scopes.get("user.visible_scope") or "").strip()
    exclude_self = bool(scopes.get("user.exclude_self"))
    current_user_id = int(bundle.get("current_user_id") or 0)

    if visible_scope == "none":
        raise PermissionError("无权限查看用户列表")

    clauses: List[Any] = []

    if visible_scope == "created_by_me":
        clauses.append(User.parent_id == current_user_id)

    if exclude_self and current_user_id > 0:
        clauses.append(User.id != current_user_id)

    return clauses


def _validate_role_name_for_create(bundle: Mapping[str, Any], target_role_name: str) -> None:
    _require_bundle_capability(bundle, "user.manage.access", "无权限管理用户")
    _require_bundle_capability(bundle, "user.create", "无权限创建用户")

    target_role = str(target_role_name or "").strip()
    if target_role not in _ALL_MANAGEABLE_ROLE_NAMES:
        raise ValueError("role_name 不合法")

    scopes = dict(bundle.get("scopes") or {})
    creatable_role_names = {
        str(role_name).strip()
        for role_name in (scopes.get("user.creatable_role_names") or [])
        if str(role_name).strip()
    }
    if target_role not in creatable_role_names:
        raise PermissionError("当前角色无权限创建该类型账号")


def _row_to_projection_dict(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _build_user_list_meta(
    bundle: Mapping[str, Any],
    *,
    page: int,
    page_size: int,
) -> Dict[str, Any]:
    capabilities = dict(bundle.get("capabilities") or {})
    scopes = dict(bundle.get("scopes") or {})

    creatable_role_names = [
        str(role_name).strip()
        for role_name in (scopes.get("user.creatable_role_names") or [])
        if str(role_name).strip()
    ]

    return {
        "capabilities": {
            "user_create": bool(capabilities.get("user.create")),
            "user_list_view": bool(capabilities.get("user.list.view")),
        },
        "scopes": {
            "user_creatable_role_names": creatable_role_names,
        },
        "pagination": {
            "page": page,
            "page_size": page_size,
        },
    }


def _build_user_row_meta(bundle: Mapping[str, Any], row: Mapping[str, Any]) -> Dict[str, Any]:
    capabilities = dict(bundle.get("capabilities") or {})
    scopes = dict(bundle.get("scopes") or {})

    current_user_id = int(bundle.get("current_user_id") or 0)
    primary_role = str(bundle.get("primary_role") or "").strip()
    row_user_id = int(row.get("id") or 0)
    row_parent_id = int(row.get("parent_id") or 0)
    row_role_name = str(row.get("role_name") or "").strip()

    exclude_self = bool(scopes.get("user.exclude_self"))
    creatable_role_names = {
        str(role_name).strip()
        for role_name in (scopes.get("user.creatable_role_names") or [])
        if str(role_name).strip()
    }

    can_update = bool(capabilities.get("user.update"))
    can_delete = bool(capabilities.get("user.delete"))

    if row_parent_id != current_user_id:
        can_update = False
        can_delete = False

    if exclude_self and row_user_id == current_user_id:
        can_update = False
        can_delete = False

    if primary_role in (ROLE_MANAGER, ROLE_SUPER_ADMIN):
        if row_role_name not in creatable_role_names:
            can_update = False
            can_delete = False

    return {
        "capabilities": {
            "user_update": can_update,
            "user_delete": can_delete,
        }
    }


def _attach_user_rows_meta(
    rows: Sequence[Mapping[str, Any]],
    bundle: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    output_rows: List[Dict[str, Any]] = []

    for row in rows:
        row_dict = _row_to_projection_dict(row)
        row_dict["meta"] = _build_user_row_meta(bundle, row_dict)
        output_rows.append(row_dict)

    return output_rows


def _ensure_manage_target_allowed(
    *,
    bundle: Mapping[str, Any],
    target_row: Mapping[str, Any],
    action: str,
) -> None:
    _require_bundle_capability(bundle, "user.manage.access", "无权限管理用户")

    if action == "update":
        _require_bundle_capability(bundle, "user.update", "无权限管理用户")
    elif action == "delete":
        _require_bundle_capability(bundle, "user.delete", "无权限管理用户")
    else:
        raise ValueError("invalid action")

    scopes = dict(bundle.get("scopes") or {})
    manage_scope = str(scopes.get("user.manage_scope") or "").strip()
    exclude_self = bool(scopes.get("user.exclude_self"))
    creatable_role_names = {
        str(role_name).strip()
        for role_name in (scopes.get("user.creatable_role_names") or [])
        if str(role_name).strip()
    }

    current_user_id = int(bundle.get("current_user_id") or 0)
    current_primary_role = str(bundle.get("primary_role") or "").strip()

    target_user_id = int(target_row.get("id") or 0)
    target_parent_id = int(target_row.get("parent_id") or 0)
    target_role_name = str(target_row.get("role_name") or "").strip()

    if exclude_self and target_user_id == current_user_id:
        raise PermissionError("不能管理自己")

    if manage_scope == "none":
        raise PermissionError("无权限管理用户")

    if manage_scope == "created_by_me" and target_parent_id != current_user_id:
        raise PermissionError("仅可管理自己创建的账号")

    if current_primary_role in (ROLE_MANAGER, ROLE_SUPER_ADMIN):
        if target_role_name not in creatable_role_names:
            if current_primary_role == ROLE_MANAGER:
                raise PermissionError("经理仅可管理业务/财务/市场账号")
            raise PermissionError("仅可管理自己创建的经理/业务/财务/市场账号")


async def _get_user_projection_row_by_id(
    *,
    db: AsyncSession,
    user_id: int,
) -> Optional[Dict[str, Any]]:
    stmt = _users_projection_stmt().where(User.id == int(user_id))
    row = (await db.execute(stmt)).mappings().first()
    if not row:
        return None
    return _row_to_projection_dict(row)


async def list_users(
    *,
    db: AsyncSession,
    current_user: User,
    current_role: str,
    keyword: Optional[str] = None,
    role: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    bundle = _compile_user_access_bundle(
        current_user=current_user,
        current_role=current_role,
    )
    pagination = _normalize_pagination(page, page_size)

    base_stmt = _users_projection_stmt()

    clauses = _build_user_list_scope_clauses(bundle)
    if clauses:
        base_stmt = base_stmt.where(*clauses)

    keyword_str = str(keyword or "").strip()
    if keyword_str:
        like = f"%{keyword_str}%"
        base_stmt = base_stmt.where(User.username.like(like))

    role_str = str(role or "").strip().lower()
    if role_str:
        base_stmt = base_stmt.where(func.lower(func.coalesce(base_stmt.selected_columns.role_name, "")) == role_str)

    count_subquery = base_stmt.order_by(None).subquery("user_list_count_sq")
    total_stmt = select(func.count()).select_from(count_subquery)
    total = int((await db.execute(total_stmt)).scalar_one() or 0)

    paged_stmt = (
        base_stmt
        .order_by(User.updated_at.desc(), User.id.desc())
        .offset(pagination["offset"])
        .limit(pagination["limit"])
    )

    raw_rows = (await db.execute(paged_stmt)).mappings().all()
    items = _attach_user_rows_meta(raw_rows, bundle)
    meta = _build_user_list_meta(
        bundle,
        page=pagination["page"],
        page_size=pagination["page_size"],
    )

    return {
        "total": total,
        "items": items,
        "meta": meta,
    }


async def get_user_projection_by_id(
    *,
    db: AsyncSession,
    user_id: int,
) -> Optional[Dict[str, Any]]:
    return await _get_user_projection_row_by_id(
        db=db,
        user_id=int(user_id),
    )


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
    bundle = _compile_user_access_bundle(
        current_user=current_user,
        current_role=current_role,
    )

    username_str = str(username or "").strip()
    if not username_str:
        raise ValueError("username is required")

    password_str = _validate_password(password)

    role_name_str = str(role_name or "").strip()
    _validate_role_name_for_create(
        bundle=bundle,
        target_role_name=role_name_str,
    )

    role_row = (
        (await db.execute(select(Role).where(Role.role_name == role_name_str)))
        .scalars()
        .first()
    )
    if not role_row:
        raise ValueError("角色不存在（请先初始化 seed）")

    team_name_str = (str(team_name).strip() if team_name else None) or None
    team_names_csv = _normalize_team_names_csv(team_names)
    _validate_team_names(team_name_str, team_names_csv)

    now = _now_bj_naive()

    parent_id: Optional[int] = None
    if str(current_role or "").strip() in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        parent_id = int(getattr(current_user, "id", 0) or 0)

    user = User(
        username=username_str,
        real_name=None,
        password_hash=hash_password(password_str),
        status=1,
        team_name=team_name_str,
        team_names=team_names_csv,
        parent_id=parent_id,
        created_at=now,
        updated_at=now,
    )
    db.add(user)

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        logger.exception("create_user flush failed: %s", exc)
        raise ValueError("用户名已存在") from exc

    db.add(UserRole(user_id=user.id, role_id=role_row.id))

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        logger.exception("create_user commit failed: %s", exc)
        raise ValueError("创建用户失败（请检查角色/唯一约束）") from exc

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
    bundle = _compile_user_access_bundle(
        current_user=current_user,
        current_role=current_role,
    )

    user_id_int = int(user_id)

    target_row = await _get_user_projection_row_by_id(
        db=db,
        user_id=user_id_int,
    )
    if not target_row:
        raise ValueError("用户不存在")

    _ensure_manage_target_allowed(
        bundle=bundle,
        target_row=target_row,
        action="update",
    )

    user = (
        await db.execute(
            select(User).where(User.id == user_id_int)
        )
    ).scalars().first()
    if not user:
        raise ValueError("用户不存在")

    changed = False

    if password is not None:
        password_str = _validate_password(password)
        user.password_hash = hash_password(password_str)
        changed = True

    team_name_str = (str(team_name).strip() if team_name is not None else None) or None
    team_names_csv = _normalize_team_names_csv(team_names)
    _validate_team_names(team_name_str, team_names_csv)

    if user.team_name != team_name_str:
        user.team_name = team_name_str
        changed = True

    if (user.team_names or "") != team_names_csv:
        user.team_names = team_names_csv
        changed = True

    if changed:
        user.updated_at = _now_bj_naive()
        await db.commit()

    return user


async def delete_user(
    *,
    db: AsyncSession,
    current_user: User,
    current_role: str,
    user_id: int,
) -> None:
    bundle = _compile_user_access_bundle(
        current_user=current_user,
        current_role=current_role,
    )

    user_id_int = int(user_id)

    target_row = await _get_user_projection_row_by_id(
        db=db,
        user_id=user_id_int,
    )
    if not target_row:
        raise ValueError("用户不存在")

    _ensure_manage_target_allowed(
        bundle=bundle,
        target_row=target_row,
        action="delete",
    )

    await db.execute(delete(UserRole).where(UserRole.user_id == user_id_int))
    await db.execute(delete(User).where(User.id == user_id_int))
    await db.commit()