# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint, Index, text
from app.core.db import Base


class OrderInfo(Base):
    __tablename__ = "order_info"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("order.id", ondelete="CASCADE"), nullable=False)

    insurance_expire_date = Column(Date, nullable=True)
    owner_phone = Column(String(32), nullable=True)

    commercial_amount = Column(Numeric(18, 2), nullable=False, server_default=text("'0.00'"))
    compulsory_amount = Column(Numeric(18, 2), nullable=False, server_default=text("'0.00'"))
    vehicle_tax_amount = Column(Numeric(18, 2), nullable=False, server_default=text("'0.00'"))
    non_vehicle_amount = Column(Numeric(18, 2), nullable=False, server_default=text("'0.00'"))
    premium_total = Column(Numeric(18, 2), nullable=False, server_default=text("'0.00'"))

    channel_commercial_point = Column(Numeric(10, 4), nullable=False, server_default=text("'0.0000'"))
    channel_commercial_supplement_point = Column(Numeric(10, 4), nullable=False, server_default=text("'0.0000'"))
    channel_compulsory_point = Column(Numeric(10, 4), nullable=False, server_default=text("'0.0000'"))
    channel_vehicle_tax_point = Column(Numeric(10, 4), nullable=False, server_default=text("'0.0000'"))
    channel_non_vehicle_point = Column(Numeric(10, 4), nullable=False, server_default=text("'0.0000'"))
    channel_reward = Column(Numeric(18, 2), nullable=False, server_default=text("'0.00'"))
    channel_total = Column(Numeric(18, 2), nullable=False, server_default=text("'0.00'"))

    customer_commercial_point = Column(Numeric(10, 4), nullable=False, server_default=text("'0.0000'"))
    customer_commercial_supplement_point = Column(Numeric(10, 4), nullable=False, server_default=text("'0.0000'"))
    customer_compulsory_point = Column(Numeric(10, 4), nullable=False, server_default=text("'0.0000'"))
    customer_vehicle_tax_point = Column(Numeric(10, 4), nullable=False, server_default=text("'0.0000'"))
    customer_non_vehicle_point = Column(Numeric(10, 4), nullable=False, server_default=text("'0.0000'"))
    customer_reward = Column(Numeric(18, 2), nullable=False, server_default=text("'0.00'"))
    customer_total = Column(Numeric(18, 2), nullable=False, server_default=text("'0.00'"))

    profit = Column(Numeric(18, 2), nullable=False, server_default=text("'0.00'"))

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    remark = Column(String(1024), nullable=True)

    __table_args__ = (
        UniqueConstraint("order_id", name="order_id"),
        Index("ix_order_info_id", "id"),
    )
