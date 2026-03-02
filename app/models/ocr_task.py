# app/models/ocr_task.py
# app/models/ocr_task.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, Integer, String, UniqueConstraint, text

from app.core.db import Base


class OcrTask(Base):
    """
    OCR 任务表

    说明：
    - scope_type/scope_id：任务作用域（如 order/ai_assistant 等）
    - active_scope_id：同一作用域并发控制键（用于“同一订单只跑一个活跃任务”之类场景）
    - status/progress：任务状态与进度
    - error_message：错误信息（可空）
    """

    __tablename__ = "ocr_task"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )

    scope_type = Column(
        String(32),
        nullable=False,
        comment="作用域类型（如 order）",
    )
    scope_id = Column(
        Integer,
        nullable=False,
        comment="作用域ID（如订单ID）",
    )

    active_scope_id = Column(
        Integer,
        nullable=True,
        comment="活跃作用域ID（用于并发控制，可空）",
    )

    status = Column(
        String(32),
        nullable=False,
        comment="任务状态（pending/processing/finished/failed/skipped 等）",
    )
    progress = Column(
        Integer,
        nullable=False,
        comment="任务进度（0~100）",
    )

    error_message = Column(
        String(2048),
        nullable=True,
        comment="错误信息（可空）",
    )

    created_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="创建时间（北京时间 naive DATETIME）",
    )
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="更新时间（北京时间 naive DATETIME）",
    )
    finished_at = Column(
        DateTime(timezone=False),
        nullable=True,
        comment="完成时间（北京时间 naive DATETIME，可空）",
    )

    __table_args__ = (
        UniqueConstraint("scope_type", "active_scope_id", name="uq_ocr_task_active_scope"),
        Index("ix_ocr_task_scope_id", "scope_id"),
        Index("ix_ocr_task_status", "status"),
        Index("ix_ocr_task_active_scope", "scope_type", "active_scope_id"),
        Index("ix_ocr_task_scope_type", "scope_type"),
        Index("ix_ocr_task_status_created", "status", "created_at"),
        Index("ix_ocr_task_scope", "scope_type", "scope_id"),
    )