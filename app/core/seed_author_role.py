# app/core/seed_author_role.py
# encoding: utf-8
"""
初始化种子数据
- 初始化角色（幂等）
- 初始化超级用户（幂等）

⚠️ 注意：
- 已取消“仅 dev/local 才允许创建默认超管”的门禁。
- 只要 AUTO_SEED_AUTH=1 并调用 seed_initial_data，就会尝试创建默认超管账号（幂等）。
- 默认不会覆盖已存在用户的密码（避免覆盖真实环境改过的密码）。
- ✅ 默认超管必须挂载三个团队（TEAM_NAMES 全量挂载，幂等补齐）。
"""

import os
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import TEAM_NAMES
from app.core.security import hash_password
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole

ROLE_SEEDS = [
    (1, "super_admin", "系统超级管理员"),
    (2, "manager", "主管角色"),
    (3, "sales", "业务账号"),
    (4, "finance", "财务账号"),
    (5, "market", "市场账号"),
]


def _team_names_seed() -> List[str]:
    teams = list(TEAM_NAMES)
    if len(teams) != 3:
        # 防御：避免 TEAM_NAMES 被误改
        raise ValueError("TEAM_NAMES 必须包含且仅包含 3 个团队")
    return teams


def _csv(parts: List[str]) -> str:
    # 稳定排序 + 去重，保证多次 seed 输出稳定
    return ",".join(sorted(set([p.strip() for p in parts if p and p.strip()])))


async def seed_roles(db: AsyncSession):
    # 以 id 为主，role_name 为辅做幂等更新
    for rid, rname, desc in ROLE_SEEDS:
        res = await db.execute(select(Role).where(Role.id == rid))
        role = res.scalar_one_or_none()

        if role:
            role.role_name = rname
            role.description = desc
            continue

        res2 = await db.execute(select(Role).where(Role.role_name == rname))
        same_name = res2.scalar_one_or_none()
        if same_name:
            same_name.description = desc
        else:
            db.add(Role(id=rid, role_name=rname, description=desc))


async def seed_super_user(db: AsyncSession):
    username = (os.getenv("SEED_SUPER_USERNAME") or "dingchang_admin").strip()
    raw_password = (os.getenv("SEED_SUPER_PASSWORD") or "dingchang_admin@123456").strip()
    real_name = (os.getenv("SEED_SUPER_REAL_NAME") or "dingchang").strip()

    if not username or not raw_password:
        return

    seed_teams = _team_names_seed()
    seed_team_names_csv = _csv(seed_teams)
    seed_team_name = seed_teams[0]  # 默认团队：赣州团队（constants 里的第一个）

    # 确保 super_admin 角色存在
    res_role = await db.execute(select(Role).where(Role.role_name == "super_admin"))
    super_role = res_role.scalar_one_or_none()
    if not super_role:
        super_role = Role(id=1, role_name="super_admin", description="系统超级管理员")
        db.add(super_role)
        await db.flush()

    # 创建或获取用户
    res_user = await db.execute(select(User).where(User.username == username))
    user = res_user.scalar_one_or_none()

    if not user:
        user = User(
            username=username,
            password_hash=hash_password(raw_password),
            real_name=real_name or None,
            parent_id=None,
            status=1,
            team_name=seed_team_name,
            team_names=seed_team_names_csv,
        )
        db.add(user)
        await db.flush()
    else:
        # 不强制重置密码，避免覆盖真实环境改过的密码
        if user.status != 1:
            user.status = 1

        # ✅ 幂等补齐团队挂载：确保至少包含这 3 个团队
        existing_csv = (getattr(user, "team_names", "") or "").strip()
        existing = [x.strip() for x in existing_csv.split(",") if x and x.strip()]
        merged_csv = _csv(existing + seed_teams)
        user.team_names = merged_csv

        # 默认团队为空或不在集合里则修正
        current_team = (getattr(user, "team_name", None) or "").strip() or None
        merged_set = set([x for x in merged_csv.split(",") if x])
        if not current_team or current_team not in merged_set:
            user.team_name = seed_team_name

    # 绑定 super_admin 角色（幂等）
    res_link = await db.execute(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_id == super_role.id,
        )
    )
    link = res_link.scalar_one_or_none()
    if not link:
        db.add(UserRole(user_id=user.id, role_id=super_role.id))


async def seed_initial_data(db: AsyncSession):
    await seed_roles(db)
    await seed_super_user(db)
