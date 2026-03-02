# app/models/role.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, UniqueConstraint

from app.core.db import Base


class Role(Base):
    """
    角色表

    说明：
    - role_name：角色标识（唯一），如 ROLE_SUPER_ADMIN / ROLE_MANAGER / ROLE_FINANCE 等
    - description：角色描述（可空）
    """

    __tablename__ = "role"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )
    role_name = Column(
        String(50),
        nullable=False,
        comment="角色名称（唯一标识）",
    )
    description = Column(
        String(200),
        nullable=True,
        comment="角色描述（可空）",
    )

    __table_args__ = (
        UniqueConstraint("role_name", name="role_name"),
    )
