# encoding: utf-8
"""
User 相关 Pydantic 模型（去兼容版）
"""

from typing import Optional
from pydantic import BaseModel


class UserCreate(BaseModel):
    """
    创建子账号请求体
    """
    username: str
    password: str
    role_id: int
    real_name: Optional[str] = None

    # ✅ 超管创建业务/财务时必须指定归属经理
    manager_id: Optional[int] = None


class UserSimple(BaseModel):
    """
    子账号列表项
    """
    id: int
    username: str
    real_name: Optional[str] = None
    role_name: Optional[str] = None
    is_online: bool = False
