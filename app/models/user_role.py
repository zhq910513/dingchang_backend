# app/models/user_role.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Index, Integer, UniqueConstraint

from app.core.db import Base


class UserRole(Base):
    """
    用户-角色关联表（多对多）

    说明：
    - user_id + role_id 唯一，避免重复绑定
    """

    __tablename__ = "user_role"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )
    user_id = Column(
        Integer,
        ForeignKey("user.id"),
        nullable=False,
        comment="用户ID（FK -> user.id）",
    )
    role_id = Column(
        Integer,
        ForeignKey("role.id"),
        nullable=False,
        comment="角色ID（FK -> role.id）",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
        Index("ix_user_role_role", "role_id"),
        Index("ix_user_role_user", "user_id"),
    )