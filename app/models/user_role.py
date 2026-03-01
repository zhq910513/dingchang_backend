# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, ForeignKey, UniqueConstraint, Index
from app.core.db import Base


class UserRole(Base):
    __tablename__ = "user_role"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    role_id = Column(Integer, ForeignKey("role.id"), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
        Index("ix_user_role_role", "role_id"),
        Index("ix_user_role_user", "user_id"),
    )
