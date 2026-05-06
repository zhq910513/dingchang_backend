# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import app.services.quote_assistant_service as qas
from app.models.quote_assistant import QuotePlatformAccount
from app.services.quote_assistant_service import (
    RESULT_NEED_MORE,
    RESULT_SUCCESS,
    _db_safe_image_url,
    _missing_platform_account_fields,
    detect_platform_credential_signal,
    detect_quote_signal,
    handle_platform_credential_message,
    list_platform_account_schemas,
    redact_quote_sensitive_text,
    save_platform_account_form,
)


class QuoteAssistantSignalTests(TestCase):
    def test_credential_values_do_not_guess_platform(self) -> None:
        samples = (
            ("abc", "pass123"),
            ("abc", "pa-123"),
            ("tp_account", "pa_123"),
        )

        for username, password in samples:
            with self.subTest(username=username, password=password):
                text = f"登录手机号 13800138000 账号 {username} 密码 {password}"

                signal = detect_quote_signal(text)
                entities = signal["entities"]
                self.assertNotIn("platform_code", entities)
                self.assertNotIn("platform_name", entities)

                credential_signal = detect_platform_credential_signal(text)
                self.assertTrue(credential_signal["is_credential"])
                self.assertEqual(credential_signal["credentials"]["login_phone"], "13800138000")
                self.assertNotIn("platform_code", credential_signal["entities"])
                self.assertNotIn("platform_name", credential_signal["entities"])

    def test_short_ascii_platform_alias_still_matches_when_standalone(self) -> None:
        text = "PA 登录手机号 13800138000 账号 abc 密码 pass123"

        signal = detect_quote_signal(text)
        self.assertEqual(signal["entities"]["platform_code"], "PA")
        self.assertEqual(signal["entities"]["platform_name"], "平安")

    def test_redact_quote_sensitive_text_keeps_chat_display_safe(self) -> None:
        text = "太平洋登录手机号 13800138000 账号 abc 密码 pass123"

        redacted = redact_quote_sensitive_text(text)

        self.assertIn("138****8000", redacted)
        self.assertIn("密码 [已隐藏]", redacted)
        self.assertNotIn("13800138000", redacted)
        self.assertNotIn("pass123", redacted)

    def test_platform_account_schema_supports_different_required_fields(self) -> None:
        schemas = {item["platform_code"]: item for item in list_platform_account_schemas()}

        self.assertIn("TP", schemas)
        self.assertIn("PICC", schemas)
        tp_required = {f["key"] for f in schemas["TP"]["fields"] if f.get("required")}
        picc_required = {f["key"] for f in schemas["PICC"]["fields"] if f.get("required")}
        self.assertEqual(tp_required, {"login_phone"})
        self.assertTrue({"login_phone", "account_username", "account_password"}.issubset(picc_required))

    def test_missing_platform_account_fields_follow_platform_schema(self) -> None:
        missing_picc = _missing_platform_account_fields(None, "PICC")
        self.assertEqual(
            {item["key"] for item in missing_picc},
            {"login_phone", "account_username", "account_password"},
        )

        tp_account = QuotePlatformAccount(
            owner_user_id=7,
            platform_code="TP",
            platform_name="太平洋",
            login_phone="13800138000",
            login_phone_mask="138****8000",
            credential_payload={},
            last_login_state="none",
        )
        self.assertEqual(_missing_platform_account_fields(tp_account, "TP"), [])

    def test_db_safe_image_url_never_persists_signed_query_url(self) -> None:
        long_signed_url = "https://example.com/idcard/a.jpg?authorization=" + ("x" * 900)

        safe_url = _db_safe_image_url(
            image={"url": long_signed_url, "preview_url": long_signed_url},
            storage_key="idcard/a.jpg",
        )

        self.assertLessEqual(len(safe_url), 512)
        self.assertNotIn("authorization=", safe_url)
        self.assertNotIn("?", safe_url)
        self.assertTrue(safe_url.endswith("/idcard/a.jpg"))


class QuoteAssistantCredentialFlowTests(IsolatedAsyncioTestCase):
    async def test_credential_only_without_platform_prompts_for_platform(self) -> None:
        db = SimpleNamespace(commit=AsyncMock())
        ctx = {"current_user_id": 7, "session_id": "quote-session-1"}
        text = "登录手机号 13800138000 账号 tp_account 密码 pa-123"

        with patch.object(qas, "_latest_active_case", new=AsyncMock(return_value=None)), patch.object(
            qas, "_save_platform_credentials", new=AsyncMock()
        ) as save_mock:
            reply, meta = await handle_platform_credential_message(db, ctx=ctx, entities={}, text=text)

        self.assertIn("哪家平台", reply)
        self.assertIn("平台名称", meta["data"]["message"])
        self.assertEqual(meta["data"]["result_status"], RESULT_NEED_MORE)
        save_mock.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_explicit_platform_credentials_are_saved(self) -> None:
        db = SimpleNamespace(commit=AsyncMock())
        ctx = {"current_user_id": 7, "session_id": "quote-session-1"}
        account = QuotePlatformAccount(
            owner_user_id=7,
            platform_code="TP",
            platform_name="太平洋",
            login_phone="13800138000",
            login_phone_mask="138****8000",
            account_username="abc",
            password_ciphertext="cipher",
            credential_payload={},
            last_login_state="none",
        )

        with patch.object(qas, "_latest_active_case", new=AsyncMock(return_value=None)), patch.object(
            qas, "_save_platform_credentials", new=AsyncMock(return_value=account)
        ) as save_mock:
            reply, meta = await handle_platform_credential_message(
                db,
                ctx=ctx,
                entities={},
                text="太平洋登录手机号 13800138000 账号 abc 密码 pass123",
            )

        self.assertIn("太平洋", reply)
        self.assertEqual(meta["data"]["result_status"], RESULT_SUCCESS)
        self.assertEqual(meta["data"]["payload"]["platform_account"]["platform_code"], "TP")
        self.assertEqual(meta["data"]["payload"]["platform_account"]["login_phone_mask"], "138****8000")
        self.assertTrue(meta["data"]["payload"]["platform_account"]["has_password"])
        save_mock.assert_awaited_once()
        db.commit.assert_awaited_once()

    async def test_platform_account_form_saves_by_schema_without_plaintext_public_payload(self) -> None:
        db = SimpleNamespace(flush=AsyncMock())
        account = QuotePlatformAccount(
            owner_user_id=7,
            platform_code="TP",
            platform_name="太平洋",
            login_phone="13800138000",
            login_phone_mask="138****8000",
            account_username="abc",
            password_ciphertext="cipher",
            credential_payload={},
            last_login_state="none",
        )

        with patch.object(qas, "_save_platform_credentials", new=AsyncMock(return_value=account)) as save_mock:
            saved = await save_platform_account_form(
                db,
                owner_user_id=7,
                platform_code="TP",
                values={
                    "login_phone": "13800138000",
                    "account_username": "abc",
                    "account_password": "pass123",
                },
            )

        self.assertIs(saved, account)
        self.assertEqual(saved.credential_payload["configured_fields"], ["login_phone", "account_username", "account_password"])
        self.assertNotIn("pass123", str(saved.credential_payload))
        save_mock.assert_awaited_once()
        db.flush.assert_awaited_once()
