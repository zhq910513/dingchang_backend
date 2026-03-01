# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Index, UniqueConstraint, text
from app.core.db import Base


class User(Base):
    __tablename__ = "user"

    id = Column(Integer, primary_key=True, autoincrement=True)

    username = Column(String(50), nullable=False)
    real_name = Column(String(50), nullable=True)

    team_name = Column(String(32), nullable=True)
    team_names = Column(String(255), nullable=True)

    password_hash = Column(String(255), nullable=False)

    parent_id = Column(Integer, ForeignKey("user.id"), nullable=True)

    status = Column(Integer, nullable=False)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint("username", name="ix_user_username"),
        Index("ix_user_parent_id", "parent_id"),
        Index("ix_user_status", "status"),
        Index("ix_user_parent_status", "parent_id", "status"),
        Index("ix_user_team_name", "team_name"),
        Index("ix_user_team_status", "team_name", "status"),
    )
