# -*- coding: utf-8 -*-
"""Focused regression checks for manual passenger-count corrections."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import ai_assistant_service
from app.services.quote_assistant_service import (
    QUOTE_DATA_OVERRIDES_KEY,
    _normalize_quote_case_data,
    _quote_material_form_payload,
    _quote_material_form_rebased_case_data,
    _quote_material_form_overrides_from_values,
    _quote_material_form_validation_error,
    detect_quote_data_override_signal,
    extract_quote_data_overrides,
)
from app.services.quote_platforms.platforms.picc.business import (
    PiccBusinessAdapter,
    PiccRequestError,
    _effective_vehicle_seat_count,
    _accept_platform_returned_vehicle_body,
    _vehicle_candidate_compatibility,
)


class QuoteSeatOverrideTests(unittest.TestCase):
    def test_command_variants_only_keep_target_value(self):
        for text in (
            "座位 改7",
            "座位6改7",
            "6座改7座",
            "座位数改为7",
            "核定载客数改成7人",
            "核定载客量调整为7",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    extract_quote_data_overrides(text),
                    {"approved_passenger_count": "7"},
                )
                signal = detect_quote_data_override_signal(text)
                self.assertTrue(signal["is_override"])
                self.assertTrue(signal["entities"]["force_requote"])

    def test_invalid_and_unrelated_commands_do_not_set_seat_count(self):
        for text in ("座位改0", "座位改100", "座位改7.5", "座位险改3万"):
            with self.subTest(text=text):
                self.assertNotIn("approved_passenger_count", extract_quote_data_overrides(text))

    def test_override_wins_over_recognized_value_and_is_persisted(self):
        normalized = _normalize_quote_case_data(
            base_data={"approved_passenger_count": "6"},
            order_data={},
            text_data={
                "approved_passenger_count": "7",
                QUOTE_DATA_OVERRIDES_KEY: {"approved_passenger_count": "7"},
            },
            images_by_slot={
                "vehicle_cert": [
                    {
                        "method": "upload",
                        "text_features": {"quote_image_upload": True},
                        "extracted_fields": {"approved_passenger_count": "6"},
                    }
                ]
            },
        )
        self.assertEqual(normalized["approved_passenger_count"], "7")
        self.assertEqual(
            normalized[QUOTE_DATA_OVERRIDES_KEY]["approved_passenger_count"],
            "7",
        )

    def test_removing_form_field_withdraws_override_and_falls_back_to_ocr(self):
        rebased, cleared = _quote_material_form_rebased_case_data(
            {
                "approved_passenger_count": "7",
                QUOTE_DATA_OVERRIDES_KEY: {"approved_passenger_count": "7"},
            },
            {},
            {"approved_passenger_count"},
        )
        self.assertIn("approved_passenger_count", cleared)
        self.assertNotIn("approved_passenger_count", rebased)
        self.assertNotIn(QUOTE_DATA_OVERRIDES_KEY, rebased)
        normalized = _normalize_quote_case_data(
            base_data=rebased,
            order_data={},
            text_data={},
            images_by_slot={
                "vehicle_cert": [
                    {
                        "method": "upload",
                        "text_features": {"quote_image_upload": True},
                        "extracted_fields": {"approved_passenger_count": "6"},
                    }
                ]
            },
        )
        self.assertEqual(normalized["approved_passenger_count"], "6")

    def test_material_form_exposes_existing_seat_count_as_editable_optional_field(self):
        payload = _quote_material_form_payload(
            mode="supplement",
            normalized_data={"approved_passenger_count": "6"},
            missing=[],
            platform_code="PICC",
            platform_name="人保",
            account_type_name="油车-旧",
        )
        optional = {item["key"]: item for item in payload["optional_fields"]}
        self.assertEqual(optional["approved_passenger_count"]["type"], "number")
        self.assertEqual(optional["approved_passenger_count"]["value"], "6")
        self.assertIn("approved_passenger_count", payload["selected_extra_field_keys"])

    def test_material_form_validates_seat_count_strictly(self):
        self.assertEqual(_quote_material_form_validation_error({"approved_passenger_count": "7"}), "")
        self.assertTrue(_quote_material_form_validation_error({"approved_passenger_count": "0"}))
        self.assertTrue(_quote_material_form_validation_error({"approved_passenger_count": "100"}))
        self.assertTrue(_quote_material_form_validation_error({"approved_passenger_count": "7座"}))

    def test_form_value_becomes_a_manual_override_after_normalization(self):
        overrides = _quote_material_form_overrides_from_values({"approved_passenger_count": "7"})
        normalized = _normalize_quote_case_data(
            base_data={"approved_passenger_count": "6"},
            order_data={},
            text_data={QUOTE_DATA_OVERRIDES_KEY: overrides},
            images_by_slot={},
        )
        vehicle = PiccBusinessAdapter()._base_used_fuel_vehicle({}, normalized, profile={})
        self.assertEqual(vehicle["seatCount"], "7")
        self.assertTrue(vehicle["seatCountManualOverride"])

    def test_offline_draft_does_not_restore_old_request_seats(self):
        payload = {
            "normalized_data": {
                "approved_passenger_count": "7",
                QUOTE_DATA_OVERRIDES_KEY: {"approved_passenger_count": "7"},
            },
            "request_body": {"vehicleForm": {"seatCount": "6", "seatCountConfirmed": True}},
        }
        body = PiccBusinessAdapter()._assemble_used_fuel_offline_request_body(object(), payload)
        self.assertEqual(body["vehicleForm"]["seatCount"], "7")
        self.assertEqual(body["quoteForm"]["prpCitemCar.seatCount"], "7")
        self.assertEqual(body["quoteForm"]["quantity"], "6")

    def test_retry_cannot_auto_accept_a_conflicting_precise_model(self):
        body = {
            "vehicleForm": {
                "seatCount": "7", "seatCountConfirmed": True, "seatCountManualOverride": True,
            },
            "quoteForm": {"prpCitemCar.seatCount": "7"},
            "preflight": {"preciseVehicle": {"vehicleSeat": "6", "vehicleId": "MODEL-6"}},
        }
        with self.assertRaises(PiccRequestError):
            _accept_platform_returned_vehicle_body(body)

    def test_picc_vehicle_and_quote_form_use_confirmed_value(self):
        adapter = PiccBusinessAdapter()
        vehicle = adapter._base_used_fuel_vehicle(
            {},
            {
                "approved_passenger_count": "7",
                QUOTE_DATA_OVERRIDES_KEY: {"approved_passenger_count": "7"},
                "vehicle_model": "测试车型",
                "vin": "TESTVIN1234567890",
                "engine_no": "TEST1234",
            },
            profile={},
        )
        self.assertEqual(vehicle["seatCount"], "7")
        self.assertTrue(vehicle["seatCountConfirmed"])
        self.assertTrue(vehicle["seatCountManualOverride"])

        selected = {"vehicleSeat": "7", "vehicleName": "测试车型"}
        products = adapter._used_fuel_products({}, profile={}, actual_value="100000", seat_count="7")
        form = adapter._build_used_fuel_quote_form(
            {},
            vehicle,
            {"ownerName": "测试车主"},
            selected,
            {},
            products,
            profile={},
            strict_config=False,
        )
        self.assertEqual(form["prpCitemCar.seatCount"], "7")
        self.assertEqual(form["quantity"], "6")
        passenger_product = next(item for item in products if item.get("code") == "BI061")
        self.assertEqual(passenger_product["insuredAmount"], "120000")

    def test_picc_rejects_only_explicit_platform_model_conflict(self):
        vehicle = {
            "seatCount": "7",
            "seatCountConfirmed": True,
            "seatCountManualOverride": True,
        }
        self.assertEqual(_effective_vehicle_seat_count(vehicle, "7"), "7")
        self.assertEqual(_effective_vehicle_seat_count(vehicle, ""), "7")
        compatible, reason = _vehicle_candidate_compatibility({"vehicleSeat": "6"}, vehicle)
        self.assertFalse(compatible)
        self.assertEqual(reason, "seat_count_mismatch")
        with self.assertRaises(PiccRequestError):
            _effective_vehicle_seat_count(vehicle, "6")

    def test_picc_keeps_legacy_seat_precedence_without_manual_override(self):
        recognized_vehicle = {"seatCount": "6", "seatCountConfirmed": True}
        self.assertEqual(
            _effective_vehicle_seat_count(
                recognized_vehicle,
                "7",
                fallback="7",
            ),
            "7",
        )
        self.assertEqual(_effective_vehicle_seat_count(recognized_vehicle, "7"), "6")

        adapter = PiccBusinessAdapter()
        selected = {"vehicleSeat": "7", "vehicleName": "测试车型"}
        products = adapter._used_fuel_products({}, profile={}, actual_value="100000", seat_count="6")
        form = adapter._build_used_fuel_quote_form(
            {},
            recognized_vehicle,
            {"ownerName": "测试车主"},
            selected,
            {},
            products,
            profile={},
            strict_config=False,
        )
        self.assertEqual(form["prpCitemCar.seatCount"], "6")
        self.assertEqual(form["quantity"], "5")

        ocr_vehicle = adapter._base_used_fuel_vehicle(
            {},
            {"approved_passenger_count": "6", "vehicle_model": "测试车型"},
            profile={},
        )
        self.assertNotIn("seatCountManualOverride", ocr_vehicle)

    def test_picc_precise_and_actual_value_requests_carry_confirmed_value(self):
        class RecordingClient:
            def __init__(self):
                self.config = SimpleNamespace(base_url="https://example.invalid")
                self.calls = []

            def request_json(self, method, path, **kwargs):
                self.calls.append((method, path, kwargs))
                return {"code": 0, "status": "0", "data": {}}

        adapter = PiccBusinessAdapter()
        client = RecordingClient()
        vehicle = {
            "seatCount": "7",
            "seatCountConfirmed": True,
            "seatCountManualOverride": True,
            "vin": "TESTVIN1234567890",
            "licenseNo": "TEST01",
            "licenseType": "02",
            "engineNo": "TEST1234",
        }
        selected = {"vehicleSeat": "7", "vehicleId": "MODEL-7", "purchasePrice": "100000"}
        adapter._query_precise_vehicle(client, vehicle, {}, selected, profile={})
        adapter._query_actual_value(client, vehicle, {}, selected, {}, profile={})
        self.assertEqual(client.calls[0][2]["params"]["seatCount"], "7")
        self.assertEqual(client.calls[1][2]["params"]["seatCount"], "7")

    def test_precise_response_cannot_silently_drop_manual_seat_conflicts(self):
        client = SimpleNamespace(
            config=SimpleNamespace(base_url="https://example.invalid"),
            request_json=lambda *args, **kwargs: {
                "code": 0, "status": "0", "result": [{"vehicleSeat": "6", "vehicleId": "MODEL-6"}],
            },
        )
        adapter = PiccBusinessAdapter()
        manual_vehicle = {"seatCount": "7", "seatCountConfirmed": True, "seatCountManualOverride": True}
        with self.assertRaises(PiccRequestError):
            adapter._query_precise_vehicle(client, manual_vehicle, {}, {"vehicleSeat": "7"}, profile={})
        recognized_vehicle = {"seatCount": "7", "seatCountConfirmed": True}
        self.assertTrue(adapter._query_precise_vehicle(client, recognized_vehicle, {}, {"vehicleSeat": "7"}, profile={}))


class QuoteSeatCommandRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_seat_command_enters_quote_adjustment_route(self):
        dispatch_result = (
            "ok",
            {"status": "success", "intent": "quote", "data": {}},
        )
        mocked = AsyncMock(return_value=dispatch_result)
        with patch.object(ai_assistant_service, "_dispatch_rule_with_db", new=mocked):
            result = await ai_assistant_service._dispatch_rule("座位 改7", {}, db=object())
        self.assertEqual(result, dispatch_result)
        call = mocked.await_args
        self.assertEqual(call.kwargs["intent"], "quote")
        self.assertEqual(
            call.kwargs["entities"][QUOTE_DATA_OVERRIDES_KEY]["approved_passenger_count"],
            "7",
        )
        self.assertTrue(call.kwargs["entities"]["force_requote"])


if __name__ == "__main__":
    unittest.main()
