# app/models/ocr_image_cache.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.types import JSON

from app.core.db import Base


class OcrImageCache(Base):
    """OCR 图片缓存表：以 storage_key 为粒度复用 OCR 返回，不依赖 image_file_id。"""

    __tablename__ = "ocr_image_cache_new"

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="主键ID（bigint）")

    storage_key = Column(String(512), nullable=False, comment="对象存储Key（BOS/S3路径）")
    sha256 = Column(String(64), nullable=True, comment="文件SHA256摘要（可空）")

    api_type = Column(String(64), nullable=False, comment="OCR接口类型")
    side = Column(String(32), nullable=False, comment="识别面（front/back/none等）")
    provider = Column(String(32), nullable=False, comment="OCR提供方（如 baidu）")

    result = Column(JSON, nullable=False, comment="OCR结果JSON（通常为原始返回）")

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"),
                        comment="创建时间（北京时间 naive DATETIME）")
    updated_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"),
                        server_onupdate=text("CURRENT_TIMESTAMP"),
                        comment="更新时间（北京时间 naive DATETIME）")

    __table_args__ = (
        UniqueConstraint("storage_key", "api_type", "side", "provider",
                         name="uq_ocr_image_cache_key_type_side_provider"),
        Index("ix_ocr_image_cache_storage_key", "storage_key"),
        Index("ix_ocr_image_cache_created_at", "created_at"),
        Index("ix_ocr_image_cache_sha256", "sha256"),
    )
