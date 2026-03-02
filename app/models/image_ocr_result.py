# app/models/image_ocr_result.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.types import JSON

from app.core.db import Base


class ImageOcrResult(Base):
    """
    图片级 OCR 结果缓存表（以 image_file 为粒度）

    说明：
    - 同一图片（image_file_id）+ provider + api_type + side 唯一
    - raw_result 存 OCR 原始返回 JSON（追溯/复用）
    - usage_count/last_used_at 用于统计与缓存淘汰策略
    """

    __tablename__ = "image_ocr_result"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )

    image_file_id = Column(
        Integer,
        ForeignKey("image_file.id", ondelete="CASCADE"),
        nullable=False,
        comment="图片文件ID（FK -> image_file.id，删除文件级联删除缓存）",
    )

    provider = Column(
        String(32),
        nullable=False,
        comment="OCR提供方（如 baidu）",
    )
    api_type = Column(
        String(64),
        nullable=False,
        comment="OCR接口类型（如 idcard/vehicle_license/vehicle_certificate）",
    )
    side = Column(
        String(32),
        nullable=False,
        comment="识别面（front/back/none等）",
    )

    raw_result = Column(
        JSON,
        nullable=False,
        comment="OCR原始返回JSON（用于追溯/复用）",
    )

    usage_count = Column(
        Integer,
        nullable=False,
        comment="命中次数/使用次数",
    )
    last_used_at = Column(
        DateTime(timezone=False),
        nullable=True,
        comment="最后一次使用时间（北京时间 naive DATETIME，可空）",
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
        UniqueConstraint("image_file_id", "provider", "api_type", "side", name="ux_ocr_cache_key"),
        Index("ix_ocr_cache_file", "image_file_id"),
    )