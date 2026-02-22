# app/api/v1/ai_assistant.py
# encoding: utf-8
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_current_user_with_role_and_teams

from app.schemas.ai_assistant import (
    AiChatRequest,
    AiChatResponse,
    AiHistoryResponse,
    AiSessionItem,
    AiSessionListResponse,
    AiActionItem,
)

from app.services.ai_assistant_service import (
    list_sessions as _list_sessions,
    get_or_create_session as _get_or_create_session,
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
    兼容：
    - dict
    - ORM/Pydantic 对象（有 id）
    - tuple/list（如 deps 返回 (user, primary, team_names, team_ids)）
    """
    target = current_user

    # ✅ 兼容 deps.get_current_user_with_role_and_teams() 返回 tuple
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
        title=str(_pick(row, "title", default="新会话") or "新会话"),
        created_at=str(_pick(row, "created_at", default="") or ""),
        updated_at=str(_pick(row, "updated_at", default="") or ""),
        last_message_preview=_pick(row, "last_message_preview", default=None),
        message_count=int(_pick(row, "message_count", default=0) or 0),
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
        items.append(
            {
                "role": _pick(m, "role", default="assistant"),
                "content": _pick(m, "content", "text", default="") or "",
                "name": _pick(m, "name", default=None),
                "metadata": _pick(m, "metadata", "meta", default=None),
                "created_at": _pick(m, "created_at", default=None),
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

    # ✅ 使用 getattr 兼容 schema 未定义字段（保持最小改动，不扩 schema）
    result = _send_message(
        owner_user_id=owner_user_id,
        session_id=body.session_id,
        message=body.message,
        history=[x.model_dump() if hasattr(x, "model_dump") else dict(x) for x in (body.history or [])],
        system_prompt=getattr(body, "system_prompt", None),
        model=getattr(body, "model", None),
        temperature=getattr(body, "temperature", None),
        max_tokens=getattr(body, "max_tokens", None),
        stream=body.stream,
        context=body.context or {},
    )

    # 兼容 service 返回结构
    session_id = str(_pick(result, "session_id", default=body.session_id or ""))
    reply = str(_pick(result, "reply", "content", "text", default="") or "")
    usage = _pick(result, "usage", default=None)

    intent = str(_pick(result, "intent", default="fallback") or "fallback")
    trace_id = str(_pick(result, "trace_id", default="") or "")
    confidence_raw = _pick(result, "confidence", default=0.0)
    try:
        confidence = float(confidence_raw)
    except Exception:
        confidence = 0.0

    actions_raw = _pick(result, "actions", default=[]) or []
    actions: List[AiActionItem] = []
    if isinstance(actions_raw, list):
        for a in actions_raw:
            if isinstance(a, dict):
                try:
                    actions.append(AiActionItem(**a))
                except Exception:
                    # 宽松兜底，避免单个 action 异常拖垮响应
                    actions.append(
                        AiActionItem(
                            type=str(a.get("type") or "suggest"),
                            label=str(a.get("label") or ""),
                        )
                    )

    if not session_id:
        # service 若未回 session_id，兜底取当前/新建会话
        if body.session_id:
            session_id = body.session_id
        else:
            try:
                s = _get_or_create_session(owner_user_id=owner_user_id)
                session_id = str(_pick(s, "session_id", "id", default=""))
            except Exception:
                session_id = "unknown"

    if not trace_id:
        # 再兜底一次，避免 schema 必填 trace_id 缺失
        trace_id = "trace-missing"

    return AiChatResponse(
        ok=True,
        session_id=session_id,
        reply=reply or "已处理",
        intent=intent,
        confidence=confidence,
        actions=actions,
        trace_id=trace_id,
        usage=usage,
    )


# -----------------------------
# 健康检查（可选）
# -----------------------------
@router.get("/health")
async def ai_assistant_health():
    return {"ok": True, "module": "quotation_assistant"}
