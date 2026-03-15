# app/schemas/user.py
# encoding: utf-8
from __future__ import annotations

from typing import Any, ClassVar, List, Optional

from pydantic import BaseModel, Field, validator


class OrmBaseModel(BaseModel):
    """
    Pydantic v1/v2 兼容：
    - v1：靠 Config.orm_mode
    - v2：可识别 model_config
    - 关键：model_config 必须是 ClassVar，不能变成响应字段
    """

    model_config: ClassVar[dict] = {"from_attributes": True}

    class Config:
        orm_mode = True


def _normalize_team_names_value(v: Any) -> List[str]:
    """
    team_names 的统一规范化（幂等）：
    - None -> []
    - CSV str -> List[str]
    - list/tuple[str] -> 清洗后返回 List[str]
    """
    if v is None:
        return []

    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        parts = [x.strip() for x in s.split(",") if x and x.strip()]
        return sorted(set(parts))

    if isinstance(v, (list, tuple)):
        parts = [str(x or "").strip() for x in v if str(x or "").strip()]
        return sorted(set(parts))

    raise ValueError("team_names 必须为 CSV 字符串、字符串数组或 None")


def _normalize_team_name(v: Any) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("team_name 必须为字符串或 None")
    s = v.strip()
    return s or None


class UserOut(OrmBaseModel):
    id: int
    username: str
    real_name: Optional[str] = None
    role_name: Optional[str] = Field(default=None, description="用户主角色名")
    team_name: Optional[str] = Field(default=None, description="默认/落点团队")
    team_names: List[str] = Field(default_factory=list, description="可访问团队集合")
    status: int = 1
    is_online: bool = False

    @validator("team_names", pre=True)
    def _v_team_names(cls, v: Any) -> List[str]:
        return _normalize_team_names_value(v)

    @validator("team_name", pre=True)
    def _v_team_name(cls, v: Any) -> Optional[str]:
        return _normalize_team_name(v)


class UserListOut(OrmBaseModel):
    total: int = 0
    items: List[UserOut] = Field(default_factory=list)


class UserCreateIn(OrmBaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=6)
    role_name: str = Field(..., min_length=1, description="角色：super_admin/manager/sales/finance/market")
    team_name: Optional[str] = None
    team_names: Optional[str] = None

    @validator("team_name", pre=True)
    def _v_create_team_name(cls, v: Any) -> Optional[str]:
        return _normalize_team_name(v)


class UserUpdateIn(OrmBaseModel):
    password: Optional[str] = Field(default=None, min_length=6)
    team_name: Optional[str] = None
    team_names: Optional[str] = None

    @validator("team_name", pre=True)
    def _v_update_team_name(cls, v: Any) -> Optional[str]:
        return _normalize_team_name(v)
