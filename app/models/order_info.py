# app/models/order_info.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric, String, text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.db import Base


class OrderInfo(Base):
    __tablename__ = "order_info"

    id = Column(Integer, primary_key=True, index=True)

    # ✅ 一单一条（强约束）
    # - 不要再同时：index=True + unique=True + UniqueConstraint + Index
    # - 否则 metadata 内会出现同名 index，create_all 直接 Duplicate key name
    order_id = Column(Integer, ForeignKey("order.id", ondelete="CASCADE"), nullable=False, unique=True)

    # ✅ 反向关系
    order = relationship("Order", back_populates="order_info", lazy="selectin")

    # Row 1
    insurance_expire_date = Column(Date, nullable=True)
    owner_phone = Column(String(32), nullable=True)

    # ✅ 金额类：DB 级默认值，避免旧数据/直写SQL出现 NULL
    commercial_amount = Column(Numeric(18, 2), nullable=False, server_default=text("0"))
    compulsory_amount = Column(Numeric(18, 2), nullable=False, server_default=text("0"))
    vehicle_tax_amount = Column(Numeric(18, 2), nullable=False, server_default=text("0"))
    non_vehicle_amount = Column(Numeric(18, 2), nullable=False, server_default=text("0"))

    premium_total = Column(Numeric(18, 2), nullable=False, server_default=text("0"))

    # Row 2 渠道点位（允许负数）
    channel_commercial_point = Column(Numeric(10, 4), nullable=False, server_default=text("0"))

    # ✅ 新增：渠道-商业后补点位（允许负数）
    channel_commercial_supplement_point = Column(Numeric(10, 4), nullable=False, server_default=text("0"))

    channel_compulsory_point = Column(Numeric(10, 4), nullable=False, server_default=text("0"))
    channel_vehicle_tax_point = Column(Numeric(10, 4), nullable=False, server_default=text("0"))
    channel_non_vehicle_point = Column(Numeric(10, 4), nullable=False, server_default=text("0"))
    channel_reward = Column(Numeric(18, 2), nullable=False, server_default=text("0"))

    channel_total = Column(Numeric(18, 2), nullable=False, server_default=text("0"))

    # Row 3 客户/产品点位（允许负数）
    customer_commercial_point = Column(Numeric(10, 4), nullable=False, server_default=text("0"))

    # ✅ 新增：客户-商业后补点位（允许负数）
    customer_commercial_supplement_point = Column(Numeric(10, 4), nullable=False, server_default=text("0"))

    customer_compulsory_point = Column(Numeric(10, 4), nullable=False, server_default=text("0"))
    customer_vehicle_tax_point = Column(Numeric(10, 4), nullable=False, server_default=text("0"))
    customer_non_vehicle_point = Column(Numeric(10, 4), nullable=False, server_default=text("0"))
    customer_reward = Column(Numeric(18, 2), nullable=False, server_default=text("0"))

    customer_total = Column(Numeric(18, 2), nullable=False, server_default=text("0"))

    # Row 4 利润
    profit = Column(Numeric(18, 2), nullable=False, server_default=text("0"))

    # ✅ 方案 A 对齐：DB 存“北京时间 naive DATETIME”
    # - 不使用 timezone=True，避免驱动/方言把 tzinfo 搞出来导致展示 +8 的坑
    created_at = Column(DateTime(timezone=False), server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(
        DateTime(timezone=False),
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
        nullable=False,
    )
