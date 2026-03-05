# app/schemas/user.py
# encoding: utf-8
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field, validator


def _split_team_names_csv(v: Any) -> List[str]:
    """
    ✅ 零兼容：只接受 CSV 字符串 / None
    - None -> []
    - str  -> split -> 去空 -> 去重 -> 排序
    其它类型一律报错（强制上游/ORM 输出口径稳定）
    """
    if v is None:
        return []
    if not isinstance(v, str):
        raise ValueError("team_names 必须为 CSV 字符串或 None（零兼容模式）")

    s = v.strip()
    if not s:
        return []
    parts = [x.strip() for x in s.split(",") if x and x.strip()]
    return sorted(set(parts))


def _normalize_team_name(v: Any) -> Optional[str]:
    """
    ✅ 零兼容：只接受 str/None
    """
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("team_name 必须为字符串或 None（零兼容模式）")
    s = v.strip()
    return s or None


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
        description="可访问团队集合（权限范围/筛选团队列表）。manager 账号可多团队。DB 存 CSV 字符串。",
    )

    status: int = 1

    class Config:
        orm_mode = True

    @validator("team_names", pre=True)
    def _v_team_names(cls, v: Any) -> List[str]:
        return _split_team_names_csv(v)

    @validator("team_name", pre=True)
    def _v_team_name(cls, v: Any) -> Optional[str]:
        return _normalize_team_name(v)


class UserListOut(BaseModel):
    total: int = 0
    items: List[UserOut] = Field(default_factory=list)
