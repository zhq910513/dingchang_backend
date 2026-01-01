# app/models/channel_group.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, JSON, String, ForeignKey, UniqueConstraint, Computed
from sqlalchemy.sql import func

from app.core.db import Base


class ChannelGroup(Base):
    __tablename__ = "channel_group"

    id = Column(Integer, primary_key=True, index=True)

    # ✅ 团队（用于隔离范围）
    team_name = Column(String(32), nullable=True, index=True)

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

    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )

    __table_args__ = (
        # ✅ 团队内唯一：同 team + code + name 只能存在一条（无论是否软删除）
        UniqueConstraint("team_name", "channel_code", "channel_name", name="uq_channel_group_team_code_name"),
    )
