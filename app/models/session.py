# app/models/session.py
# encoding: utf-8
"""
用户会话模型

说明：
- 使用 session_token 作为登录态标识
- expired: 0=有效, 1=手动过期
- 在线判断会结合 last_active_at + 全局 SESSION_TIMEOUT_SECONDS
"""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Index
from sqlalchemy.sql import func

from app.core.db import Base


class UserSession(Base):
    __tablename__ = "user_session"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 对齐 user.id 类型
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)

    # unique 本身会隐式创建索引，不再额外 index=True
    session_token = Column(String(255), unique=True, nullable=False)

    # ✅ 与其它表对齐：timezone=True
    last_active_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # 0 有效，1 手动过期
    expired = Column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("ix_user_session_user_expired", "user_id", "expired"),
    )

    def __repr__(self) -> str:
        return (
            f"<UserSession id={self.id} user_id={self.user_id} "
            f"expired={self.expired}>"
        )
