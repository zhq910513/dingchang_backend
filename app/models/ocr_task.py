# app/models/ocr_task.py
# encoding: utf-8
"""
OCR 异步任务表：
- 由 API 创建任务记录
- 由 services/ocr_worker.py 更新进度/状态/错误信息

✅ 幂等兜底（DB 级）：
- active_scope_id：任务活跃时 = scope_id；任务结束后置 NULL
- UNIQUE(scope_type, active_scope_id)：保证同 scope_type 下同时只能有一个“活跃任务”
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, Index, UniqueConstraint, text
from sqlalchemy.sql import func

from app.core.db import Base


class OcrTask(Base):
    __tablename__ = "ocr_task"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ✅ 通用任务范围：
    # scope_type: 'order' / 'order_batch' / 'xxx'
    # scope_id: 对应实体的主键
    scope_type = Column(String(32), nullable=False, default="order", server_default=text("'order'"), index=True)
    scope_id = Column(Integer, nullable=False, index=True)

    # ✅ 活跃约束字段：活跃时等于 scope_id，结束后置 NULL
    active_scope_id = Column(Integer, nullable=True)

    # pending / processing / finished / failed / skipped / finished_with_errors
    status = Column(String(32), nullable=False, default="pending", server_default=text("'pending'"), index=True)

    # 0~100
    progress = Column(Integer, nullable=False, default=0, server_default=text("0"))

    # 失败原因/错误摘要
    error_message = Column(String(2048), nullable=True)

    # ✅ 与全局口径对齐：DB 存“北京时间 naive DATETIME”
    created_at = Column(DateTime(timezone=False), server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(
        DateTime(timezone=False),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )
    finished_at = Column(DateTime(timezone=False), nullable=True)

    __table_args__ = (
        Index("ix_ocr_task_scope", "scope_type", "scope_id"),
        Index("ix_ocr_task_status_created", "status", "created_at"),
        Index("ix_ocr_task_active_scope", "scope_type", "active_scope_id"),
        # ✅ DB 级幂等：同 scope_type 只能有一个 active_scope_id 非 NULL 的任务
        # 说明：多数数据库下 UNIQUE 对 NULL 允许重复，正好满足“仅活跃任务受限”的语义
        UniqueConstraint("scope_type", "active_scope_id", name="uq_ocr_task_active_scope"),
    )

    def __repr__(self) -> str:
        return f"<OcrTask id={self.id} scope={self.scope_type}:{self.scope_id} status={self.status}>"
