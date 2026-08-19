# -*- coding: utf-8 -*-
"""Regression checks for invalid assistant commands during quote flows."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ai_assistant_service import (  # noqa: E402
    _dispatch_rule_with_db,
    _detect_intent,
    db_create_session,
    db_delete_session,
    db_list_messages,
    send_message,
)
from app.core.db import async_session_factory, engine  # noqa: E402


CTX = {
    "current_user_id": 1,
    "role_name": "super_admin",
    "session_id": "invalid-command-regression",
}


async def _run() -> None:
    intent, confidence, entities = _detect_intent("随便说一句未收录命令")
    assert intent == "fallback", (intent, confidence, entities)

    with (
        patch(
            "app.services.ai_assistant_service.has_waiting_sms_task",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.services.ai_assistant_service.handle_quote_images_message",
            new=AsyncMock(side_effect=AssertionError("invalid text must not enter image handling")),
        ),
        patch(
            "app.services.ai_assistant_service.handle_quote_text_material_message",
            new=AsyncMock(side_effect=AssertionError("invalid text must not mutate quote materials")),
        ),
    ):
        reply, meta = await _dispatch_rule_with_db(
            "随便说一句未收录命令",
            CTX,
            db=object(),
            intent=intent,
            confidence=confidence,
            entities=entities,
        )
        assert meta.get("intent") == "fallback", meta
        assert meta.get("data", {}).get("result_status") == "invalid_command", meta
        assert "指令错误" in reply, reply

    with (
        patch(
            "app.services.ai_assistant_service.has_waiting_sms_task",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.ai_assistant_service.handle_quote_images_message",
            new=AsyncMock(side_effect=AssertionError("ordinary text must not enter image handling")),
        ),
        patch(
            "app.services.ai_assistant_service.handle_quote_text_material_message",
            new=AsyncMock(side_effect=AssertionError("ordinary text must not mutate quote materials")),
        ),
    ):
        reply, meta = await _dispatch_rule_with_db(
            "这不是验证码，也不是报价命令",
            CTX,
            db=object(),
            intent="fallback",
            confidence=0.4,
            entities={},
        )
        assert meta.get("intent") == "fallback", meta
        assert "指令错误" in reply, reply

    quote_intent, quote_confidence, quote_entities = _detect_intent("人保报价")
    assert quote_intent == "quote", (quote_intent, quote_confidence, quote_entities)
    with (
        patch(
            "app.services.ai_assistant_service.has_waiting_sms_task",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.services.ai_assistant_service.handle_quote_message",
            new=AsyncMock(
                return_value=(
                    "人保报价已进入处理队列",
                    {
                        "status": "success",
                        "intent": "quote",
                        "data": {"result_status": "not_ready"},
                    },
                )
            ),
        ) as quote_handler,
    ):
        invalid_reply, invalid_meta = await _dispatch_rule_with_db(
            "这条指令不存在",
            CTX,
            db=object(),
            intent="fallback",
            confidence=0.4,
            entities={},
        )
        assert invalid_meta.get("intent") == "fallback", invalid_meta
        assert "指令错误" in invalid_reply, invalid_reply

        reply, meta = await _dispatch_rule_with_db(
            "人保报价",
            CTX,
            db=object(),
            intent=quote_intent,
            confidence=quote_confidence,
            entities=quote_entities,
        )
        assert quote_handler.await_count == 1
        assert meta.get("intent") == "quote", meta
        assert "已进入处理队列" in reply, reply

    image_reply = (
        "",
        {
            "status": "success",
            "intent": "quote_image_collect",
            "silent": True,
            "ui_visible": False,
            "data": {"result_status": "success"},
        },
    )
    with (
        patch(
            "app.services.ai_assistant_service.has_waiting_sms_task",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.services.ai_assistant_service.handle_quote_images_message",
            new=AsyncMock(return_value=image_reply),
        ) as image_handler,
    ):
        reply, meta = await _dispatch_rule_with_db(
            "这张是行驶证",
            {
                **CTX,
                "uploaded_images": [{"storage_key": "related/a" * 8, "md5": "0" * 32}],
            },
            db=object(),
            intent="fallback",
            confidence=0.4,
            entities={},
        )
        assert image_handler.await_count == 1
        assert meta.get("intent") == "quote_image_collect", meta
        assert reply == "", reply

    with (
        patch(
            "app.services.ai_assistant_service.has_waiting_sms_task",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.ai_assistant_service.handle_quote_message",
            new=AsyncMock(
                return_value=(
                    "验证码已提交，继续报价",
                    {
                        "status": "success",
                        "intent": "quote",
                        "data": {"result_status": "not_ready"},
                    },
                )
            ),
        ),
    ):
        reply, meta = await _dispatch_rule_with_db(
            "735315",
            CTX,
            db=object(),
            intent="fallback",
            confidence=0.4,
            entities={},
        )
        assert meta.get("intent") == "quote", meta
        assert "验证码已提交" in reply, reply


async def _verify_persisted_fallback_message() -> None:
    session_id = ""
    async with async_session_factory() as db:
        try:
            session = await db_create_session(
                db,
                owner_user_id=1,
                title=f"Codex 无效指令回归 {uuid.uuid4().hex[:8]}",
            )
            session_id = str(session["session_id"])
            await db.commit()

            result = await send_message(
                owner_user_id="1",
                session_id=session_id,
                message="随便说一句未收录命令",
                context={**CTX, "session_id": session_id},
                client_msg_id=f"invalid-command-{uuid.uuid4().hex}",
                db=db,
            )
            await db.commit()
            assert result.get("intent") == "fallback", result
            assert result.get("data", {}).get("result_status") == "invalid_command", result
            assert "指令错误" in str(result.get("reply") or ""), result

            history_page = await db_list_messages(
                db,
                owner_user_id=1,
                session_id=session_id,
                limit=20,
            )
            history = history_page.get("items", []) if isinstance(history_page, dict) else []
            assistant_messages = [item for item in history if item.get("role") == "assistant"]
            assert any("指令错误" in str(item.get("content") or "") for item in assistant_messages), history
        finally:
            if session_id:
                await db_delete_session(db, owner_user_id=1, session_id=session_id)
                await db.commit()


async def _main_async() -> None:
    try:
        await _run()
        await _verify_persisted_fallback_message()
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_main_async())
    print("PASS invalid command flow: visible fallback, persisted reply, no quote mutation, SMS code preserved")


if __name__ == "__main__":
    main()
