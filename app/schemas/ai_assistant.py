# app/schemas/ai_assistant.py
# encoding: utf-8
from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class AiChatIn(BaseModel):
    message: str
    order_id: Optional[int] = None
    images: List[Dict[str, Any]] = Field(default_factory=list)


class AiChatOut(BaseModel):
    reply: str
    ok: bool = True


class AiSessionItem(BaseModel):
    session_id: str
    title: str = ""
    updated_at: Optional[str] = None


class AiSessionListOut(BaseModel):
    total: int = 0
    items: List[AiSessionItem] = Field(default_factory=list)


# =========================
# Sessions: create / delete
# =========================

class AiCreateSessionIn(BaseModel):
    """创建会话入参（冻结契约）"""
    title: Optional[str] = None


class AiDeleteSessionOut(BaseModel):
    """删除会话出参（冻结契约）"""
    ok: bool = True
    session_id: str
