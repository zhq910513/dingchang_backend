# encoding: utf-8
"""
@author: The King
@project: dingchang_backend
@file: user_role.py
@time: 2025/12/8 22:38
"""

from sqlalchemy import Column, Integer, ForeignKey, UniqueConstraint, Index

from app.core.db import Base


class UserRole(Base):
    __tablename__ = "user_role"

    id = Column(Integer, primary_key=True, autoincrement=True)

    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    role_id = Column(Integer, ForeignKey("role.id"), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
        Index("ix_user_role_user", "user_id"),
        Index("ix_user_role_role", "role_id"),
    )

    def __repr__(self) -> str:
        return f"<UserRole id={self.id} user_id={self.user_id} role_id={self.role_id}>"
