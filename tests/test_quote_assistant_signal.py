# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

import app.services.quote_assistant_service as qas
from app.models.quote_assistant import QuotePlatformAccount
from app.services.quote_assistant_service import (
    RESULT_NEED_MORE,
    RESULT_NOT_READY,
    RESULT_SUCCESS,
    _cancel_waiting_tasks_for_case,
    _classify_image_with_optional_ocr,
    _db_safe_image_url,
    _missing_platform_account_fields,
    _start_sms_task,
    detect_platform_credential_signal,
    detect_quote_signal,
    extract_quote_fields,
    handle_quote_message,
    handle_platform_credential_message,
    list_platform_account_schemas,
    redact_quote_sensitive_text,
    save_platform_account_form,
)
from app.services.image_slot_classifier import classify_image_slot


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

    def test_redact_sms_code_keeps_chat_history_safe_when_waiting_sms(self) -> None:
        self.assertEqual(
            redact_quote_sensitive_text("123456", hide_unlabeled_sms_code=True),
            "[短信验证码已隐藏]",
        )
        self.assertEqual(redact_quote_sensitive_text("验证码：123456"), "验证码：[已隐藏]")
        self.assertEqual(redact_quote_sensitive_text("5577"), "5577")

    def test_owner_phone_is_not_saved_as_platform_login_phone(self) -> None:
        text = (
            "太平洋报价 车主:张三 车主手机号:13900000001 "
            "车牌号:赣B12345 VIN:LSVFA49J2A1234567 发动机号:ENG12345 车型:大众朗逸"
        )

        quote_signal = detect_quote_signal(text)
        credential_signal = detect_platform_credential_signal(text)

        self.assertEqual(quote_signal["entities"]["owner_phone"], "13900000001")
        self.assertNotIn("login_phone", credential_signal["credentials"])
        self.assertFalse(credential_signal["is_credential"])

    def test_extract_quote_fields_handles_compound_owner_name_label(self) -> None:
        fields = extract_quote_fields(
            "太平洋报价 车主姓名:报价链路测试 车主手机号:13900000001 "
            "车牌号:赣A12345 VIN:LSVFA49J2A1234567 发动机号:ENG12345 车型:大众朗逸"
        )

        self.assertEqual(fields["owner_name"], "报价链路测试")
        self.assertEqual(fields["owner_phone"], "13900000001")
        self.assertEqual(fields["plate_no"], "赣A12345")
        self.assertEqual(fields["vehicle_model"], "大众朗逸")

    def test_extract_quote_fields_does_not_treat_owner_phone_label_as_owner_name(self) -> None:
        fields = extract_quote_fields("太平洋报价 车主手机号:13900000001 车牌号:赣A12345")

        self.assertNotIn("owner_name", fields)
        self.assertEqual(fields["owner_phone"], "13900000001")

    def test_extract_quote_fields_does_not_treat_insured_phone_label_as_owner_name(self) -> None:
        fields = extract_quote_fields("太平洋报价 被保险人手机号:13900000001 车牌号:赣A12345")

        self.assertNotIn("owner_name", fields)
        self.assertEqual(fields["owner_phone"], "13900000001")

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

    def test_image_context_hint_places_image_into_correct_slot(self) -> None:
        front = classify_image_slot(
            provided_slot_key="related",
            original_name="demo.jpg",
            storage_key="related/demo.jpg",
            ocr_text="这张是身份证正面",
        )
        sub = classify_image_slot(
            provided_slot_key="related",
            original_name="demo.jpg",
            storage_key="related/demo2.jpg",
            ocr_text="这是行驶证副页照片",
        )

        self.assertEqual(front.predicted_slot_key, "idcard_front")
        self.assertEqual(front.method, "context_hint_rule")
        self.assertGreaterEqual(front.confidence, 0.78)
        self.assertEqual(sub.predicted_slot_key, "driving_license_sub")

    def test_strong_filename_hint_skips_slow_ocr_path(self) -> None:
        front = classify_image_slot(
            provided_slot_key="related",
            original_name="id-front.jpg",
            storage_key="related/id-front.jpg",
        )
        cert = classify_image_slot(
            provided_slot_key="related",
            original_name="vehicle-cert.jpg",
            storage_key="related/vehicle-cert.jpg",
        )

        self.assertEqual(front.predicted_slot_key, "idcard_front")
        self.assertEqual(front.method, "strong_filename_rule")
        self.assertGreaterEqual(front.confidence, 0.78)
        self.assertEqual(cert.predicted_slot_key, "vehicle_cert")


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class QuoteAssistantWaitingTaskTests(IsolatedAsyncioTestCase):
    async def test_weak_context_hint_does_not_block_real_ocr_classification(self) -> None:
        raw = {"words_result": [{"words": "机动车行驶证 号牌号码 车辆类型 所有人 品牌型号 发动机号码"}]}

        with patch.object(qas, "call_ocr", Mock(return_value=raw)), patch.object(
            qas, "_extract_by_type", Mock(return_value={})
        ):
            classification, ocr_raw, _ = await _classify_image_with_optional_ocr(
                image={
                    "storage_key": "related/demo.jpg",
                    "url": "https://example.com/demo.jpg",
                    "context_hint": "张三资料",
                    "ocr_text": "张三资料",
                },
                provided_slot="related",
                storage_key="related/demo.jpg",
            )

        self.assertEqual(classification.predicted_slot_key, "driving_license_main")
        self.assertEqual(classification.method, "ocr_rule")
        self.assertEqual(ocr_raw, raw)

    async def test_repeated_quote_command_while_waiting_sms_only_prompts_for_code(self) -> None:
        db = SimpleNamespace(commit=AsyncMock())
        case = SimpleNamespace(
            id=11,
            case_no="QA202605170001",
            status="waiting_sms",
            platform_code="TP",
            platform_name="太平洋",
            order_id=5571,
        )
        task = SimpleNamespace(
            id=21,
            status="waiting_sms",
            login_state="sms_required",
            sms_phone_mask="138****8000",
            trace_id="trace-waiting",
        )

        with patch.object(qas, "_find_waiting_task", new=AsyncMock(return_value=(case, task))), patch.object(
            qas, "_add_event", new=AsyncMock()
        ):
            reply, meta = await handle_quote_message(
                db,
                ctx={"current_user_id": 7, "session_id": "quote-session-1"},
                entities={},
                text="太平洋报价",
            )

        self.assertIn("等待短信验证码", reply)
        self.assertEqual(meta["data"]["result_status"], RESULT_NOT_READY)
        self.assertEqual(meta["data"]["payload"]["quote_task"]["id"], 21)
        db.commit.assert_awaited_once()

    async def test_start_sms_task_expires_old_waiting_task_before_creating_new_one(self) -> None:
        expired = SimpleNamespace(
            id=31,
            status="waiting_sms",
            login_state="sms_required",
            error_detail=None,
            started_at=qas._now() - timedelta(seconds=qas.QUOTE_SMS_CODE_TTL_SECONDS + 5),
            created_at=None,
            finished_at=None,
            updated_at=None,
        )
        db = SimpleNamespace(
            execute=AsyncMock(return_value=_ScalarResult([expired])),
            flush=AsyncMock(),
            add=Mock(),
        )
        case = SimpleNamespace(
            id=12,
            platform_code="TP",
            platform_name="太平洋",
            status="ready",
            current_task_id=31,
            updated_at=None,
        )
        account = QuotePlatformAccount(
            id=41,
            owner_user_id=7,
            platform_code="TP",
            platform_name="太平洋",
            login_phone="13800138000",
            login_phone_mask="138****8000",
            credential_payload={},
            last_login_state="none",
        )

        with patch.object(qas, "_mark_platform_account_used", new=AsyncMock()), patch.object(
            qas, "_add_event", new=AsyncMock()
        ):
            task = await _start_sms_task(
                db,
                case=case,
                owner_user_id=7,
                snapshot={"normalized_data": {"plate_no": "赣A12345"}},
                trace_id="trace-new",
                platform_account=account,
            )

        self.assertIsNot(task, expired)
        self.assertEqual(expired.status, "failed")
        self.assertEqual(expired.login_state, "failed")
        self.assertEqual(expired.error_detail, "sms_code_expired")
        self.assertEqual(case.status, "waiting_sms")
        db.add.assert_called_once()

    async def test_material_change_cancels_waiting_sms_task(self) -> None:
        task = SimpleNamespace(
            id=51,
            status="waiting_sms",
            login_state="sms_required",
            error_detail=None,
            finished_at=None,
            updated_at=None,
        )
        db = SimpleNamespace(execute=AsyncMock(return_value=_ScalarResult([task])))
        case = SimpleNamespace(id=13, current_task_id=51, updated_at=None)

        cancelled = await _cancel_waiting_tasks_for_case(
            db,
            case=case,
            reason="cancelled_by_material_change",
        )

        self.assertEqual(cancelled, 1)
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(task.login_state, "failed")
        self.assertEqual(task.error_detail, "cancelled_by_material_change")
        self.assertIsNone(case.current_task_id)


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
