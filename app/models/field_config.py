# app/models/field_config.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.types import JSON

from app.core.db import Base


class FieldConfig(Base):
    """
    字段配置表（配置化表单/展示用）

    说明：
    - module + field_name 唯一
    - required/visible/editable 为 int（通常 0/1）
    - options/validators/extra/view_roles/edit_roles 为 JSON（可空）
    """

    __tablename__ = "field_config"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )
    module = Column(
        String(50),
        nullable=False,
        comment="模块标识（如 order/finance/ai_assistant 等）",
    )
    field_name = Column(
        String(100),
        nullable=False,
        comment="字段名（程序字段key，用于绑定数据源）",
    )

    label = Column(
        String(100),
        nullable=False,
        comment="字段展示名称（中文标签）",
    )
    type = Column(
        String(50),
        nullable=False,
        comment="字段类型（如 text/number/date/select 等，具体枚举由前端/业务约定）",
    )

    required = Column(
        Integer,
        nullable=False,
        comment="是否必填（0/1，int 存储）",
    )
    visible = Column(
        Integer,
        nullable=False,
        comment="是否可见（0/1，int 存储）",
    )
    editable = Column(
        Integer,
        nullable=False,
        comment="是否可编辑（0/1，int 存储）",
    )
    sort = Column(
        Integer,
        nullable=False,
        comment="字段排序（升序）",
    )

    options = Column(
        JSON,
        nullable=True,
        comment="可选项配置（JSON，可空；用于 select/radio/checkbox 等）",
    )
    validators = Column(
        JSON,
        nullable=True,
        comment="校验规则配置（JSON，可空）",
    )
    extra = Column(
        JSON,
        nullable=True,
        comment="扩展配置（JSON，可空）",
    )
    view_roles = Column(
        JSON,
        nullable=True,
        comment="可查看角色（JSON，可空；用于字段级权限/展示控制的配置预留）",
    )
    edit_roles = Column(
        JSON,
        nullable=True,
        comment="可编辑角色（JSON，可空；用于字段级权限/编辑控制的配置预留）",
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
        UniqueConstraint("module", "field_name", name="uq_field_config_module_field"),
        Index("ix_field_config_module", "module"),
        Index("ix_field_config_module_sort", "module", "sort"),
    )


class FieldGroup(Base):
    """
    字段分组表（配置化分组，用于详情页面分区展示）

    说明：
    - module + group_key 唯一
    """

    __tablename__ = "field_group"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )
    module = Column(
        String(50),
        nullable=False,
        comment="模块标识（与 FieldConfig.module 对齐）",
    )

    group_key = Column(
        String(100),
        nullable=False,
        comment="分组Key（程序key）",
    )
    group_name = Column(
        String(100),
        nullable=False,
        comment="分组名称（中文展示名）",
    )

    order_index = Column(
        Integer,
        nullable=False,
        comment="分组排序（升序）",
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
        UniqueConstraint("module", "group_key", name="uq_field_group_module_key"),
        Index("ix_field_group_module", "module"),
        Index("ix_field_group_module_order", "module", "order_index"),
    )


class FieldGroupField(Base):
    """
    字段分组-字段关系表（多对多）

    说明：
    - group_id + field_id 唯一
    - order_index：字段在组内排序
    """

    __tablename__ = "field_group_field"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )
    group_id = Column(
        Integer,
        ForeignKey("field_group.id", ondelete="CASCADE"),
        nullable=False,
        comment="分组ID（FK -> field_group.id，删除分组级联删除关系）",
    )
    field_id = Column(
        Integer,
        ForeignKey("field_config.id", ondelete="CASCADE"),
        nullable=False,
        comment="字段配置ID（FK -> field_config.id，删除字段级联删除关系）",
    )
    order_index = Column(
        Integer,
        nullable=False,
        comment="字段在分组内排序（升序）",
    )

    __table_args__ = (
        UniqueConstraint("group_id", "field_id", name="uq_field_group_field_pair"),
        Index("ix_field_group_field_group_id", "group_id"),
        Index("ix_field_group_field_group_order", "group_id", "order_index"),
        Index("ix_field_group_field_field_id", "field_id"),
    )