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

from app.services.quote_assistant_service import (
    _is_runtime_duplicate_quote_result,
    _runtime_detail,
    _active_image_extracted_data,
    _quote_end_date_text,
    _quote_image_extracted_fields_from_features,
    _quote_auto_notice_message_id,
    _quote_snapshot_with_auto_adjusted_dates,
    _quote_result_insurance_date_auto_adjustments,
    _quote_auto_notice_dedupe_key,
    _is_quote_auto_notice_duplicate_error,
    _has_reusable_renewal_quote_context,
    _normalize_quote_case_data,
    _platform_default_values_with_legacy_fixes,
    _normalize_quote_product_exclusions,
    _extract_quote_product_exclusions,
    extract_quote_config_overrides,
)
from app.services.ai_assistant_service import (
    _message_preview_text,
    _session_preview_needs_recompute,
)
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
    _vehicle_candidate_score,
    _used_fuel_model_query_terms,
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


class PiccPICCQuoteProfileRegressionTests(unittest.TestCase):
    def test_merged_chinese_brand_model_code_has_standalone_fallback(self) -> None:
        terms = _used_fuel_model_query_terms(
            "雷克萨斯JTHKR5BH",
            "小型轿车",
            brand_name="",
            vehicle_name="",
            vin="JTHKR5BH3J2327186",
        )
        self.assertLess(terms.index("JTHKR5BH"), terms.index("雷克萨斯JTHKR5BH"))
        self.assertIn("JTHKR5BH轿车", terms)

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
                },
            ),
            0,
        )

    def test_vehicle_query_retries_standalone_model_code_after_combined_name(self) -> None:
        class _VehicleFallbackClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")
                self.names: list[str] = []

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                name = str(kwargs["params"]["jyVehicleRequest.vehicleName"])
                self.names.append(name)
                if "CT200h" in name:
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
            "vehicleNameHint": "",
            "vin": "JTHKR5BH3J2327186",
        }
        rows = _adapter()._query_vehicle_candidates(client, vehicle)
        self.assertEqual(len(rows["result"]), 1)
        self.assertEqual(client.names, ["雷克萨斯LEXUS CT200h轿车*"])
        self.assertEqual(vehicle["modelQueryMatched"], "雷克萨斯LEXUS CT200h轿车")

    def test_vin_prefix_is_used_when_model_field_only_contains_brand(self) -> None:
        terms = _used_fuel_model_query_terms(
            "雷克萨斯",
            "小型轿车",
            brand_name="雷克萨斯",
            vehicle_name="",
            vin="JTHKR5BH3J2327186",
        )
        self.assertLess(terms.index("CT200h"), terms.index("JTHKR5BH"))
        self.assertIn("雷克萨斯LEXUS CT200h轿车", terms)
        self.assertIn("JTHKR5BH", terms)
        self.assertIn("JTHKR5BH轿车", terms)

        row = {
            "vehicleName": "JTHKR5BH 小型轿车",
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
            0,
        )

    def test_vin_prefix_query_is_reached_after_brand_queries(self) -> None:
        class _VinFallbackClient:
            def __init__(self) -> None:
                self.config = SimpleNamespace(base_url="https://picc.test")
                self.names: list[str] = []

            def request_json(self, method: str, path: str, **kwargs: object) -> dict:
                name = str(kwargs["params"]["jyVehicleRequest.vehicleName"])
                self.names.append(name)
                if "CT200h" in name:
                    return {
                        "status": 0,
                        "result": [
                            {
                                "vehicleName": "JTHKR5BH 小型轿车",
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
        self.assertEqual(client.names, ["雷克萨斯LEXUS CT200h轿车*"])
        self.assertEqual(vehicle["modelQueryMatched"], "雷克萨斯LEXUS CT200h轿车")

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

    def test_renewal_prepare_merge_keeps_default_when_renewal_default_is_zero(self) -> None:
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
                    "车上人员责任险（司机）": "0",
                    "车上人员责任险（乘客）": "",
                    "第三者责任险": "300",
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
                    "车上人员责任险（司机）": "30000",
                    "车上人员责任险（乘客）": "30000",
                    "第三者责任险": "200",
                },
                "platform_default_config": {"resolved_type_name": "油车-旧"},
            },
            account_type_name="油车-旧",
        )

        merged_defaults = captured["default_config_json"]
        self.assertEqual(merged_defaults["车上人员责任险（司机）"], "30000")
        self.assertEqual(merged_defaults["车上人员责任险（乘客）"], "30000")
        self.assertEqual(merged_defaults["第三者责任险"], "300")
        self.assertEqual(merged_defaults["共享主险限额"], True)
        self.assertEqual(body["preflight"]["renewalQuoteFieldPriority"], "会话明确调参值 > 有效续保接口返回值 > 默认参数配置 > profile内置默认值")

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

    def test_0813_renewal_prefill_and_result_with_default_joint_sales(self) -> None:
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
        result = adapter._build_used_fuel_quote_result_from_response(ctx, {}, request_body, _har_response_json(har, 52))
        self.assertEqual(result["risk_score"], 45)
        self.assertEqual(str(result["premium_total"]), "3294.79")
        self.assertEqual(result["result_card"]["commercial_premium"], "1741.79")
        self.assertEqual(result["result_card"]["compulsory_premium"], "855.00")
        self.assertEqual(result["result_card"]["vehicle_tax"], "300.00")
        self.assertEqual(result["result_card"]["joint_sales_premium"], "398.00")
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
        result = _adapter()._build_used_fuel_quote_result_from_response(ctx, {}, body, final_response)
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

    def test_0818_result_uses_request_road_rescue_quantity_when_response_returns_zero(self) -> None:
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
        result = _adapter()._build_used_fuel_quote_result_from_response(
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
        self.assertEqual(road_rescue["amount_text"], "7次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
