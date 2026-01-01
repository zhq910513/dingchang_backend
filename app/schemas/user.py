# app/schemas/user.py
# encoding: utf-8
"""
User 相关 Pydantic 模型（去兼容版）
"""

from typing import Optional, List

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    """
    创建子账号请求体
    """
    username: str
    password: str
    role_id: int
    real_name: Optional[str] = None

    # ✅ 超管创建业务/财务/市场时必须指定归属经理
    manager_id: Optional[int] = None

    # ✅ 兼容旧：创建经理/下属账号时可传单团队（例如 "赣州团队"）
    # - 下属账号：最终会落到 user.team_name
    # - 经理账号：可作为 team_names 的补充输入
    team_name: Optional[str] = None

    # ✅ 新增：创建经理账号时支持多选团队（赣州/南昌/九江）
    # - 仅在创建 role=manager 时使用
    # - users.py 中会把 team_name 一并合并到 team_names
    team_names: List[str] = Field(default_factory=list)


class UserSimple(BaseModel):
    """
    子账号列表项
    """
    id: int
    username: str
    real_name: Optional[str] = None
    role_name: Optional[str] = None
    is_online: bool = False

    # ✅ 补齐：用于前端展示团队（兼容单团队 & 多团队）
    team_name: Optional[str] = None
    team_names: Optional[List[str]] = None
