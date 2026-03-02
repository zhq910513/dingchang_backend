# app/models/user.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import relationship

from app.core.db import Base


class User(Base):
    """用户表：账号、团队信息与层级关系。"""

    __tablename__ = "user_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")

    username = Column(String(50), nullable=False, comment="用户名（唯一）")
    real_name = Column(String(50), nullable=True, comment="真实姓名（可空）")

    team_name = Column(String(32), nullable=True, comment="团队名称（单团队字段，历史口径，可空）")
    team_names = Column(String(255), nullable=True, comment="团队名称列表（多团队字段，常为逗号分隔，可空）")

    password_hash = Column(String(255), nullable=False, comment="密码哈希（不可明文存储）")

    parent_id = Column(Integer, ForeignKey("user_new.id"), nullable=True, comment="上级用户ID（自关联，可空）")

    status = Column(Integer, nullable=False, comment="账号状态（int 枚举；通常 1=启用）")

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"),
                        comment="创建时间（北京时间 naive DATETIME）")
    updated_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"),
                        server_onupdate=text("CURRENT_TIMESTAMP"),
                        comment="更新时间（北京时间 naive DATETIME）")

    # 关系：自关联（上级/下级）
    parent = relationship("User", remote_side=[id], lazy="selectin", doc="上级用户（自关联）")
    children = relationship("User", lazy="selectin", doc="下级用户列表（自关联）")

    # 关系：用户角色（多对多）
    roles = relationship(
        "Role",
        secondary="user_role_new",
        back_populates="users",
        lazy="selectin",
        doc="用户拥有的角色列表（多对多）",
    )

    # 关系：会话
    sessions = relationship("UserSession", back_populates="user", lazy="selectin", doc="用户会话列表")

    # 关系：订单（两个外键到 user）
    created_orders = relationship(
        "Order",
        foreign_keys="Order.created_by",
        back_populates="creator",
        lazy="selectin",
        doc="我创建的订单列表",
    )
    sales_orders = relationship(
        "Order",
        foreign_keys="Order.salesperson_id",
        back_populates="salesperson",
        lazy="selectin",
        doc="我作为业务员的订单列表",
    )

    __table_args__ = (
        UniqueConstraint("username", name="ix_user_username"),
        Index("ix_user_parent_id", "parent_id"),
        Index("ix_user_status", "status"),
        Index("ix_user_parent_status", "parent_id", "status"),
        Index("ix_user_team_name", "team_name"),
        Index("ix_user_team_status", "team_name", "status"),
    )
