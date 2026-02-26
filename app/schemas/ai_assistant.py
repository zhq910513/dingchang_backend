# app/schemas/ai_assistant.py
# encoding: utf-8
from __future__ import annotations

from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


AiRole = Literal["system", "user", "assistant", "tool"]


class AiChatMessage(BaseModel):
    role: AiRole
    content: str = ""
    name: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class AiChatRequest(BaseModel):
    session_id: Optional[str] = Field(None, description="会话ID", max_length=64)
    message: str = Field(..., min_length=1, max_length=1000, description="用户消息")
    history: List[AiChatMessage] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)
    stream: bool = False


class AiErrorInfo(BaseModel):
    code: str
    message: str


class AiActionItem(BaseModel):
    type: str
    target: Optional[str] = None
    label: Optional[str] = None
    key: Optional[str] = None
    value: Optional[Any] = None
    extra: Optional[Dict[str, Any]] = None


class AiChatResponse(BaseModel):
    ok: bool = True
    session_id: str
    reply: str
    intent: str = "fallback"
    confidence: float = 0.0
    actions: List[AiActionItem] = Field(default_factory=list)
    trace_id: str
    error: Optional[AiErrorInfo] = None
    usage: Optional[Dict[str, Any]] = None

    # ✅ 统一结构化回传（前端按这个渲染，不猜字段）
    # {
    #   "result_status": "success|empty|invalid_command|need_more_info|not_ready|failed",
    #   "message": "人性化说明",
    #   "entities": {...},
    #   "payload": {...}
    # }
    data: Optional[Dict[str, Any]] = None


class AiSessionItem(BaseModel):
    session_id: str
    title: str
    created_at: str
    updated_at: str
    last_message_preview: Optional[str] = None
    message_count: int = 0


class AiSessionListResponse(BaseModel):
    total: int
    items: List[AiSessionItem] = Field(default_factory=list)


class AiHistoryResponse(BaseModel):
    session_id: str
    items: List[AiChatMessage] = Field(default_factory=list)
