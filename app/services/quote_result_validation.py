# -*- coding: utf-8 -*-
"""Shared guards for deciding whether a quote result is real and displayable."""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Mapping


RUNTIME_QUOTE_SUCCESS_STATUSES = frozenset({"success", "ok", "quoted"})

REQUIRED_QUOTE_PROVENANCE_SOURCE = "platform_quote_response"

UNTRUSTED_QUOTE_MARKERS = (
    "fake",
    "stub",
    "mock",
    "simulat",
    "synthetic",
    "placeholder",
    "假报价",
    "模拟报价",
    "占位报价",
)

CORE_PREMIUM_KEYS = (
    "commercial_premium",
    "compulsory_premium",
)

CORE_PREMIUM_ITEM_NAME_HINTS = (
    "商业",
    "交强",
    "机动车损失保险",
    "新能源汽车损失保险",
    "车辆损失险",
    "车损险",
    "机动车第三者责任保险",
    "新能源汽车第三者责任保险",
    "第三者责任险",
    "机动车车上人员责任保险",
    "新能源汽车车上人员责任保险",
    "车上人员责任险",
)

NON_MOTOR_PREMIUM_ITEM_NAME_HINTS = (
    "车船",
    "非车",
    "途家",
    "途顺",
    "意外",
)

NON_CORE_MOTOR_PREMIUM_ITEM_NAME_HINTS = (
    "附加",
    "医保外",
    "道路救援",
    "增值服务",
    "外部电网",
)

JOINT_SALES_ITEM_NAME_HINTS = (
    "途家安顺",
    "途顺家安",
)

TRUSTED_JOINT_SALES_SOURCES = frozenset({
    "platform_quote_response",
    "joint_sales_plan_response",
})

TRUSTED_EXACT_AMOUNT_SOURCES = frozenset(
    {
        "quote_response.data.biPremium",
        "quote_response.data.ciPremium",
        "quote_response.data.sumPayTax",
        "quote_response.data.thisPayTax",
        "quote_response.data.carShipTaxes",
        "quote_response.data.sumYelPremium",
        "quote_response.data.sumPremium",
        "quote_response.data.totalPremium",
        "quote_response.data.premiumTotal",
        "joint_sales_plan_response.selected_plan.planPremium",
        "derived_from_quote_response.data.prePayTax+delayPayTax",
        "derived_from_quote_response.data.sumPremium+joint_sales_plan_response.selected_plan.planPremium",
        "derived_from_quote_response_components",
        "derived_from_quote_response_components+joint_sales_plan_response.selected_plan.planPremium",
        "derived_from_real_quote",
    }
)

TRUSTED_AMOUNT_SOURCE_PATTERNS = (
    re.compile(r"^quote_response(?:\.data)?\.itemKindTempList\[(?:\d+|\*)\]\.premium(?:\.sum)?$"),
)


def _text(value: Any) -> str:
    try:
        return "" if value is None else str(value)
    except Exception:
        return ""


def _positive_numeric_evidence(value: Any) -> bool:
    if value in (None, "") or isinstance(value, bool):
        return False
    try:
        number = Decimal(_text(value).replace(",", "").replace("元", "").strip())
    except Exception:
        return False
    return number.is_finite() and number > 0


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def _parse_money(value: Any) -> tuple[bool, Decimal]:
    if not _has_value(value) or isinstance(value, bool):
        return False, Decimal("0")
    try:
        number = Decimal(_text(value).replace(",", "").replace("元", "").strip())
    except Exception:
        return False, Decimal("0")
    return number.is_finite(), number


def _money_equal(left: Any, right: Any) -> bool:
    left_ok, left_value = _parse_money(left)
    right_ok, right_value = _parse_money(right)
    return left_ok and right_ok and left_value == right_value


def _normalized_amounts(provenance: Mapping[str, Any]) -> Mapping[str, Any]:
    value = provenance.get("normalized_amounts")
    return value if isinstance(value, Mapping) else {}


def _normalized_amount(
    provenance: Mapping[str, Any],
    name: str,
) -> tuple[bool, Any, str]:
    entry = _object(_normalized_amounts(provenance).get(name))
    if not entry or not _has_value(entry.get("value")):
        return False, None, ""
    return True, entry.get("value"), _text(entry.get("source")).strip()


def _trusted_normalized_source(source: Any) -> bool:
    text = _text(source).strip()
    return text in TRUSTED_EXACT_AMOUNT_SOURCES or any(
        pattern.fullmatch(text) for pattern in TRUSTED_AMOUNT_SOURCE_PATTERNS
    )


def _summary_amount_category(name: Any) -> str:
    text = _text(name).strip()
    if not text:
        return ""
    if any(hint in text for hint in NON_MOTOR_PREMIUM_ITEM_NAME_HINTS):
        if any(hint in text for hint in JOINT_SALES_ITEM_NAME_HINTS):
            return "joint_sales"
        return ""
    if any(hint in text for hint in NON_CORE_MOTOR_PREMIUM_ITEM_NAME_HINTS):
        return ""
    if "交强" in text:
        return "compulsory"
    if "商业" in text:
        return "commercial"
    if "车船" in text:
        return "vehicle_tax"
    return ""


def _validate_amount_against_normalized(
    *,
    label: str,
    actual: Any,
    expected_present: bool,
    expected: Any,
    expected_source: str,
) -> str:
    if not _has_value(actual):
        return ""
    if not expected_present:
        return f"{label}没有平台响应金额依据，未生成报价结果"
    if not _trusted_normalized_source(expected_source):
        return f"{label}金额溯源不是平台响应或真实派生结果，未生成报价结果"
    if not _money_equal(actual, expected):
        return f"{label}与平台响应金额不一致，未生成报价结果"
    return ""


def _evidence_rows(provenance: Mapping[str, Any], *keys: str) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for key in keys:
        value = provenance.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            rows.append(value)
    return rows


def _trusted_positive_evidence(
    rows: list[Mapping[str, Any]],
    *,
    source_names: tuple[str, ...],
) -> bool:
    for row in rows:
        source = _text(row.get("source")).strip()
        if not _trusted_normalized_source(source):
            continue
        source_name = _text(row.get("name") or row.get("kind")).strip().lower()
        if source_names and source_name not in source_names:
            continue
        if _positive_numeric_evidence(row.get("value")):
            return True
    return False


def _joint_sales_has_real_source(result: Mapping[str, Any], card: Mapping[str, Any]) -> bool:
    """Reject a positive non-car amount that only came from configuration."""
    positive_amount = any(
        _positive_numeric_evidence(source.get(key))
        for source in (result, card)
        for key in ("joint_sales_premium", "jointSalesPremium")
    )
    if not positive_amount:
        for item in result.get("price_items") or []:
            row = _object(item)
            if any(hint in _text(row.get("name")) for hint in JOINT_SALES_ITEM_NAME_HINTS):
                positive_amount = _positive_numeric_evidence(
                    row.get("premium") if row.get("premium") not in (None, "") else row.get("amount")
                )
                if positive_amount:
                    break
    if not positive_amount:
        return True

    provenance = _object(result.get("quote_provenance"))
    source = _text(result.get("joint_sales_source")).strip().lower()
    if source not in TRUSTED_JOINT_SALES_SOURCES:
        return False
    if source == "joint_sales_plan_response":
        joint_sales = _object(result.get("joint_sales"))
        selected_plan = _object(joint_sales.get("selected_plan"))
        if not (
            joint_sales.get("success") is True
            and bool(selected_plan)
            and (
                _positive_numeric_evidence(selected_plan.get("planPremium"))
                or _positive_numeric_evidence(selected_plan.get("planAmount"))
            )
        ):
            return False
    return _trusted_positive_evidence(
        _evidence_rows(provenance, "joint_sales_evidence", "joint_sales_sources"),
        source_names=("joint_sales", "tujia_anshun", "途家安顺"),
    )


def quote_result_real_data_error(result: Any) -> str:
    """Return a user-safe reason when a result cannot prove a real quote.

    A transport-level success is not enough. The normalized result must carry
    a platform-response provenance marker and at least one positive core motor
    premium. Zero is valid for an individual unselected product, but cannot by
    itself prove that a platform returned a completed quote. A joint-sales plan,
    risk score, tax, or coverage amount is never a substitute for the car quote.
    """
    if not isinstance(result, Mapping):
        return "平台返回成功状态但没有返回报价结果"

    result_status = _text(result.get("status")).strip().lower()
    if result_status not in RUNTIME_QUOTE_SUCCESS_STATUSES:
        return "平台没有返回成功报价结果"

    direct_markers = (
        result.get("mode"),
        result.get("status"),
        result.get("message"),
        result.get("remark"),
    )
    if (
        result.get("stub") is True
        or result.get("fake") is True
        or result.get("mock") is True
        or any(
            any(marker in _text(value).lower() for marker in UNTRUSTED_QUOTE_MARKERS)
            for value in direct_markers
            if value not in (None, "")
        )
    ):
        return "平台返回的是占位或模拟报价结果，未生成真实报价"

    provenance = _object(result.get("quote_provenance"))
    provenance_source = _text(
        provenance.get("source") or result.get("quote_source")
    ).strip().lower()
    if provenance_source != REQUIRED_QUOTE_PROVENANCE_SOURCE:
        return "平台返回成功状态但缺少报价响应溯源，未生成报价结果"
    response_status = _text(provenance.get("response_status")).strip().lower()
    if response_status not in {"0", "success", "ok", "quoted"}:
        return "平台报价响应状态异常，未生成报价结果"

    normalized_amount_entries = _normalized_amounts(provenance)
    if not normalized_amount_entries:
        return "平台报价结果缺少标准化金额溯源，未生成报价结果"
    for amount_name, amount_entry in normalized_amount_entries.items():
        if not isinstance(amount_entry, Mapping):
            return f"{amount_name}金额溯源格式异常，未生成报价结果"
        if not _has_value(amount_entry.get("value")):
            return f"{amount_name}金额缺少平台响应值，未生成报价结果"
        valid_value, _ = _parse_money(amount_entry.get("value"))
        if not valid_value:
            return f"{amount_name}金额不是有效数字，未生成报价结果"
        if not _trusted_normalized_source(amount_entry.get("source")):
            return f"{amount_name}金额溯源不是平台响应或真实派生结果，未生成报价结果"

    card = _object(result.get("result_card") or result.get("resultCard"))
    if not card:
        return "平台返回成功状态但缺少真实报价结果卡，未生成报价结果"
    if not _joint_sales_has_real_source(result, card):
        return "结果包含无法证明来源的途家安顺保费，未生成报价结果"
    price_items = result.get("price_items")

    expected_amounts: dict[str, tuple[bool, Any, str]] = {
        name: _normalized_amount(provenance, name)
        for name in (
            "commercial",
            "compulsory",
            "vehicle_tax",
            "joint_sales",
            "total_without_vehicle_tax",
            "total_with_vehicle_tax",
        )
    }

    result_fields = (
        ("commercial_premium", "commercial"),
        ("compulsory_premium", "compulsory"),
        ("vehicle_tax", "vehicle_tax"),
        ("joint_sales_premium", "joint_sales"),
        ("total_without_vehicle_tax", "total_without_vehicle_tax"),
        ("total_with_vehicle_tax", "total_with_vehicle_tax"),
        ("premium_total", "total_with_vehicle_tax"),
    )
    for result_field, amount_name in result_fields:
        if result_field not in result:
            continue
        error = _validate_amount_against_normalized(
            label=result_field,
            actual=result.get(result_field),
            expected_present=expected_amounts[amount_name][0],
            expected=expected_amounts[amount_name][1],
            expected_source=expected_amounts[amount_name][2],
        )
        if error:
            return error

    for item in price_items if isinstance(price_items, list) else []:
        row = _object(item)
        category = _summary_amount_category(row.get("name"))
        if not category:
            continue
        actual = row.get("amount")
        if not _has_value(actual):
            actual = row.get("premium")
        error = _validate_amount_against_normalized(
            label=category,
            actual=actual,
            expected_present=expected_amounts[category][0],
            expected=expected_amounts[category][1],
            expected_source=expected_amounts[category][2],
        )
        if error:
            return error

    card_fields = (
        ("commercial_premium", "commercial"),
        ("compulsory_premium", "compulsory"),
        ("vehicle_tax", "vehicle_tax"),
        ("joint_sales_premium", "joint_sales"),
        ("total_without_vehicle_tax", "total_without_vehicle_tax"),
        ("total_with_vehicle_tax", "total_with_vehicle_tax"),
        ("total_premium", "total_with_vehicle_tax"),
    )
    for card_field, amount_name in card_fields:
        if card_field not in card:
            continue
        error = _validate_amount_against_normalized(
            label=card_field,
            actual=card.get(card_field),
            expected_present=expected_amounts[amount_name][0],
            expected=expected_amounts[amount_name][1],
            expected_source=expected_amounts[amount_name][2],
        )
        if error:
            return error

    if "premium_total" in result:
        error = _validate_amount_against_normalized(
            label="premium_total",
            actual=result.get("premium_total"),
            expected_present=expected_amounts["total_with_vehicle_tax"][0],
            expected=expected_amounts["total_with_vehicle_tax"][1],
            expected_source=expected_amounts["total_with_vehicle_tax"][2],
        )
        if error:
            return error

    has_core_premium = False

    if isinstance(price_items, list):
        for item in price_items:
            row = _object(item)
            name = _text(row.get("name")).strip()
            if not name:
                continue
            # The normalized commercial/compulsory summary may use `amount`;
            # an individual motor coverage row must provide an explicit
            # `premium`; its insured amount must never be treated as a premium.
            is_core_motor = any(hint in name for hint in CORE_PREMIUM_ITEM_NAME_HINTS)
            is_non_motor = any(hint in name for hint in NON_MOTOR_PREMIUM_ITEM_NAME_HINTS)
            # An add-on can include the full parent-risk name, for example
            # "附加医保外...(机动车第三者责任保险)". It is not proof that
            # the parent motor quote was returned.
            is_add_on = any(
                hint in name for hint in NON_CORE_MOTOR_PREMIUM_ITEM_NAME_HINTS
            )
            if (
                is_core_motor
                and not is_non_motor
                and not is_add_on
                and _positive_numeric_evidence(row.get("premium"))
            ):
                has_core_premium = True
                break
            if (
                not is_add_on
                and is_core_motor
                and any(hint in name for hint in ("商业", "交强"))
                and any(
                _positive_numeric_evidence(row.get(key))
                for key in ("amount", "value")
                )
            ):
                has_core_premium = True
                break

    if not has_core_premium:
        has_core_premium = any(
            _positive_numeric_evidence(source.get(key))
            for source in (result, card)
            for key in CORE_PREMIUM_KEYS
        )

    if not has_core_premium:
        return "平台返回成功状态但缺少真实商业险或交强险保费，未生成报价结果"

    core_evidence = _evidence_rows(
        provenance,
        "core_premium_evidence",
        "core_premium_sources",
    )
    if not _trusted_positive_evidence(
        core_evidence,
        source_names=(
            "commercial",
            "商业",
            "commercial_premium",
            "compulsory",
            "交强",
            "compulsory_premium",
        ),
    ):
        return "核心保费缺少平台响应字段证据，未生成报价结果"

    return ""


def quote_result_has_real_data(result: Any) -> bool:
    return not quote_result_real_data_error(result)
