# -*- coding: utf-8 -*-
"""Offline regression checks for the quote assistant's PICC state transitions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.quote_assistant_service import (
    _is_runtime_duplicate_quote_result,
    _runtime_detail,
    _quote_end_date_text,
    _quote_auto_notice_message_id,
    _quote_snapshot_with_auto_adjusted_dates,
    _quote_auto_notice_dedupe_key,
    _is_quote_auto_notice_duplicate_error,
)
from app.services.ai_assistant_service import (
    _message_preview_text,
    _session_preview_needs_recompute,
)
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
    _reinsure_notice_adjustment_kinds,
    _reinsure_notice_suggested_start_date,
)


def _adapter() -> PiccBusinessAdapter:
    # The tested method only touches the client when a selected vehicle exists.
    return object.__new__(PiccBusinessAdapter)


class _QuoteResponseClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.config = SimpleNamespace(base_url="https://picc.test")

    def request_json(self, *args: object, **kwargs: object) -> dict:
        return self.response


class PiccPICCQuoteProfileRegressionTests(unittest.TestCase):
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

    def test_explicit_default_config_can_override_profile_license_and_tax_fields(self) -> None:
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
        self.assertEqual(form["prpCitemCar.licenseType"], "99")
        self.assertEqual(form["prpCitemCar.licenseColorCode"], "88")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
