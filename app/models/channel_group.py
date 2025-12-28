# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, JSON, String, ForeignKey, UniqueConstraint, Computed
from sqlalchemy.sql import func

from app.core.db import Base


class ChannelGroup(Base):
    __tablename__ = "channel_group"

    id = Column(Integer, primary_key=True, index=True)

    # 渠道代码
    channel_code = Column(String(64), nullable=False, index=True)

    # 渠道名称（必填）
    channel_name = Column(String(128), nullable=False, index=True)

    # 归属地
    region = Column(String(128), nullable=True, default="")

    # 归属人（用于权限范围：谁创建的）
    created_by = Column(Integer, ForeignKey("user.id"), nullable=True, index=True)

    # 联系方式（JSON list）
    contacts = Column(JSON, nullable=False, default=list)

    # 软删除：NULL=有效；非NULL=已删除
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # 计算列：是否已删除（0/1）
    is_deleted = Column(Integer, Computed("deleted_at IS NOT NULL", persisted=True), nullable=False, index=True)

    # ✅ 计算列：用于“软删除后允许反复重建”的唯一约束
    # active: 0；deleted: NULL（MySQL UNIQUE 允许多个 NULL）
    active_guard = Column(Integer, Computed("CASE WHEN deleted_at IS NULL THEN 0 ELSE NULL END", persisted=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )

    __table_args__ = (
        # ✅ 唯一：同 code+name 在“未删除(active_guard=0)”状态下只能一条；删除后允许无限次重建
        UniqueConstraint("channel_code", "channel_name", "active_guard", name="uq_channel_group_code_name_active"),
    )
