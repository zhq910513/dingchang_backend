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

    # ✅ 团队（单团队）：用于业务/财务/市场等“单团队账号”的数据隔离；super_admin 可为空
    team_name = Column(String(32), nullable=True, index=True)

    # ✅ 团队集合（多团队）：用于“经理账号多选团队”落库
    # 约定：逗号分隔字符串，例如 "赣州团队,南昌团队"
    team_names = Column(String(255), nullable=True)

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
        Index("ix_user_team_status", "team_name", "status"),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"
