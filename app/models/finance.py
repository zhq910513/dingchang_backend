# app/models/finance.py
# encoding: utf-8
"""
@author: The King
@project: dingchang_backend
@file: finance.py
@time: 2025/12/8 22:39
"""

from sqlalchemy import Column, BigInteger, ForeignKey, Numeric, Integer, DateTime, String, Index, UniqueConstraint
from sqlalchemy.sql import func

from app.core.db import Base


class FinanceRecord(Base):
    __tablename__ = "finance_record"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # ✅ 一单一条财务记录（强约束，方便后续做金额/备注）
    # 唯一约束统一放到 __table_args__，避免重复定义
    order_id = Column(Integer, ForeignKey("order.id"), nullable=False, index=True)

    supplier_id = Column(Integer, nullable=True, index=True)

    # 0=未打款 1=已打款（数字）
    upstream_paid = Column(Integer, default=0, nullable=False)
    downstream_paid = Column(Integer, default=0, nullable=False)

    settle_amount = Column(Numeric(18, 2), nullable=True)
    actual_amount = Column(Numeric(18, 2), nullable=True)

    note = Column(String(255), nullable=True)

    # ✅ 与其它表对齐：timezone=True
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("order_id", name="uq_finance_record_order_id"),
        Index("ix_finance_record_order_supplier", "order_id", "supplier_id"),
    )