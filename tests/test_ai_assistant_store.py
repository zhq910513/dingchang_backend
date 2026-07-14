# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import app.services.ai_assistant_service as aas
from app.services.ai_assistant_service import (
    _Store,
    _detect_intent,
    _order_field_lines,
    _order_list_style_lines,
    _order_multi_summary_lines,
    _order_payload_from_order,
    _safe_metadata_for_history,
)
from app.services.quote_assistant_service import _normalize_form_credentials


class AiAssistantStoreTests(TestCase):
    def test_history_pagination_and_delete_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"STORAGE_DIR": tmpdir}):
            store = _Store()
            session = store.create_session(owner_user_id="7")
            session_id = session["session_id"]

            for i in range(10):
                store.append_message(
                    owner_user_id="7",
                    session_id=session_id,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"msg-{i}",
                    metadata={"idx": i},
                )

            first_page = store.list_messages(owner_user_id="7", session_id=session_id, limit=3)
            self.assertEqual([m["content"] for m in first_page["items"]], ["msg-7", "msg-8", "msg-9"])
            self.assertTrue(first_page["has_more"])

            second_page = store.list_messages(
                owner_user_id="7",
                session_id=session_id,
                cursor=first_page["next_cursor"],
                limit=5,
            )
            self.assertEqual([m["content"] for m in second_page["items"]], ["msg-2", "msg-3", "msg-4", "msg-5", "msg-6"])
            self.assertTrue(second_page["has_more"])

            third_page = store.list_messages(
                owner_user_id="7",
                session_id=session_id,
                cursor=second_page["next_cursor"],
                limit=5,
            )
            self.assertEqual([m["content"] for m in third_page["items"]], ["msg-0", "msg-1"])
            self.assertFalse(third_page["has_more"])

            self.assertTrue(store.delete_session(owner_user_id="7", session_id=session_id))
            self.assertEqual(store.list_sessions(owner_user_id="7"), [])
            self.assertIsNone(store.get_session(owner_user_id="7", session_id=session_id))
            with self.assertRaises(ValueError):
                store.list_messages(owner_user_id="7", session_id=session_id)

    def test_multiple_store_instances_reload_latest_file_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"STORAGE_DIR": tmpdir}):
            store_a = _Store()
            store_b = _Store()
            session = store_a.create_session(owner_user_id="7")
            session_id = session["session_id"]

            store_b.append_message(owner_user_id="7", session_id=session_id, role="user", content="from-b")
            store_a.append_message(owner_user_id="7", session_id=session_id, role="assistant", content="from-a")

            page = store_b.list_messages(owner_user_id="7", session_id=session_id, limit=5)
            self.assertEqual([m["content"] for m in page["items"]], ["from-b", "from-a"])

    def test_session_list_includes_preview_and_message_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"STORAGE_DIR": tmpdir}):
            store = _Store()
            session = store.create_session(owner_user_id="7")
            session_id = session["session_id"]

            store.append_message(
                owner_user_id="7",
                session_id=session_id,
                role="user",
                content="太平洋登录手机号 138****8000 账号 abc 密码[已隐藏]",
            )
            store.append_message(
                owner_user_id="7",
                session_id=session_id,
                role="assistant",
                content="平台登录资料已保存",
            )

            rows = store.list_sessions(owner_user_id="7")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["message_count"], 2)
            self.assertEqual(rows[0]["last_message_preview"], "平台登录资料已保存")
            self.assertIn("created_at", rows[0])

    def test_recall_images_marks_user_and_assistant_image_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"STORAGE_DIR": tmpdir}):
            store = _Store()
            session = store.create_session(owner_user_id="7")
            session_id = session["session_id"]
            image = {"storage_key": "related/md5/demo.jpg", "url": "http://local/demo.jpg"}

            store.append_message(
                owner_user_id="7",
                session_id=session_id,
                role="user",
                content="已上传1张图片",
                metadata={"page_context": {"images": [dict(image)], "uploaded_images": [dict(image)]}},
            )
            store.append_message(
                owner_user_id="7",
                session_id=session_id,
                role="assistant",
                content="已归位图片",
                metadata={"data": {"payload": {"attached_images": [dict(image)]}}},
            )

            result = store.recall_images(
                owner_user_id="7",
                session_id=session_id,
                storage_keys=["related/md5/demo.jpg"],
            )

            self.assertEqual(result["updated_messages"], 2)
            self.assertEqual(result["updated_images"], 3)
            page = store.list_messages(owner_user_id="7", session_id=session_id, limit=5)
            user_img = page["items"][0]["metadata"]["page_context"]["images"][0]
            assistant_img = page["items"][1]["metadata"]["data"]["payload"]["attached_images"][0]
            self.assertTrue(user_img["recalled"])
            self.assertTrue(assistant_img["recalled"])

    def test_history_metadata_strips_signed_urls_and_sensitive_context(self) -> None:
        signed = "https://dingchang.fwh.bcebos.com/backup/a/b.jpg?authorization=abc&x-bce-security-token=secret"
        meta = {
            "page_context": {
                "current_user_id": 7,
                "role_name": "sales",
                "team_names": ["一队"],
                "uploaded_images": [{"storage_key": "backup/a/b.jpg", "url": signed}],
            },
            "data": {
                "payload": {
                    "attached_images": [{"storage_key": "backup/a/b.jpg", "preview_url": signed}],
                    "authorization": "abc",
                    "x-bce-security-token": "secret",
                }
            },
        }

        safe = _safe_metadata_for_history(meta)
        text = str(safe)

        self.assertNotIn("authorization=", text)
        self.assertNotIn("x-bce-security-token", text)
        self.assertNotIn("current_user_id", text)
        self.assertNotIn("role_name", text)
        self.assertNotIn("team_names", text)
        self.assertEqual(
            safe["data"]["payload"]["attached_images"][0]["storage_key"],
            "backup/a/b.jpg",
        )


class AiAssistantSendMessageRedactionTests(IsolatedAsyncioTestCase):
    async def test_waiting_sms_redacts_unlabeled_numeric_code_in_history(self) -> None:
        async def fake_get_db():
            yield SimpleNamespace()

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"STORAGE_DIR": tmpdir}):
            store = _Store()
            with patch.object(aas, "_store", store), patch.object(aas, "get_db", fake_get_db), patch.object(
                aas, "has_waiting_sms_task", new=AsyncMock(return_value=True)
            ), patch.object(aas, "has_expired_waiting_sms_task", new=AsyncMock(return_value=False)), patch.object(
                aas,
                "_dispatch_rule",
                new=AsyncMock(
                    return_value=(
                        "验证码已提交，报价流程继续。",
                        {"status": "success", "intent": "quote", "data": {"result_status": "success"}},
                    )
                ),
            ):
                resp = await aas.send_message(owner_user_id="7", message="123456", context={})

            page = store.list_messages(owner_user_id="7", session_id=resp["session_id"], limit=5)
            user_messages = [m["content"] for m in page["items"] if m["role"] == "user"]

            self.assertEqual(user_messages, ["[短信验证码已隐藏]"])

    async def test_numeric_order_like_text_is_preserved_when_not_waiting_sms(self) -> None:
        async def fake_get_db():
            yield SimpleNamespace()

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"STORAGE_DIR": tmpdir}):
            store = _Store()
            with patch.object(aas, "_store", store), patch.object(aas, "get_db", fake_get_db), patch.object(
                aas, "has_waiting_sms_task", new=AsyncMock(return_value=False)
            ), patch.object(aas, "has_expired_waiting_sms_task", new=AsyncMock(return_value=False)), patch.object(
                aas,
                "_dispatch_rule",
                new=AsyncMock(
                    return_value=(
                        "请补充订单查询条件。",
                        {"status": "success", "intent": "fallback", "data": {"result_status": "invalid_command"}},
                    )
                ),
            ):
                resp = await aas.send_message(owner_user_id="7", message="5577", context={})

            page = store.list_messages(owner_user_id="7", session_id=resp["session_id"], limit=5)
            user_messages = [m["content"] for m in page["items"] if m["role"] == "user"]

            self.assertEqual(user_messages, ["5577"])


class AiAssistantOrderQueryReplyTests(TestCase):
    def _fake_order(self, order_id: int, owner_name: str = "张三", plate_no: str = "赣B10001"):
        return SimpleNamespace(
            id=order_id,
            created_at="2026-05-05 10:20:30",
            is_finished=True,
            is_paid=True,
            is_rebate=False,
            dynamic_data={
                "owner_name": owner_name,
                "plate_no": plate_no,
                "vin": f"VIN{order_id:014d}"[-17:],
                "engine_no": f"ENG{order_id}",
                "vehicle_model": "测试车型",
                "first_register_date": "20240102",
                "id_number": "360100199001011234",
            },
            ocr_raw_json={},
            order_info=SimpleNamespace(
                insurance_expire_date="20260506",
                owner_phone="13800138000",
                commercial_amount=Decimal("1000"),
                compulsory_amount=Decimal("200"),
                vehicle_tax_amount=Decimal("30"),
                non_vehicle_amount=Decimal("4.5"),
                premium_total=Decimal("1234.5"),
                channel_total=Decimal("150.2"),
                customer_total=Decimal("80.1"),
                profit=Decimal("70.1"),
                remark="",
            ),
            finance_record=None,
            images=[],
            salesperson=SimpleNamespace(
                real_name="业务员A",
                username="sales_a",
                team_names="一队",
                parent=SimpleNamespace(real_name="经理A", username="mgr_a"),
            ),
            customer_group=SimpleNamespace(customer_name="客户A", market="车险"),
            channel_group=SimpleNamespace(channel_name="渠道A"),
        )

    def test_loose_name_plus_field_is_order_query(self) -> None:
        intent, _, entities = _detect_intent("张三 车牌号")

        self.assertEqual(intent, "query_order")
        self.assertEqual(entities.get("owner_name"), "张三")
        self.assertEqual(entities.get("query_fields"), ["plate_no"])

    def test_loose_order_query_with_owner_name_is_order_query(self) -> None:
        intent, _, entities = _detect_intent("查订单 张三")

        self.assertEqual(intent, "query_order")
        self.assertEqual(entities.get("owner_name"), "张三")

    def test_loose_order_query_supports_company_owner_name(self) -> None:
        intent, _, entities = _detect_intent("查订单 上高县金轮汽车运输有限公司")

        self.assertEqual(intent, "query_order")
        self.assertEqual(entities.get("owner_name"), "上高县金轮汽车运输有限公司")

    def test_order_phone_query_does_not_treat_phone_as_owner_name(self) -> None:
        intent, _, entities = _detect_intent("查订单 18162267199")

        self.assertEqual(intent, "query_order")
        self.assertEqual(entities.get("owner_phone"), "18162267199")
        self.assertNotIn("owner_name", entities)

    def test_quote_command_does_not_add_order_query_fields(self) -> None:
        intent, _, entities = _detect_intent("人保报价")

        self.assertEqual(intent, "quote")
        self.assertNotIn("query_fields", entities)

    def test_single_order_explicit_field_returns_only_requested_chinese_field(self) -> None:
        order = self._fake_order(101)
        payload = _order_payload_from_order(order)

        lines = _order_field_lines(order, payload, ["plate_no"])

        self.assertEqual(lines, ["车牌号：赣B10001"])
        self.assertNotIn("dynamic_data", "\n".join(lines))

    def test_single_order_default_uses_order_list_style_chinese_labels(self) -> None:
        order = self._fake_order(101)
        payload = _order_payload_from_order(order)

        text = "\n".join(_order_list_style_lines(order, payload))

        self.assertIn("订单号：101", text)
        self.assertIn("车主：张三", text)
        self.assertIn("保费金额：1234.50", text)
        self.assertIn("是否回款：是", text)
        self.assertNotIn("dynamic_data", text)
        self.assertNotIn("order_info", text)

    def test_multiple_orders_are_condensed_and_do_not_dump_raw_payload(self) -> None:
        orders = [self._fake_order(i, owner_name="张三", plate_no=f"赣B10{i:03d}") for i in range(101, 107)]

        lines, display_rows = _order_multi_summary_lines(orders, query_fields=[], truncated=True)
        text = "\n".join(lines)

        self.assertEqual(len(display_rows), 5)
        self.assertIn("找到超过5条匹配订单", text)
        self.assertIn("订单 101", text)
        self.assertIn("客户/渠道/业务员", text)
        self.assertIn("车主/车牌/车型", text)
        self.assertIn("金额：保费", text)
        self.assertIn("结果超过5条", text)
        self.assertNotIn("dynamic_data", text)
        self.assertNotIn("order_info", text)


class QuoteAssistantCredentialTests(TestCase):
    def test_existing_required_platform_credentials_can_be_reused_when_form_blank(self) -> None:
        existing = SimpleNamespace(
            login_phone="13800138000",
            account_username="picc_user",
            password_ciphertext="encrypted-password",
            credential_payload={"saved_extra_fields": [], "configured_fields": []},
        )

        code, _, credentials, extra_public, extra_secret, configured = _normalize_form_credentials(
            platform_code="PICC",
            platform_name=None,
            values={},
            existing_account=existing,
        )

        self.assertEqual(code, "PICC")
        self.assertEqual(credentials, {})
        self.assertEqual(extra_public, {})
        self.assertEqual(extra_secret, {})
        self.assertEqual(set(configured), {"login_phone", "account_username", "account_password"})
