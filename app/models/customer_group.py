# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Computed, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.core.db import Base


class CustomerGroup(Base):
    """客户组表：客户字典数据（含软删除）。"""

    __tablename__ = "customer_group_new"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    team_name = Column(String(32), nullable=True, comment="团队名称（历史归属/标记，可空）")

    customer_code = Column(String(64), nullable=False, comment="客户代码（必填）")
    customer_name = Column(String(128), nullable=False, comment="客户名称（必填）")

    market = Column(String(128), nullable=True, comment="市场/渠道属性（可空）")
    region = Column(String(128), nullable=True, comment="归属地区/区域（可空）")

    created_by = Column(
        Integer,
        ForeignKey("user_new.id"),
        nullable=True,
        comment="创建人用户ID（FK -> user.id，可空）",
    )

    contacts = Column(JSON, nullable=False, comment="联系方式（JSON，结构由业务层定义，NOT NULL）")

    deleted_at = Column(DateTime(timezone=False), nullable=True, comment="删除时间（软删除标记，可空）")
    is_deleted = Column(
        Integer,
        Computed("(deleted_at is not null)", persisted=True),
        nullable=False,
        comment="是否已删除（generated column：deleted_at 非空为 1）",
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

    creator = relationship("User", lazy="selectin", doc="创建人用户（可空）")
    orders = relationship("Order", back_populates="customer_group", lazy="selectin", doc="关联订单列表")

    __table_args__ = (
        UniqueConstraint("customer_code", "customer_name", name="uq_customer_group_code_name"),
        Index("created_by", "created_by"),
        Index("ix_customer_group_customer_code", "customer_code"),
        Index("ix_customer_group_customer_name", "customer_name"),
        Index("ix_customer_group_is_deleted_team_name_code", "is_deleted", "team_name", "customer_code"),
        Index("ix_customer_group_is_deleted_team_name_name", "is_deleted", "team_name", "customer_name"),
    )
