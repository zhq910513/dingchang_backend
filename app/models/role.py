# encoding: utf-8
from __future__ import annotations

from sqlalchemy import Column, Integer, String, UniqueConstraint
from app.core.db import Base


class Role(Base):
    __tablename__ = "role"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role_name = Column(String(50), nullable=False)
    description = Column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("role_name", name="role_name"),
    )
