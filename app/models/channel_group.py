# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint, Computed, Index, text
from sqlalchemy.types import JSON
from app.core.db import Base


class ChannelGroup(Base):
    __tablename__ = "channel_group"

    id = Column(Integer, primary_key=True, autoincrement=True)

    team_name = Column(String(32), nullable=True)

    channel_code = Column(String(64), nullable=False)
    channel_name = Column(String(128), nullable=False)

    region = Column(String(128), nullable=True)

    created_by = Column(Integer, ForeignKey("user.id"), nullable=True)

    contacts = Column(JSON, nullable=False)

    deleted_at = Column(DateTime(timezone=False), nullable=True)
    is_deleted = Column(Integer, Computed("(deleted_at is not null)", persisted=True), nullable=False)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint("team_name", "channel_code", "channel_name", name="uq_channel_group_team_code_name"),
        Index("ix_channel_group_deleted_at", "deleted_at"),
        Index("ix_channel_group_created_by", "created_by"),
        Index("ix_channel_group_team_name", "team_name"),
        Index("ix_channel_group_channel_code", "channel_code"),
        Index("ix_channel_group_is_deleted", "is_deleted"),
        Index("ix_channel_group_id", "id"),
        Index("ix_channel_group_channel_name", "channel_name"),
    )
