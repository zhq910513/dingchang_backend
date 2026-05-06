from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ocr_cleaner import (  # noqa: E402
    CLEANING_RULE_VERSION,
    clean_dynamic_data_for_ocr,
    describe_cleaning_rules,
)
from app.services.ocr_worker import _merge_if_empty  # noqa: E402
from app.services.order_fact_service import build_order_fact_payload  # noqa: E402


RUN_LOG = Path("logs/ocr-cleaner-retest.json")


def _assert_equal(label: str, actual: Any, expected: Any, failures: list[dict[str, Any]]) -> None:
    ok = actual == expected
    item = {"label": label, "ok": ok, "actual": actual, "expected": expected}
    if not ok:
        failures.append(item)


def main() -> int:
    failures: list[dict[str, Any]] = []

    dirty = {
        "owner_name": " \u59d3\u540d: \u5f20\u4e09 ",
        "plate_no": " \u53f7\u724c\u53f7\u7801: \u8d63 A12345 ",
        "vin": "\u8f66\u8f86\u8bc6\u522b\u4ee3\u53f7: LC0C76C41R4095092 5\u4eba",
        "engine_no": " \u53d1\u52a8\u673a\u53f7: A024676-5 ",
        "vehicle_model": " \u54c1\u724c\u578b\u53f7: \u6bd4\u4e9a\u8fea \u79e6PLUS ",
        "first_register_date": "2024\u5e741\u67082\u65e5",
        "issue_date": "2024/01/12",
        "id_number": "\u516c\u6c11\u8eab\u4efd\u53f7\u7801:360426199101134023\u516c",
        "id_birth_date": "",
        "id_validity": "2013.3.5 - \u957f\u671f",
        "approved_passenger_count": "\u4e94\u4eba",
        "manufacturer_name": "\u8054\u7cfb\u4eba:\u9a6c\u9b41\u57fa/\u8054\u7cfb\u7535\u8bdd:023-67921044",
        "vehicle_brand_name": "\u4e2d\u56fd",
        "id_birth": "19910113",
        "id_nation": "\u6c49",
        "register_date": "20240101",
        "id_issue_authority": " \u5fb7\u5b89\u53bf\u516c\u5b89\u5c40 ",
        "dla_approved_passengers": "7\u4eba",
        "dl_old_key": "should be removed",
    }
    cleaned = clean_dynamic_data_for_ocr(dirty)

    expectations = {
        "owner_name": "\u5f20\u4e09",
        "plate_no": "\u8d63A12345",
        "vin": "LC0C76C41R4095092",
        "engine_no": "A0246765",
        "vehicle_model": "\u6bd4\u4e9a\u8fea \u79e6PLUS",
        "first_register_date": "2024-01-02",
        "issue_date": "2024-01-12",
        "id_number": "360426199101134023",
        "id_birth_date": "1991-01-13",
        "id_valid_from": "2013-03-05",
        "id_valid_to": "\u957f\u671f",
        "approved_passenger_count": "5",
        "manufacturer_name": None,
        "vehicle_brand_name": None,
        "id_ethnicity": "\u6c49",
        "id_issuer": "\u5fb7\u5b89\u53bf\u516c\u5b89\u5c40",
    }

    for key, expected in expectations.items():
        _assert_equal(f"clean_{key}", cleaned.get(key), expected, failures)

    for removed_key in (
        "id_birth",
        "id_nation",
        "register_date",
        "id_issue_authority",
        "dla_approved_passengers",
        "dl_old_key",
    ):
        _assert_equal(f"removed_{removed_key}", removed_key in cleaned, False, failures)

    invalid = clean_dynamic_data_for_ocr(
        {
            "vin": "KG",
            "plate_no": "\u672a\u4e0a\u724c",
            "id_number": "\u745e\u660c\u4e30\u5ea6\u7269\u6d41\u6709\u9650\u516c\u53f8",
            "first_register_date": "2024-13-40",
            "approved_passenger_count": "0\u4eba",
            "manufacturer_name": "(m1)/\u6700\u5927\u51c0\u529f\u7387(kW)",
        }
    )
    _assert_equal("invalid_vin_null", invalid.get("vin"), None, failures)
    _assert_equal("invalid_plate_null", invalid.get("plate_no"), None, failures)
    _assert_equal("invalid_id_number_null", invalid.get("id_number"), None, failures)
    _assert_equal("invalid_date_null", invalid.get("first_register_date"), None, failures)
    _assert_equal("invalid_passenger_null", invalid.get("approved_passenger_count"), None, failures)
    _assert_equal("invalid_manufacturer_null", invalid.get("manufacturer_name"), None, failures)

    compatibility_cases = {
        "date_yyyymmdd": ({"first_register_date": "20240112"}, "first_register_date", "2024-01-12"),
        "date_month_level": ({"first_register_date": "202401"}, "first_register_date", "2024-01"),
        "social_credit_kept": ({"id_number": " 91360123MA7EL3W17Q "}, "id_number", "91360123MA7EL3W17Q"),
        "passenger_sum_expression": ({"approved_passenger_count": "2+3\u4eba"}, "approved_passenger_count", "5"),
    }
    for label, (payload, key, expected) in compatibility_cases.items():
        _assert_equal(label, clean_dynamic_data_for_ocr(payload).get(key), expected, failures)

    cleaned_existing = clean_dynamic_data_for_ocr({"vin": "KG"})
    cleaned_extracted = clean_dynamic_data_for_ocr({"vin": "LC0C76C41R4095092 5\u4eba"})
    merged = _merge_if_empty(cleaned_existing, cleaned_extracted)
    _assert_equal("ocr_merge_fills_invalid_existing", merged.get("vin"), "LC0C76C41R4095092", failures)

    manual_existing = clean_dynamic_data_for_ocr({"vin": "LVAV2JVB0JE111269"})
    other_extracted = clean_dynamic_data_for_ocr({"vin": "LC0C76C41R4095092"})
    kept = _merge_if_empty(manual_existing, other_extracted)
    _assert_equal("ocr_merge_keeps_valid_existing", kept.get("vin"), "LVAV2JVB0JE111269", failures)

    fact = build_order_fact_payload(cleaned)
    _assert_equal("fact_vin", fact.get("vin"), "LC0C76C41R4095092", failures)
    _assert_equal("fact_first_register_date", str(fact.get("first_register_date")), "2024-01-02", failures)
    _assert_equal("fact_id_number", fact.get("id_number"), "360426199101134023", failures)

    doc = {
        "ok": not failures,
        "rule_version": CLEANING_RULE_VERSION,
        "rules": describe_cleaning_rules(),
        "checked": 39,
        "failures": failures,
        "sample_cleaned": cleaned,
        "sample_fact": {key: str(value) if value is not None else None for key, value in fact.items()},
    }
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    RUN_LOG.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": doc["ok"], "log": str(RUN_LOG), "failures": failures[:5]}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
