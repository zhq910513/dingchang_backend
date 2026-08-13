# -*- coding: utf-8 -*-
"""Local database round-trip verification for visible PICC duplicate-insurance notices."""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.db import engine
from app.models.quote_assistant import QuoteAssistantSession
from app.services.ai_assistant_service import (
    db_append_message,
    db_create_session,
    db_delete_session,
    db_list_messages,
    db_list_sessions,
)

OWNER_USER_ID = 1
NOTICE = (
    "重复投保提示\n\n"
    "车辆VIN:LOCAL-ROUNDTRIP-TEST近期已在我司承保，请核实后进行报价，避免重复投保。"
)


async def main() -> None:
    async with async_session_factory() as db:
        exists = (
            await db.execute(select(QuoteAssistantSession.id).where(QuoteAssistantSession.owner_user_id == OWNER_USER_ID).limit(1))
        ).scalar_one_or_none()
        if exists is None:
            raise RuntimeError(f"本地数据库不存在 owner_user_id={OWNER_USER_ID}，无法做隔离会话回归")

        session = await db_create_session(db, owner_user_id=OWNER_USER_ID, title="Codex 本地回归会话")
        session_id = session["session_id"]
        try:
            await db_append_message(
                db,
                owner_user_id=OWNER_USER_ID,
                session_id=session_id,
                role="assistant",
                content=NOTICE,
                metadata={
                    "intent": "quote",
                    "data": {
                        "result_status": "not_ready",
                        "message": NOTICE,
                        "payload": {
                            "ui_visible": True,
                            "platform_auto_notice": {
                                "type": "duplicate_quote_notice",
                                "message": NOTICE,
                                "source": "local_roundtrip_test",
                                "dedupe_key": "local-roundtrip-test",
                            },
                        },
                    },
                },
                message_id=f"qa-local-roundtrip-{uuid.uuid4().hex[:24]}",
            )
            await db.commit()

            sessions = await db_list_sessions(db, owner_user_id=OWNER_USER_ID, limit=50)
            current = next((item for item in sessions["items"] if item["session_id"] == session_id), None)
            if current is None:
                raise AssertionError("新建隔离会话未出现在会话列表")
            preview = str(current.get("last_message_preview") or "")
            if "重复投保提示" not in preview:
                raise AssertionError(f"重复投保提示未进入会话预览: {preview!r}")

            history = await db_list_messages(db, owner_user_id=OWNER_USER_ID, session_id=session_id, limit=20)
            items = history.get("items") or []
            if not any("重复投保提示" in str(item.get("content") or "") for item in items):
                raise AssertionError("重复投保提示未从历史记录返回")
            if not any(
                str(((item.get("metadata") or {}).get("data") or {}).get("payload", {}).get("platform_auto_notice", {}).get("type") or "")
                == "duplicate_quote_notice"
                for item in items
            ):
                raise AssertionError("历史记录缺少 duplicate_quote_notice 标记")

            print("PASS local DB round-trip: visible duplicate notice persisted, previewed, and returned in history")
        finally:
            await db.rollback()
            await db_delete_session(db, owner_user_id=OWNER_USER_ID, session_id=session_id)
            await db.commit()
            print("CLEANUP local DB round-trip session deleted")


if __name__ == "__main__":
    async def _run() -> None:
        try:
            await main()
        finally:
            await engine.dispose()

    asyncio.run(_run())
