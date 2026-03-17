# app/schemas/user.py
# encoding: utf-8
from __future__ import annotations

from datetime import datetime
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


def _normalize_team_names_csv_or_none(v: Any) -> Optional[str]:
    """
    输入侧保持当前契约：
    - None / 空 -> None
    - CSV str / list[str] -> 规范化后 CSV 字符串
    """
    arr = _normalize_team_names_value(v)
    return ",".join(arr) if arr else None


def _normalize_team_name(v: Any) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("team_name 必须为字符串或 None")
    s = v.strip()
    return s or None


class UserRowCapabilitiesOut(OrmBaseModel):
    user_update: bool = False
    user_delete: bool = False


class UserRowMetaOut(OrmBaseModel):
    capabilities: UserRowCapabilitiesOut = Field(default_factory=UserRowCapabilitiesOut)


class UserListCapabilitiesOut(OrmBaseModel):
    user_create: bool = False
    user_list_view: bool = False


class UserListScopesOut(OrmBaseModel):
    user_creatable_role_names: List[str] = Field(default_factory=list)


class UserListPaginationOut(OrmBaseModel):
    page: int = 1
    page_size: int = 20


class UserListMetaOut(OrmBaseModel):
    capabilities: UserListCapabilitiesOut = Field(default_factory=UserListCapabilitiesOut)
    scopes: UserListScopesOut = Field(default_factory=UserListScopesOut)
    pagination: UserListPaginationOut = Field(default_factory=UserListPaginationOut)


class UserOut(OrmBaseModel):
    id: int
    username: str
    real_name: Optional[str] = None
    role_name: Optional[str] = Field(default=None, description="用户主角色名")
    team_name: Optional[str] = Field(default=None, description="默认/落点团队")
    team_names: List[str] = Field(default_factory=list, description="可访问团队集合")
    status: int = 1
    is_online: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    meta: UserRowMetaOut = Field(default_factory=UserRowMetaOut)

    @validator("team_names", pre=True)
    def _v_team_names(cls, v: Any) -> List[str]:
        return _normalize_team_names_value(v)

    @validator("team_name", pre=True)
    def _v_team_name(cls, v: Any) -> Optional[str]:
        return _normalize_team_name(v)


class UserListOut(OrmBaseModel):
    total: int = 0
    items: List[UserOut] = Field(default_factory=list)
    meta: UserListMetaOut = Field(default_factory=UserListMetaOut)


class UserCreateIn(OrmBaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=6)
    role_name: str = Field(..., min_length=1, description="角色：super_admin/manager/sales/finance/market")
    team_name: Optional[str] = None
    team_names: Optional[str] = None

    @validator("team_name", pre=True)
    def _v_create_team_name(cls, v: Any) -> Optional[str]:
        return _normalize_team_name(v)

    @validator("team_names", pre=True)
    def _v_create_team_names(cls, v: Any) -> Optional[str]:
        return _normalize_team_names_csv_or_none(v)


class UserUpdateIn(OrmBaseModel):
    password: Optional[str] = Field(default=None, min_length=6)
    team_name: Optional[str] = None
    team_names: Optional[str] = None

    @validator("team_name", pre=True)
    def _v_update_team_name(cls, v: Any) -> Optional[str]:
        return _normalize_team_name(v)

    @validator("team_names", pre=True)
    def _v_update_team_names(cls, v: Any) -> Optional[str]:
        return _normalize_team_names_csv_or_none(v)