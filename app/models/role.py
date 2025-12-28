# encoding: utf-8
"""
@author: The King
@project: dingchang_backend
@file: role.py
@time: 2025/12/8 22:38
"""

from sqlalchemy import Column, Integer, String

from app.core.db import Base


class Role(Base):
    __tablename__ = "role"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # unique 本身会带索引意义，不需要再额外 index=True 或显式 Index
    role_name = Column(String(50), unique=True, nullable=False)
    description = Column(String(200), nullable=True)
