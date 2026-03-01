# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, BigInteger, DateTime, UniqueConstraint, Index, text
from app.core.db import Base


class ImageFile(Base):
    __tablename__ = "image_file"

    id = Column(Integer, primary_key=True, autoincrement=True)

    sha256 = Column(String(64), nullable=True)
    md5 = Column(String(32), nullable=True)

    original_name = Column(String(255), nullable=True)
    content_type = Column(String(128), nullable=True)

    storage_key = Column(String(512), nullable=False)
    url = Column(String(512), nullable=False)

    etag = Column(String(128), nullable=True)
    size = Column(BigInteger, nullable=False)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint("storage_key", name="storage_key"),
        Index("ix_image_file_created_at", "created_at"),
        Index("ix_image_file_sha256", "sha256"),
        Index("ix_image_file_md5", "md5"),
    )
