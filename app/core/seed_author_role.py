# app/core/seed_author_role.py
# encoding: utf-8
"""
初始化种子数据（dev）
- 初始化角色（幂等）
- 初始化超级用户（幂等）

✅ 安全门禁（本轮修复）：
- 默认仅允许在 dev/local 环境创建“默认超管账号”
- 或者显式设置环境变量 ALLOW_DEV_SEED_SUPER_USER=1 才允许创建
- 支持 env 覆盖账号/密码/姓名，避免写死口令
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


def _truthy(v: str | None) -> bool:
    s = (v or "").strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _guess_env_name() -> str:
    # 多种常见 env 变量兜底
    for k in ("APP_ENV", "ENV", "FASTAPI_ENV", "PYTHON_ENV", "ENVIRONMENT"):
        v = (os.getenv(k) or "").strip().lower()
        if v:
            return v
    # 尝试从 settings 读取（如果项目有）
    try:
        from app.core.config import settings  # type: ignore

        for attr in ("ENV", "env", "environment", "APP_ENV"):
            v = str(getattr(settings, attr, "") or "").strip().lower()
            if v:
                return v
    except Exception:
        pass
    return ""


def _allow_seed_super_user() -> bool:
    # 显式开关优先
    if _truthy(os.getenv("ALLOW_DEV_SEED_SUPER_USER")):
        return True

    env_name = _guess_env_name()
    # 仅 dev/local 类环境允许默认超管种子
    return env_name in ("dev", "development", "local", "test")


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
    # ✅ 安全门禁：非允许环境直接跳过
    if not _allow_seed_super_user():
        return

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
