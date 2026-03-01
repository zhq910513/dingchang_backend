# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, BigInteger, String, DateTime, UniqueConstraint, Index, text
from sqlalchemy.types import JSON
from app.core.db import Base


class OcrImageCache(Base):
    __tablename__ = "ocr_image_cache"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    storage_key = Column(String(512), nullable=False)
    sha256 = Column(String(64), nullable=True)

    api_type = Column(String(64), nullable=False)
    side = Column(String(32), nullable=False)
    provider = Column(String(32), nullable=False)

    result = Column(JSON, nullable=False)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint("storage_key", "api_type", "side", "provider", name="uq_ocr_image_cache_key_type_side_provider"),
        Index("ix_ocr_image_cache_storage_key", "storage_key"),
        Index("ix_ocr_image_cache_created_at", "created_at"),
        Index("ix_ocr_image_cache_sha256", "sha256"),
    )
