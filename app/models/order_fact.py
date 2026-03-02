# app/models/order_fact.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Date, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import relationship

from app.core.db import Base


class OrderFact(Base):
    """
    订单级事实投影表（新增）

    说明：
    - 一对一绑定 order（主键=order_id）
    - 存放“固定规范字段集”，用于列表筛选/统一口径
    """

    __tablename__ = "order_fact_new"

    order_id = Column(Integer, ForeignKey("order_new.id", ondelete="CASCADE"), primary_key=True,
                      comment="订单ID（PK&FK -> order.id）")

    vin = Column(String(64), nullable=True, index=True, comment="车架号VIN（规范字段）")
    plate_no = Column(String(32), nullable=True, index=True, comment="车牌号（规范字段）")
    owner_name = Column(String(128), nullable=True, comment="车主/所有人（规范字段）")
    engine_no = Column(String(64), nullable=True, comment="发动机号（规范字段）")
    vehicle_model = Column(String(255), nullable=True, comment="品牌型号/车辆型号（规范字段）")
    first_register_date = Column(Date, nullable=True, comment="初登日期/注册日期（规范字段，DATE）")
    id_number = Column(String(32), nullable=True, index=True, comment="身份证号（规范字段）")

    updated_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"),
                        server_onupdate=text("CURRENT_TIMESTAMP"),
                        comment="更新时间（北京时间 naive DATETIME）")

    order = relationship("Order", lazy="selectin", doc="所属订单")

    __table_args__ = (
        Index("ix_order_fact_vin", "vin"),
        Index("ix_order_fact_plate_no", "plate_no"),
        Index("ix_order_fact_id_number", "id_number"),
    )
