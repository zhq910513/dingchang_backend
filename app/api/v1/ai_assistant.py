# app/api/v1/ai_assistant.py
# encoding: utf-8
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_with_role_and_teams
from app.core.db import get_db
from app.schemas.ai_assistant import (
    AiChatRequest,
    AiChatResponse,
    AiSessionListResponse,
    AiSessionItem,
    AiHistoryResponse,
    AiChatMessage,
)
from app.services.ai_assistant_service import (
    AiProviderError,
    chat_once,
    prepare_stream_session,
    finalize_stream_reply,
    stream_chat_completion_sse,
    list_sessions as _list_sessions,
    get_history as _get_history,
    delete_session as _delete_session,
)

router = APIRouter(prefix="/ai-assistant", tags=["ai-assistant"])


def _safe_user_name(u) -> str:
    if not u:
        return ""
    return (
        str(getattr(u, "full_name", None) or "").strip()
        or str(getattr(u, "real_name", None) or "").strip()
        or str(getattr(u, "username", None) or "").strip()
    )


@router.get("/sessions", response_model=AiSessionListResponse)
async def list_ai_sessions(
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _ = db
    _user, _role_name, _team_names, _team_ids = user_with_role

    rows = _list_sessions()
    return AiSessionListResponse(
        total=len(rows),
        items=[AiSessionItem(**x) for x in rows]
    )


@router.get("/sessions/{session_id}/history", response_model=AiHistoryResponse)
async def get_ai_history(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _ = db
    _user, _role_name, _team_names, _team_ids = user_with_role

    items = _get_history(session_id)
    return AiHistoryResponse(
        session_id=session_id,
        items=[AiChatMessage(**x) for x in items]
    )


@router.delete("/sessions/{session_id}")
async def delete_ai_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _ = db
    _user, _role_name, _team_names, _team_ids = user_with_role

    ok = _delete_session(session_id)
    return {"ok": bool(ok)}


@router.post("/chat", response_model=AiChatResponse)
async def ai_chat(
    payload: AiChatRequest,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _ = db
    current_user, role_name, team_names, _team_ids = user_with_role

    if payload.stream:
        # 前端如果传了 stream=true，这个接口不处理，走 /chat/stream
        raise HTTPException(status_code=400, detail="stream=true 请使用 /ai-assistant/chat/stream")

    try:
        result = chat_once(
            session_id=payload.session_id,
            user_message=payload.message,
            history=[m.dict() for m in (payload.history or [])],
            system_prompt_override=payload.system_prompt,
            model=payload.model,
            temperature=float(payload.temperature),
            max_tokens=int(payload.max_tokens),
            user_info={
                "user_id": int(getattr(current_user, "id", 0) or 0),
                "username": _safe_user_name(current_user),
                "role_name": role_name,
                "team_names": list(team_names or ()),
            },
            page_context=payload.context or {},
        )
    except AiProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI助手异常: {str(e) or e.__class__.__name__}")

    return AiChatResponse(
        ok=True,
        session_id=result["session_id"],
        reply=result["reply"],
        model=result["model"],
        usage=result.get("usage"),
    )


@router.post("/chat/stream")
async def ai_chat_stream(
    payload: AiChatRequest,
    db: AsyncSession = Depends(get_db),
    user_with_role=Depends(get_current_user_with_role_and_teams),
):
    _ = db
    current_user, role_name, team_names, _team_ids = user_with_role

    try:
        prepared = prepare_stream_session(
            session_id=payload.session_id,
            user_message=payload.message,
            history=[m.dict() for m in (payload.history or [])],
            system_prompt_override=payload.system_prompt,
            model=payload.model,
            temperature=float(payload.temperature),
            max_tokens=int(payload.max_tokens),
            user_info={
                "user_id": int(getattr(current_user, "id", 0) or 0),
                "username": _safe_user_name(current_user),
                "role_name": role_name,
                "team_names": list(team_names or ()),
            },
            page_context=payload.context or {},
        )
    except AiProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI助手预处理异常: {str(e) or e.__class__.__name__}")

    session = prepared["session"]
    model = prepared["model"]
    messages = prepared["messages"]
    temperature = float(prepared["temperature"])
    max_tokens = int(prepared["max_tokens"])
    user_message = str(prepared["user_message"] or "")

    def event_gen():
        """
        SSE 输出约定：
        1) 首帧发 meta（session_id/model）
        2) 中间透传 provider data
        3) 收尾发 done
        """
        t0 = time.time()
        acc_text_parts: List[str] = []

        # meta
        meta = {"type": "meta", "session_id": session.session_id, "model": model}
        yield f"data: {json.dumps(meta, ensure_ascii=False)}\n\n"

        try:
            for chunk in stream_chat_completion_sse(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                # 解析一份，用于本地累计 assistant 文本；同时原样透传
                try:
                    if chunk.startswith("data: "):
                        raw = chunk[len("data: "):].strip()
                        if raw and raw != "[DONE]":
                            obj = json.loads(raw)
                            choices = obj.get("choices") or []
                            if choices:
                                delta = choices[0].get("delta") or {}
                                part = delta.get("content")
                                if part:
                                    acc_text_parts.append(str(part))
                except Exception:
                    pass

                yield chunk

            reply = "".join(acc_text_parts).strip()
            cost_ms = int((time.time() - t0) * 1000)

            # provider没返回可解析delta时，至少落一条占位，避免会话丢失
            if not reply:
                reply = "(空响应)"

            finalize_stream_reply(
                session=session,
                user_message=user_message,
                assistant_reply=reply,
                model=model,
                cost_ms=cost_ms,
            )

            done = {"type": "done", "session_id": session.session_id, "cost_ms": cost_ms}
            yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"

        except AiProviderError as e:
            err = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        except Exception as e:
            err = {"type": "error", "message": f"AI助手流式异常: {str(e) or e.__class__.__name__}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
