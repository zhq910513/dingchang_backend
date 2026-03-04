# encoding: utf-8
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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
