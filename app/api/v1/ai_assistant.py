# app/api/v1/ai_assistant.py
# encoding: utf-8
from __future__ import annotations

"""
报价助手 API（Schema 已冻结）：
- app.schemas.ai_assistant.AiChatIn / AiChatOut
- app.schemas.ai_assistant.AiSessionListOut / AiSessionItem

注意：
- 保留 /sessions /chat /health
- /sessions/{id}/history 作为调试辅助，不强制 schema（返回 dict）
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_current_user_with_role_and_teams
from app.schemas.ai_assistant import AiChatIn, AiChatOut, AiSessionItem, AiSessionListOut
from app.services.ai_assistant_service import (
    list_sessions as _list_sessions,
    create_session as _create_session,
    delete_session as _delete_session,
    list_messages as _list_messages,
    send_message as _send_message,  # async
)

router = APIRouter(prefix="/ai-assistant", tags=["报价助手"])


def _uid_from_current_user(current_user: Any) -> int:
    target = current_user
    if isinstance(current_user, (tuple, list)) and current_user:
        target = current_user[0]

    if isinstance(target, dict):
        uid = target.get("id")
    else:
        uid = getattr(target, "id", None)

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
        title=str(_pick(row, "title", default="") or ""),
        updated_at=str(_pick(row, "updated_at", default="") or "") or None,
    )


class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


class DeleteSessionResponse(BaseModel):
    ok: bool = True
    session_id: str


@router.get("/sessions", response_model=AiSessionListOut)
async def list_ai_sessions(
        current_user=Depends(get_current_user_with_role_and_teams),
):
    owner_user_id = _uid_from_current_user(current_user)
    rows = _list_sessions(owner_user_id=owner_user_id) or []
    items = [_to_session_item(x) for x in rows]
    return AiSessionListOut(total=len(items), items=items)


@router.post("/sessions")
async def create_ai_session(
        body: CreateSessionRequest,
        current_user=Depends(get_current_user_with_role_and_teams),
):
    owner_user_id = _uid_from_current_user(current_user)
    row = _create_session(owner_user_id=owner_user_id, title=(body.title or "").strip() or None)
    return {
        "ok": True,
        "data": {
            "session_id": str(_pick(row, "session_id", "id", default="")),
            "title": str(_pick(row, "title", default="") or ""),
            "updated_at": str(_pick(row, "updated_at", default="") or "") or None,
        },
    }


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


@router.get("/sessions/{session_id}/history")
async def get_ai_history(
        session_id: str,
        current_user=Depends(get_current_user_with_role_and_teams),
):
    owner_user_id = _uid_from_current_user(current_user)
    rows = _list_messages(session_id=session_id, owner_user_id=owner_user_id) or []

    items: List[Dict[str, Any]] = []
    for m in rows:
        items.append(
            {
                "role": _pick(m, "role", default="assistant"),
                "content": _pick(m, "content", "text", default="") or "",
                "created_at": _pick(m, "created_at", default=None),
            }
        )

    return {"session_id": session_id, "items": items}


@router.post("/chat", response_model=AiChatOut)
async def ai_chat(
        body: AiChatIn,
        current_user=Depends(get_current_user_with_role_and_teams),
):
    owner_user_id = _uid_from_current_user(current_user)

    result = await _send_message(
        owner_user_id=owner_user_id,
        session_id=None,
        message=body.message,
        history=[],
        system_prompt=None,
        model=None,
        temperature=None,
        max_tokens=None,
        stream=False,
        context={"order_id": body.order_id, "images": body.images},
    )

    reply = str(_pick(result, "reply", "content", "text", default="") or "")
    return AiChatOut(reply=reply or "已处理", ok=True)


@router.get("/health")
async def ai_assistant_health():
    return {"ok": True, "module": "quotation_assistant"}
