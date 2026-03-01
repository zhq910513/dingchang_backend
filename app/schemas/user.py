# encoding: utf-8
from __future__ import annotations

from typing import Optional, List
from pydantic import BaseModel, Field


class UserOut(BaseModel):
    id: int
    username: str
    real_name: Optional[str] = None
    team_name: Optional[str] = None
    team_names: Optional[str] = None
    status: int = 1


class UserListOut(BaseModel):
    total: int = 0
    items: List[UserOut] = Field(default_factory=list)
