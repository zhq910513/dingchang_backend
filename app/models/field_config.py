# encoding: utf-8
"""
@author: The King
@project: dingchang_backend
@file: field_config.py
@time: 2025/12/8 22:39
"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.core.db import Base


class FieldConfig(Base):
    """
    字段定义：配置化字段的元数据（按 module 隔离）
    """
    __tablename__ = "field_config"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 模块维度（order/finance/customer/xxx）
    module = Column(String(50), nullable=False, index=True)

    # 字段唯一标识（在同一 module 内唯一）
    field_name = Column(String(100), nullable=False)

    # 展示信息
    label = Column(String(100), nullable=False)
    type = Column(String(50), nullable=False, default="text")  # text/number/date/select/amount/...

    # 通用控制（✅建议强制非空，避免 NULL 三值逻辑）
    required = Column(Boolean, nullable=False, default=False)
    visible = Column(Boolean, nullable=False, default=True)
    editable = Column(Boolean, nullable=False, default=True)
    sort = Column(Integer, nullable=False, default=0)

    # JSON 扩展
    options = Column(JSON, nullable=True)       # 下拉/枚举/远程选项定义等（建议用 {"items":[...]}）
    validators = Column(JSON, nullable=True)    # 统一校验规则
    extra = Column(JSON, nullable=True)         # 任意扩展字段

    # 简化的角色可见/可编辑（JSON list）
    # ✅保留 nullable=True：None 表示“不限制”，与当前接口逻辑一致
    view_roles = Column(JSON, nullable=True)    # ["admin", "finance"]
    edit_roles = Column(JSON, nullable=True)    # ["admin"]

    # ✅与 Order 模型对齐：timezone=True
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # 组映射关系
    group_links = relationship(
        "FieldGroupField",
        back_populates="field",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,  # 配合 FK ondelete
    )

    __table_args__ = (
        UniqueConstraint("module", "field_name", name="uq_field_config_module_field"),
        Index("ix_field_config_module_sort", "module", "sort"),
    )


class FieldGroup(Base):
    """
    字段组，比如“证件信息”、“基础信息”
    建议按 module 隔离
    """
    __tablename__ = "field_group"

    id = Column(Integer, primary_key=True, autoincrement=True)

    module = Column(String(50), nullable=False, index=True)

    group_key = Column(String(100), nullable=False)
    group_name = Column(String(100), nullable=False)

    order_index = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    field_links = relationship(
        "FieldGroupField",
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,  # 配合 FK ondelete
    )

    __table_args__ = (
        UniqueConstraint("module", "group_key", name="uq_field_group_module_key"),
        Index("ix_field_group_module_order", "module", "order_index"),
    )


class FieldGroupField(Base):
    """
    字段组与字段的多对多映射
    """
    __tablename__ = "field_group_field"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ✅建议加 ondelete=CASCADE，DB 层自动清理映射
    group_id = Column(Integer, ForeignKey("field_group.id", ondelete="CASCADE"), nullable=False, index=True)
    field_id = Column(Integer, ForeignKey("field_config.id", ondelete="CASCADE"), nullable=False, index=True)

    order_index = Column(Integer, nullable=False, default=0)

    group = relationship("FieldGroup", back_populates="field_links", lazy="selectin")
    field = relationship("FieldConfig", back_populates="group_links", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("group_id", "field_id", name="uq_field_group_field_pair"),
        # ✅常用查询：按 group_id 取 mapping 并按 order_index 排序
        Index("ix_field_group_field_group_order", "group_id", "order_index"),
    )


# ----------------------------
# 兼容旧引用（不改老代码也能先跑）
# ----------------------------
FieldDefinition = FieldConfig
FieldGroupMapping = FieldGroupField
