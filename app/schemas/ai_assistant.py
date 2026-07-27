# app/schemas/ai_assistant.py
# encoding: utf-8
from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class AiChatIn(BaseModel):
    session_id: Optional[str] = Field(default=None, max_length=128)
    message: str = Field(..., min_length=1, max_length=2000)
    order_id: Optional[int] = Field(default=None, ge=1)
    images: List[Dict[str, Any]] = Field(default_factory=list)
    history: List[Dict[str, Any]] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)
    stream: bool = False


class AiChatOut(BaseModel):
    reply: str
    ok: bool = True
    session_id: Optional[str] = None
    intent: Optional[str] = None
    trace_id: Optional[str] = None
    confidence: float = 0.0
    actions: List[Dict[str, Any]] = Field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    silent: bool = False
    ui_visible: bool = True


class AiSessionItem(BaseModel):
    session_id: str
    title: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_message_preview: str = ""
    message_count: int = 0


class AiSessionListOut(BaseModel):
    total: int = 0
    items: List[AiSessionItem] = Field(default_factory=list)
    next_cursor: Optional[str] = None
    has_more: bool = False


# =========================
# Sessions: create / delete
# =========================

class AiCreateSessionIn(BaseModel):
    """创建会话入参（冻结契约）"""
    title: Optional[str] = Field(default=None, max_length=100)


class AiDeleteSessionOut(BaseModel):
    """删除会话出参（冻结契约）"""
    ok: bool = True
    session_id: str


class AiRecallSessionImageIn(BaseModel):
    storage_keys: List[str] = Field(default_factory=list)
    message_id: Optional[str] = Field(default=None, max_length=128)


class AiPlatformAccountTypeIn(BaseModel):
    platform_code: str = Field(..., min_length=1, max_length=32)
    platform_name: Optional[str] = Field(default=None, max_length=64)
    type_name: str = Field(..., min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=255)
    match_rules: Dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    enabled: bool = True


class AiPlatformAccountProfileIn(BaseModel):
    platform_code: str = Field(..., min_length=1, max_length=32)
    platform_name: Optional[str] = Field(default=None, max_length=64)
    account_type_name: Optional[str] = Field(default=None, max_length=64)
    account_username: str = Field(..., min_length=1, max_length=128)
    account_password: Optional[str] = Field(default=None, max_length=256)
    login_phone: Optional[str] = Field(default=None, max_length=32)
    email: Optional[str] = Field(default=None, max_length=128)
    account_owner_user_id: Optional[int] = Field(default=None, ge=1)
    account_owner_name: Optional[str] = Field(default=None, max_length=64)
    auto_login: bool = True
    enabled: bool = True
    quota_limit: Optional[int] = Field(default=None, ge=0, le=1000000)
    quota_period_type: Optional[str] = Field(default="day", max_length=16)
    confirm_enabled_edit: bool = False


class AiPlatformAccountLoginChallengeIn(BaseModel):
    code: str = Field(..., min_length=4, max_length=8)


class AiPlatformDefaultConfigIn(BaseModel):
    platform_code: str = Field(..., min_length=1, max_length=32)
    platform_name: Optional[str] = Field(default=None, max_length=64)
    account_type_name: str = Field(..., min_length=1, max_length=64)
    default_values: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
