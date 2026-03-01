# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint, Index, text
from app.core.db import Base


class UserSession(Base):
    __tablename__ = "user_session"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    session_token = Column(String(255), nullable=False)

    last_active_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))

    expired = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("session_token", name="session_token"),
        Index("ix_user_session_user_expired", "user_id", "expired"),
    )
