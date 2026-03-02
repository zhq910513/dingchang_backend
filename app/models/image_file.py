# app/models/image_file.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, UniqueConstraint, text

from app.core.db import Base


class ImageFile(Base):
    """
    图片文件元数据表（全局文件实体）

    说明：
    - storage_key 全局唯一（对象存储路径）
    - url 为可访问链接（通常为公网URL或CDN URL）
    - sha256/md5 用于去重/校验（可空）
    """

    __tablename__ = "image_file"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )

    sha256 = Column(
        String(64),
        nullable=True,
        comment="文件SHA256摘要（可空，用于去重/校验）",
    )
    md5 = Column(
        String(32),
        nullable=True,
        comment="文件MD5摘要（可空，用于去重/校验）",
    )

    original_name = Column(
        String(255),
        nullable=True,
        comment="原始文件名（上传时的名称，可空）",
    )
    content_type = Column(
        String(128),
        nullable=True,
        comment="文件MIME类型（如 image/jpeg，可空）",
    )

    storage_key = Column(
        String(512),
        nullable=False,
        comment="对象存储Key（BOS/S3路径，全局唯一）",
    )
    url = Column(
        String(512),
        nullable=False,
        comment="可访问URL（通常为公网URL或CDN URL）",
    )

    etag = Column(
        String(128),
        nullable=True,
        comment="对象存储ETag（可空，用于一致性校验）",
    )
    size = Column(
        BigInteger,
        nullable=False,
        comment="文件大小（字节）",
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
        UniqueConstraint("storage_key", name="storage_key"),
        Index("ix_image_file_created_at", "created_at"),
        Index("ix_image_file_sha256", "sha256"),
        Index("ix_image_file_md5", "md5"),
    )