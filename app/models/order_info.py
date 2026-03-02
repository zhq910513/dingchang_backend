# app/models/order_info.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, text

from app.core.db import Base


class OrderInfo(Base):
    """
    订单信息扩展表（1:1）

    说明：
    - 与 order 表通过 order_id 一对一关联（Unique）
    - 存放保费结构、点位、渠道/客户合计、利润等财务字段
    - remark：订单备注（真实来源：order_info.remark）
    """

    __tablename__ = "order_info"

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
        comment="订单ID（FK -> order.id，删除订单级联删除）",
    )

    insurance_expire_date = Column(
        Date,
        nullable=True,
        comment="保险到期日（DATE，可空）",
    )
    owner_phone = Column(
        String(32),
        nullable=True,
        comment="车主联系电话（可空）",
    )

    commercial_amount = Column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("'0.00'"),
        comment="商业险保费金额（元）",
    )
    compulsory_amount = Column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("'0.00'"),
        comment="交强险保费金额（元）",
    )
    vehicle_tax_amount = Column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("'0.00'"),
        comment="车船税金额（元）",
    )
    non_vehicle_amount = Column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("'0.00'"),
        comment="非车险金额（元）",
    )
    premium_total = Column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("'0.00'"),
        comment="总保费（元）= 商业险 + 交强险 + 车船税 + 非车险",
    )

    channel_commercial_point = Column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("'0.0000'"),
        comment="渠道商业险点位（‰或%口径由业务层定义，保留4位小数）",
    )
    channel_commercial_supplement_point = Column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("'0.0000'"),
        comment="渠道商业险补点点位（保留4位小数）",
    )
    channel_compulsory_point = Column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("'0.0000'"),
        comment="渠道交强险点位（保留4位小数）",
    )
    channel_vehicle_tax_point = Column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("'0.0000'"),
        comment="渠道车船税点位（保留4位小数）",
    )
    channel_non_vehicle_point = Column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("'0.0000'"),
        comment="渠道非车险点位（保留4位小数）",
    )
    channel_reward = Column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("'0.00'"),
        comment="渠道返利金额（元）",
    )
    channel_total = Column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("'0.00'"),
        comment="渠道合计金额（元）",
    )

    customer_commercial_point = Column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("'0.0000'"),
        comment="客户商业险点位（保留4位小数）",
    )
    customer_commercial_supplement_point = Column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("'0.0000'"),
        comment="客户商业险补点点位（保留4位小数）",
    )
    customer_compulsory_point = Column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("'0.0000'"),
        comment="客户交强险点位（保留4位小数）",
    )
    customer_vehicle_tax_point = Column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("'0.0000'"),
        comment="客户车船税点位（保留4位小数）",
    )
    customer_non_vehicle_point = Column(
        Numeric(10, 4),
        nullable=False,
        server_default=text("'0.0000'"),
        comment="客户非车险点位（保留4位小数）",
    )
    customer_reward = Column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("'0.00'"),
        comment="客户返利金额（元）",
    )
    customer_total = Column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("'0.00'"),
        comment="客户合计金额（元）",
    )

    profit = Column(
        Numeric(18, 2),
        nullable=False,
        server_default=text("'0.00'"),
        comment="利润（元）",
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

    remark = Column(
        String(1024),
        nullable=True,
        comment="订单备注（用于订单/财务列表展示；导出不包含）",
    )

    __table_args__ = (
        UniqueConstraint("order_id", name="order_id"),
        Index("ix_order_info_id", "id"),
    )
