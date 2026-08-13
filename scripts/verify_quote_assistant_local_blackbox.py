# -*- coding: utf-8 -*-
"""Service-level local checks for quote assistant chat visibility and material forms."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db import async_session_factory
from app.core.db import engine
from app.core.constants import ROLE_SUPER_ADMIN
from app.services.ai_assistant_service import (
    db_append_message,
    db_create_session,
    db_delete_session,
    db_list_messages,
    send_message,
)


OWNER_USER_ID = 1
CONTEXT = {
    "current_user_id": OWNER_USER_ID,
    "role_name": ROLE_SUPER_ADMIN,
    "team_names": [],
}
PICC_QUOTE = "\u4eba\u4fdd\u62a5\u4ef7"
MANUAL = "\u624b\u5de5"
SUPPLEMENT = "\u8865\u8d44\u6599"


def _payload(result: dict) -> dict:
    return (
        result.get("data", {}).get("payload", {})
        if isinstance(result.get("data"), dict)
        else {}
    )


async def _send(
    db,
    *,
    session_id: str,
    message: str,
    client_msg_id: str = "",
) -> dict:
    result = await send_message(
        owner_user_id=str(OWNER_USER_ID),
        session_id=session_id,
        message=message,
        context=dict(CONTEXT),
        client_msg_id=client_msg_id or None,
        db=db,
    )
    await db.commit()
    return result


async def main() -> None:
    async with async_session_factory() as db:
        session = await db_create_session(
            db,
            owner_user_id=OWNER_USER_ID,
            title="Codex local blackbox verification",
        )
        session_id = str(session["session_id"])
        try:
            quote = await _send(db, session_id=session_id, message=PICC_QUOTE)
            quote_data = quote.get("data") if isinstance(quote.get("data"), dict) else {}
            quote_message = str(quote.get("reply") or "")
            assert quote.get("intent") == "quote", quote
            assert quote_data.get("result_status") == "need_more_info", quote
            assert "\u7f3a\u5c11\u5b57\u6bb5" in quote_message, quote_message
            assert quote.get("ui_visible") is True, quote

            stable_client_msg_id = f"client_local_blackbox_{session_id[:16]}"
            missing_again = await _send(
                db,
                session_id=session_id,
                message=PICC_QUOTE,
                client_msg_id=stable_client_msg_id,
            )
            first_user_message = missing_again.get("user_message", {})
            assert str(first_user_message.get("id") or "").endswith(stable_client_msg_id), missing_again
            assert (
                first_user_message.get("metadata", {}).get("client_msg_id") == stable_client_msg_id
            ), missing_again
            before_replay = await db_list_messages(
                db,
                owner_user_id=OWNER_USER_ID,
                session_id=session_id,
                limit=20,
            )
            before_replay_count = len(before_replay.get("items") or [])
            replay = await _send(
                db,
                session_id=session_id,
                message=PICC_QUOTE,
                client_msg_id=stable_client_msg_id,
            )
            assert replay.get("cached") is True, replay
            after_replay = await db_list_messages(
                db,
                owner_user_id=OWNER_USER_ID,
                session_id=session_id,
                limit=20,
            )
            assert len(after_replay.get("items") or []) == before_replay_count, after_replay

            manual = await _send(db, session_id=session_id, message=MANUAL)
            manual_payload = _payload(manual)
            manual_form = manual_payload.get("quote_material_form", {})
            assert manual_form.get("mode") == "manual", manual
            assert manual.get("ui_visible") is False, manual
            assert not str(manual.get("reply") or "").strip(), manual

            manual_client_msg_id = f"client_local_manual_{session_id[:16]}"
            manual_once = await _send(
                db,
                session_id=session_id,
                message=MANUAL,
                client_msg_id=manual_client_msg_id,
            )
            manual_twice = await _send(
                db,
                session_id=session_id,
                message=MANUAL,
                client_msg_id=manual_client_msg_id,
            )
            assert manual_twice.get("cached") is True, manual_twice
            assert _payload(manual_twice).get("quote_material_form", {}).get("mode") == "manual", manual_twice
            assert manual_once.get("ui_visible") is False and manual_twice.get("ui_visible") is False, (manual_once, manual_twice)

            quote_with_notice_client_msg_id = f"client_local_notice_{session_id[:16]}"
            notice_user = await db_append_message(
                db,
                owner_user_id=OWNER_USER_ID,
                session_id=session_id,
                role="user",
                content=PICC_QUOTE,
                metadata={
                    "status": "success",
                    "intent": "user_input",
                    "client_msg_id": quote_with_notice_client_msg_id,
                },
                message_id=f"{session_id[:12]}_{quote_with_notice_client_msg_id}"[:64],
            )
            await db_append_message(
                db,
                owner_user_id=OWNER_USER_ID,
                session_id=session_id,
                role="assistant",
                content="该车辆商业险保险期间与现存有效保单重复投保，系统建议将起保日期调整为2026-09-18 00时00分。",
                metadata={
                    "status": "success",
                    "intent": "quote",
                    "trace_id": "local-notice-first",
                    "data": {
                        "result_status": "not_ready",
                        "message": "平台提示",
                        "payload": {
                            "ui_visible": True,
                            "platform_auto_notice": {"type": "insurance_date_adjust"},
                        },
                    },
                },
            )
            await db_append_message(
                db,
                owner_user_id=OWNER_USER_ID,
                session_id=session_id,
                role="assistant",
                content="人保风险水平：36 分",
                metadata={
                    "status": "success",
                    "intent": "quote",
                    "trace_id": "local-result-last",
                    "data": {
                        "result_status": "success",
                        "message": "报价流程已完成",
                        "payload": {
                            "ui_visible": True,
                            "quote_result": {
                                "risk_score": 36,
                                "result_image_url": "https://example.invalid/quote-result.png",
                            },
                        },
                    },
                },
            )
            await db.commit()
            cached_result_after_notice = await _send(
                db,
                session_id=session_id,
                message=PICC_QUOTE,
                client_msg_id=quote_with_notice_client_msg_id,
            )
            assert cached_result_after_notice.get("cached") is True, cached_result_after_notice
            assert cached_result_after_notice.get("reply") == "人保风险水平：36 分", cached_result_after_notice
            assert cached_result_after_notice.get("trace_id") == "local-result-last", cached_result_after_notice
            assert (
                cached_result_after_notice.get("user_message", {}).get("id") == notice_user.get("id")
            ), cached_result_after_notice

            supplement = await _send(db, session_id=session_id, message=SUPPLEMENT)
            supplement_payload = _payload(supplement)
            supplement_form = supplement_payload.get("quote_material_form", {})
            assert supplement_form.get("mode") == "supplement", supplement
            assert supplement.get("ui_visible") is False, supplement
            assert not str(supplement.get("reply") or "").strip(), supplement

            history = await db_list_messages(
                db,
                owner_user_id=OWNER_USER_ID,
                session_id=session_id,
                limit=20,
            )
            items = history.get("items") or []
            assistant_items = [
                item
                for item in items
                if str(item.get("role") or "").lower() == "assistant"
            ]
            assert len(assistant_items) == 4, assistant_items
            assert "\u7f3a\u5c11\u5b57\u6bb5" in str(assistant_items[0].get("content") or ""), assistant_items
            print(
                "PASS local blackbox: missing-material quote is visible; "
                "manual/supplement forms are silent; cached replay returns final result after notices"
            )
        finally:
            await db.rollback()
            await db_delete_session(
                db,
                owner_user_id=OWNER_USER_ID,
                session_id=session_id,
            )
            await db.commit()
            print("CLEANUP local blackbox session deleted")


if __name__ == "__main__":
    async def _run() -> None:
        try:
            await main()
        finally:
            await engine.dispose()

    asyncio.run(_run())
