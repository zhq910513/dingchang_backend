# app/models/ocr_image_cache.py
# encoding: utf-8
"""
OCR 结果缓存表：
- 方案A：优先用 storage_key 做幂等/缓存（sha256 可后算再补）
"""

from __future__ import annotations

from sqlalchemy import Column, BigInteger, String, DateTime, UniqueConstraint, Index, text
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from app.core.db import Base


class OcrImageCache(Base):
    __tablename__ = "ocr_image_cache"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # 方案A：永久定位（直传 BOS 时立即可用）
    storage_key = Column(String(512), nullable=False, default="", server_default=text("''"))

    # sha256（可选，后算补齐）
    sha256 = Column(String(64), nullable=True)

    api_type = Column(String(64), nullable=False)

    # side：front/back/main/sub 等；没有就用空字符串
    side = Column(String(32), nullable=False, default="", server_default=text("''"))

    provider = Column(String(32), nullable=False, default="baidu", server_default=text("'baidu'"))

    result = Column(JSON, nullable=False)

    # ✅ 全局时间口径：北京时间 naive DATETIME
    created_at = Column(DateTime(timezone=False), server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(
        DateTime(timezone=False),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "storage_key", "api_type", "side", "provider",
            name="uq_ocr_image_cache_key_type_side_provider",
        ),
        Index("ix_ocr_image_cache_storage_key", "storage_key"),
        Index("ix_ocr_image_cache_sha256", "sha256"),
        Index("ix_ocr_image_cache_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<OcrImageCache key={self.storage_key[:16]}... "
            f"api_type={self.api_type} side={self.side} provider={self.provider}>"
        )
