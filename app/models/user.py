# app/models/user.py
# encoding: utf-8
"""
User 模型定义
"""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Index
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.core.db import Base


class User(Base):
    __tablename__ = "user"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 登录用账号（唯一）
    username = Column(String(50), unique=True, nullable=False, index=True)

    # 角色姓名 / 真实姓名（用于页面展示，比如订单里的“业务员”）
    real_name = Column(String(50), nullable=True)

    # 密码哈希
    password_hash = Column(String(255), nullable=False)

    # 创建者（父账号），超级管理员的 parent_id 为空
    parent_id = Column(Integer, ForeignKey("user.id"), nullable=True, index=True)

    # 0 禁用，1 启用
    status = Column(Integer, default=1, nullable=False, index=True)

    # ✅ 与其它表对齐：timezone=True
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    parent = relationship("User", remote_side=[id], backref="children")

    __table_args__ = (
        Index("ix_user_parent_status", "parent_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"
