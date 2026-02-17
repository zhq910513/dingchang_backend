# app/models/image_file.py
# encoding: utf-8
"""
图片文件表（方案B1：MD5 固定 key + 直传 BOS）
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Index, text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.db import Base


class ImageFile(Base):
    __tablename__ = "image_file"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 旧字段保留但不再使用（避免历史迁移爆炸）
    # ✅ 改为非唯一：避免“同内容但扩展名/Content-Type 误判导致 storage_key 不同”的冲突
    sha256 = Column(String(64), nullable=True)

    # ✅ B1：文件内容 MD5（用于去重/查询；不做唯一，避免误判扩展名造成冲突）
    md5 = Column(String(32), nullable=True)

    original_name = Column(String(255), nullable=True)
    content_type = Column(String(128), nullable=True)

    # ✅ BOS object key（唯一；unique 本身带索引）
    storage_key = Column(String(512), nullable=False, unique=True)

    # 直链（私有桶不可直接访问；展示时后端会签名覆盖）
    # ✅ DB 级默认值，避免直写 SQL / 老数据出现 NULL
    url = Column(String(512), nullable=False, server_default=text("''"))

    etag = Column(String(128), nullable=True)

    # ✅ DB 级默认值：避免直写 SQL / 迁移脚本出现 NULL
    size = Column(BigInteger, nullable=False, server_default=text("0"))

    # ✅ 方案 A 对齐：DB 存“北京时间 naive DATETIME”
    # - 不使用 timezone=True，避免驱动/方言把 tzinfo 搞出来导致展示 +8 的坑
    created_at = Column(DateTime(timezone=False), server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(
        DateTime(timezone=False),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )

    order_images = relationship(
        "OrderImage",
        back_populates="image_file",
        passive_deletes=True,
        lazy="selectin",
    )

    ocr_results = relationship(
        "ImageOcrResult",
        back_populates="image_file",
        passive_deletes=True,
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_image_file_md5", "md5"),
        Index("ix_image_file_sha256", "sha256"),
        Index("ix_image_file_created_at", "created_at"),
    )
