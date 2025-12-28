# app/models/order.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Index, UniqueConstraint
from sqlalchemy.orm import relationship, synonym
from sqlalchemy.sql import func

from app.core.db import Base


class Order(Base):
    __tablename__ = "order"

    id = Column(Integer, primary_key=True, index=True)

    module = Column(String(32), nullable=False, default="order", index=True)

    created_by = Column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    salesperson_id = Column(Integer, ForeignKey("user.id"), nullable=False, index=True)

    customer_group_id = Column(Integer, ForeignKey("customer_group.id"), nullable=True, index=True)
    channel_group_id = Column(Integer, ForeignKey("channel_group.id"), nullable=True, index=True)

    is_finished = Column(Boolean, nullable=False, default=False, index=True)
    is_rebate = Column(Boolean, nullable=False, default=False)
    is_paid = Column(Boolean, nullable=False, default=False)

    status = Column(Integer, nullable=False, default=0)
    audit_status = Column(Integer, nullable=False, default=0)

    dynamic_data = Column(JSON, nullable=False, default=dict)
    ocr_raw_json = Column(JSON, nullable=False, default=dict)

    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp(), nullable=False)

    images = relationship(
        "OrderImage",
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # ✅ 新增：订单信息块（一对一）
    order_info = relationship(
        "OrderInfo",
        back_populates="order",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    creator = relationship("User", foreign_keys=[created_by], lazy="joined")
    salesperson = relationship("User", foreign_keys=[salesperson_id], lazy="joined")

    customer_group = relationship("CustomerGroup", foreign_keys=[customer_group_id], lazy="joined")
    channel_group = relationship("ChannelGroup", foreign_keys=[channel_group_id], lazy="joined")


class OrderImage(Base):
    __tablename__ = "order_image"

    id = Column(Integer, primary_key=True, index=True)

    order_id = Column(Integer, ForeignKey("order.id", ondelete="CASCADE"), nullable=False, index=True)

    slot_key = Column(String(64), nullable=False, index=True)
    slot = synonym("slot_key")

    storage_key = Column(String(512), nullable=False, index=True, default="")
    image_url = Column(String(512), nullable=False, default="")

    image_file_id = Column(
        Integer,
        ForeignKey("image_file.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False)

    order = relationship("Order", back_populates="images", lazy="selectin")
    image_file = relationship("ImageFile", back_populates="order_images", lazy="selectin")

    __table_args__ = (
        # ✅ DB 级去重：同订单、同slot、同storage_key 不允许重复（防并发/重试插重）
        UniqueConstraint("order_id", "slot_key", "storage_key", name="uq_order_image_order_slot_storage"),
        Index("ix_order_image_order_slot", "order_id", "slot_key"),
    )
