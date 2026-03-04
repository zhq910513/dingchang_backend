# encoding: utf-8
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    id: int
    username: str
    real_name: Optional[str] = None

    team_name: Optional[str] = Field(
        default=None,
        description="默认/落点团队（单值）。用于子账号归属或默认选择；登录时后端会确保其包含在 team_names 中。",
    )

    team_names: List[str] = Field(
        default_factory=list,
        description="可访问团队集合（权限范围/筛选团队列表）。manager 账号可多团队。",
    )

    status: int = 1


class UserListOut(BaseModel):
    total: int = 0
    items: List[UserOut] = Field(default_factory=list)
