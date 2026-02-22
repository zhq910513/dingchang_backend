# app/api/v1/ai_assistant.py
# encoding: utf-8
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import get_current_user_with_role_and_teams

# 你现有 schema（如果字段名有差异，按你的文件微调）
from app.schemas.ai_assistant import (
    AiChatRequest,
    AiChatResponse,
    AiHistoryResponse,
    AiSessionItem,
    AiSessionListResponse,
)

# 你现有 service（函数名按你项目里的实际名字对齐）
# 这里用别名，避免和路由函数重名
from app.services.ai_assistant_service import (
    list_sessions as _list_sessions,
    get_or_create_session as _get_or_create_session,   # 若你没有这个函数，可删掉对应接口
    create_session as _create_session,
    delete_session as _delete_session,
    list_messages as _list_messages,
    send_message as _send_message,
)

router = APIRouter(prefix="/ai-assistant", tags=["报价助手"])


# -----------------------------
# 辅助函数
# -----------------------------
def _uid_from_current_user(current_user: Any) -> int:
    """
    兼容 dict / ORM / Pydantic 对象三种形态
    """
    if isinstance(current_user, dict):
        uid = current_user.get("id")
    else:
        uid = getattr(current_user, "id", None)

    try:
        uid = int(uid)
    except Exception:
        uid = 0

    if uid <= 0:
        raise HTTPException(status_code=401, detail="无法识别当前用户")
    return uid


def _pick(obj: Any, *keys: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj.get(k)
        return default
    for k in keys:
        if hasattr(obj, k):
            return getattr(obj, k)
    return default


def _to_session_item(row: Any) -> AiSessionItem:
    return AiSessionItem(
        session_id=str(_pick(row, "session_id", "id", default="")),
        title=str(_pick(row, "title", default="新会话") or "新会话"),
        created_at=str(_pick(row, "created_at", default="") or ""),
        updated_at=str(_pick(row, "updated_at", default="") or ""),
        last_message_preview=_pick(row, "last_message_preview", default=None),
    )


# -----------------------------
# 额外请求体（前端会用到）
# -----------------------------
class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


class DeleteSessionResponse(BaseModel):
    ok: bool = True
    session_id: str


# -----------------------------
# 会话列表
# -----------------------------
@router.get("/sessions", response_model=AiSessionListResponse)
async def list_ai_sessions(
    current_user=Depends(get_current_user_with_role_and_teams),
):
    owner_user_id = _uid_from_current_user(current_user)

    # ✅ 修复点：必须传 owner_user_id
    rows = _list_sessions(owner_user_id=owner_user_id) or []

    items = [_to_session_item(x) for x in rows]
    return AiSessionListResponse(total=len(items), items=items)


# -----------------------------
# 新建会话
# -----------------------------
@router.post("/sessions")
async def create_ai_session(
    body: CreateSessionRequest,
    current_user=Depends(get_current_user_with_role_and_teams),
):
    owner_user_id = _uid_from_current_user(current_user)

    row = _create_session(
        owner_user_id=owner_user_id,
        title=(body.title or "").strip() or None,
    )

    # 保持返回宽松，前端兼容 r.data.data / r.data
    return {
        "ok": True,
        "data": {
            "session_id": str(_pick(row, "session_id", "id", default="")),
            "title": _pick(row, "title", default="新会话"),
            "created_at": str(_pick(row, "created_at", default="") or ""),
            "updated_at": str(_pick(row, "updated_at", default="") or ""),
        },
    }


# -----------------------------
# 删除会话
# -----------------------------
@router.delete("/sessions/{session_id}", response_model=DeleteSessionResponse)
async def delete_ai_session(
    session_id: str,
    current_user=Depends(get_current_user_with_role_and_teams),
):
    owner_user_id = _uid_from_current_user(current_user)

    ok = _delete_session(session_id=session_id, owner_user_id=owner_user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在或无权限")

    return DeleteSessionResponse(ok=True, session_id=session_id)


# -----------------------------
# 历史消息
# -----------------------------
@router.get("/sessions/{session_id}/history", response_model=AiHistoryResponse)
async def get_ai_history(
    session_id: str,
    current_user=Depends(get_current_user_with_role_and_teams),
):
    owner_user_id = _uid_from_current_user(current_user)

    rows = _list_messages(session_id=session_id, owner_user_id=owner_user_id) or []

    items: List[Dict[str, Any]] = []
    for m in rows:
      # 兼容 service 返回 dict/对象
        items.append(
            {
                "role": _pick(m, "role", default="assistant"),
                "content": _pick(m, "content", "text", default="") or "",
                "name": _pick(m, "name", default=None),
                "metadata": _pick(m, "metadata", "meta", default=None),
            }
        )

    return AiHistoryResponse(session_id=session_id, items=items)


# -----------------------------
# 发送消息（非流式）
# -----------------------------
@router.post("/chat", response_model=AiChatResponse)
async def ai_chat(
    body: AiChatRequest,
    current_user=Depends(get_current_user_with_role_and_teams),
):
    owner_user_id = _uid_from_current_user(current_user)

    # 若前端没传 session_id，可在 service 层自动创建
    # 也可改成先 create_session 再 send_message
    result = _send_message(
        owner_user_id=owner_user_id,
        session_id=body.session_id,
        message=body.message,
        history=[x.model_dump() if hasattr(x, "model_dump") else dict(x) for x in (body.history or [])],
        system_prompt=body.system_prompt,
        model=body.model,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        stream=body.stream,
        context=body.context or {},
    )

    # 兼容 service 不同返回结构
    session_id = str(_pick(result, "session_id", default=body.session_id or ""))
    reply = str(_pick(result, "reply", "content", "text", default="") or "")
    model_name = str(_pick(result, "model", default=body.model or "rule-engine"))
    usage = _pick(result, "usage", default=None)

    if not session_id:
        # 如果 service 没回 session_id，尝试取“当前/新建会话”
        if body.session_id:
            session_id = body.session_id
        else:
            # 有的实现会有 get_or_create
            try:
                s = _get_or_create_session(owner_user_id=owner_user_id)
                session_id = str(_pick(s, "session_id", "id", default=""))
            except Exception:
                session_id = "unknown"

    return AiChatResponse(
        ok=True,
        session_id=session_id,
        reply=reply or "已处理",
        model=model_name,
        usage=usage,
    )


# -----------------------------
# 健康检查（可选）
# -----------------------------
@router.get("/health")
async def ai_assistant_health():
    return {"ok": True, "module": "quotation_assistant"}
