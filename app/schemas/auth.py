# encoding: utf-8
"""
Auth 相关 Pydantic 模型
"""

from typing import Optional
from pydantic import BaseModel


class LoginRequest(BaseModel):
    """登录请求体"""
    username: str
    password: str


class LoginUserOut(BaseModel):
    id: int
    username: str
    real_name: Optional[str] = None
    full_name: Optional[str] = None
    status: Optional[int] = None

    # ✅ 权限判断用机器码
    role_name: str
    # ✅ 展示用中文描述
    role_label: str


class LoginResponse(BaseModel):
    """登录响应"""
    session_token: str
    user: LoginUserOut
