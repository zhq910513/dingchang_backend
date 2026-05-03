# encoding: utf-8
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.core.db import Base


class Order(Base):
    """订单表：订单主记录（含 dynamic_data 与 ocr_raw_json）。"""

    __tablename__ = "order_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")

    module = Column(
        String(32),
        nullable=False,
        server_default=text("'order'"),
        comment="模块标识（默认 'order'）",
    )

    created_by = Column(
        Integer,
        ForeignKey("user_new.id"),
        nullable=False,
        comment="创建人用户ID（FK -> user.id）",
    )
    salesperson_id = Column(
        Integer,
        ForeignKey("user_new.id"),
        nullable=False,
        comment="业务员用户ID（FK -> user.id）",
    )

    customer_group_id = Column(
        Integer,
        ForeignKey("customer_group_new.id"),
        nullable=True,
        comment="客户组ID（FK -> customer_group.id，可空）",
    )
    channel_group_id = Column(
        Integer,
        ForeignKey("channel_group_new.id"),
        nullable=True,
        comment="渠道组ID（FK -> channel_group.id，可空）",
    )

    is_finished = Column(
        Boolean,
        nullable=False,
        server_default=text("0"),
        comment="是否完成（0/1）",
    )
    is_rebate = Column(
        Boolean,
        nullable=False,
        server_default=text("0"),
        comment="是否返利（0/1）",
    )
    is_paid = Column(
        Boolean,
        nullable=False,
        server_default=text("0"),
        comment="是否已支付（0/1）",
    )

    status = Column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="订单状态（int 枚举）",
    )
    audit_status = Column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="审核状态（int 枚举）",
    )

    dynamic_data = Column(
        JSON,
        nullable=False,
        comment="订单级动态数据（JSON，后续建议收口为固定字段集）",
    )
    ocr_raw_json = Column(
        JSON,
        nullable=False,
        comment="OCR原始返回快照（JSON，按 slot_key 存 raw/error）",
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

    # 关系：用户
    creator = relationship(
        "User",
        foreign_keys=[created_by],
        back_populates="created_orders",
        lazy="selectin",
        doc="创建人用户",
    )
    salesperson = relationship(
        "User",
        foreign_keys=[salesperson_id],
        back_populates="sales_orders",
        lazy="selectin",
        doc="业务员用户",
    )

    # 关系：字典表
    customer_group = relationship(
        "CustomerGroup",
        back_populates="orders",
        lazy="selectin",
        doc="客户组",
    )
    channel_group = relationship(
        "ChannelGroup",
        back_populates="orders",
        lazy="selectin",
        doc="渠道组",
    )

    # 关系：订单扩展
    order_info = relationship(
        "OrderInfo",
        back_populates="order",
        uselist=False,
        lazy="selectin",
        doc="订单信息扩展（1:1）",
    )
    finance_record = relationship(
        "FinanceRecord",
        back_populates="order",
        uselist=False,
        lazy="selectin",
        doc="财务记录（1:1）",
    )

    # 关系：图片
    images = relationship(
        "OrderImage",
        back_populates="order",
        lazy="selectin",
        doc="订单图片列表",
    )

    __table_args__ = (
        Index("ix_order_salesperson_id", "salesperson_id"),
        Index("ix_order_salesperson_id_id", "salesperson_id", "id"),
        Index("ix_order_channel_group_id", "channel_group_id"),
        Index("ix_order_created_by", "created_by"),
        Index("ix_order_customer_group_id", "customer_group_id"),
        Index("ix_order_list_module_id", "module", "id"),
        Index("ix_order_list_is_finished_id", "is_finished", "id"),
        Index("ix_order_list_is_paid_id", "is_paid", "id"),
        Index("ix_order_list_is_rebate_id", "is_rebate", "id"),
        Index("ix_order_list_created_at_id", "created_at", "id"),
        Index("ix_order_finance_finished_created_id", "is_finished", "created_at", "id"),
        Index("ix_order_finance_finished_paid_id", "is_finished", "is_paid", "id"),
        Index("ix_order_finance_finished_rebate_id", "is_finished", "is_rebate", "id"),
        Index("ix_order_list_finished_salesperson_id", "is_finished", "salesperson_id", "id"),
        Index("ix_order_list_salesperson_finished_id", "salesperson_id", "is_finished", "id"),
        Index("ix_order_list_salesperson_created_at_id", "salesperson_id", "created_at", "id"),
    )


class OrderImage(Base):
    """订单图片表：一行代表订单某卡槽的一张图片。"""

    __tablename__ = "order_image_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")

    order_id = Column(
        Integer,
        ForeignKey("order_new.id", ondelete="CASCADE"),
        nullable=False,
        comment="订单ID（FK -> order.id，删除订单级联删除图片记录）",
    )

    slot_key = Column(
        String(64),
        nullable=False,
        comment="卡槽Key（slot_key，与 slot_field_config 对齐）",
    )
    storage_key = Column(
        String(512),
        nullable=False,
        comment="对象存储Key（BOS/S3路径）",
    )

    image_url = Column(
        String(512),
        nullable=False,
        server_default=text("''"),
        comment="展示URL（默认空串；可回填签名/公网URL，勿作为唯一真源）",
    )

    image_file_id = Column(
        Integer,
        ForeignKey("image_file_new.id", ondelete="SET NULL"),
        nullable=True,
        comment="图片文件ID（FK -> image_file.id，可空）",
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

    # 关系
    order = relationship(
        "Order",
        back_populates="images",
        lazy="selectin",
        doc="所属订单",
    )
    image_file = relationship(
        "ImageFile",
        back_populates="order_images",
        lazy="selectin",
        doc="关联图片文件（可空）",
    )

    __table_args__ = (
        UniqueConstraint("order_id", "slot_key", "storage_key", name="uq_order_image_order_slot_storage"),
        Index("ix_order_image_storage_key", "storage_key"),
        Index("ix_order_image_slot_key", "slot_key"),
        Index("ix_order_image_order_slot", "order_id", "slot_key"),
        Index("ix_order_image_order_id", "order_id"),
        Index("ix_order_image_image_file_id", "image_file_id"),
        Index("ix_order_image_updated_at", "updated_at"),
    )
