# app/models/user_role.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.db import Base


class UserRole(Base):
    """用户-角色关联表（多对多桥表）。"""

    __tablename__ = "user_role_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="用户ID（FK -> user.id）")
    role_id = Column(Integer, ForeignKey("role_new.id"), nullable=False, comment="角色ID（FK -> role.id）")

    user = relationship("User", lazy="selectin", doc="关联用户")
    role = relationship("Role", lazy="selectin", doc="关联角色")

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
        Index("ix_user_role_role", "role_id"),
        Index("ix_user_role_user", "user_id"),
    )
