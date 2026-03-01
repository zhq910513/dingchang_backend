# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint, Index, text
from sqlalchemy.types import JSON
from app.core.db import Base


class ImageOcrResult(Base):
    __tablename__ = "image_ocr_result"

    id = Column(Integer, primary_key=True, autoincrement=True)

    image_file_id = Column(Integer, ForeignKey("image_file.id", ondelete="CASCADE"), nullable=False)

    provider = Column(String(32), nullable=False)
    api_type = Column(String(64), nullable=False)
    side = Column(String(32), nullable=False)

    raw_result = Column(JSON, nullable=False)

    usage_count = Column(Integer, nullable=False)
    last_used_at = Column(DateTime(timezone=False), nullable=True)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint("image_file_id", "provider", "api_type", "side", name="ux_ocr_cache_key"),
        Index("ix_ocr_cache_file", "image_file_id"),
    )
