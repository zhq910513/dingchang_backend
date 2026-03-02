# app/models/role.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.db import Base


class Role(Base):
    """角色表：定义系统角色（如 super_admin/manager/finance/market/sales）。"""

    __tablename__ = "role_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    role_name = Column(String(50), nullable=False, comment="角色名称（唯一标识）")
    description = Column(String(200), nullable=True, comment="角色描述（可空）")

    # 关系：角色下的用户（多对多）
    users = relationship(
        "User",
        secondary="user_role_new",
        back_populates="roles",
        lazy="selectin",
        doc="拥有该角色的用户列表（多对多）",
    )

    __table_args__ = (
        UniqueConstraint("role_name", name="role_name"),
    )
