# app/models/finance.py
# encoding: utf-8
"""
@author: The King
@project: dingchang_backend
@file: finance.py
@time: 2025/12/8 22:39
"""

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.sql import func

from app.core.db import Base


class FinanceRecord(Base):
    __tablename__ = "finance_record"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # ✅ 一单一条财务记录（强约束，方便后续做金额/备注）
    order_id = Column(Integer, ForeignKey("order.id"), nullable=False, index=True)

    supplier_id = Column(Integer, nullable=True, index=True)

    # 0=未打款 1=已打款（数字）
    upstream_paid = Column(Integer, nullable=False, default=0)
    downstream_paid = Column(Integer, nullable=False, default=0)

    settle_amount = Column(Numeric(18, 2), nullable=True)
    actual_amount = Column(Numeric(18, 2), nullable=True)

    note = Column(String(255), nullable=True)

    # ✅ 全局口径对齐：DB 存“北京时间 naive DATETIME”（timezone=False）
    created_at = Column(DateTime(timezone=False), server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(
        DateTime(timezone=False),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("order_id", name="uq_finance_record_order_id"),
        Index("ix_finance_record_order_supplier", "order_id", "supplier_id"),
    )
