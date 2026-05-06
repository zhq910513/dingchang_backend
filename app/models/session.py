# app/models/session.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import relationship

from app.core.db import Base


class UserSession(Base):
    """用户会话表：用于 X-Session-Token 鉴权与超时控制。"""

    __tablename__ = "user_session_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    user_id = Column(Integer, ForeignKey("user_new.id"), nullable=False, comment="用户ID（FK -> user.id）")

    # ✅ 注意：字段名是 session_token（不是 token）
    session_token = Column(String(255), nullable=False, comment="会话Token（唯一，用于鉴权）")

    last_active_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="最后活跃时间（北京时间 naive DATETIME）",
    )
    created_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="创建时间（北京时间 naive DATETIME）",
    )

    # 0=未过期 1=过期（由业务控制）
    expired = Column(Integer, nullable=False, comment="是否过期（0=未过期，1=过期）")

    user = relationship("User", back_populates="sessions", lazy="selectin", doc="所属用户")

    __table_args__ = (
        UniqueConstraint("session_token", name="session_token"),
        Index("ix_user_session_user_expired", "user_id", "expired"),
        Index("ix_user_session_expired_user_last_active", "expired", "user_id", "last_active_at"),
    )
