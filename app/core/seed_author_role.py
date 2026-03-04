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
"""

import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole

ROLE_SEEDS = [
    (1, "super_admin", "系统超级管理员"),
    (2, "manager", "主管角色"),
    (3, "sales", "业务账号"),
    (4, "finance", "财务账号"),
    (5, "market", "市场账号"),  # ✅ 新增：市场账号（具体可见/可写范围由接口与前端权限控制）
]


async def seed_roles(db: AsyncSession):
    # 以 id 为主，role_name 为辅做幂等更新
    for rid, rname, desc in ROLE_SEEDS:
        stmt = select(Role).where(Role.id == rid)
        res = await db.execute(stmt)
        role = res.scalar_one_or_none()

        if role:
            # 轻量更新，保持与“ON DUPLICATE KEY UPDATE”一致
            role.role_name = rname
            role.description = desc
        else:
            # 若有人手动改了 id，但已有同名 role，也复用
            stmt2 = select(Role).where(Role.role_name == rname)
            res2 = await db.execute(stmt2)
            same_name = res2.scalar_one_or_none()

            if same_name:
                same_name.description = desc
            else:
                db.add(Role(id=rid, role_name=rname, description=desc))


async def seed_super_user(db: AsyncSession):
    # ✅ 已取消环境门禁：只要调用到这里就允许创建默认超管（幂等）
    username = (os.getenv("SEED_SUPER_USERNAME") or "dingchang_admin").strip()
    raw_password = (os.getenv("SEED_SUPER_PASSWORD") or "dingchang_admin@123456").strip()
    real_name = (os.getenv("SEED_SUPER_REAL_NAME") or "dingchang").strip()

    if not username or not raw_password:
        # 防御：避免写入空账号
        return

    # 确保 super_admin 角色存在
    stmt_role = select(Role).where(Role.role_name == "super_admin")
    res_role = await db.execute(stmt_role)
    super_role = res_role.scalar_one_or_none()
    if not super_role:
        super_role = Role(id=1, role_name="super_admin", description="系统超级管理员")
        db.add(super_role)
        await db.flush()

    # 创建或获取用户
    stmt_user = select(User).where(User.username == username)
    res_user = await db.execute(stmt_user)
    user = res_user.scalar_one_or_none()

    if not user:
        user = User(
            username=username,
            password_hash=hash_password(raw_password),
            real_name=real_name or None,
            parent_id=None,
            status=1,
        )
        db.add(user)
        await db.flush()
    else:
        # 不强制重置密码，避免覆盖真实环境改过的密码
        # 但确保启用
        if user.status != 1:
            user.status = 1

    # 绑定 super_admin 角色（幂等）
    stmt_link = select(UserRole).where(
        UserRole.user_id == user.id,
        UserRole.role_id == super_role.id,
    )
    res_link = await db.execute(stmt_link)
    link = res_link.scalar_one_or_none()

    if not link:
        db.add(UserRole(user_id=user.id, role_id=super_role.id))


async def seed_initial_data(db: AsyncSession):
    await seed_roles(db)
    await seed_super_user(db)
