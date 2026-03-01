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
    team_names: List[str] = Field(default_factory=list)
    team_name: Optional[str] = None
