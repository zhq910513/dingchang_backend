# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, UniqueConstraint, Index, ForeignKey, text
from sqlalchemy.types import JSON
from app.core.db import Base


class FieldConfig(Base):
    __tablename__ = "field_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    module = Column(String(50), nullable=False)
    field_name = Column(String(100), nullable=False)

    label = Column(String(100), nullable=False)
    type = Column(String(50), nullable=False)

    required = Column(Integer, nullable=False)
    visible = Column(Integer, nullable=False)
    editable = Column(Integer, nullable=False)
    sort = Column(Integer, nullable=False)

    options = Column(JSON, nullable=True)
    validators = Column(JSON, nullable=True)
    extra = Column(JSON, nullable=True)
    view_roles = Column(JSON, nullable=True)
    edit_roles = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint("module", "field_name", name="uq_field_config_module_field"),
        Index("ix_field_config_module", "module"),
        Index("ix_field_config_module_sort", "module", "sort"),
    )


class FieldGroup(Base):
    __tablename__ = "field_group"

    id = Column(Integer, primary_key=True, autoincrement=True)
    module = Column(String(50), nullable=False)

    group_key = Column(String(100), nullable=False)
    group_name = Column(String(100), nullable=False)

    order_index = Column(Integer, nullable=False)

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint("module", "group_key", name="uq_field_group_module_key"),
        Index("ix_field_group_module", "module"),
        Index("ix_field_group_module_order", "module", "order_index"),
    )


class FieldGroupField(Base):
    __tablename__ = "field_group_field"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("field_group.id", ondelete="CASCADE"), nullable=False)
    field_id = Column(Integer, ForeignKey("field_config.id", ondelete="CASCADE"), nullable=False)
    order_index = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("group_id", "field_id", name="uq_field_group_field_pair"),
        Index("ix_field_group_field_group_id", "group_id"),
        Index("ix_field_group_field_group_order", "group_id", "order_index"),
        Index("ix_field_group_field_field_id", "field_id"),
    )
