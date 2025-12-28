# app/models/image_ocr_result.py
# encoding: utf-8
"""
图片 OCR 结果表：
- 一张图片（ImageFile）可以有多条 OCR 结果
- 按 provider/api_type/side 维度区分
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.db import Base


class ImageOcrResult(Base):
    __tablename__ = "image_ocr_result"

    # ✅ PK 自带索引；明确自增
    id = Column(Integer, primary_key=True, autoincrement=True)

    image_file_id = Column(
        Integer,
        ForeignKey("image_file.id", ondelete="CASCADE"),
        nullable=False,
    )

    # provider/api_type/side：都保持非 NULL，避免三值逻辑坑
    provider = Column(String(32), nullable=False, default="baidu")
    api_type = Column(String(64), nullable=False)

    # side 统一：不要 NULL（用 "" 表示无 side）
    side = Column(String(32), nullable=False, default="")

    raw_result = Column(JSON, nullable=False)

    usage_count = Column(Integer, nullable=False, default=0)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        # ✅ OCR 缓存唯一键：同一张图 + 同 provider + 同 api_type + 同 side 只能有一条
        Index("ux_ocr_cache_key", "image_file_id", "provider", "api_type", "side", unique=True),
        # ✅ 常用查询：按 image_file_id 拉取
        Index("ix_ocr_cache_file", "image_file_id"),
    )

    image_file = relationship("ImageFile", back_populates="ocr_results", lazy="selectin")
