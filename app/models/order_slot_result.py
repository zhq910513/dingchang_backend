# app/models/order_slot_result.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import relationship

from app.core.db import Base


class OrderSlotResult(Base):
    """
    订单-卡槽 OCR 事实表（新增）

    说明：
    - 一条记录对应：某订单的某卡槽（slot_key）的识别结果
    - raw_json：OCR原始返回（追溯/回放）
    - recognized_json：抽取后的结构化字段（业务用/展示用）
    - 默认采用：同一订单同一卡槽唯一（最新覆盖）
    """

    __tablename__ = "order_slot_result_new"

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="主键ID（bigint）")

    order_id = Column(Integer, ForeignKey("order_new.id", ondelete="CASCADE"), nullable=False, index=True,
                      comment="订单ID（FK -> order.id）")
    slot_key = Column(String(64), nullable=False, index=True, comment="卡槽Key（slot_key）")

    order_image_id = Column(Integer, ForeignKey("order_image_new.id", ondelete="SET NULL"), nullable=True, index=True,
                            comment="订单图片ID（FK -> order_image.id，可空）")
    image_file_id = Column(Integer, ForeignKey("image_file_new.id", ondelete="SET NULL"), nullable=True, index=True,
                           comment="图片文件ID（FK -> image_file.id，可空）")

    provider = Column(String(32), nullable=False, server_default=text("'baidu'"), comment="OCR提供方（默认 baidu）")
    api_type = Column(String(64), nullable=False, comment="OCR接口类型（如 idcard/vehicle_license/...）")
    side = Column(String(32), nullable=False, server_default=text("''"), comment="识别面（front/back/none）")

    status = Column(String(32), nullable=False, server_default=text("'ok'"), comment="识别状态（ok/failed）")
    error_message = Column(String(512), nullable=True, comment="错误信息（可空）")

    raw_json = Column(JSON, nullable=False, comment="OCR原始返回JSON（追溯/回放）")
    recognized_json = Column(JSON, nullable=False, comment="抽取后的结构化字段JSON（业务用/展示用）")

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"),
                        comment="创建时间（北京时间 naive DATETIME）")
    updated_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"),
                        server_onupdate=text("CURRENT_TIMESTAMP"),
                        comment="更新时间（北京时间 naive DATETIME）")

    # 关系
    order = relationship("Order", lazy="selectin", doc="所属订单")
    order_image = relationship("OrderImage", lazy="selectin", doc="关联订单图片（可空）")
    image_file = relationship("ImageFile", lazy="selectin", doc="关联图片文件（可空）")

    __table_args__ = (
        UniqueConstraint("order_id", "slot_key", name="uq_order_slot_result_order_slot"),
        Index("ix_order_slot_result_order_slot", "order_id", "slot_key"),
    )
