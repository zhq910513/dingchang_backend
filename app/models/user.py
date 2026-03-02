# app/models/user.py
# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text

from app.core.db import Base


class User(Base):
    """
    用户表

    说明：
    - team_name：单团队字段（历史口径，可空）
    - team_names：多团队字段（逗号分隔字符串等历史形态，可空）
    - parent_id：上级/主管/父级用户（自关联，可空）
    """

    __tablename__ = "user"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )

    username = Column(
        String(50),
        nullable=False,
        comment="用户名（唯一）",
    )
    real_name = Column(
        String(50),
        nullable=True,
        comment="真实姓名（可空）",
    )

    team_name = Column(
        String(32),
        nullable=True,
        comment="团队名称（单团队字段，历史口径，可空）",
    )
    team_names = Column(
        String(255),
        nullable=True,
        comment="团队名称列表（多团队字段，常为逗号分隔字符串，可空）",
    )

    password_hash = Column(
        String(255),
        nullable=False,
        comment="密码哈希（不可明文存储）",
    )

    parent_id = Column(
        Integer,
        ForeignKey("user.id"),
        nullable=True,
        comment="上级用户ID（FK -> user.id，可空）",
    )

    status = Column(
        Integer,
        nullable=False,
        comment="账号状态（int 枚举；通常 1=启用，其它=停用/禁用，由业务层定义）",
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
        UniqueConstraint("username", name="ix_user_username"),
        Index("ix_user_parent_id", "parent_id"),
        Index("ix_user_status", "status"),
        Index("ix_user_parent_status", "parent_id", "status"),
        Index("ix_user_team_name", "team_name"),
        Index("ix_user_team_status", "team_name", "status"),
    )
