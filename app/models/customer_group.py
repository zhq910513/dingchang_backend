# app/models/customer_group.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, JSON, String, ForeignKey, UniqueConstraint, Computed
from sqlalchemy.sql import func

from app.core.db import Base


class CustomerGroup(Base):
    __tablename__ = "customer_group"

    id = Column(Integer, primary_key=True, index=True)

    # ✅ 团队（共享数据模式下仅用于历史归属/标记，不再作为隔离或唯一性边界）
    team_name = Column(String(32), nullable=True, index=True)

    # 客户代码
    customer_code = Column(String(64), nullable=False, index=True)

    # 客户名称（必填）
    customer_name = Column(String(128), nullable=False, index=True)

    # 市场（选填）
    market = Column(String(128), nullable=True)

    # 归属地
    region = Column(String(128), nullable=True, default="")

    # 归属人（用于权限范围：谁创建的）
    created_by = Column(Integer, ForeignKey("user.id"), nullable=True, index=True)

    # 联系方式：支持多个号码/多个方式（统一用 JSON list 存）
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
        # ✅ 共享唯一：同 code + name 全局只能存在一条（无论是否软删除）
        UniqueConstraint("customer_code", "customer_name", name="uq_customer_group_code_name"),
    )
