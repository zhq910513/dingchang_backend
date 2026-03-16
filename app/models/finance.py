# encoding: utf-8
from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, text
from sqlalchemy.orm import relationship

from app.core.db import Base


class FinanceRecord(Base):
    """财务记录表（订单维度，1:1）。"""

    __tablename__ = "finance_record_new"

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="主键ID（bigint）")

    order_id = Column(
        Integer,
        ForeignKey("order_new.id"),
        nullable=False,
        comment="订单ID（FK -> order.id，一对一）",
    )
    supplier_id = Column(Integer, nullable=True, comment="供应商ID（可空；当前无外键）")

    upstream_paid = Column(
        Integer,
        nullable=False,
        server_default=text("'0'"),
        comment="上游是否已支付（0/1，int）",
    )
    downstream_paid = Column(
        Integer,
        nullable=False,
        server_default=text("'0'"),
        comment="下游是否已支付（0/1，int）",
    )

    settle_amount = Column(Numeric(18, 2), nullable=True, comment="结算金额（元，可空）")
    actual_amount = Column(Numeric(18, 2), nullable=True, comment="实收金额（元，可空）")
    note = Column(String(255), nullable=True, comment="财务备注（可空）")

    created_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="创建时间",
    )
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        comment="更新时间",
    )

    order = relationship("Order", back_populates="finance_record", lazy="selectin", doc="所属订单")

    __table_args__ = (
        Index("ix_finance_record_order_id", "order_id", unique=True),
        Index("ix_finance_record_supplier_id", "supplier_id"),
        Index("ix_finance_record_order_supplier", "order_id", "supplier_id"),
    )
