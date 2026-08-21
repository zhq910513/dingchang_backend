# -*- coding: utf-8 -*-
"""Offline regression checks for the quote assistant's PICC state transitions."""

from __future__ import annotations

import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from types import MethodType, SimpleNamespace

from sqlalchemy.exc import IntegrityError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.quote_assistant_service import (
    CASE_STATUS_READY,
    CASE_STATUS_WAITING_SMS,
    FAILURE_CODE_ACCOUNT_LOGIN,
    FAILURE_CODE_ACCOUNT_MISSING,
    FAILURE_CODE_DEFAULT_CONFIG_CHANGED,
    FAILURE_CODE_DEFAULT_CONFIG_MISSING,
    FAILURE_CODE_MATERIAL_CHANGED,
    FAILURE_CODE_MATERIAL_MISSING,
    FAILURE_CODE_PLATFORM,
    FAILURE_CODE_RESULT_MATERIALIZATION,
    FAILURE_CODE_SESSION_EXPIRED,
    FAILURE_CODE_STALE_TIMEOUT,
    QUOTE_CHAT_POLARITY_AFFIRM,
    QUOTE_CHAT_POLARITY_NEGATE,
    QUOTE_DUPLICATE_CONFIRM_HINT,
    QUOTE_FLOW_NORMAL,
    QUOTE_FLOW_RENEWAL,
    QUOTE_RUNNING_TASK_STALE_SECONDS,
    QUOTE_SMS_CODE_TTL_SECONDS,
    RESULT_FAILED,
    RESULT_NEED_MORE,
    RESULT_NOT_READY,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_WAITING_DUPLICATE_CONFIRM,
    TASK_STATUS_WAITING_SMS,
    _align_case_status_with_running_quote_task,
    _attach_quote_failure,
    _auto_renewal_probe_fallthrough_response,
    _build_quote_preflight_blocked_response,
    _build_quote_user_failure_response,
    _cancel_orphaned_waiting_duplicate_confirm_tasks,
    _extract_quote_product_exclusions,
    _failure_code_for_platform_dialog_subtype,
    _format_quote_preflight_reply,
    _is_duplicate_quote_cancel_text,
    _is_duplicate_quote_confirmation_text,
    _is_runtime_duplicate_quote_result,
    _is_silent_auto_renewal_not_found_response,
    _is_sms_task_expired,
    _looks_like_unclear_chat_polarity_attempt,
    _mark_quote_task_cancelled,
    _material_preflight_items,
    _mk_data,
    _primary_preflight_failure_code,
    _quote_chat_polarity_exact,
    _quote_failure_fields,
    _quote_task_is_sms_wait,
    _runtime_detail,
    _active_image_extracted_data,
    _quote_car_name_from_features,
    _quote_end_date_text,
    _quote_image_extracted_fields_from_features,
    _quote_sales_model_hint_from_model_text,
    _merge_quote_extracted_prefer,
    _backfill_quote_sales_model_fields,
    _quote_auto_notice_message_id,
    _quote_snapshot_with_auto_adjusted_dates,
    _quote_result_insurance_date_auto_adjustments,
    _quote_result_reply_text,
    _quote_auto_notice_already_persisted,
    _quote_auto_notice_dedupe_key,
    _is_quote_auto_notice_duplicate_error,
    _has_reusable_renewal_quote_context,
    _missing_requirements_for_quote_flow,
    _normalize_quote_case_data,
    _platform_default_values_with_legacy_fixes,
    _picc_existing_proposal_table_card_for_display,
    _picc_result_coverage_items_for_display,
    _quote_task_is_stale,
    _quote_task_stale_base_time,
    _should_auto_probe_renewal_before_normal_quote,
    quote_message_may_interrupt_running_task,
    _normalize_quote_product_exclusions,
    extract_quote_config_overrides,
)
from app.services.quote_platforms.platforms.picc.presentation import (
    picc_result_amount_text,
    picc_result_kind_name,
)
from app.services.quote_result_image import _proposal_info_rows, save_quote_result_card_image
from app.services.ai_assistant_service import (
    _humanize_exception,
    _message_preview_text,
    _quote_result_needs_async_image,
    _session_preview_needs_recompute,
    _quote_result_mark_async_image_failed,
    _reschedule_pending_quote_result_images_from_page,
    _schedule_async_quote_result_image_completion_once,
)
from app.api.v1.ai_assistant import _chat_context
from app.services.ocr_cleaner import clean_dynamic_data_for_ocr, correct_vehicle_cert_field
from app.services.quote_platforms.base import PlatformRuntimeResult
from app.services.quote_platforms.platforms.picc.business import (
    PiccBusinessAdapter,
    PiccBusinessRequestError,
    PiccDuplicateQuoteError,
    _contains_duplicate_quote,
    _duplicate_quote_notice_from_success_dialog,
    _end_date_text,
    _format_reinsure_items_prompt,
    _insurance_date_error_adjustment_kinds,
    _insurance_date_adjustment_needed,
    _insurance_date_adjustment_target_day,
    _proposal_start_datetime_from_quote_response,
    _quote_form_kind_index,
    _reinsure_notice_adjustment_kinds,
    _reinsure_notice_suggested_start_date,
    _picc_encrypt_renewal_policy_no,
    _renewal_candidate_score,
    _pick_renewal_policy_candidate,
    _vehicle_brand_prefix,
    _vehicle_candidate_score,
    _apply_vehicle_model_seed_hints,
    _vehicle_model_hint_is_usable,
    _vehicle_model_seed_terms,
    _vehicle_model_resolution_failure_message,
    _vehicle_query_resource_codes,
    _vehicle_rows_correlated_to_vin,
    _used_fuel_model_query_terms,
)


def _adapter() -> PiccBusinessAdapter:
    # The tested method only touches the client when a selected vehicle exists.
    return object.__new__(PiccBusinessAdapter)


class PiccDynamicResultPresentationTests(unittest.TestCase):
    def test_platform_energy_names_are_preserved_and_missing_names_use_energy_fallback(self) -> None:
        self.assertEqual(
            picc_result_kind_name(
                "051050",
                platform_name="新能源汽车损失保险",
                is_new_energy=True,
            ),
            "新能源汽车损失保险",
        )
        self.assertEqual(
            picc_result_kind_name("051063", is_new_energy=True),
            "附加医保外医疗费用责任险（新能源汽车第三者责任保险）",
        )
        self.assertEqual(
            picc_result_kind_name("051051", is_new_energy=False),
            "机动车第三者责任保险",
        )

    def test_unknown_dynamic_kind_is_not_replaced_by_a_fixed_motor_label(self) -> None:
        self.assertEqual(
            picc_result_kind_name(
                "099999",
                platform_name="电池专项增值服务",
                is_new_energy=True,
            ),
            "电池专项增值服务",
        )

    def test_road_rescue_quantity_is_rendered_as_service_times(self) -> None:
        self.assertEqual(
            picc_result_amount_text(
                {
                    "code": "051064",
                    "name": "附加机动车增值服务特约条款（道路救援服务）",
                    "quantity": "7",
                    "amount_text": "-",
                }
            ),
            "7次",
        )

    def test_display_rows_keep_platform_order_dynamic_names_and_quantity(self) -> None:
        rows = _picc_result_coverage_items_for_display(
            {
                "vehicle_energy_type": "new_energy",
                "coverage_items": [
                    {
                        "code": "051050",
                        "name": "机动车损失保险",
                        "amount": "219800",
                        "premium": "3098.85",
                    },
                    {
                        "code": "051064",
                        "platform_name": "道路救援服务",
                        "quantity": "5",
                        "premium": "0",
                    },
                ],
            },
            seat_count="5",
        )
        self.assertEqual([row["name"] for row in rows], ["新能源汽车损失保险", "道路救援服务"])
        self.assertEqual(rows[1]["amount_text"], "5次")
        self.assertEqual(rows[1]["quantity"], "5")

    def test_existing_proposal_table_keeps_card_coverage_rows_when_result_top_level_is_empty(self) -> None:
        card = {
            "style": "picc_proposal_table",
            "vehicle_energy_type": "fuel",
            "coverage_items": [
                {
                    "code": "051050",
                    "name": "机动车损失保险",
                    "amount": "155900",
                    "premium": "2477.15",
                },
                {
                    "code": "051064",
                    "name": "附加机动车增值服务特约条款（道路救援服务）",
                    "quantity": "7",
                    "premium": "0.00",
                },
            ],
            "proposal_info": {"plate_no": "赣A12345", "vin": "LGXTEST0000000001"},
        }
        result = {
            "vehicle_energy_type": "fuel",
            "request_body": {"vehicleForm": {"seatCount": "5"}, "quoteForm": {}},
            "quote_provenance": {"normalized_amounts": {}},
        }
        display_card = _picc_existing_proposal_table_card_for_display(result, card)
        rows = display_card["proposal_coverage_items"]
        self.assertEqual([row["name"] for row in rows], ["机动车损失保险", "附加机动车增值服务特约条款（道路救援服务）"])
        self.assertEqual(rows[1]["amount_text"], "7次")

    def test_proposal_info_rows_keep_legacy_field_order(self) -> None:
        rows = _proposal_info_rows(
            {
                "proposal_info": {
                    "insured_name": "杨响",
                    "plate_no": "048407",
                    "engine_no": "8A6048407",
                    "vin": "LGXCH4CD6T0353958",
                    "vehicle_type": "A01-客车",
                    "vehicle_usage": "21-家庭自用汽车",
                    "vehicle_model": "比亚迪BYD6480AMBE",
                    "model_match_method": "目录关键词",
                    "enroll_date": "2026-08-19",
                    "ton_count": "0千克",
                    "seat_count": "5人",
                    "purchase_price": "144900元",
                    "claim_summary": "商业险连续承保年数0年",
                    "bi_start_date": "2026-08-20 00:00",
                    "ci_start_date": "2026-08-20 00:00",
                }
            }
        )
        self.assertEqual(rows[3], ("车辆型号", "比亚迪BYD6480AMBE", "初登日期", "2026-08-19"))
        self.assertEqual(rows[4], ("核定载质量", "0千克", "核定载客量(包括司机)", "5人"))
        self.assertEqual(rows[5][0], "新车购置价")
        self.assertEqual(rows[6][0], "商业险起保日期")

    def test_picc_result_builder_wires_energy_names_and_road_rescue_quantity(self) -> None:
        result = _adapter()._build_motor_quote_result_from_response(
            ctx=None,
            quote_payload={},
            request_body={
                "accountTypeName": "新能源车-新",
                "vehicleForm": {"seatCount": "5"},
                "ownerForm": {},
                "quoteForm": {},
                "preflight": {},
            },
            quote_response={
                "status": 0,
                "data": {"biPremium": "100.00", "ciPremium": "0.00", "sumPayTax": "0.00"},
                "itemKindTempList": [
                    {"kindCode": "051050", "kindName": "", "amount": "219800", "premium": "100.00"},
                    {
                        "kindCode": "051064",
                        "kindName": "附加机动车增值服务特约条款（道路救援服务）",
                        "quantity": "7",
                        "premium": "0.00",
                    },
                ],
            },
        )
        self.assertEqual(result["vehicle_energy_type"], "new_energy")
        rows = result["result_card"]["proposal_coverage_items"]
        self.assertEqual(rows[0]["name"], "新能源汽车损失保险")
        self.assertEqual(rows[1]["amount_text"], "7次")


class RunningQuoteInterruptionTests(unittest.TestCase):
    def test_chat_context_uses_the_request_session_id(self) -> None:
        context = _chat_context(
            SimpleNamespace(
                user=SimpleNamespace(id=7),
                primary_role="super_admin",
                team_names=(),
            ),
            SimpleNamespace(
                context={"session_id": "forged-session"},
                images=[],
                order_id=None,
                session_id="real-session",
            ),
        )
        self.assertEqual(context["session_id"], "real-session")
        self.assertEqual(context["current_user_id"], 7)

    def test_only_snapshot_changing_messages_bypass_running_quote_serialization(self) -> None:
        self.assertFalse(quote_message_may_interrupt_running_task("今天天气怎么样", {}))
        self.assertTrue(quote_message_may_interrupt_running_task("人保报价", {}))
        self.assertTrue(quote_message_may_interrupt_running_task("三者改成500万", {}))
        self.assertTrue(quote_message_may_interrupt_running_task("过户车", {}))

    def test_uploaded_images_and_material_form_submission_interrupt_running_quote(self) -> None:
        image = {"storage_key": "related/example.jpg", "md5": "a" * 32}
        self.assertTrue(quote_message_may_interrupt_running_task("图片已提交", {"images": [image]}))
        self.assertTrue(
            quote_message_may_interrupt_running_task(
                "已提交补充资料",
                {"page_context": {"quote_material_form_submit": True}},
            )
        )


class AutoRenewalProbeFallthroughTests(unittest.TestCase):
    def test_auto_probe_only_for_picc_used_car_normal_flow(self) -> None:
        data = {"plate_no": "赣A12345", "engine_no": "ENG001"}
        self.assertTrue(
            _should_auto_probe_renewal_before_normal_quote(
                platform_code="PICC",
                quote_flow_type=QUOTE_FLOW_NORMAL,
                account_type_name="油车-旧",
                normalized_data=data,
            )
        )
        self.assertFalse(
            _should_auto_probe_renewal_before_normal_quote(
                platform_code="PICC",
                quote_flow_type=QUOTE_FLOW_RENEWAL,
                account_type_name="油车-旧",
                normalized_data=data,
            )
        )
        self.assertFalse(
            _should_auto_probe_renewal_before_normal_quote(
                platform_code="PICC",
                quote_flow_type=QUOTE_FLOW_NORMAL,
                account_type_name="油车-新",
                normalized_data=data,
            )
        )

    def test_fallthrough_marker_and_not_found_do_not_steal_normal_quote(self) -> None:
        case = SimpleNamespace(id=11, order_id=None)
        _, fallthrough = _auto_renewal_probe_fallthrough_response(
            case=case,
            trace_id="trace-fallthrough",
            reason="auto_renewal_probe_runtime_failed",
        )
        self.assertTrue(_is_silent_auto_renewal_not_found_response(fallthrough))

        # Without the explicit marker, responses must not trigger fallthrough.
        not_found_without_marker = {
            "status": "success",
            "data": {
                "result_status": RESULT_NOT_READY,
                "message": "没有此车辆信息或不是可续保车辆",
                "payload": {"renewal_lookup": {"found": False}},
            },
        }
        self.assertFalse(_is_silent_auto_renewal_not_found_response(not_found_without_marker))

        probe_failed_without_marker = {
            "status": "failed",
            "data": {
                "result_status": RESULT_FAILED,
                "message": "人保续保查询失败：网络异常",
                "payload": {"renewal_lookup": {"status": "failed"}, "operation": "renewal_lookup"},
            },
        }
        self.assertFalse(_is_silent_auto_renewal_not_found_response(probe_failed_without_marker))

        real_quote_success = {
            "status": "success",
            "data": {
                "result_status": "success",
                "message": "报价成功",
                "payload": {"quote_task": {"status": "success"}},
            },
        }
        self.assertFalse(_is_silent_auto_renewal_not_found_response(real_quote_success))


class StaleRunningTaskClockTests(unittest.TestCase):
    def test_stale_clock_ignores_updated_at_bumps(self) -> None:
        started = datetime(2026, 8, 20, 12, 0, 0)
        updated = started + timedelta(seconds=QUOTE_RUNNING_TASK_STALE_SECONDS + 60)
        task = SimpleNamespace(started_at=started, updated_at=updated, created_at=started)
        self.assertEqual(_quote_task_stale_base_time(task), started)
        self.assertTrue(
            _quote_task_is_stale(
                task,
                now=started + timedelta(seconds=QUOTE_RUNNING_TASK_STALE_SECONDS + 1),
            )
        )


class QuoteCaseTaskAlignmentTests(unittest.IsolatedAsyncioTestCase):
    def test_sms_submit_leaves_waiting_sms_when_task_runs(self) -> None:
        case = SimpleNamespace(status=CASE_STATUS_WAITING_SMS)
        _align_case_status_with_running_quote_task(case)
        self.assertEqual(case.status, CASE_STATUS_READY)

    async def test_mark_cancelled_distinguishes_sms_wait_from_duplicate_wait(self) -> None:
        now = datetime(2026, 8, 20, 12, 0, 0)
        sms_task = SimpleNamespace(
            status=TASK_STATUS_WAITING_SMS,
            login_state="sms_required",
            error_detail=None,
            response_payload={},
            finished_at=None,
            updated_at=None,
        )
        dup_task = SimpleNamespace(
            status=TASK_STATUS_WAITING_DUPLICATE_CONFIRM,
            login_state="authenticated",
            error_detail=None,
            response_payload={"keep": 1},
            finished_at=None,
            updated_at=None,
        )
        self.assertTrue(_quote_task_is_sms_wait(sms_task))
        self.assertFalse(_quote_task_is_sms_wait(dup_task))

        self.assertTrue(
            await _mark_quote_task_cancelled(
                AsyncMock(),
                task=sms_task,
                reason="材料已更新，请重新发起报价",
                now=now,
            )
        )
        self.assertEqual(sms_task.status, TASK_STATUS_CANCELLED)
        self.assertEqual(sms_task.login_state, "failed")

        self.assertFalse(
            await _mark_quote_task_cancelled(
                AsyncMock(),
                task=dup_task,
                reason="遗留重复投保确认已失效",
                now=now,
                response_extra={"orphaned_duplicate_confirm_cancelled": True},
            )
        )
        self.assertEqual(dup_task.status, TASK_STATUS_CANCELLED)
        self.assertEqual(dup_task.login_state, "authenticated")
        self.assertTrue(dup_task.response_payload.get("orphaned_duplicate_confirm_cancelled"))

    def test_sms_task_expiry_uses_started_at(self) -> None:
        started = datetime(2026, 8, 20, 12, 0, 0)
        fresh = SimpleNamespace(started_at=started, created_at=started)
        expired = SimpleNamespace(
            started_at=started - timedelta(seconds=QUOTE_SMS_CODE_TTL_SECONDS + 5),
            created_at=started,
        )
        with patch("app.services.quote_assistant_service._now", return_value=started):
            self.assertFalse(_is_sms_task_expired(fresh))
            self.assertTrue(_is_sms_task_expired(expired))


class OrphanDuplicateConfirmCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancels_task_when_case_no_longer_waiting_duplicate(self) -> None:
        case = SimpleNamespace(
            id=9,
            status=CASE_STATUS_READY,
            current_task_id=77,
            session_id="s1",
            updated_at=None,
        )
        task = SimpleNamespace(
            id=77,
            status=TASK_STATUS_WAITING_DUPLICATE_CONFIRM,
            login_state="authenticated",
            error_detail=None,
            response_payload={},
            finished_at=None,
            updated_at=None,
        )
        result = MagicMock()
        result.all.return_value = [(case, task)]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        db.flush = AsyncMock()

        with patch("app.services.quote_assistant_service._add_event", new_callable=AsyncMock) as add_event:
            cancelled = await _cancel_orphaned_waiting_duplicate_confirm_tasks(
                db,
                owner_user_id=1,
                session_id="s1",
                for_update=True,
            )

        self.assertEqual(cancelled, 1)
        self.assertEqual(task.status, TASK_STATUS_CANCELLED)
        self.assertIsNone(case.current_task_id)
        self.assertTrue(task.response_payload.get("orphaned_duplicate_confirm_cancelled"))
        add_event.assert_awaited()
        db.flush.assert_awaited()

    async def test_skips_when_owner_invalid(self) -> None:
        db = AsyncMock()
        cancelled = await _cancel_orphaned_waiting_duplicate_confirm_tasks(
            db,
            owner_user_id=0,
            session_id="s1",
        )
        self.assertEqual(cancelled, 0)
        db.execute.assert_not_called()


class _QuoteResponseClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.config = SimpleNamespace(base_url="https://picc.test")

    def request_json(self, *args: object, **kwargs: object) -> dict:
        return self.response


class _RecordingClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.config = SimpleNamespace(base_url="https://jiangx.yxgl-picc.cn:41001")

    def request_json(self, *args: object, **kwargs: object) -> dict:
        self.calls.append((args, kwargs))
        return self.response


class _HarRouteClient:
    def __init__(self, routes: dict[str, dict]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.config = SimpleNamespace(base_url="https://jiangx.yxgl-picc.cn:41001")

    def request_json(self, method: str, path: str, **kwargs: object) -> dict:
        self.calls.append((path, kwargs))
        for key, response in self.routes.items():
            if key in path:
                return response
        raise AssertionError(f"Unexpected HAR route: {method} {path}")


def _load_0813_renewal_har() -> dict:
    matches = list(Path(r"D:\HuaweiMoveData\Users\king\Documents").glob("0813*.har"))
    if not matches:
        raise unittest.SkipTest("未找到 0813 正确续保 HAR，跳过 HAR 对齐测试")
    return json.loads(matches[0].read_text(encoding="utf-8-sig"))


def _load_0817_correct_quote_har() -> dict:
    path = Path(r"D:\HuaweiMoveData\Users\king\Documents\0817正确报价.har")
    if not path.exists():
        raise unittest.SkipTest("未找到 0817 正确报价 HAR，跳过 HAR 对齐测试")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_0818_smooth_quote_har() -> dict:
    path = Path(r"D:\HuaweiMoveData\Users\king\Documents\0818最终完整顺畅报价.har")
    if not path.exists():
        raise unittest.SkipTest("未找到 0818 最终完整顺畅报价 HAR，跳过 HAR 对齐测试")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_0818_original_quote_har() -> dict:
    path = Path(r"D:\HuaweiMoveData\Users\king\Documents\0818正确报价.har")
    if not path.exists():
        raise unittest.SkipTest("未找到 0818 正确报价 HAR，跳过 HAR 对齐测试")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _har_response_json(har: dict, entry_index: int) -> dict:
    return json.loads(har["log"]["entries"][entry_index]["response"]["content"].get("text") or "{}")


def _har_form_params(har: dict, entry_index: int) -> dict:
    text = har["log"]["entries"][entry_index]["request"].get("postData", {}).get("text") or ""
    return dict(urllib.parse.parse_qsl(text, keep_blank_values=True))


class QuoteFlowMaterialRequirementRegressionTests(unittest.TestCase):
    def test_renewal_case_uses_lookup_requirements_across_status_entry_points(self) -> None:
        data = {
            "quote_flow_type": "renewal_motor_quote",
            "plate_no": "赣A12345",
            "vin": "LSJEM4092TK037865",
        }

        missing = _missing_requirements_for_quote_flow(
            data,
            {},
            platform_code="PICC",
            account_type_name="油车-旧",
        )

        self.assertEqual(missing, [])

    def test_normal_case_keeps_full_material_requirements(self) -> None:
        data = {
            "quote_flow_type": "normal_motor_quote",
            "plate_no": "赣A12345",
            "vin": "LSJEM4092TK037865",
        }

        missing = _missing_requirements_for_quote_flow(
            data,
            {},
            platform_code="PICC",
            account_type_name="油车-旧",
        )
        missing_keys = {str(item.get("key") or "") for item in missing}

        self.assertIn("engine_no", missing_keys)
        self.assertIn("first_register_date", missing_keys)
        self.assertIn("vehicle_model", missing_keys)
        self.assertIn("owner_name", missing_keys)


class PiccPICCQuoteProfileRegressionTests(unittest.TestCase):
    def test_merged_chinese_brand_model_code_is_not_guessed_as_a_sales_model(self) -> None:
        terms = _used_fuel_model_query_terms(
            "雷克萨斯JTHKR5BH",
            "小型轿车",
            brand_name="",
            vehicle_name="",
            vin="JTHKR5BH3J2327186",
        )
        self.assertIn("JTHKR5BH", terms)
        self.assertNotIn("CT200h", terms)
        self.assertEqual(terms[-1], "JTHKR5BH")

    def test_real_alphanumeric_model_hints_are_usable_but_vin_prefix_is_not(self) -> None:
        vehicle = {"vin": "JTHKR5BH3J2327186"}
        for model in ("CT200h", "ES300h", "Model3", "KONA"):
            self.assertTrue(_vehicle_model_hint_is_usable(vehicle, model), model)
        self.assertFalse(_vehicle_model_hint_is_usable(vehicle, "JTHKR5BH"))
        self.assertFalse(_vehicle_model_hint_is_usable(vehicle, "雷克萨斯JTHKR5BH"))

        row = {
            "vehicleName": "JTHKR5BH 小型轿车",
            "modelCode": "PICC-MODEL-JTHKR5BH",
            "vehicleModelCode": "PICC-PLAT-JTHKR5BH",
        }
        self.assertGreater(
            _vehicle_candidate_score(
                row,
                {
                    "rawModelName": "雷克萨斯JTHKR5BH",
                    "modelName": "雷克萨斯JTHKR5BH",
                    "brandNameHint": "",
                    "vin": "JTHKR5BH3J2327186",
                },
            ),
            0,
        )

    def test_vehicle_query_uses_certificate_sales_model_hint_before_vin_prefix(self) -> None:
        class _VehicleFallbackClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")
                self.names: list[str] = []

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                name = str(kwargs["params"]["jyVehicleRequest.vehicleName"])
                self.names.append(name)
                if "CT200H" in name.upper():
                    return {
                        "status": 0,
                        "result": [
                            {
                                "vehicleName": "雷克萨斯LEXUS CT200h轿车",
                                "modelCode": "PICC-MODEL-JTHKR5BH",
                                "vehicleModelCode": "PICC-PLAT-JTHKR5BH",
                                "purchasePrice": "280000",
                            }
                        ],
                    }
                return {"status": 0, "result": []}

        client = _VehicleFallbackClient()
        vehicle = {
            "rawModelName": "雷克萨斯JTHKR5BH",
            "modelName": "雷克萨斯JTHKR5BH",
            "vehicleType": "小型轿车",
            "brandNameHint": "",
            "vehicleNameHint": "CT200h",
            "vin": "JTHKR5BH3J2327186",
        }
        rows = _adapter()._query_vehicle_candidates(client, vehicle)
        self.assertEqual(len(rows["result"]), 1)
        self.assertEqual(client.names[0].upper(), "CT200H*")
        self.assertEqual(vehicle["modelQueryMatched"].upper(), "CT200H")
        self.assertEqual(vehicle["modelQueryMatchKind"], "sales_model")
        self.assertEqual(vehicle["modelQueryMatchLabel"], "销售车型直查")
        self.assertEqual(vehicle.get("vehicleQueryResourcesUsed"), "0524")

    def test_history_vehicle_seed_restores_lexus_sales_model_without_guessing(self) -> None:
        seed = {
            "vin": "JTHKR5BH3J2327186",
            "engineNo": "5ZR2A03174",
            "licenseNo": "赣G73C52",
            "selectedModelName": "雷克萨斯LEXUS CT200h轿车",
            "rawModelName": "雷克萨斯LEXUS CT200h轿车",
            "vehicleFgwCode": "CT200H",
            "modelQueryTerms": [
                "雷克萨斯LEXUS CT200h轿车",
                "CT200h",
                "JTHKR5BH",
            ],
        }
        vehicle = {
            "rawModelName": "雷克萨斯JTHKR5BH",
            "modelName": "雷克萨斯JTHKR5BH",
            "vehicleType": "小型轿车",
            "brandNameHint": "雷克萨斯",
            "vehicleNameHint": "",
            "vin": "JTHKR5BH3J2327186",
            "engineNo": "5ZR2A03174",
            "licenseNo": "赣G73C52",
        }

        terms = _vehicle_model_seed_terms(vehicle, seed)
        self.assertIn("雷克萨斯LEXUS CT200h轿车", terms)
        self.assertIn("CT200h", terms)
        self.assertNotIn("JTHKR5BH", terms)
        self.assertFalse(any("CT200HCT200H" in term.upper() for term in terms))

        _apply_vehicle_model_seed_hints(vehicle, seed)
        self.assertEqual(vehicle["rawModelName"], "雷克萨斯LEXUS CT200h轿车")
        self.assertEqual(vehicle["vehicleNameHint"], "雷克萨斯LEXUS CT200h轿车")

        class _HistorySeedClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")
                self.names: list[str] = []

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                name = str(kwargs["params"]["jyVehicleRequest.vehicleName"])
                self.names.append(name)
                if "CT200H" in name.upper():
                    return {
                        "status": 0,
                        "result": [
                            {
                                "vehicleName": "雷克萨斯LEXUS CT200h轿车",
                                "vehicleFgwCode": "CT200H",
                                "purchasePrice": "232000",
                            }
                        ],
                    }
                return {"status": 0, "result": []}

        client = _HistorySeedClient()
        rows = _adapter()._query_vehicle_candidates(client, vehicle)
        self.assertEqual(len(rows["result"]), 1)
        self.assertNotIn("JTHKR5BH*", client.names[:2])

    def test_history_vehicle_seed_is_rejected_when_identifiers_differ(self) -> None:
        vehicle = {
            "rawModelName": "雷克萨斯JTHKR5BH",
            "modelName": "雷克萨斯JTHKR5BH",
            "vehicleType": "小型轿车",
            "brandNameHint": "雷克萨斯",
            "vin": "JTHKR5BH3J2327186",
            "engineNo": "5ZR2A03174",
        }
        seed = {
            "vin": "OTHER5BH3J2327186",
            "engineNo": "5ZR2A03174",
            "selectedModelName": "雷克萨斯LEXUS CT200h轿车",
            "vehicleFgwCode": "CT200H",
        }
        _apply_vehicle_model_seed_hints(vehicle, seed)
        self.assertNotIn("trustedModelSeedTerms", vehicle)
        self.assertEqual(vehicle["rawModelName"], "雷克萨斯JTHKR5BH")

    def test_vin_prefix_only_query_returns_picc_fail_without_rows_and_continues(self) -> None:
        class _OnlyVinPrefixClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")
                self.names: list[str] = []

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                name = str(kwargs["params"]["jyVehicleRequest.vehicleName"])
                self.names.append(name)
                return {"status": -1, "statusText": "Fail"}

        client = _OnlyVinPrefixClient()
        vehicle = {
            "rawModelName": "雷克萨斯JTHKR5BH",
            "modelName": "雷克萨斯JTHKR5BH",
            "vehicleType": "小型轿车",
            "brandNameHint": "",
            "vehicleNameHint": "",
            "vin": "JTHKR5BH3J2327186",
        }
        rows = _adapter()._query_vehicle_candidates(client, vehicle)
        self.assertEqual(rows["result"], [])
        self.assertIn("JTHKR5BH*", client.names)

    def test_vin_prefix_is_only_a_correlation_hint_when_model_field_only_contains_brand(self) -> None:
        terms = _used_fuel_model_query_terms(
            "雷克萨斯",
            "小型轿车",
            brand_name="雷克萨斯",
            vehicle_name="",
            vin="JTHKR5BH3J2327186",
        )
        self.assertIn("JTHKR5BH", terms)
        self.assertNotIn("CT200h", terms)

        row = {
            "vehicleName": "雷克萨斯某车型",
            "vehicleFgwCode": "JTHKR5BH",
            "modelCode": "PICC-MODEL-JTHKR5BH",
            "vehicleModelCode": "PICC-PLAT-JTHKR5BH",
        }
        self.assertGreater(
            _vehicle_candidate_score(
                row,
                {
                    "rawModelName": "雷克萨斯",
                    "modelName": "雷克萨斯",
                    "brandNameHint": "雷克萨斯",
                    "vin": "JTHKR5BH3J2327186",
                },
            ),
            1000,
        )

    def test_broad_brand_query_accepts_only_vin_correlated_rows(self) -> None:
        class _VinFallbackClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")
                self.names: list[str] = []

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                name = str(kwargs["params"]["jyVehicleRequest.vehicleName"])
                self.names.append(name)
                if name.startswith("雷克萨斯*"):
                    return {
                        "status": 0,
                        "result": [
                            {
                                "vehicleName": "雷克萨斯某车型",
                                "vehicleFgwCode": "JTHKR5BH",
                                "modelCode": "PICC-MODEL-JTHKR5BH",
                                "vehicleModelCode": "PICC-PLAT-JTHKR5BH",
                                "purchasePrice": "280000",
                            }
                        ],
                    }
                return {"status": 0, "result": []}

        client = _VinFallbackClient()
        vehicle = {
            "rawModelName": "雷克萨斯",
            "modelName": "雷克萨斯",
            "vehicleType": "小型轿车",
            "brandNameHint": "雷克萨斯",
            "vehicleNameHint": "",
            "vin": "JTHKR5BH3J2327186",
        }
        rows = _adapter()._query_vehicle_candidates(client, vehicle)
        self.assertEqual(len(rows["result"]), 1)
        self.assertIn("雷克萨斯*", client.names)
        self.assertIn("VIN前缀关联", vehicle["modelQueryMatched"])
        self.assertEqual(vehicle["modelQueryMatchKind"], "vin_correlated")
        self.assertEqual(vehicle["modelQueryMatchLabel"], "VIN前缀关联")

    def test_broad_brand_rows_without_vin_correlation_are_rejected(self) -> None:
        class _UnsafeBroadClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                return {
                    "status": 0,
                    "result": [
                        {
                            "vehicleName": "雷克萨斯其他车型",
                            "vehicleFgwCode": "OTHER-VDS",
                            "purchasePrice": "280000",
                        }
                    ],
                }

        client = _UnsafeBroadClient()
        vehicle = {
            "rawModelName": "雷克萨斯",
            "modelName": "雷克萨斯",
            "vehicleType": "小型轿车",
            "brandNameHint": "雷克萨斯",
            "vehicleNameHint": "",
            "vin": "JTHKR5BH3J2327186",
        }
        rows = _adapter()._query_vehicle_candidates(client, vehicle)
        self.assertEqual(rows["result"], [])

    def test_brand_query_without_vin_does_not_silently_drop_or_auto_pick(self) -> None:
        class _BrandOnlyClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")
                self.names: list[str] = []

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                self.names.append(str(kwargs["params"]["jyVehicleRequest.vehicleName"]))
                return {
                    "status": 0,
                    "result": [
                        {
                            "vehicleName": "雷克萨斯LEXUS CT200h轿车",
                            "vehicleFgwCode": "JTHKR5BH",
                            "purchasePrice": "280000",
                        },
                        {
                            "vehicleName": "雷克萨斯最便宜误选车型",
                            "vehicleFgwCode": "OTHER",
                            "purchasePrice": "100000",
                        },
                    ],
                }

        client = _BrandOnlyClient()
        vehicle = {
            "rawModelName": "雷克萨斯",
            "modelName": "雷克萨斯",
            "vehicleType": "小型轿车",
            "brandNameHint": "雷克萨斯",
            "vehicleNameHint": "",
            "vin": "",
        }
        rows = _adapter()._query_vehicle_candidates(client, vehicle)
        self.assertEqual(rows["result"], [])
        self.assertEqual(vehicle.get("modelQueryBlockReason"), "broad_brand_without_vin")
        msg = _vehicle_model_resolution_failure_message(vehicle, ["雷克萨斯", "雷克萨斯轿车"])
        self.assertIn("缺少车架号关联或销售车型", msg)

    def test_brand_only_model_without_hint_is_not_auto_accepted(self) -> None:
        class _CheapestLexusClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                return {
                    "status": 0,
                    "result": [
                        {
                            "vehicleName": "雷克萨斯最便宜误选车型",
                            "purchasePrice": "100000",
                        }
                    ],
                }

        vehicle = {
            "rawModelName": "雷克萨斯",
            "modelName": "雷克萨斯",
            "vehicleType": "小型轿车",
            "brandNameHint": "",
            "vehicleNameHint": "",
            "vin": "",
        }
        rows = _adapter()._query_vehicle_candidates(_CheapestLexusClient(), vehicle)
        self.assertEqual(rows["result"], [])
        self.assertEqual(vehicle.get("modelQueryBlockReason"), "brand_only_without_vin_or_sales_model")

    def test_polluted_brand_suffix_is_stripped_from_query_terms(self) -> None:
        self.assertEqual(_vehicle_brand_prefix("雷克萨斯轿车"), "雷克萨斯")
        terms = _used_fuel_model_query_terms(
            "雷克萨斯",
            "小型轿车",
            brand_name="雷克萨斯轿车",
            vehicle_name="雷克萨斯",
            vin="",
        )
        self.assertNotIn("雷克萨斯轿车雷克萨斯", terms)
        self.assertIn("雷克萨斯", terms)
        self.assertFalse(_vehicle_model_hint_is_usable({"vin": ""}, "纯电动轿车"))
        self.assertFalse(_vehicle_model_hint_is_usable({"vin": ""}, "增程式混合动力"))

    def test_quote_success_reply_hides_vehicle_model_resolution_details(self) -> None:
        result = {
            "platform_code": "PICC",
            "risk_score": "42",
            "result_card": {
                "proposal_info": {
                    "vehicle_model": "雷克萨斯LEXUS CT200h轿车",
                    "model_match_method": "销售车型直查",
                },
            },
        }
        reply = _quote_result_reply_text(result, platform_name="人保")
        self.assertIn("风险水平：42 分", reply)
        self.assertNotIn("选定车型", reply)
        self.assertNotIn("匹配方式", reply)
        proposal = result["result_card"]["proposal_info"]
        self.assertEqual(proposal["vehicle_model"], "雷克萨斯LEXUS CT200h轿车")
        self.assertEqual(proposal["model_match_method"], "销售车型直查")

    def test_vehicle_query_resource_codes_prefer_defaults_then_profile(self) -> None:
        self.assertEqual(
            _vehicle_query_resource_codes(profile={"vehicle_query_resources": "0524"}),
            ["0524"],
        )
        self.assertEqual(
            _vehicle_query_resource_codes(
                profile={"vehicle_query_resources": "0524"},
                defaults={"车型查询资源码": "0999,0524"},
            ),
            ["0999", "0524"],
        )
        self.assertEqual(
            _vehicle_query_resource_codes(
                profile={"vehicle_query_resources": "0524"},
                defaults={"车型查询资源码": "0999"},
                vehicle={"vehicleQueryResources": ["1111", "0524"]},
            ),
            ["1111", "0524"],
        )

    def test_vehicle_query_falls_back_across_configured_resource_codes(self) -> None:
        class _ResourceFallbackClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")
                self.calls: list[tuple[str, str]] = []

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                params = kwargs["params"]
                resource = str(params["jyVehicleRequest.resources"])
                name = str(params["jyVehicleRequest.vehicleName"])
                self.calls.append((resource, name))
                if resource == "0524":
                    return {"status": 0, "result": []}
                if resource == "0999" and "CT200H" in name.upper():
                    return {
                        "status": 0,
                        "result": [
                            {
                                "vehicleName": "雷克萨斯LEXUS CT200h轿车",
                                "purchasePrice": "280000",
                            }
                        ],
                    }
                return {"status": 0, "result": []}

        client = _ResourceFallbackClient()
        vehicle = {
            "rawModelName": "雷克萨斯",
            "modelName": "雷克萨斯",
            "vehicleType": "小型轿车",
            "brandNameHint": "雷克萨斯",
            "vehicleNameHint": "CT200h",
            "vin": "JTHKR5BH3J2327186",
        }
        rows = _adapter()._query_vehicle_candidates(
            client,
            vehicle,
            defaults={"车型查询资源码": "0524,0999"},
        )
        self.assertEqual(len(rows["result"]), 1)
        self.assertEqual(vehicle["vehicleQueryResourcesUsed"], "0999")
        self.assertEqual(vehicle["vehicleQueryResourcesTried"], ["0524", "0999"])
        self.assertEqual(client.calls[0][0], "0524")
        self.assertEqual(client.calls[1][0], "0999")
        self.assertEqual(vehicle["modelQueryMatchKind"], "sales_model")

    def test_brand_and_vin_prefix_rows_without_correlation_are_rejected(self) -> None:
        class _MergedHintClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")
                self.names: list[str] = []

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                self.names.append(str(kwargs["params"]["jyVehicleRequest.vehicleName"]))
                return {
                    "status": 0,
                    "result": [
                        {
                            "vehicleName": "雷克萨斯CT200h轿车",
                            "vehicleFgwCode": "OTHER-VDS",
                            "purchasePrice": "280000",
                        }
                    ],
                }

        client = _MergedHintClient()
        vehicle = {
            "rawModelName": "雷克萨斯JTHKR5BH",
            "modelName": "雷克萨斯JTHKR5BH",
            "vehicleType": "小型轿车",
            "brandNameHint": "雷克萨斯",
            "vehicleNameHint": "",
            "vin": "JTHKR5BH3J2327186",
        }
        rows = _adapter()._query_vehicle_candidates(client, vehicle)
        self.assertEqual(rows["result"], [])
        self.assertIn("JTHKR5BH*", client.names)

    def test_car_name_accepts_alphanumeric_sales_model(self) -> None:
        self.assertEqual(
            _quote_car_name_from_features(
                {"generic_ocr_text": "CarName CT200h VehicleType 小型轿车"}
            ),
            "CT200h",
        )

    def test_sales_model_is_split_from_license_brand_model_text(self) -> None:
        self.assertEqual(
            _quote_sales_model_hint_from_model_text("雷克萨斯CT200h", vin="JTHKR5BH3J2327186"),
            "CT200h",
        )
        self.assertEqual(
            _quote_sales_model_hint_from_model_text("雷克萨斯LEXUS CT200h", vin=""),
            "CT200h",
        )
        self.assertEqual(
            _quote_sales_model_hint_from_model_text("雷克萨斯JTHKR5BH", vin="JTHKR5BH3J2327186"),
            "",
        )
        filled = _backfill_quote_sales_model_fields(
            {"vehicle_model": "雷克萨斯CT200h", "vin": "JTHKR5BH3J2327186"}
        )
        self.assertEqual(filled.get("car_name"), "CT200h")
        self.assertEqual(filled.get("vehicle_brand_name"), "雷克萨斯")

    def test_slot_merge_prefers_better_vin_and_model_over_later_weak_ocr(self) -> None:
        merged = _merge_quote_extracted_prefer(
            {
                "vin": "JTHKR5BH3J2327186",
                "vehicle_model": "雷克萨斯CT200h",
                "engine_no": "2ZR1234567",
            },
            {
                "vin": "JTHKR5BH",
                "vehicle_model": "雷克萨斯",
                "engine_no": "2ZR",
            },
        )
        self.assertEqual(merged.get("vin"), "JTHKR5BH3J2327186")
        self.assertEqual(merged.get("vehicle_model"), "雷克萨斯CT200h")
        self.assertEqual(merged.get("engine_no"), "2ZR1234567")

    def test_driving_license_slot_keeps_derived_car_name(self) -> None:
        images_by_slot = {
            "driving_license_main": [
                {
                    "extracted_fields": {
                        "vin": "JTHKR5BH3J2327186",
                        "vehicle_model": "雷克萨斯CT200h",
                        "plate_no": "粤B12345",
                    },
                    "method": "order_slot",
                    "text_features": {"quote_image_upload": True},
                }
            ]
        }
        active = _active_image_extracted_data(images_by_slot)
        self.assertEqual(active.get("car_name"), "CT200h")
        normalized = _normalize_quote_case_data(
            base_data={},
            order_data={},
            text_data={},
            images_by_slot=images_by_slot,
        )
        self.assertEqual(normalized.get("car_name"), "CT200h")

    def test_vehicle_certificate_car_name_is_backfilled_from_historical_ocr_text(self) -> None:
        base_fields = {
            "vehicle_model": "DFL7000NAA2BEY",
            "vehicle_brand_name": "东风日产牌",
            "vin": "LGBH92E29NY123456",
            "engine_no": "TZ200XS5UR",
        }
        fallback_text = "CarModelDFL7000NAA2BEY\nCarBrand东风日产牌\nCarName纯电动轿车\nVehicleType轿车"
        text_features = {"ocr_extracted_fields": base_fields}

        fields = _quote_image_extracted_fields_from_features(text_features, fallback_text)
        self.assertEqual(fields.get("car_name"), "纯电动轿车")
        self.assertEqual(fields.get("vehicle_model"), "DFL7000NAA2BEY")

        images_by_slot = {
            "vehicle_cert": [
                {
                    "text_features": text_features,
                    "ocr_text_sample": fallback_text,
                    "method": "order_slot",
                }
            ]
        }
        active = _active_image_extracted_data(images_by_slot)
        self.assertEqual(active.get("car_name"), "纯电动轿车")

        normalized = _normalize_quote_case_data(
            base_data={},
            order_data={},
            text_data={},
            images_by_slot=images_by_slot,
        )
        self.assertEqual(normalized.get("car_name"), "纯电动轿车")

        terms = _used_fuel_model_query_terms(
            "DFL7000NAA2BEY",
            "轿车",
            "纯电动轿车",
            brand_name=normalized.get("vehicle_brand_name"),
            vehicle_name=normalized.get("car_name"),
        )
        self.assertIn("东风日产DFL7000NAA2BEY纯电动轿车", terms)
        self.assertIn("DFL7000NAA2BEY纯电动轿车", terms)

    def test_vehicle_cert_cleaning_keeps_engine_letter_l(self) -> None:
        self.assertEqual(correct_vehicle_cert_field("engine_no", "W24L33464"), "W24L33464")
        cleaned = clean_dynamic_data_for_ocr(
            {
                "engine_no": "W24L33464",
                "vin": "LC0C76C4XR6182655",
            }
        )
        self.assertEqual(cleaned["engine_no"], "W24L33464")
        self.assertEqual(cleaned["vin"], "LC0C76C4XR6182655")

    def test_new_energy_used_legacy_driver_passenger_defaults_are_patched(self) -> None:
        source_values = {
            "第三者责任险": "300",
            "车上人员责任险（司机）": "1",
            "车上人员责任险（乘客）": "1",
        }
        values = _platform_default_values_with_legacy_fixes(
            "PICC",
            "新能源车-旧",
            source_values,
        )
        self.assertEqual(values["车上人员责任险（司机）"], "4")
        self.assertEqual(values["车上人员责任险（乘客）"], "4")
        self.assertEqual(source_values["车上人员责任险（司机）"], "1")

        oil_values = _platform_default_values_with_legacy_fixes(
            "PICC",
            "油车-旧",
            {
                "车上人员责任险（司机）": "1",
                "车上人员责任险（乘客）": "1",
            },
        )
        self.assertEqual(oil_values["车上人员责任险（司机）"], "1")
        self.assertEqual(oil_values["车上人员责任险（乘客）"], "1")

    def test_profile_defaults_cover_four_account_types(self) -> None:
        from app.services.quote_platforms.platforms.picc.business import (
            NEW_ENERGY_NEW_ACCOUNT_TYPE,
            USED_FUEL_ACCOUNT_TYPE,
            _profile_license_color_code,
            _profile_license_type,
            _profile_tax_defaults,
        )

        oil_energy = {"is_energy_car": "0"}
        new_energy = {"is_energy_car": "1"}
        oil_profile = {"account_type_name": USED_FUEL_ACCOUNT_TYPE, "license_type": "02", "license_color_code": "01", "tax_type": "1", "tax_calculate_mode": "C1", "tax_abate_type": "1"}
        energy_profile = {"account_type_name": NEW_ENERGY_NEW_ACCOUNT_TYPE, "license_type": "52", "license_color_code": "52", "tax_type": "2", "tax_calculate_mode": "C1", "tax_abate_type": "1"}

        self.assertEqual(_profile_license_type(oil_profile, oil_energy), "02")
        self.assertEqual(_profile_license_color_code(oil_profile, oil_energy), "01")
        self.assertEqual(_profile_license_type(energy_profile, new_energy), "52")
        self.assertEqual(_profile_license_color_code(energy_profile, new_energy), "52")

        oil_tax = _profile_tax_defaults(oil_profile, oil_energy, "2026-08-13")
        self.assertEqual(oil_tax["tax_type"], "1")
        self.assertEqual(oil_tax["calculate_mode"], "C1")
        self.assertEqual(oil_tax["tax_abate_type"], "1")
        self.assertEqual(oil_tax["tax_abate_reason"], "")

        energy_tax = _profile_tax_defaults(energy_profile, new_energy, "2026-08-13")
        self.assertEqual(energy_tax["tax_type"], "2")
        self.assertEqual(energy_tax["calculate_mode"], "C1")
        self.assertEqual(energy_tax["tax_abate_type"], "1")
        self.assertEqual(energy_tax["tax_abate_reason"], "06")
        self.assertEqual(energy_tax["duty_paid_proof_no"], "0012061001")
        self.assertEqual(energy_tax["pay_start_date"], "2026-01-01")
        self.assertEqual(energy_tax["pay_end_date"], "2026-12-31")

    def test_final_quote_form_uses_profile_license_and_tax_fields(self) -> None:
        from app.services.quote_platforms.platforms.picc.business import (
            NEW_ENERGY_NEW_ACCOUNT_TYPE,
            USED_FUEL_ACCOUNT_TYPE,
            _motor_quote_profile,
            _picc_business_defaults,
        )

        adapter = _adapter()
        defaults = _picc_business_defaults({})
        vehicle = {
            "licenseNo": "赣G12345",
            "engineNo": "R20322895",
            "vin": "LHGCY1628T8046465",
            "enrollDate": "2025-08-01",
            "startDateBI": "2026-08-13",
            "startDateCI": "2026-08-13",
            "modelName": "测试车型",
            "actualValue": "50000",
            "purchasePrice": "50000",
            "seatCount": "5",
        }
        owner = {"ownerName": "张三", "ownerIdNo": "360402199001011234", "ownerPhone": "13900000000"}
        selected = {"vehicleId": "MODEL001", "vehicleModelCode": "PLAT001", "purchasePrice": "50000"}

        oil_form = adapter._build_used_fuel_quote_form(
            defaults,
            vehicle,
            owner,
            selected,
            {},
            [],
            profile=_motor_quote_profile(USED_FUEL_ACCOUNT_TYPE),
        )
        self.assertEqual(oil_form["prpCitemCar.licenseType"], "02")
        self.assertEqual(oil_form["prpCitemCar.licenseColorCode"], "01")
        self.assertEqual(oil_form["prpCcarShipTax.taxType"], "1")
        self.assertEqual(oil_form["prpCcarShipTax.calculateMode"], "C1")
        self.assertEqual(oil_form["prpCcarShipTax.taxAbateType"], "1")
        self.assertEqual(oil_form["prpCcarShipTax.taxAbateReason"], "")
        self.assertEqual(oil_form["prpCcarShipTax.dutyPaidProofNo"], "")

        energy_form = adapter._build_used_fuel_quote_form(
            defaults,
            vehicle,
            owner,
            selected,
            {},
            [],
            profile=_motor_quote_profile(NEW_ENERGY_NEW_ACCOUNT_TYPE),
        )
        self.assertEqual(energy_form["prpCitemCar.licenseType"], "52")
        self.assertEqual(energy_form["prpCitemCar.licenseColorCode"], "52")
        self.assertEqual(energy_form["prpCcarShipTax.taxType"], "2")
        self.assertEqual(energy_form["prpCcarShipTax.calculateMode"], "C1")
        self.assertEqual(energy_form["prpCcarShipTax.taxAbateType"], "1")
        self.assertEqual(energy_form["prpCcarShipTax.taxAbateReason"], "06")
        self.assertEqual(energy_form["prpCcarShipTax.dutyPaidProofNo"], "0012061001")
        self.assertEqual(energy_form["prpCcarShipTax.payStartDate"], "2026-01-01")
        self.assertEqual(energy_form["prpCcarShipTax.payEndDate"], "2026-12-31")

    def test_final_quote_form_reuses_persisted_period_time_fields(self) -> None:
        from app.services.quote_platforms.platforms.picc.business import (
            NEW_ENERGY_USED_ACCOUNT_TYPE,
            _motor_quote_profile,
            _picc_business_defaults,
        )

        adapter = _adapter()
        form = adapter._build_used_fuel_quote_form(
            _picc_business_defaults({}),
            {
                "licenseNo": "赣KF88172",
                "engineNo": "W24L33464",
                "vin": "LC0C76C4XR6182655",
                "enrollDate": "2024-01-01",
                "startDateBI": "2026-10-01",
                "startHourBI": "0",
                "startMinuteBI": "0",
                "startDateCI": "2026-09-30",
                "startHourCI": "14",
                "startMinuteCI": "0",
                "modelName": "测试车型",
                "actualValue": "95600.18",
                "purchasePrice": "95600.18",
                "seatCount": "5",
            },
            {"ownerName": "夏玲珍", "ownerIdNo": "360402199001011234", "ownerPhone": "13900000000"},
            {"vehicleId": "MODEL001", "vehicleModelCode": "PLAT001", "purchasePrice": "95600.18"},
            {},
            [],
            profile=_motor_quote_profile(NEW_ENERGY_USED_ACCOUNT_TYPE),
        )
        self.assertEqual(form["prpCmain.startDate"], "2026-10-01")
        self.assertEqual(form["prpCmain.starthourbi"], "0")
        self.assertEqual(form["prpCmain.startDateCI"], "2026-09-30")
        self.assertEqual(form["prpCmain.starthourci"], "14")
        self.assertEqual(form["prpCmain.endDateCI"], "2027-09-30")
        self.assertEqual(form["prpCmain.endhourci"], "14")

    def test_invalid_default_license_is_ignored_but_tax_fields_can_override_profile(self) -> None:
        from app.services.quote_platforms.platforms.picc.business import (
            NEW_ENERGY_USED_ACCOUNT_TYPE,
            _motor_quote_profile,
            _picc_business_defaults,
        )

        adapter = _adapter()
        defaults = _picc_business_defaults(
            {
                "号牌种类": "99",
                "车牌颜色代码": "88",
                "车船税类型": "7",
                "车船税计算方式": "CX",
                "车船税减免类型": "9",
                "车船税减免原因": "77",
                "完税证明号": "TAXPROOF",
                "车船税起始日期": "2026-02-01",
                "车船税终止日期": "2026-11-30",
            }
        )
        form = adapter._build_used_fuel_quote_form(
            defaults,
            {
                "licenseNo": "赣GD68721",
                "engineNo": "R20322895",
                "vin": "LHGCY1628T8046465",
                "enrollDate": "2025-08-01",
                "startDateBI": "2026-08-13",
                "startDateCI": "2026-08-13",
                "modelName": "测试车型",
                "actualValue": "50000",
                "purchasePrice": "50000",
                "seatCount": "5",
            },
            {"ownerName": "张三", "ownerIdNo": "360402199001011234", "ownerPhone": "13900000000"},
            {"vehicleId": "MODEL001", "vehicleModelCode": "PLAT001", "purchasePrice": "50000"},
            {},
            [],
            profile=_motor_quote_profile(NEW_ENERGY_USED_ACCOUNT_TYPE),
        )
        self.assertEqual(form["prpCitemCar.licenseType"], "52")
        self.assertEqual(form["prpCitemCar.licenseColorCode"], "52")
        self.assertEqual(form["prpCcarShipTax.taxType"], "7")
        self.assertEqual(form["prpCcarShipTax.calculateMode"], "CX")
        self.assertEqual(form["prpCcarShipTax.taxAbateType"], "9")
        self.assertEqual(form["prpCcarShipTax.taxAbateReason"], "77")
        self.assertEqual(form["prpCcarShipTax.dutyPaidProofNo"], "TAXPROOF")
        self.assertEqual(form["prpCcarShipTax.payStartDate"], "2026-02-01")
        self.assertEqual(form["prpCcarShipTax.payEndDate"], "2026-11-30")

    def test_stale_vehicle_license_values_do_not_override_energy_profile(self) -> None:
        from app.services.quote_platforms.platforms.picc.business import (
            NEW_ENERGY_USED_ACCOUNT_TYPE,
            _motor_quote_profile,
            _picc_business_defaults,
        )

        adapter = _adapter()
        form = adapter._build_used_fuel_quote_form(
            _picc_business_defaults({}),
            {
                "licenseNo": "赣GD68721",
                "licenseType": "02",
                "licenseColorCode": "01",
                "engineNo": "R20322895",
                "vin": "LHGCY1628T8046465",
                "enrollDate": "2025-08-01",
                "startDateBI": "2026-08-13",
                "startDateCI": "2026-08-13",
                "modelName": "纯电动轿车",
                "actualValue": "50000",
                "purchasePrice": "50000",
                "seatCount": "5",
            },
            {"ownerName": "张三", "ownerIdNo": "360402199001011234", "ownerPhone": "13900000000"},
            {"vehicleId": "MODEL001", "vehicleModelCode": "PLAT001", "purchasePrice": "50000"},
            {},
            [],
            profile=_motor_quote_profile(NEW_ENERGY_USED_ACCOUNT_TYPE),
        )
        self.assertEqual(form["prpCitemCar.licenseType"], "52")
        self.assertEqual(form["prpCitemCar.licenseColorCode"], "52")


class PiccInsuranceDateRegressionTests(unittest.TestCase):
    def test_platform_prompt_extracts_compact_datetime(self) -> None:
        message = (
            "\u8be5\u8f66\u8f86\u5546\u4e1a\u9669\u4fdd\u9669\u671f\u95f4"
            "\u4e0e\u73b0\u5b58\u6709\u6548\u4fdd\u5355\u91cd\u590d\u6295\u4fdd"
            "\u7cfb\u7edf\u5efa\u8bae\u5c06\u8d77\u4fdd\u65e5\u671f\u8c03\u6574\u4e3a"
            "2026-09-1800\u65f600\u5206\u8bf7\u786e\u8ba4\u662f\u5426\u8c03\u6574?"
        )
        self.assertEqual(_reinsure_notice_suggested_start_date(message), "2026-09-18")
        self.assertEqual(_reinsure_notice_adjustment_kinds(message), ["bi"])

    def test_general_period_errors_detect_business_and_compulsory_independently(self) -> None:
        self.assertEqual(
            _insurance_date_error_adjustment_kinds("商业险保险期间不能在当前时间之前，请核对商业险保险期间"),
            ["bi"],
        )
        self.assertEqual(
            _insurance_date_error_adjustment_kinds("交强险保险期限不能在当前时间之前，请修改交强险保险期限"),
            ["ci"],
        )
        self.assertEqual(
            _insurance_date_error_adjustment_kinds("商业险保险期间请修改为2026-09-18，交强险保险期间请修改为2026-09-20"),
            ["bi", "ci"],
        )

    def test_effective_date_only_never_requests_an_adjustment(self) -> None:
        adapter = _adapter()
        response = {
            "data": {
                "normalizeErrorMsg": "\u8be5\u8f66\u8f86\u5b58\u5728\u91cd\u590d\u6295\u4fdd\u8bb0\u5f55",
                "prpReInsureItems": [
                    {
                        "effectiveDate": "2026-08-20 00:00:00",
                        "itemList": [{"coverageRealCode": "051050"}],
                    }
                ],
            }
        }
        adjustment = adapter._insurance_date_adjustment_from_platform_response(
            client=None,
            platform_response=response,
            request_body={
                "quoteForm": {"prpCmain.startDate": "2026-08-13"},
                "vehicleForm": {"startDateBI": "2026-08-13"},
            },
        )
        self.assertEqual(adjustment, {})

    def test_compulsory_only_reinsure_prompt_uses_compulsory_label(self) -> None:
        prompt = _format_reinsure_items_prompt(
            [
                {
                    "adviseStartDate": "2026-09-18 00:00:00",
                    "itemList": [{"coverageRealCode": "051074", "coverageName": "机动车交通事故责任强制保险"}],
                }
            ]
        )
        self.assertIn("交强险保险期间", prompt)
        self.assertNotIn("商业险保险期间", prompt)

    def test_later_existing_commercial_date_is_preserved_and_synchronized(self) -> None:
        form = {"prpCmain.startDate": "2026-08-13"}
        vehicle = {"startDateBI": "2026-08-18"}
        self.assertEqual(
            _insurance_date_adjustment_target_day(
                form,
                vehicle,
                kind="bi",
                target_day="2026-08-15",
            ),
            "2026-08-18",
        )
        self.assertTrue(
            _insurance_date_adjustment_needed(
                form,
                vehicle,
                kind="bi",
                target_day="2026-08-15",
            )
        )

        body, changed, notice = _adapter()._apply_insurance_date_adjustment_to_request_body(
            client=None,
            request_body={"quoteForm": form, "vehicleForm": vehicle},
            adjustment={
                "adjustment_kinds": ["bi"],
                "commercial_start_date": "2026-08-15",
                "message": "period adjustment",
            },
        )
        self.assertTrue(changed)
        self.assertEqual(body["quoteForm"]["prpCmain.startDate"], "2026-08-18")
        self.assertEqual(body["vehicleForm"]["startDateBI"], "2026-08-18")
        self.assertEqual(notice["commercial_start_date"], "2026-08-18")

    def test_compulsory_date_synchronizes_start_and_end(self) -> None:
        body, changed, notice = _adapter()._apply_insurance_date_adjustment_to_request_body(
            client=None,
            request_body={
                "quoteForm": {
                    "prpCmain.startDateCI": "2026-08-13",
                    "prpCmain.endDateCI": "2027-08-12",
                },
                "vehicleForm": {"startDateCI": "2026-08-18"},
            },
            adjustment={
                "adjustment_kinds": ["ci"],
                "compulsory_start_date": "2026-08-15",
                "message": "period adjustment",
            },
        )
        self.assertTrue(changed)
        self.assertEqual(body["quoteForm"]["prpCmain.startDateCI"], "2026-08-18")
        self.assertEqual(body["vehicleForm"]["startDateCI"], "2026-08-18")
        self.assertEqual(
            body["quoteForm"]["prpCmain.endDateCI"],
            _end_date_text("2026-08-18"),
        )
        self.assertEqual(notice["compulsory_start_date"], "2026-08-18")

    def test_compulsory_only_adjustment_does_not_query_commercial_value(self) -> None:
        adapter = _adapter()

        def unexpected_actual_value_query(*args: object, **kwargs: object) -> object:
            raise AssertionError("交强险改期不应重新查询商业险实际价值")

        adapter._query_actual_value = unexpected_actual_value_query
        body, changed, _ = adapter._apply_insurance_date_adjustment_to_request_body(
            client=None,
            request_body={
                "quoteForm": {
                    "prpCmain.startDate": "2026-08-13",
                    "prpCmain.startDateCI": "2026-08-13",
                    "prpCmain.endDateCI": "2027-08-12",
                },
                "vehicleForm": {
                    "startDateBI": "2026-08-13",
                    "startDateCI": "2026-08-13",
                },
                "preflight": {"selectedVehicle": {"actualValue": "50000"}},
            },
            adjustment={
                "adjustment_kinds": ["ci"],
                "compulsory_start_date": "2026-08-18",
                "message": "period adjustment",
            },
        )
        self.assertTrue(changed)
        self.assertEqual(body["vehicleForm"]["startDateBI"], "2026-08-13")
        self.assertEqual(body["vehicleForm"]["startDateCI"], "2026-08-18")

    def test_only_changed_insurance_kind_is_persisted_in_notice(self) -> None:
        body, changed, notice = _adapter()._apply_insurance_date_adjustment_to_request_body(
            client=None,
            request_body={
                "quoteForm": {
                    "prpCmain.startDate": "2026-08-18",
                    "prpCmain.startDateCI": "2026-08-13",
                    "prpCmain.endDateCI": "2027-08-12",
                },
                "vehicleForm": {
                    "startDateBI": "2026-08-18",
                    "startDateCI": "2026-08-13",
                },
            },
            adjustment={
                "adjustment_kinds": ["bi", "ci"],
                "commercial_start_date": "2026-08-15",
                "compulsory_start_date": "2026-08-18",
                "message": "period adjustment",
            },
        )
        self.assertTrue(changed)
        self.assertEqual(body["vehicleForm"]["startDateBI"], "2026-08-18")
        self.assertEqual(body["vehicleForm"]["startDateCI"], "2026-08-18")
        self.assertEqual(notice["commercial_start_date"], "")
        self.assertEqual(notice["compulsory_start_date"], "2026-08-18")
        self.assertEqual(notice["adjustment_kinds"], ["ci"])

    def test_old_notice_cannot_move_a_synchronized_date_backwards(self) -> None:
        form = {
            "prpCmain.startDateCI": "2026-08-18",
            "prpCmain.endDateCI": _end_date_text("2026-08-18"),
        }
        vehicle = {"startDateCI": "2026-08-18"}
        self.assertFalse(
            _insurance_date_adjustment_needed(
                form,
                vehicle,
                kind="ci",
                target_day="2026-08-15",
            )
        )

    def test_result_date_prefers_platform_response(self) -> None:
        form = {
            "prpCmain.startDate": "2026-08-13",
            "prpCmain.starthourbi": "0",
            "prpCmain.startminutebi": "0",
        }
        response = {
            "startDateBI": "2026-08-18",
            "startHourBI": "15",
            "startMinuteBI": "30",
        }
        self.assertEqual(
            _proposal_start_datetime_from_quote_response(response, form, kind="bi"),
            "2026-08-18 15:30",
        )

    def test_case_snapshot_persists_final_period_fields(self) -> None:
        snapshot = {
            "normalized_data": {
                "commercial_start_date": "2026-08-13",
                "compulsory_start_date": "2026-08-13",
            },
            "request_body": {
                "quoteForm": {
                    "prpCmain.startDate": "2026-08-13",
                    "prpCmain.startDateCI": "2026-08-13",
                    "prpCmain.endDateCI": "2027-08-12",
                },
                "vehicleForm": {
                    "startDateBI": "2026-08-13",
                    "startDateCI": "2026-08-13",
                },
            },
        }
        persisted = _quote_snapshot_with_auto_adjusted_dates(
            snapshot,
            {
                "commercial_start_date": "2026-08-18",
                "compulsory_start_date": "2026-08-20",
            },
        )
        form = persisted["request_body"]["quoteForm"]
        vehicle = persisted["request_body"]["vehicleForm"]
        self.assertEqual(persisted["normalized_data"]["commercial_start_date"], "2026-08-18")
        self.assertEqual(persisted["normalized_data"]["compulsory_start_date"], "2026-08-20")
        self.assertEqual(form["prpCmain.startDate"], "2026-08-18")
        self.assertEqual(vehicle["startDateBI"], "2026-08-18")
        self.assertEqual(form["prpCmain.startDateCI"], "2026-08-20")
        self.assertEqual(vehicle["startDateCI"], "2026-08-20")
        self.assertEqual(form["prpCmain.endDateCI"], _quote_end_date_text("2026-08-20"))

    def test_case_snapshot_persists_final_period_time_fields(self) -> None:
        snapshot = {
            "normalized_data": {
                "commercial_start_date": "2026-08-18",
                "compulsory_start_date": "2026-08-18",
            },
            "request_body": {
                "quoteForm": {
                    "prpCmain.startDate": "2026-08-18",
                    "prpCmain.starthourbi": "0",
                    "prpCmain.startminutebi": "0",
                    "prpCmain.startDateCI": "2026-08-18",
                    "prpCmain.starthourci": "0",
                    "prpCmain.startminuteci": "0",
                    "prpCmain.endDateCI": "2027-08-17",
                    "prpCmain.endhourci": "24",
                    "prpCmain.endminuteci": "0",
                },
                "vehicleForm": {
                    "startDateBI": "2026-08-18",
                    "startDateCI": "2026-08-18",
                },
            },
        }
        result = {
            "platform_auto_notices": [
                {
                    "type": "insurance_date_adjust",
                    "commercial_start_date": "2026-10-01",
                    "commercial_start_hour": "0",
                    "commercial_start_minute": "0",
                    "compulsory_start_date": "2026-09-30",
                    "compulsory_start_hour": "14",
                    "compulsory_start_minute": "0",
                }
            ]
        }
        adjustments = _quote_result_insurance_date_auto_adjustments(result)
        persisted = _quote_snapshot_with_auto_adjusted_dates(snapshot, adjustments)
        form = persisted["request_body"]["quoteForm"]
        vehicle = persisted["request_body"]["vehicleForm"]
        self.assertEqual(persisted["normalized_data"]["commercial_start_date"], "2026-10-01")
        self.assertEqual(persisted["normalized_data"]["commercial_start_hour"], "0")
        self.assertEqual(persisted["normalized_data"]["compulsory_start_date"], "2026-09-30")
        self.assertEqual(persisted["normalized_data"]["compulsory_start_hour"], "14")
        self.assertEqual(form["prpCmain.startDateCI"], "2026-09-30")
        self.assertEqual(form["prpCmain.starthourci"], "14")
        self.assertEqual(form["prpCmain.endDateCI"], "2027-09-30")
        self.assertEqual(form["prpCmain.endhourci"], "14")
        self.assertEqual(vehicle["startHourCI"], "14")

    def test_road_rescue_command_can_adjust_or_remove_product(self) -> None:
        overrides = extract_quote_config_overrides("道路救援 7")
        self.assertEqual(overrides["机动车增值服务特约条款（道路救援服务）"], "7")
        exclusions = _extract_quote_product_exclusions("不要道路救援")
        self.assertIn("机动车增值服务特约条款（道路救援服务）", exclusions)
        self.assertIn(
            "机动车增值服务特约条款（道路救援服务）",
            _normalize_quote_product_exclusions(["道路救援"]),
        )

    def test_duplicate_insurance_with_explicit_period_change_enters_retry_path(self) -> None:
        response = {
            "status": -1,
            "statusText": "Fail",
            "data": {
                "normalizeErrorMsg": (
                    "该车辆商业险保险期间与现存有效保单重复投保，"
                    "系统建议将起保日期调整为2026-09-18 00时00分，请确认是否调整？"
                ),
                "prpReInsureItems": [
                    {
                        "adviseStartDate": "2026-09-18 00:00:00",
                        "itemList": [{"coverageRealCode": "051050"}],
                    }
                ],
            },
        }
        with self.assertRaises(PiccBusinessRequestError) as caught:
            _adapter()._submit_used_fuel_quote(
                _QuoteResponseClient(response),
                {"quoteForm": {"prpCmain.startDate": "2026-08-13"}},
            )
        self.assertEqual(caught.exception.platform_response, response)

    def test_plain_duplicate_insurance_remains_duplicate_quote(self) -> None:
        response = {
            "status": -1,
            "statusText": "Fail",
            "data": {
                "normalizeErrorMsg": "该车辆近期已在我司承保，请核实后进行报价，避免重复投保。",
            },
        }
        with self.assertRaises(PiccDuplicateQuoteError):
            _adapter()._submit_used_fuel_quote(
                _QuoteResponseClient(response),
                {"quoteForm": {"prpCmain.startDate": "2026-08-13"}},
            )

    def test_plain_repeat_word_is_not_duplicate_quote(self) -> None:
        self.assertFalse(_contains_duplicate_quote({"data": {"errorMsg": "请勿重复点击提交按钮，请稍后查看结果"}}))
        self.assertTrue(_contains_duplicate_quote({"data": {"errorMsg": "该车辆近期已在我司承保，请核实后进行报价，避免重复投保。"}}))


class QuotePromptStateRegressionTests(unittest.TestCase):
    def test_period_adjustment_is_not_treated_as_duplicate_quote_stop(self) -> None:
        result = PlatformRuntimeResult(
            status="failed",
            message=(
                "\u8be5\u8f66\u8f86\u5546\u4e1a\u9669\u4fdd\u9669\u671f\u95f4"
                "\u4e0e\u73b0\u5b58\u6709\u6548\u4fdd\u5355\u91cd\u590d\u6295\u4fdd"
                "\u7cfb\u7edf\u5efa\u8bae\u5c06\u8d77\u4fdd\u65e5\u671f\u8c03\u6574\u4e3a"
                "2026-09-18 00\u65f600\u5206"
            ),
            data={},
        )
        self.assertFalse(_is_runtime_duplicate_quote_result(result))

    def test_plain_duplicate_quote_still_retries_once(self) -> None:
        result = PlatformRuntimeResult(
            status="duplicate_quote",
            message="\u8f66\u8f86\u8fd1\u671f\u5df2\u5728\u6211\u53f8\u627f\u4fdd",
            data={},
        )
        self.assertTrue(_is_runtime_duplicate_quote_result(result))

    def test_runtime_detail_prefers_platform_original_message(self) -> None:
        result = PlatformRuntimeResult(
            status="failed",
            message="报价提交失败：错误信息",
            data={
                "platform_response": {
                    "raw_message": "身份证有效期止期不能早于当前日期，请核实证件有效期。",
                    "message": "错误信息",
                    "response": {
                        "normalizeErrorMsg": "身份证有效期止期不能早于当前日期，请核实证件有效期。",
                    },
                },
            },
        )
        detail = _runtime_detail(result, "平台报价失败")
        self.assertIn("身份证有效期", detail)
        self.assertNotEqual(detail, "报价提交失败：错误信息")

    def test_successful_quote_keeps_duplicate_insurance_notice_when_no_date_change_is_needed(self) -> None:
        dialog = {
            "message": (
                "该车辆商业险保险期间与现存有效保单重复投保，\n"
                "系统建议将起保日期调整为2026-08-18 00时00分"
            )
        }
        notice = _duplicate_quote_notice_from_success_dialog(
            dialog,
            has_period_auto_notice=False,
        )
        self.assertEqual(notice["type"], "duplicate_quote_notice")
        self.assertIn("重复投保", notice["message"])
        self.assertEqual(
            _duplicate_quote_notice_from_success_dialog(
                dialog,
                has_period_auto_notice=True,
            ),
            {},
        )

    def test_notice_dedupe_key_ignores_whitespace_only(self) -> None:
        first = _quote_auto_notice_dedupe_key(
            trace_id="trace-1",
            task_id=42,
            notice_type="insurance_date_adjust",
            message="A\n B",
        )
        second = _quote_auto_notice_dedupe_key(
            trace_id="trace-1",
            task_id=42,
            notice_type="insurance_date_adjust",
            message="A B",
        )
        different_type = _quote_auto_notice_dedupe_key(
            trace_id="trace-1",
            task_id=42,
            notice_type="duplicate_quote_notice",
            message="A B",
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, different_type)
        self.assertEqual(
            _quote_auto_notice_message_id(first),
            _quote_auto_notice_message_id(second),
        )

    def test_old_auto_notice_text_does_not_suppress_new_quote_task(self) -> None:
        import asyncio

        text = (
            "重复投保提示\n\n"
            "车辆VIN:LGXCH4CD6T0353958近期已在我司承保，请核实后进行报价，避免重复投保。"
        )
        old_key = _quote_auto_notice_dedupe_key(
            trace_id="trace-old",
            task_id=101,
            notice_type="duplicate_quote_notice",
            message=text,
        )
        new_key = _quote_auto_notice_dedupe_key(
            trace_id="trace-new",
            task_id=102,
            notice_type="duplicate_quote_notice",
            message=text,
        )
        old_metadata = {
            "trace_id": "trace-old",
            "data": {
                "payload": {
                    "platform_auto_notice": {
                        "type": "duplicate_quote_notice",
                        "message": text,
                        "dedupe_key": old_key,
                    },
                },
            },
        }
        result = MagicMock()
        result.all.return_value = [(text, old_metadata)]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)

        self.assertFalse(
            asyncio.run(_quote_auto_notice_already_persisted(
                db,
                owner_user_id=1,
                session_id="session-1",
                dedupe_key=new_key,
                message=text,
                trace_id="trace-new",
            ))
        )

        same_trace_metadata = {
            **old_metadata,
            "trace_id": "trace-new",
            "data": {
                "payload": {
                    "platform_auto_notice": {
                        "type": "duplicate_quote_notice",
                        "message": text,
                    },
                },
            },
        }
        result.all.return_value = [(text, same_trace_metadata)]
        self.assertTrue(
            asyncio.run(_quote_auto_notice_already_persisted(
                db,
                owner_user_id=1,
                session_id="session-1",
                dedupe_key=new_key,
                message=text,
                trace_id="trace-new",
            ))
        )

    def test_auto_notice_duplicate_key_errors_are_swallowed_only_for_message_id(self) -> None:
        mysql_error = IntegrityError(
            "INSERT",
            {},
            Exception("1062 Duplicate entry 'qa-notice-abc' for key 'uq_quote_assistant_message_id'"),
        )
        sqlite_error = IntegrityError(
            "INSERT",
            {},
            Exception("UNIQUE constraint failed: quote_assistant_message_new.message_id"),
        )
        unrelated_error = IntegrityError(
            "INSERT",
            {},
            Exception("1062 Duplicate entry 'abc' for key 'uq_some_other_constraint'"),
        )
        self.assertTrue(_is_quote_auto_notice_duplicate_error(mysql_error))
        self.assertTrue(_is_quote_auto_notice_duplicate_error(sqlite_error))
        self.assertFalse(_is_quote_auto_notice_duplicate_error(unrelated_error))

    def test_duplicate_quote_notice_remains_visible_in_history_preview(self) -> None:
        text = (
            "重复投保提示\n\n"
            "车辆VIN:LHGCY1628T8046465近期已在我司承保，请核实后进行报价，避免重复投保。"
        )
        self.assertFalse(_session_preview_needs_recompute(text))
        preview = _message_preview_text(
            "assistant",
            text,
            {
                "intent": "quote",
                "data": {
                    "result_status": "not_ready",
                    "payload": {
                        "platform_auto_notice": {
                            "type": "duplicate_quote_notice",
                            "message": text,
                        },
                        "ui_visible": True,
                    },
                },
            },
        )
        self.assertIn("重复投保提示", preview)

    def test_legacy_duplicate_confirm_prompt_is_still_hidden_from_preview(self) -> None:
        self.assertTrue(_session_preview_needs_recompute("平台提示可能重复投保，等待重复投保确认"))
        preview = _message_preview_text(
            "assistant",
            "重复投保提示\n请确认是否继续报价",
            {
                "intent": "quote",
                "data": {
                    "result_status": "not_ready",
                    "payload": {
                        "duplicate_quote_confirm_required": True,
                        "duplicate_quote_warning": "请确认是否继续报价",
                    },
                },
            },
        )
        self.assertEqual(preview, "")


class PiccRenewalHarRegressionTests(unittest.TestCase):
    def test_renewal_prefill_ignores_zero_amount_coverages(self) -> None:
        adapter = _adapter()
        defaults = adapter._renewal_product_defaults_from_prefill(
            {
                "renewItemKindVoList": [
                    {"kindCode": "051051", "amount": "0"},
                    {"kindCode": "051052", "unitAmount": "0", "amount": "0"},
                    {"kindCode": "051053", "unitAmount": "", "amount": "0"},
                    {"kindCode": "051063", "amount": "0", "sharedAmountFlag": "1"},
                    {"kindCode": "051064", "quantity": "0"},
                    {"kindCode": "051074", "amount": ""},
                ]
            }
        )
        self.assertNotIn("第三者责任险", defaults)
        self.assertNotIn("车上人员责任险（司机）", defaults)
        self.assertNotIn("车上人员责任险（乘客）", defaults)
        self.assertNotIn("医保外医疗费用责任险（第三者责任险）", defaults)
        self.assertNotIn("机动车增值服务特约条款（道路救援服务）", defaults)
        self.assertNotIn("交强险", defaults)

        positive_defaults = adapter._renewal_product_defaults_from_prefill(
            {
                "renewItemKindVoList": [
                    {"kindCode": "051051", "amount": "3000000"},
                    {"kindCode": "051052", "unitAmount": "30000"},
                    {"kindCode": "051053", "unitAmount": "30000"},
                    {"kindCode": "051063", "amount": "3000000", "sharedAmountFlag": "1"},
                    {"kindCode": "051064", "quantity": "7"},
                ]
            }
        )
        self.assertEqual(positive_defaults["第三者责任险"], "300")
        self.assertEqual(positive_defaults["车上人员责任险（司机）"], "30000")
        self.assertEqual(positive_defaults["车上人员责任险（乘客）"], "30000")
        self.assertEqual(positive_defaults["共享主险限额"], True)
        self.assertEqual(positive_defaults["机动车增值服务特约条款（道路救援服务）"], "7")

    def test_renewal_prepare_merge_keeps_configured_defaults_over_renewal_defaults(self) -> None:
        adapter = _adapter()
        captured: dict[str, object] = {}

        def fake_fetch(self, client, selected):
            return {"data": "prefill"}

        def fake_prefill(self, prefill, selected):
            return {
                "account_type_name": "油车-旧",
                "license_type": "02",
                "license_color_code": "01",
                "renewal_quote_field_defaults": {
                    "车上人员责任险（司机）": "30000",
                    "车上人员责任险（乘客）": "10000",
                    "第三者责任险": "200",
                    "共享主险限额": True,
                },
            }

        def fake_prepare(self, client, ctx, payload, account_type_name="油车-旧"):
            captured["default_config_json"] = dict(payload["default_config_json"])
            captured["normalized_data"] = dict(payload["normalized_data"])
            captured["account_type_name"] = account_type_name
            return {
                "quoteForm": {},
                "preflight": {},
                "accountTypeName": account_type_name,
            }

        def fake_apply(self, body, renewal_data, prefill, selected):
            return dict(body)

        adapter._fetch_renewal_policy_prefill = MethodType(fake_fetch, adapter)
        adapter._renewal_prefill_vehicle_data = MethodType(fake_prefill, adapter)
        adapter._prepare_used_fuel_quote = MethodType(fake_prepare, adapter)
        adapter._apply_renewal_prefill_to_quote_body = MethodType(fake_apply, adapter)

        body = adapter._prepare_renewal_used_fuel_quote(
            SimpleNamespace(),
            SimpleNamespace(account_type_name="油车-旧"),
            {
                "quote_flow_type": "renewal_motor_quote",
                "normalized_data": {
                    "account_type_name": "油车-旧",
                    "plate_no": "赣G12345",
                    "engine_no": "E1234",
                    "vin": "LHGCY1628T8046465",
                    "renewal_lookup": {
                        "found": True,
                        "selected": {
                            "policy_no": "P1",
                            "policy_no_encode": "ENC1",
                            "license_type": "02",
                        },
                    },
                },
                "default_config_json": {
                    "车上人员责任险（司机）": "3",
                    "车上人员责任险（乘客）": "3",
                    "第三者责任险": "300",
                    "共享主险限额": True,
                },
                "platform_default_config": {"resolved_type_name": "油车-旧"},
            },
            account_type_name="油车-旧",
        )

        merged_defaults = captured["default_config_json"]
        self.assertEqual(merged_defaults["车上人员责任险（司机）"], "3")
        self.assertEqual(merged_defaults["车上人员责任险（乘客）"], "3")
        self.assertEqual(merged_defaults["第三者责任险"], "300")
        self.assertEqual(merged_defaults["共享主险限额"], True)
        self.assertEqual(
            body["preflight"]["renewalMergeTrace"]["ignoredConfiguredRenewalDefaults"],
            {
                "车上人员责任险（司机）": "30000",
                "车上人员责任险（乘客）": "10000",
                "第三者责任险": "200",
                "共享主险限额": True,
            },
        )
        self.assertEqual(body["preflight"]["renewalQuoteFieldPriority"], "会话明确调参值 > 默认参数配置 > 有效续保接口返回值 > profile内置默认值")

    def test_quote_form_pre_submit_blocks_selected_zero_amounts(self) -> None:
        form = {
            "prpCitemCar.licenseType": "02",
            "prpCitemKindVos[0].kindCode": "051052",
            "prpCitemKindVos[0].kindName": "车上人员责任险（司机）",
            "prpCitemKindVos[0].chooseFlag": "true",
            "prpCitemKindVos[0].amount": "0",
        }
        with self.assertRaisesRegex(Exception, "司机.*保额无效"):
            _adapter()._validate_picc_quote_form_before_submit(form, account_type_name="油车-旧")

    def test_quote_form_pre_submit_blocks_energy_account_mismatch(self) -> None:
        form = {
            "prpCitemCar.licenseType": "52",
            "prpCitemKindVos[0].kindCode": "051051",
            "prpCitemKindVos[0].kindName": "第三者责任险",
            "prpCitemKindVos[0].chooseFlag": "true",
            "prpCitemKindVos[0].amount": "300",
        }
        with self.assertRaisesRegex(Exception, "燃油车号牌种类不能是52"):
            _adapter()._validate_picc_quote_form_before_submit(form, account_type_name="油车-旧")

    def test_quote_form_pre_submit_allows_blue_plate_hybrid_fields(self) -> None:
        form = {
            "prpCitemCar.licenseType": "02",
            "prpCitemCar.licenseColorCode": "01",
            "prpCitemCar.isEnergyCar": "1",
            "prpCitemCar.vehicleFuelType": "D5",
            "energyFlag": "1",
            "energyTypePlat": "4",
            "energyTypePlatTemp": "增程式混合动力",
            "prpCitemKindVos[0].kindCode": "051051",
            "prpCitemKindVos[0].kindName": "第三者责任险",
            "prpCitemKindVos[0].chooseFlag": "true",
            "prpCitemKindVos[0].amount": "300",
        }
        _adapter()._validate_picc_quote_form_before_submit(form, account_type_name="油车-旧")

    def test_renewal_lookup_attempts_try_engine_and_vin_with_02_and_52(self) -> None:
        attempts = _adapter()._renewal_lookup_param_attempts(
            plate_no="赣GD68721",
            engine_no="W24133464",
            vin="LC0C76C4XR6182655",
            last_policy_no="",
            license_type="02",
            is_owner=True,
        )
        strategies = {item["strategy"] for item in attempts}
        self.assertIn("engine_last4", strategies)
        self.assertIn("engine_last4_license_52", strategies)
        self.assertIn("vin_last6", strategies)
        self.assertIn("vin_last6_license_52", strategies)
        by_strategy = {item["strategy"]: item["params"] for item in attempts}
        self.assertEqual(by_strategy["engine_last4_license_52"]["licenseType4Renew"], "52")
        self.assertEqual(by_strategy["vin_last6_license_52"]["frameNo4Renew2"], "182655")

    def test_renewal_candidate_scoring_prefers_exact_vin_over_earlier_flagged_engine_hit(self) -> None:
        current = {
            "plate_no": "赣GD68721",
            "vin": "LC0C76C4XR6182655",
            "engine_no": "W24133464",
            "license_type": "52",
            "commercial_start_date": "2026-10-01",
        }
        candidates = [
            {
                "policy_no": "WEAK_ENGINE",
                "policy_no_encode": "ENC_WEAK_ENGINE",
                "risk_code": "DAA",
                "license_no": "赣G00000",
                "vin": "LOTHER00000000000",
                "engine_no": "W24133464",
                "license_type": "02",
                "end_date": "2026-08-01",
                "renewal_or_copy_flag": "1",
            },
            {
                "policy_no": "EXACT_VIN",
                "policy_no_encode": "ENC_EXACT_VIN",
                "risk_code": "DZA",
                "license_no": "赣GD68721",
                "vin": "LC0C76C4XR6182655",
                "engine_no": "W24133464",
                "license_type": "52",
                "end_date": "2026-09-30",
                "renewal_or_copy_flag": "0",
            },
        ]
        selected = _pick_renewal_policy_candidate(candidates, current)
        self.assertEqual(selected["policy_no"], "EXACT_VIN")
        self.assertLess(_renewal_candidate_score(candidates[0], current), 0)

    def test_reusable_renewal_context_rejects_changed_vehicle_identity(self) -> None:
        base = {
            "plate_no": "赣GD68721",
            "vin": "LC0C76C4XR6182655",
            "engine_no": "W24133464",
            "license_type": "52",
            "renewal_lookup": {
                "found": True,
                "selected": {
                    "policy_no": "P1",
                    "policy_no_encode": "ENC1",
                    "license_no": "赣GD68721",
                    "vin": "LC0C76C4XR6182655",
                    "engine_no": "W24133464",
                    "license_type": "52",
                },
            },
        }
        self.assertTrue(_has_reusable_renewal_quote_context(base))
        changed_vin = dict(base, vin="LC0C76C4XR6182656")
        self.assertFalse(_has_reusable_renewal_quote_context(changed_vin))
        changed_license_type = dict(base, license_type="02")
        self.assertFalse(_has_reusable_renewal_quote_context(changed_license_type))

    def test_0813_renewal_result_does_not_use_configured_joint_sales_without_plan(self) -> None:
        har = _load_0813_renewal_har()
        renewal_rows = _har_response_json(har, 4)["data"]["list"]
        summaries = [_adapter()._renewal_candidate_summary(row) for row in renewal_rows]
        selected = next(item for item in summaries if item["policy_no"] == "PDZA202536040000299779")
        self.assertEqual(selected["policy_no"], "PDZA202536040000299779")
        self.assertEqual(
            _picc_encrypt_renewal_policy_no(selected["policy_no"]),
            "kLClt0iQAjKsv9l7wKpMCWDViiZKruKN919RBCDiGkA=",
        )

        adapter = _adapter()
        client = _RecordingClient(_har_response_json(har, 8))
        prefill = adapter._fetch_renewal_policy_prefill(client, selected)
        call_kwargs = client.calls[-1][1]
        self.assertEqual(call_kwargs["params"]["policyNo"], "kLClt0iQAjKsv9l7wKpMCWDViiZKruKN919RBCDiGkA=")
        self.assertEqual(
            call_kwargs["params"]["policyNoEncode"],
            "KSGmIBQGIvub4cWWlqxslOgiZU+x62r8hFwPhTql4WIxH/8ed8GqCoiMTL1LFqZu",
        )

        vehicle_data = adapter._renewal_prefill_vehicle_data(prefill, selected)
        self.assertEqual(vehicle_data["plate_no"], "赣G872F6")
        self.assertEqual(vehicle_data["vin"], "L6T7622Z6MF008872")
        self.assertEqual(vehicle_data["engine_no"], "M3GA4904371")
        self.assertEqual(vehicle_data["commercial_start_date"], "2026-08-25")
        self.assertEqual(vehicle_data["compulsory_start_date"], "2026-08-24")
        defaults = vehicle_data["renewal_quote_field_defaults"]
        self.assertEqual(defaults["第三者责任险"], "300")
        self.assertEqual(defaults["车上人员责任险（司机）"], "40000")
        self.assertEqual(defaults["车上人员责任险（乘客）"], "20000")
        self.assertEqual(defaults["共享主险限额"], True)
        self.assertNotIn("机动车损失保险", defaults)

        request_body = {
            "accountTypeName": "油车-旧",
            "vehicleForm": vehicle_data["renewal_request_body_seed"]["vehicleForm"],
            "ownerForm": vehicle_data["renewal_request_body_seed"]["ownerForm"],
            "quoteForm": _har_form_params(har, 52),
            "jointSaleForm": {
                "tujiaAnshun": {
                    "enabled": True,
                    "success": True,
                    "premium": "398",
                    "amount": "200000",
                }
            },
            "preflight": {},
        }
        ctx = SimpleNamespace(account_type_name="油车-旧")
        result = adapter._build_motor_quote_result_from_response(ctx, {}, request_body, _har_response_json(har, 52))
        self.assertEqual(result["risk_score"], 45)
        self.assertEqual(str(result["premium_total"]), "2896.79")
        self.assertEqual(result["result_card"]["commercial_premium"], "1741.79")
        self.assertEqual(result["result_card"]["compulsory_premium"], "855.00")
        self.assertEqual(result["result_card"]["vehicle_tax"], "300.00")
        self.assertEqual(result["result_card"]["joint_sales_premium"], "")
        self.assertEqual(result["result_card"]["proposal_info"]["plate_no"], "赣G872F6")

    def test_0813_renewal_prepare_builds_quote_body_like_har(self) -> None:
        har = _load_0813_renewal_har()
        renewal_rows = _har_response_json(har, 4)["data"]["list"]
        selected = _pick_renewal_policy_candidate([_adapter()._renewal_candidate_summary(row) for row in renewal_rows])
        client = _HarRouteClient(
            {
                "quotePolicy.do": _har_response_json(har, 8),
                "jyQuery.do": _har_response_json(har, 11),
                "QtPrpPreciseVehicleQuery.do": _har_response_json(har, 23),
                "calActualVal.do": _har_response_json(har, 12),
                "queryQtTaxabate.do": _har_response_json(har, 22),
                "queryCarchecker.do": _har_response_json(har, 28),
                "verifyPersonalAgtControl.do": _har_response_json(har, 51),
                "duplicateInsuredVinNo.do": _har_response_json(har, 27),
                "choosePlanInfoForJointSale.do": {
                    "status": 0,
                    "statusText": "Success",
                    "data": {
                        "planInfoListMap": {
                            "05": [
                                {"planName": "HAR fake", "planCode": "P1", "planPremium": "398", "planAmount": "200000"}
                            ]
                        }
                    },
                },
            }
        )
        payload = {
            "quote_flow_type": "renewal_motor_quote",
            "normalized_data": {
                "account_type_name": "油车-旧",
                "plate_no": "赣G872F6",
                "engine_no": "M3GA4904371",
                "vin": "L6T7622Z6MF008872",
                "renewal_lookup": {
                    "found": True,
                    "selected": selected,
                    "candidates": [],
                },
            },
            "default_config_json": {},
            "platform_default_config": {"resolved_type_name": "油车-旧"},
        }
        ctx = SimpleNamespace(account_type_name="油车-旧")
        body = _adapter()._prepare_renewal_used_fuel_quote(client, ctx, payload, account_type_name="油车-旧")
        form = body["quoteForm"]
        self.assertEqual(form["prpCitemCar.licenseNo"], "赣G872F6")
        self.assertEqual(form["prpCitemCar.licenseType"], "02")
        self.assertEqual(form["prpCmain.startDate"], "2026-08-25")
        self.assertEqual(form["prpCmain.startDateCI"], "2026-08-24")
        self.assertEqual(form["prpCitemCar.actualValue"], "70155.60")
        self.assertEqual(form["renewed"], "1")
        self.assertEqual(form["lastPolicyNo"], "PDZA202536040000299779")
        self.assertEqual(form["prpCitemCar.lastBIPolicyNo"], "PDAA202536040000208495")
        self.assertEqual(form["prpCitemCar.lastCIPolicyNo"], "PDZA202536040000299779")
        self.assertEqual(form["prpCitemCar.Nodamageyears"], "0")
        self.assertEqual(form["prpCitemCarExt.noDamYearsBI"], "0")
        self.assertEqual(form["prpCitemCarExt.lastDamagedBI"], "0")
        self.assertEqual(form["prpCitemCarExt.lastDamagedCI"], "1")
        self.assertEqual(form["prpCcarShipTax.leviedDate"], "2026-08-25")
        self.assertEqual(form["prpCcarShipTax.payStartDate"], "2026-01-01")
        self.assertEqual(form["prpCcarShipTax.payEndDate"], "2026-12-31")
        self.assertEqual(form["monopolyCode"], "3604731000027")
        self.assertEqual(form["groupCodeValidStatus"], "1")
        self.assertEqual(form["prpCitemKindVos[1].kindCode"], "051050")
        self.assertEqual(form["prpCitemKindVos[1].amount"], "70155.60")
        self.assertEqual(form["prpCitemKindVos[2].kindCode"], "051051")
        self.assertEqual(form["prpCitemKindVos[2].amount"], "300")
        self.assertEqual(form["prpCitemKindVos[3].amount"], "40000")
        self.assertEqual(form["prpCitemKindVos[4].amount"], "20000")
        self.assertEqual(form["prpCitemKindVos[5].sharedAmountFlag"], "1")
        self.assertEqual(body["jointSaleForm"]["tujiaAnshun"]["premium"], "398")
        self.assertEqual(body["jointSaleForm"]["tujiaAnshun"]["amount"], "200000")

    def test_0813_renewal_driver_passenger_adjustment_matches_final_har_quote(self) -> None:
        har = _load_0813_renewal_har()
        renewal_rows = _har_response_json(har, 4)["data"]["list"]
        selected = _pick_renewal_policy_candidate([_adapter()._renewal_candidate_summary(row) for row in renewal_rows])
        client = _HarRouteClient(
            {
                "quotePolicy.do": _har_response_json(har, 8),
                "jyQuery.do": _har_response_json(har, 11),
                "QtPrpPreciseVehicleQuery.do": _har_response_json(har, 23),
                "calActualVal.do": _har_response_json(har, 12),
                "queryQtTaxabate.do": _har_response_json(har, 22),
                "queryCarchecker.do": _har_response_json(har, 28),
                "verifyPersonalAgtControl.do": _har_response_json(har, 65),
                "duplicateInsuredVinNo.do": _har_response_json(har, 27),
                "choosePlanInfoForJointSale.do": {
                    "status": 0,
                    "statusText": "Success",
                    "data": {
                        "planInfoListMap": {
                            "05": [
                                {"planName": "HAR fake", "planCode": "P1", "planPremium": "398", "planAmount": "200000"}
                            ]
                        }
                    },
                },
            }
        )
        payload = {
            "quote_flow_type": "renewal_motor_quote",
            "normalized_data": {
                "account_type_name": "油车-旧",
                "plate_no": "赣G872F6",
                "engine_no": "M3GA4904371",
                "vin": "L6T7622Z6MF008872",
                "renewal_lookup": {
                    "found": True,
                    "selected": selected,
                    "candidates": [selected],
                },
                "quote_field_overrides": {
                    "车上人员责任险（司机）": "30000",
                    "车上人员责任险（乘客）": "30000",
                },
            },
            "default_config_json": {
                "车上人员责任险（司机）": "30000",
                "车上人员责任险（乘客）": "30000",
                "途家安顺保费": "0",
            },
            "platform_default_config": {"resolved_type_name": "油车-旧"},
        }
        ctx = SimpleNamespace(account_type_name="油车-旧")
        body = _adapter()._prepare_renewal_used_fuel_quote(client, ctx, payload, account_type_name="油车-旧")
        form = body["quoteForm"]
        self.assertEqual(form["prpCitemKindVos[3].kindCode"], "051052")
        self.assertEqual(form["prpCitemKindVos[3].amount"], "30000")
        self.assertEqual(form["prpCitemKindVos[4].kindCode"], "051053")
        self.assertEqual(form["prpCitemKindVos[4].amount"], "30000")
        self.assertEqual(form["prpCitemKindVos[5].amount"], "3000000")

        self.assertEqual(form["prpCitemKindVos[5].sharedAmountFlag"], "1")
        self.assertEqual(body["jointSaleForm"]["tujiaAnshun"]["premium"], "0")
        self.assertEqual(body["jointSaleForm"]["tujiaAnshun"]["amount"], "0")

        final_response = _har_response_json(har, 66)
        self.assertEqual(final_response["data"]["sumYelPremium"], 0)
        result = _adapter()._build_motor_quote_result_from_response(ctx, {}, body, final_response)
        self.assertEqual(result["risk_score"], 44)
        self.assertEqual(str(result["premium_total"]), "2921.46")
        self.assertEqual(result["result_card"]["commercial_premium"], "1766.46")
        self.assertEqual(result["result_card"]["compulsory_premium"], "855.00")
        self.assertEqual(result["result_card"]["vehicle_tax"], "300.00")
        self.assertEqual(result["result_card"]["joint_sales_premium"], "")
        self.assertEqual(result["result_card"]["total_without_vehicle_tax"], "2621.46")
        self.assertEqual(result["result_card"]["total_with_vehicle_tax"], "2921.46")

    def test_0817_implicit_renewal_quote_adjusts_compulsory_hour_like_manual_har(self) -> None:
        har = _load_0817_correct_quote_har()
        source_form = _har_form_params(har, 133)
        expected_form = _har_form_params(har, 174)
        adapter = _adapter()
        client = _HarRouteClient(
            {
                "getCurrentTime.do": {
                    "status": 0,
                    "data": {"currentTime": "2026-08-17"},
                }
            }
        )
        request_body = {
            "accountTypeName": "新能源车-旧",
            "quoteForm": source_form,
            "vehicleForm": {
                "startDateBI": source_form["prpCmain.startDate"],
                "startDateCI": source_form["prpCmain.startDateCI"],
            },
            "defaultFields": {},
            "preflight": {},
        }

        adjustment = adapter._insurance_date_adjustment_from_platform_response(
            client,
            _har_response_json(har, 133),
            request_body=request_body,
        )
        self.assertEqual(adjustment["adjustment_kinds"], ["ci"])
        self.assertEqual(adjustment["compulsory_start_date"], "2026-09-30")
        self.assertEqual(adjustment["compulsory_start_hour"], "14")

        adjusted_body, changed, notice = adapter._apply_insurance_date_adjustment_to_request_body(
            client,
            request_body,
            adjustment,
        )
        self.assertTrue(changed)
        adjusted_form = adjusted_body["quoteForm"]
        self.assertEqual(adjusted_form["prpCmain.startDate"], expected_form["prpCmain.startDate"])
        self.assertEqual(adjusted_form["prpCmain.starthourbi"], expected_form["prpCmain.starthourbi"])
        self.assertEqual(adjusted_form["prpCmain.startDateCI"], expected_form["prpCmain.startDateCI"])
        self.assertEqual(adjusted_form["prpCmain.starthourci"], expected_form["prpCmain.starthourci"])
        self.assertEqual(adjusted_form["prpCmain.endDateCI"], expected_form["prpCmain.endDateCI"])
        self.assertEqual(adjusted_form["prpCmain.endhourci"], expected_form["prpCmain.endhourci"])
        self.assertEqual(notice["compulsory_start_hour"], "14")

    def test_0818_sync_period_keeps_configured_coverages_and_adds_road_rescue(self) -> None:
        har = _load_0818_smooth_quote_har()
        source_form = _har_form_params(har, 72)
        expected_form = _har_form_params(har, 142)
        adapter = _adapter()
        client = _HarRouteClient(
            {
                "getCurrentTime.do": {
                    "status": 0,
                    "data": {"currentTime": "2026-08-18"},
                }
            }
        )
        request_body = {
            "accountTypeName": "新能源车-旧",
            "quoteForm": source_form,
            "vehicleForm": {
                "startDateBI": source_form["prpCmain.startDate"],
                "startDateCI": source_form["prpCmain.startDateCI"],
                "seatCount": source_form.get("prpCitemCar.seatCount", "5"),
            },
            "defaultFields": {},
            "preflight": {},
        }

        adjustment = adapter._insurance_date_adjustment_from_platform_response(
            client,
            _har_response_json(har, 72),
            request_body=request_body,
        )
        self.assertEqual(adjustment["adjustment_kinds"], ["bi"])
        self.assertEqual(adjustment["commercial_start_date"], "2026-10-01")
        self.assertTrue(adjustment.get("reinsure_items"))

        adjusted_body, changed, notice = adapter._apply_insurance_date_adjustment_to_request_body(
            client,
            request_body,
            adjustment,
        )
        self.assertTrue(changed)
        adjusted_form = adjusted_body["quoteForm"]
        self.assertEqual(adjusted_form["prpCmain.startDate"], expected_form["prpCmain.startDate"])
        self.assertEqual(adjusted_form["prpCitemKindVos[2].amount"], expected_form["prpCitemKindVos[2].amount"])
        self.assertEqual(adjusted_form["prpCitemKindVos[3].amount"], expected_form["prpCitemKindVos[3].amount"])
        self.assertEqual(adjusted_form["prpCitemKindVos[4].amount"], expected_form["prpCitemKindVos[4].amount"])
        self.assertEqual(adjusted_form["prpCitemKindVos[5].amount"], expected_form["prpCitemKindVos[5].amount"])
        self.assertEqual(adjusted_form["prpCitemKindVos[5].sharedAmountFlag"], expected_form["prpCitemKindVos[5].sharedAmountFlag"])
        road_rescue_index = _quote_form_kind_index(adjusted_form, "051064")
        self.assertIsNotNone(road_rescue_index)
        self.assertEqual(adjusted_form[f"prpCitemKindVos[{road_rescue_index}].quantity"], "7")
        self.assertEqual(notice["commercial_start_date"], "2026-10-01")

    def test_0818_sync_period_restores_missing_passenger_before_medical(self) -> None:
        har = _load_0818_original_quote_har()
        source_form = _har_form_params(har, 52)
        adapter = _adapter()
        client = _HarRouteClient(
            {
                "getCurrentTime.do": {
                    "status": 0,
                    "data": {"currentTime": "2026-08-18"},
                }
            }
        )
        request_body = {
            "accountTypeName": "新能源车-旧",
            "quoteForm": source_form,
            "vehicleForm": {
                "startDateBI": source_form["prpCmain.startDate"],
                "startDateCI": source_form["prpCmain.startDateCI"],
                "seatCount": source_form.get("prpCitemCar.seatCount", "5"),
            },
            "defaultFields": {},
            "preflight": {},
        }

        adjustment = adapter._insurance_date_adjustment_from_platform_response(
            client,
            _har_response_json(har, 52),
            request_body=request_body,
        )
        adjusted_body, changed, _notice = adapter._apply_insurance_date_adjustment_to_request_body(
            client,
            request_body,
            adjustment,
        )
        self.assertTrue(changed)
        form = adjusted_body["quoteForm"]
        self.assertEqual(form["prpCitemKindVos[2].kindCode"], "051051")
        self.assertEqual(form["prpCitemKindVos[2].amount"], "300")
        self.assertEqual(form["prpCitemKindVos[3].kindCode"], "051052")
        self.assertEqual(form["prpCitemKindVos[3].amount"], "40000")
        self.assertEqual(form["prpCitemKindVos[4].kindCode"], "051053")
        self.assertEqual(form["prpCitemKindVos[4].amount"], "40000")
        self.assertEqual(form["prpCitemKindVos[5].kindCode"], "051063")
        self.assertEqual(form["prpCitemKindVos[5].amount"], "3000000")
        self.assertEqual(form["prpCitemKindVos[5].sharedAmountFlag"], "1")
        self.assertEqual(form["prpCitemKindVos[6].kindCode"], "051064")
        self.assertEqual(form["prpCitemKindVos[6].quantity"], "7")

    def test_0818_result_keeps_response_road_rescue_quantity_when_response_returns_zero(self) -> None:
        har = _load_0818_smooth_quote_har()
        request_body = {
            "accountTypeName": "新能源车-旧",
            "quoteForm": _har_form_params(har, 142),
            "vehicleForm": {
                "licenseNo": "赣KF88172",
                "vin": "LC0C76C4XR6182655",
                "engineNo": "W24133464",
                "seatCount": "5",
            },
            "ownerForm": {"ownerName": "柯雄"},
            "jointSaleForm": {"tujiaAnshun": {"enabled": False, "success": True, "premium": "0", "amount": "0"}},
            "preflight": {},
        }
        ctx = SimpleNamespace(account_type_name="新能源车-旧")
        result = _adapter()._build_motor_quote_result_from_response(
            ctx,
            {},
            request_body,
            _har_response_json(har, 142),
        )
        road_rescue = next(
            item
            for item in result["result_card"]["proposal_coverage_items"]
            if item["code"] == "051064"
        )
        self.assertEqual(road_rescue["amount_text"], "-")


class QuoteFailureEnvelopeTests(unittest.TestCase):
    def test_failure_fields_and_attach_force_visibility(self) -> None:
        fields = _quote_failure_fields(
            code=FAILURE_CODE_STALE_TIMEOUT,
            reason="报价任务超时，已自动中止",
        )
        self.assertEqual(fields["failure_code"], FAILURE_CODE_STALE_TIMEOUT)
        self.assertTrue(fields["failure_reason"])
        self.assertTrue(fields["next_action"])

        data = _mk_data(result_status=RESULT_FAILED, message="x", payload={"silent": True})
        data["silent"] = True
        data["ui_visible"] = False
        _attach_quote_failure(
            data,
            code=FAILURE_CODE_PLATFORM,
            reason="平台返回业务错误",
        )
        self.assertEqual(data["failure_code"], FAILURE_CODE_PLATFORM)
        self.assertFalse(data["silent"])
        self.assertTrue(data["ui_visible"])
        self.assertEqual(data["payload"]["failure_code"], FAILURE_CODE_PLATFORM)


class AsyncQuoteImageFailureTests(unittest.TestCase):
    def test_failed_async_image_generation_clears_pending_flag(self) -> None:
        result = {
            "result_image_pending": True,
            "result_image_async": False,
            "total_premium": "1234.56",
        }
        _quote_result_mark_async_image_failed(result)
        self.assertFalse(result["result_image_pending"], result)
        self.assertTrue(result["result_image_async_failed"], result)

    def test_pending_image_detection_accepts_string_true(self) -> None:
        with patch("app.services.ai_assistant_service.quote_result_real_data_error", return_value=""):
            self.assertTrue(_quote_result_needs_async_image({"result_image_pending": "true"}))

    def test_garbled_chat_exception_detail_is_humanized(self) -> None:
        self.assertEqual(_humanize_exception(Exception("'' is")), "处理失败，请稍后重试")

    def test_quote_result_image_payload_includes_timing_breakdown(self) -> None:
        with patch("app.services.quote_result_image.render_quote_result_card_png", return_value=b"png"), patch(
            "app.services.quote_result_image._storage.put_object"
        ) as put_object, patch(
            "app.services.quote_result_image._storage.object_public_url",
            return_value="https://storage.test/quote.png",
        ):
            payload = save_quote_result_card_image({"title": "报价结果"}, trace_id="trace-1")

        self.assertEqual(payload["image_url"], "https://storage.test/quote.png")
        self.assertIn("render_ms", payload)
        self.assertIn("upload_ms", payload)
        self.assertIn("total_ms", payload)
        put_object.assert_called_once()

    def test_pending_image_scheduler_uses_asyncio_create_task(self) -> None:
        created = []

        def fake_create_task(coro):
            created.append(coro)
            coro.close()
            return object()

        with patch("app.services.ai_assistant_service.asyncio.create_task", side_effect=fake_create_task):
            scheduled = _schedule_async_quote_result_image_completion_once(
                owner_user_id=1,
                session_id="sid",
                assistant_message_id="mid",
                quote_task_id=8,
                trace_id="trace-1",
            )

        self.assertTrue(scheduled)
        self.assertEqual(len(created), 1)

    def test_pending_image_scheduler_rejects_missing_identifiers(self) -> None:
        with patch("app.services.ai_assistant_service.asyncio.create_task") as create_task:
            self.assertFalse(
                _schedule_async_quote_result_image_completion_once(
                    owner_user_id=1,
                    session_id="",
                    assistant_message_id="mid",
                    quote_task_id=8,
                    trace_id="trace-1",
                )
            )
            self.assertFalse(
                _schedule_async_quote_result_image_completion_once(
                    owner_user_id=0,
                    session_id="sid",
                    assistant_message_id="mid",
                    quote_task_id=8,
                    trace_id="trace-1",
                )
            )
        create_task.assert_not_called()

    def test_history_page_reschedules_pending_image(self) -> None:
        scheduled = []

        def fake_schedule(**kwargs):
            scheduled.append(kwargs)
            return True

        page = [
            {
                "id": "m1",
                "metadata": {
                    "data": {
                        "payload": {
                            "quote_result": {
                                "result_image_pending": True,
                                "result_card": {"title": "报价结果"},
                                "trace_id": "trace-1",
                            },
                            "quote_task": {"id": 8},
                        }
                    }
                },
            }
        ]
        with patch("app.services.ai_assistant_service._quote_result_needs_async_image", return_value=True), patch(
            "app.services.ai_assistant_service._schedule_async_quote_result_image_completion_once",
            side_effect=fake_schedule,
        ):
            import asyncio

            asyncio.run(_reschedule_pending_quote_result_images_from_page(owner_user_id=1, session_id="sid", items=page))
        self.assertEqual(len(scheduled), 1, scheduled)
        self.assertEqual(scheduled[0]["assistant_message_id"], "m1")
        self.assertEqual(scheduled[0]["quote_task_id"], 8)

    def test_dialog_subtype_maps_to_failure_code(self) -> None:
        self.assertEqual(
            _failure_code_for_platform_dialog_subtype("session_expired"),
            FAILURE_CODE_SESSION_EXPIRED,
        )
        self.assertEqual(
            _failure_code_for_platform_dialog_subtype("quota_full"),
            "quota_full",
        )
        self.assertEqual(
            _failure_code_for_platform_dialog_subtype("quote_business_error"),
            FAILURE_CODE_PLATFORM,
        )

    def test_build_user_failure_response_is_visible(self) -> None:
        case = SimpleNamespace(id=11, order_id=22, case_no="QA1", status=CASE_STATUS_READY)
        task = SimpleNamespace(id=33, status="failed")
        reply, body = _build_quote_user_failure_response(
            reply="人保报价材料已更新，我已停止本次报价；请确认材料后重新发起报价。",
            case=case,
            task=task,
            trace_id="t-1",
            failure_code=FAILURE_CODE_MATERIAL_CHANGED,
            failure_reason="材料已更新，请重新发起报价",
            result_status=RESULT_NOT_READY,
            response_status="success",
        )
        self.assertIn("材料已更新", reply)
        self.assertFalse(body.get("silent"))
        self.assertTrue(body.get("ui_visible"))
        self.assertEqual(body["data"]["failure_code"], FAILURE_CODE_MATERIAL_CHANGED)
        self.assertEqual(body["data"]["payload"]["failure_code"], FAILURE_CODE_MATERIAL_CHANGED)
        self.assertTrue(body["data"]["next_action"])

        reply2, body2 = _build_quote_user_failure_response(
            reply="结果图失败",
            case=case,
            task=task,
            trace_id="t-2",
            failure_code=FAILURE_CODE_RESULT_MATERIALIZATION,
            failure_reason="真实报价结果无法生成",
        )
        self.assertEqual(body2["data"]["failure_code"], FAILURE_CODE_RESULT_MATERIALIZATION)
        self.assertEqual(body2["status"], "failed")

        reply3, body3 = _build_quote_user_failure_response(
            reply="默认参数已更新",
            case=case,
            task=task,
            trace_id="t-3",
            failure_code=FAILURE_CODE_DEFAULT_CONFIG_CHANGED,
            result_status=RESULT_NOT_READY,
            response_status="success",
        )
        self.assertEqual(body3["data"]["failure_code"], FAILURE_CODE_DEFAULT_CONFIG_CHANGED)
        self.assertFalse(body3.get("silent"))


class ChatSessionLockReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_is_noop_without_bound_lock(self) -> None:
        from app.services.chat_session_lock import release_chat_session_lock_for_platform_io

        marker = []
        async with release_chat_session_lock_for_platform_io():
            marker.append(1)
        self.assertEqual(marker, [1])

    async def test_release_allows_other_waiter_during_io(self) -> None:
        import asyncio
        from app.services.chat_session_lock import (
            bind_chat_session_lock,
            release_chat_session_lock_for_platform_io,
            reset_chat_session_lock,
        )

        lock = asyncio.Lock()
        order: list[str] = []
        io_started = asyncio.Event()
        waiter_done = asyncio.Event()

        async def holder() -> None:
            async with lock:
                token = bind_chat_session_lock(lock)
                try:
                    order.append("holder_start")
                    async with release_chat_session_lock_for_platform_io():
                        order.append("io_start")
                        io_started.set()
                        await waiter_done.wait()
                        order.append("io_end")
                    order.append("holder_end")
                finally:
                    reset_chat_session_lock(token)

        async def waiter() -> None:
            await io_started.wait()
            async with lock:
                order.append("waiter")
            waiter_done.set()

        await asyncio.gather(holder(), waiter())
        self.assertEqual(order, ["holder_start", "io_start", "waiter", "io_end", "holder_end"])

    async def test_nested_release_is_reentrant(self) -> None:
        import asyncio
        from app.services.chat_session_lock import (
            bind_chat_session_lock,
            release_chat_session_lock_for_platform_io,
            reset_chat_session_lock,
        )

        lock = asyncio.Lock()
        async with lock:
            token = bind_chat_session_lock(lock)
            try:
                async with release_chat_session_lock_for_platform_io():
                    self.assertFalse(lock.locked())
                    async with release_chat_session_lock_for_platform_io():
                        self.assertFalse(lock.locked())
                    self.assertFalse(lock.locked())
                self.assertTrue(lock.locked())
            finally:
                reset_chat_session_lock(token)


class QuotePreflightChecklistTests(unittest.TestCase):
    def test_material_and_account_combined_reply(self) -> None:
        items = _material_preflight_items(
            [
                {"type": "field", "key": "plate_no", "label": "车牌号"},
                {"type": "image", "key": "vehicle_cert", "label": "行驶证"},
            ]
        )
        items.append(
            {
                "code": "account_login",
                "category": "account",
                "label": "人保没有已登录可用账号",
                "detail": "",
                "failure_code": FAILURE_CODE_ACCOUNT_LOGIN,
            }
        )
        items.append(
            {
                "code": "default_config_missing",
                "category": "default_config",
                "label": "人保（新能源车-旧）尚未启用默认参数配置",
                "detail": "",
                "failure_code": FAILURE_CODE_DEFAULT_CONFIG_MISSING,
            }
        )
        self.assertEqual(_primary_preflight_failure_code(items), FAILURE_CODE_MATERIAL_MISSING)
        reply = _format_quote_preflight_reply(platform_name="人保", items=items, override_summary="商业险起保日")
        self.assertIn("报价前检查未通过", reply)
        self.assertIn("缺少字段", reply)
        self.assertIn("缺少图片", reply)
        self.assertIn("账号：", reply)
        self.assertIn("默认参数：", reply)
        self.assertIn("已记录本次调整：商业险起保日", reply)

        no_material = [x for x in items if x["failure_code"] != FAILURE_CODE_MATERIAL_MISSING]
        self.assertEqual(_primary_preflight_failure_code(no_material), FAILURE_CODE_ACCOUNT_LOGIN)

        case = SimpleNamespace(id=7, order_id=8, case_no="QA7", status=CASE_STATUS_READY)
        reply2, body = _build_quote_preflight_blocked_response(
            case=case,
            platform_code="PICC",
            platform_name="人保",
            selected_account_type_name="新能源车-旧",
            items=no_material,
        )
        self.assertEqual(body["data"]["result_status"], RESULT_NEED_MORE)
        self.assertEqual(body["data"]["failure_code"], FAILURE_CODE_ACCOUNT_LOGIN)
        self.assertTrue(body["data"]["payload"]["preflight_blocked"])
        self.assertEqual(len(body["data"]["payload"]["preflight_checklist"]), 2)
        self.assertIn("账号：", reply2)


class QuoteChatPolarityWhitelistTests(unittest.TestCase):
    def test_exact_whitelist_only(self) -> None:
        self.assertEqual(_quote_chat_polarity_exact("好的"), QUOTE_CHAT_POLARITY_AFFIRM)
        self.assertEqual(_quote_chat_polarity_exact("继续报价"), QUOTE_CHAT_POLARITY_AFFIRM)
        self.assertEqual(_quote_chat_polarity_exact("取消"), QUOTE_CHAT_POLARITY_NEGATE)
        self.assertEqual(_quote_chat_polarity_exact("不要继续"), QUOTE_CHAT_POLARITY_NEGATE)
        self.assertIsNone(_quote_chat_polarity_exact("看着办"))
        self.assertIsNone(_quote_chat_polarity_exact("先这样吧"))
        self.assertIsNone(_quote_chat_polarity_exact("嗯嗯可以吧"))

    def test_duplicate_confirm_helpers_and_unclear_attempt(self) -> None:
        self.assertTrue(_is_duplicate_quote_confirmation_text("可以"))
        self.assertTrue(_is_duplicate_quote_cancel_text("算了"))
        self.assertFalse(_is_duplicate_quote_confirmation_text("看着办"))
        self.assertTrue(_looks_like_unclear_chat_polarity_attempt("看着办"))
        self.assertTrue(_looks_like_unclear_chat_polarity_attempt("嗯嗯"))
        self.assertFalse(_looks_like_unclear_chat_polarity_attempt("继续报价"))
        self.assertFalse(_looks_like_unclear_chat_polarity_attempt("查订单"))
        self.assertIn("继续报价", QUOTE_DUPLICATE_CONFIRM_HINT)
        exclusions = _extract_quote_product_exclusions("不要车损")
        self.assertTrue(any("损失" in item or "车损" in item for item in exclusions))


if __name__ == "__main__":
    unittest.main(verbosity=2)
