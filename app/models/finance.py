# app/models/finance.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, text

from app.core.db import Base


class FinanceRecord(Base):
    """
    财务记录表（订单维度，1:1）

    说明：
    - order_id 一对一唯一（生产 DDL 存在两个 UNIQUE KEY 名称：uq_finance_record_order_id / ix_finance_record_order_id）
    - upstream_paid / downstream_paid：上游/下游是否已支付（当前用 int 0/1 存储）
    - settle_amount / actual_amount：结算金额/实收金额（可空）
    - note：备注（可空）
    """

    __tablename__ = "finance_record"

    id = Column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="主键ID（bigint）",
    )

    order_id = Column(
        Integer,
        ForeignKey("order.id"),
        nullable=False,
        comment="订单ID（FK -> order.id，一对一财务记录）",
    )
    supplier_id = Column(
        Integer,
        nullable=True,
        comment="供应商ID（可空；当前未设置外键，口径由业务层定义）",
    )

    upstream_paid = Column(
        Integer,
        nullable=False,
        server_default=text("'0'"),
        comment="上游是否已支付（0/1，int 存储）",
    )
    downstream_paid = Column(
        Integer,
        nullable=False,
        server_default=text("'0'"),
        comment="下游是否已支付（0/1，int 存储）",
    )

    settle_amount = Column(
        Numeric(18, 2),
        nullable=True,
        comment="结算金额（元，可空）",
    )
    actual_amount = Column(
        Numeric(18, 2),
        nullable=True,
        comment="实收金额（元，可空）",
    )
    note = Column(
        String(255),
        nullable=True,
        comment="财务备注（可空）",
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

    __table_args__ = (
        # 生产 DDL：UNIQUE KEY uq_finance_record_order_id (order_id)
        UniqueConstraint("order_id", name="uq_finance_record_order_id"),
        # 生产 DDL：另一个 UNIQUE KEY 名称 ix_finance_record_order_id (order_id)
        # 注意：逻辑重复但名称不同，为保持与生产 DDL 一致而保留
        UniqueConstraint("order_id", name="ix_finance_record_order_id"),
        Index("ix_finance_record_supplier_id", "supplier_id"),
        Index("ix_finance_record_order_supplier", "order_id", "supplier_id"),
    )
