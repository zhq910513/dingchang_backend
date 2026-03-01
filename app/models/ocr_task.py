# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, UniqueConstraint, Index, text
from app.core.db import Base


class OcrTask(Base):
    __tablename__ = "ocr_task"

    id = Column(Integer, primary_key=True, autoincrement=True)

    scope_type = Column(String(32), nullable=False)
    scope_id = Column(Integer, nullable=False)

    active_scope_id = Column(Integer, nullable=True)

    status = Column(String(32), nullable=False)
    progress = Column(Integer, nullable=False)

    error_message = Column(String(2048), nullable=True)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )
    finished_at = Column(DateTime(timezone=False), nullable=True)

    __table_args__ = (
        UniqueConstraint("scope_type", "active_scope_id", name="uq_ocr_task_active_scope"),
        Index("ix_ocr_task_scope_id", "scope_id"),
        Index("ix_ocr_task_status", "status"),
        Index("ix_ocr_task_active_scope", "scope_type", "active_scope_id"),
        Index("ix_ocr_task_scope_type", "scope_type"),
        Index("ix_ocr_task_status_created", "status", "created_at"),
        Index("ix_ocr_task_scope", "scope_type", "scope_id"),
    )
