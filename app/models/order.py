# app/models/order.py
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
from sqlalchemy.types import JSON

from app.core.db import Base


class Order(Base):
    """
    订单表（MySQL 表名为 `order`，属于保留字，ORM 层需保持与 DDL 一致）

    说明：
    - dynamic_data：订单级 JSON 数据（当前用于承载规范字段；后续将逐步收口为固定字段集）
    - ocr_raw_json：OCR 原始返回快照（按 slot_key 存储 raw，便于追溯/回放）
    """

    __tablename__ = "order"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )

    module = Column(
        String(32),
        nullable=False,
        server_default=text("'order'"),
        comment="模块标识（默认 'order'，用于多模块复用同表场景）",
    )

    created_by = Column(
        Integer,
        ForeignKey("user.id"),
        nullable=False,
        comment="创建人用户ID（FK -> user.id）",
    )
    salesperson_id = Column(
        Integer,
        ForeignKey("user.id"),
        nullable=False,
        comment="业务员用户ID（FK -> user.id，用于权限范围与归属）",
    )

    customer_group_id = Column(
        Integer,
        ForeignKey("customer_group.id"),
        nullable=True,
        comment="客户组ID（FK -> customer_group.id，可空）",
    )
    channel_group_id = Column(
        Integer,
        ForeignKey("channel_group.id"),
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
        comment="订单状态（int 枚举，具体含义由业务层定义）",
    )
    audit_status = Column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="审核状态（int 枚举，具体含义由业务层定义）",
    )

    dynamic_data = Column(
        JSON,
        nullable=False,
        comment="订单级动态数据（JSON，当前承载规范字段；后续计划收口为固定字段集）",
    )
    ocr_raw_json = Column(
        JSON,
        nullable=False,
        comment="OCR原始返回快照（JSON，按 slot_key 存 raw/error，用于追溯/回放）",
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
        Index("ix_order_salesperson_id", "salesperson_id"),
        Index("ix_order_id", "id"),
        Index("ix_order_channel_group_id", "channel_group_id"),
        Index("ix_order_created_by", "created_by"),
        Index("ix_order_module", "module"),
        Index("ix_order_customer_group_id", "customer_group_id"),
        Index("ix_order_is_finished", "is_finished"),
    )


class OrderImage(Base):
    """
    订单图片表：一行代表订单某卡槽的一张图片

    说明：
    - slot_key：卡槽 key（与 slot_field_config 对齐）
    - storage_key：对象存储 key（BOS/S3 路径）
    - image_file_id：指向 image_file（可空；为空时仍可用 storage_key 生成 URL）
    - image_url：展示 URL（默认空串；可回填签名/公网 URL，业务层输出应以 image_file.url/签名为准）
    """

    __tablename__ = "order_image"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )

    order_id = Column(
        Integer,
        ForeignKey("order.id", ondelete="CASCADE"),
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
        ForeignKey("image_file.id", ondelete="SET NULL"),
        nullable=True,
        comment="图片文件ID（FK -> image_file.id；删除 image_file 时置空）",
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
        UniqueConstraint("order_id", "slot_key", "storage_key", name="uq_order_image_order_slot_storage"),
        Index("ix_order_image_storage_key", "storage_key"),
        Index("ix_order_image_slot_key", "slot_key"),
        Index("ix_order_image_order_slot", "order_id", "slot_key"),
        Index("ix_order_image_id", "id"),
        Index("ix_order_image_order_id", "order_id"),
        Index("ix_order_image_image_file_id", "image_file_id"),
        Index("ix_order_image_updated_at", "updated_at"),
    )
