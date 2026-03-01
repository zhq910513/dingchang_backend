# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint, Index, text
from sqlalchemy.types import JSON
from app.core.db import Base


class Order(Base):
    __tablename__ = "order"

    id = Column(Integer, primary_key=True, autoincrement=True)

    module = Column(String(32), nullable=False, server_default=text("'order'"))

    created_by = Column(Integer, ForeignKey("user.id"), nullable=False)
    salesperson_id = Column(Integer, ForeignKey("user.id"), nullable=False)

    customer_group_id = Column(Integer, ForeignKey("customer_group.id"), nullable=True)
    channel_group_id = Column(Integer, ForeignKey("channel_group.id"), nullable=True)

    is_finished = Column(Boolean, nullable=False, server_default=text("0"))
    is_rebate = Column(Boolean, nullable=False, server_default=text("0"))
    is_paid = Column(Boolean, nullable=False, server_default=text("0"))

    status = Column(Integer, nullable=False, server_default=text("0"))
    audit_status = Column(Integer, nullable=False, server_default=text("0"))

    dynamic_data = Column(JSON, nullable=False)
    ocr_raw_json = Column(JSON, nullable=False)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        Index("ix_order_salesperson_id", "salesperson_id"),
        Index("ix_order_id", "id"),
        Index("ix_order_channel_group_id", "channel_group_id"),
        Index("ix_order_created_by", "created_by"),
        Index("ix_order_module", "module"),
        Index("ix_order_customer_group_id", "customer_group_id"),
        Index("ix_order_is_finished", "is_finished"),
    )


class OrderImage(Base):
    __tablename__ = "order_image"

    id = Column(Integer, primary_key=True, autoincrement=True)

    order_id = Column(Integer, ForeignKey("order.id", ondelete="CASCADE"), nullable=False)

    slot_key = Column(String(64), nullable=False)
    storage_key = Column(String(512), nullable=False)

    image_url = Column(String(512), nullable=False, server_default=text("''"))

    image_file_id = Column(Integer, ForeignKey("image_file.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint("order_id", "slot_key", "storage_key", name="uq_order_image_order_slot_storage"),
        Index("ix_order_image_storage_key", "storage_key"),
        Index("ix_order_image_slot_key", "slot_key"),
        Index("ix_order_image_order_slot", "order_id", "slot_key"),
        Index("ix_order_image_id", "id"),
        Index("ix_order_image_order_id", "order_id"),
        Index("ix_order_image_image_file_id", "image_file_id"),
        Index("ix_order_image_updated_at", "updated_at"),
    )
