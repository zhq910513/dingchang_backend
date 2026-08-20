# -*- coding: utf-8 -*-
"""Fail-closed checks for normalized quote-result truthfulness."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.quote_assistant_service import (
    _enrich_quote_result_for_display,
    _quote_runtime_result_or_failure,
    _runtime_result_payload,
)
from app.services.quote_platforms.base import PlatformRuntimeResult
from app.services.quote_platforms.platforms.picc.business import PiccBusinessAdapter
from app.services.quote_result_validation import quote_result_real_data_error
from app.api.v1.ai_assistant import _normalize_quote_result_metadata


def _result(**overrides: object) -> dict:
    result = {
        "status": "quoted",
        "mode": "picc_used_fuel_real",
        "platform_code": "PICC",
        "quote_provenance": {
            "source": "platform_quote_response",
            "platform_code": "PICC",
            "response_status": 0,
            "core_premium_evidence": [
                {
                    "name": "commercial",
                    "source": "quote_response.data.biPremium",
                    "value": "1288.80",
                }
            ],
            "normalized_amounts": {
                "commercial": {
                    "value": "1288.80",
                    "source": "quote_response.data.biPremium",
                },
                "total_with_vehicle_tax": {
                    "value": "1288.80",
                    "source": "quote_response.data.totalPremium",
                },
            },
        },
        "premium_total": "1288.80",
        "price_items": [{"name": "商业险", "amount": "1288.80"}],
        "result_card": {
            "style": "picc_proposal_table",
            "commercial_premium": "1288.80",
        },
    }
    result.update(overrides)
    return result


def _assert_rejected(name: str, result: dict) -> None:
    error = quote_result_real_data_error(result)
    assert error, (name, result)


def main() -> None:
    assert not quote_result_real_data_error(_result())
    assert not quote_result_real_data_error(
        _result(
            premium_total="",
            price_items=[{"name": "机动车损失保险", "amount": "32000", "premium": "812.60"}],
        )
    )

    _assert_rejected(
        "vehicle-tax-only",
        _result(
            premium_total="",
            price_items=[{"name": "车船税", "amount": "420.00"}],
            result_card={"vehicle_tax": "420.00"},
        ),
    )
    _assert_rejected(
        "coverage-amount-only",
        _result(
            premium_total="",
            price_items=[{"name": "机动车损失保险", "amount": "32000"}],
            result_card={"coverage_items": [{"name": "机动车损失保险", "amount": "32000"}]},
        ),
    )
    _assert_rejected(
        "risk-score-only",
        _result(premium_total="", price_items=[], risk_score=36, result_card={"risk_score": 36}),
    )
    _assert_rejected(
        "joint-sales-only",
        _result(
            premium_total="398.00",
            price_items=[{"name": "途家安顺", "amount": "398.00"}],
            result_card={"joint_sales_premium": "398.00"},
        ),
    )
    _assert_rejected(
        "addon-only",
        _result(
            premium_total="120.00",
            price_items=[
                {
                    "name": "附加医保外医疗费用责任险（机动车第三者责任保险）",
                    "premium": "120.00",
                }
            ],
        ),
    )
    addon_only_from_picc_parser = PiccBusinessAdapter()._build_used_fuel_quote_result_from_response(
        ctx=None,  # The result builder reads account type from the request body first.
        quote_payload={},
        request_body={
            "accountTypeName": "油车-旧",
            "vehicleForm": {},
            "ownerForm": {},
            "quoteForm": {},
            "preflight": {},
        },
        quote_response={
            "status": 0,
            "data": {
                "piccScore": "36",
                "quotationNo": "TEST-ADDON-ONLY",
            },
            "itemKindTempList": [
                {
                    "kindCode": "051063",
                    "kindName": "附加医保外医疗费用责任险（机动车第三者责任保险）",
                    "premium": "120.00",
                    "amount": "2000000",
                }
            ],
        },
    )
    _assert_rejected("picc-parser-addon-only", addon_only_from_picc_parser)
    _assert_rejected(
        "zero-only",
        _result(
            premium_total="0.00",
            price_items=[{"name": "商业险", "amount": "0.00"}, {"name": "车船税", "amount": "0.00"}],
        ),
    )
    _assert_rejected("stub-marker", _result(mode="stub"))
    _assert_rejected("fake-marker", _result(fake=True))
    _assert_rejected(
        "missing-response-provenance",
        _result(quote_provenance={}),
    )
    _assert_rejected(
        "joint-sales-config-only",
        _result(
            joint_sales={"enabled": True, "success": False, "premium": "398", "amount": "0"},
            joint_sales_source="none",
            joint_sales_premium="398.00",
            price_items=[
                {"name": "商业险", "amount": "1288.80"},
                {"name": "途家安顺", "amount": "398.00"},
            ],
        ),
    )
    assert not quote_result_real_data_error(
        _result(
            joint_sales={
                "enabled": True,
                "success": True,
                "premium": "398",
                "amount": "200000",
                "selected_plan": {"planPremium": "398", "planAmount": "200000"},
            },
            joint_sales_source="joint_sales_plan_response",
            joint_sales_premium="398.00",
            quote_provenance={
                **_result()["quote_provenance"],
                "normalized_amounts": {
                    **_result()["quote_provenance"]["normalized_amounts"],
                    "joint_sales": {
                        "value": "398.00",
                        "source": "joint_sales_plan_response.selected_plan.planPremium",
                    },
                },
                "joint_sales_evidence": [
                    {
                        "name": "joint_sales",
                        "source": "joint_sales_plan_response.selected_plan.planPremium",
                        "value": "398.00",
                    }
                ],
            },
            price_items=[
                {"name": "商业险", "amount": "1288.80"},
                {"name": "途家安顺", "amount": "398.00"},
            ],
        )
    )
    _assert_rejected(
        "display-card-amount-mismatch",
        _result(
            result_card={
                "commercial_premium": "999.00",
                "total_premium": "999.00",
            }
        ),
    )
    _assert_rejected(
        "top-level-commercial-amount-mismatch",
        _result(
            commercial_premium="999.00",
        ),
    )
    _assert_rejected(
        "result-total-amount-mismatch",
        _result(premium_total="999.00"),
    )
    _assert_rejected(
        "config-only-joint-plan",
        _result(
            joint_sales={
                "enabled": True,
                "success": True,
                "premium": "398",
                "amount": "200000",
                "selected_plan": {},
            },
            joint_sales_source="joint_sales_plan_response",
            joint_sales_premium="398.00",
            price_items=[
                {"name": "商业险", "amount": "1288.80"},
                {"name": "途家安顺", "amount": "398.00"},
            ],
        ),
    )

    failed_joint_query_result = PiccBusinessAdapter()._build_used_fuel_quote_result_from_response(
        ctx=SimpleNamespace(account_type_name="油车-旧"),
        quote_payload={},
        request_body={
            "accountTypeName": "油车-旧",
            "vehicleForm": {},
            "ownerForm": {},
            "quoteForm": {},
            "preflight": {},
            "jointSaleForm": {
                "tujiaAnshun": {
                    "enabled": True,
                    "success": False,
                    "premium": "398",
                    "amount": "0",
                    "message": "joint sales plan lookup failed",
                }
            },
        },
        quote_response={
            "status": 0,
            "data": {
                "biPremium": "1288.80",
                "ciPremium": "855.00",
                "sumPremium": "2143.80",
                "totalPremium": "2143.80",
            },
        },
    )
    assert failed_joint_query_result["joint_sales_premium"] == ""
    assert all(item["name"] != "途家安顺" for item in failed_joint_query_result["price_items"])
    assert failed_joint_query_result["premium_total"] == 2143.8

    runtime = _quote_runtime_result_or_failure(
        PlatformRuntimeResult(
            status="success",
            message="平台返回成功",
            data={"quote_result": _result(mode="stub")},
        )
    )
    assert runtime.status == "failed", runtime
    assert "quote_result" not in (runtime.data or {}), runtime.data
    assert "占位或模拟报价结果" in str(runtime.message), runtime

    failed_payload = _runtime_result_payload(
        PlatformRuntimeResult(
            status="failed",
            message="平台请求失败",
            data={
                "diagnostics": {
                    "quote_result": _result(),
                    "nested": [{"quoteResult": _result()}],
                }
            },
        )
    )
    assert "quote_result" not in str(failed_payload), failed_payload
    assert "quoteResult" not in str(failed_payload), failed_payload

    try:
        _enrich_quote_result_for_display(_result(mode="stub"))
    except ValueError as exc:
        assert "不能生成结果图" in str(exc), exc
    else:
        raise AssertionError("display enrichment accepted an untrusted result")

    missing_tax_result = PiccBusinessAdapter()._build_used_fuel_quote_result_from_response(
        ctx=SimpleNamespace(account_type_name="油车-旧"),
        quote_payload={},
        request_body={
            "accountTypeName": "油车-旧",
            "vehicleForm": {},
            "ownerForm": {},
            "quoteForm": {},
            "preflight": {},
        },
        quote_response={
            "status": 0,
            "data": {
                "biPremium": "1288.80",
                "ciPremium": "855.00",
                "sumPremium": "2143.80",
            },
        },
    )
    assert missing_tax_result["premium_total"] is None, missing_tax_result
    assert missing_tax_result["result_card"]["total_with_vehicle_tax"] == "", missing_tax_result
    assert not quote_result_real_data_error(missing_tax_result), missing_tax_result

    valid_history = _normalize_quote_result_metadata(
        {
            "data": {
                "result_status": "success",
                "payload": {
                    "quote_result": {
                        **_result(),
                        "result_image": "https://oss.example/real-quote.png",
                    }
                },
            }
        }
    )
    valid_history_result = valid_history["data"]["payload"]["quote_result"]
    assert valid_history_result["result_image"]["image_url"].endswith("real-quote.png"), valid_history

    invalid_history = _normalize_quote_result_metadata(
        {
            "data": {
                "result_status": "success",
                "payload": {
                    "quote_result": {
                        **_result(mode="stub"),
                        "result_image": "https://oss.example/should-not-show.png",
                    }
                },
            }
        }
    )
    invalid_history_result = invalid_history["data"]["payload"]["quote_result"]
    assert invalid_history["data"]["result_status"] == "failed", invalid_history
    assert invalid_history_result["quote_result_unavailable"] is True, invalid_history
    assert "result_image" not in invalid_history_result, invalid_history
    assert "quote_result_validation_error" in invalid_history, invalid_history

    invalid_direct_history = _normalize_quote_result_metadata(
        {
            "data": {
                "result_status": "success",
                "quote_result": {
                    **_result(quote_provenance={}),
                    "premium_total": "1288.80",
                },
            }
        }
    )
    direct_result = invalid_direct_history["data"]["quote_result"]
    assert direct_result["quote_result_unavailable"] is True, invalid_direct_history
    assert "result_image" not in direct_result, invalid_direct_history

    print(
        "PASS quote-result truthfulness: valid premium accepted; tax-only, "
        "coverage-only, addon-only, joint-sales-only, score-only, zero-only, missing-provenance, "
        "and stub/fake/config-only results rejected; invalid historical results hidden"
    )


if __name__ == "__main__":
    main()
