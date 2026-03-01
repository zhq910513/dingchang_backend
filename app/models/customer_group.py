# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint, Computed, Index, text
from sqlalchemy.types import JSON
from app.core.db import Base


class CustomerGroup(Base):
    __tablename__ = "customer_group"

    id = Column(Integer, primary_key=True, autoincrement=True)

    team_name = Column(String(32), nullable=True)

    customer_code = Column(String(64), nullable=False)
    customer_name = Column(String(128), nullable=False)

    market = Column(String(128), nullable=True)
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
        UniqueConstraint("team_name", "customer_code", "customer_name", name="uq_customer_group_team_code_name"),
        # production uses KEY `created_by` (not ix_*), we keep exact name by Index
        Index("created_by", "created_by"),
    )
