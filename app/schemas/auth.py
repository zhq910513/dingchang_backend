# encoding: utf-8
from __future__ import annotations

from typing import Optional, List

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    token: str
    user_id: int
    role_name: str
    team_names: List[str] = Field(
        default_factory=list,
        description="可访问团队集合（权限范围/筛选团队列表）。manager 账号可多团队。",
    )
    team_name: Optional[str] = Field(
        default=None,
        description="默认/落点团队（单值）。登录时后端会确保其包含在 team_names 中。",
    )
