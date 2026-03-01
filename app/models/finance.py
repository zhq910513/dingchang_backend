# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, BigInteger, Integer, DateTime, ForeignKey, Numeric, String, UniqueConstraint, Index, text
from app.core.db import Base


class FinanceRecord(Base):
    __tablename__ = "finance_record"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    order_id = Column(Integer, ForeignKey("order.id"), nullable=False)
    supplier_id = Column(Integer, nullable=True)

    upstream_paid = Column(Integer, nullable=False, server_default=text("'0'"))
    downstream_paid = Column(Integer, nullable=False, server_default=text("'0'"))

    settle_amount = Column(Numeric(18, 2), nullable=True)
    actual_amount = Column(Numeric(18, 2), nullable=True)
    note = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint("order_id", name="uq_finance_record_order_id"),
        # production has another UNIQUE KEY with name ix_finance_record_order_id
        UniqueConstraint("order_id", name="ix_finance_record_order_id"),
        Index("ix_finance_record_supplier_id", "supplier_id"),
        Index("ix_finance_record_order_supplier", "order_id", "supplier_id"),
    )
