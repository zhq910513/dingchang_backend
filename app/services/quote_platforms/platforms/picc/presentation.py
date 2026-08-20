# encoding: utf-8
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Optional


PICC_FUEL_KIND_NAMES = {
    "051050": "机动车损失保险",
    "051051": "机动车第三者责任保险",
    "051052": "机动车车上人员责任保险（司机）",
    "051053": "机动车车上人员责任保险（乘客）",
    "051063": "附加医保外医疗费用责任险（机动车第三者责任保险）",
    "051064": "附加机动车增值服务特约条款（道路救援服务）",
    "051074": "交强险",
    "051085": "附加外部电网故障损失险",
}

PICC_NEW_ENERGY_KIND_NAMES = {
    **PICC_FUEL_KIND_NAMES,
    "051050": "新能源汽车损失保险",
    "051051": "新能源汽车第三者责任保险",
    "051052": "新能源汽车车上人员责任保险（司机）",
    "051053": "新能源汽车车上人员责任保险（乘客）",
    "051063": "附加医保外医疗费用责任险（新能源汽车第三者责任保险）",
}

_KNOWN_KIND_ALIASES = {
    "051050": {"机动车损失保险", "新能源汽车损失保险", "车辆损失险", "车损险", "车损"},
    "051051": {"机动车第三者责任保险", "新能源汽车第三者责任保险", "第三者责任险", "三者险", "三者"},
    "051052": {
        "机动车车上人员责任保险（司机）",
        "新能源汽车车上人员责任保险（司机）",
        "车上人员责任险（司机）",
        "车上人员责任险(司机)",
        "司机险",
    },
    "051053": {
        "机动车车上人员责任保险（乘客）",
        "新能源汽车车上人员责任保险（乘客）",
        "车上人员责任险（乘客）",
        "车上人员责任险(乘客)",
        "乘客险",
    },
    "051063": {
        "附加医保外医疗费用责任险（机动车第三者责任保险）",
        "附加医保外医疗费用责任险（新能源汽车第三者责任保险）",
        "医保外医疗费用责任险（第三者责任险）",
        "医保外医疗费用责任险(第三者责任险)",
        "医保外医疗费用责任险(三者)",
    },
    "051064": {
        "附加机动车增值服务特约条款（道路救援服务）",
        "机动车增值服务特约条款（道路救援服务）",
        "道路救援服务",
        "道路救援",
    },
    "051074": {"机动车交通事故责任强制保险", "交强险"},
    "051085": {"附加外部电网故障损失险"},
}


def _text(value: Any) -> str:
    try:
        return "" if value is None else str(value).strip()
    except Exception:
        return ""


def _decimal(value: Any) -> Decimal:
    if value in (None, "") or isinstance(value, bool):
        return Decimal("0")
    try:
        return Decimal(_text(value).replace(",", "").replace("元", ""))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _positive_int(value: Any) -> int:
    try:
        number = int(Decimal(_text(value) or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    return number if number > 0 else 0


def _compact_decimal(value: Decimal, *, places: str = "0.01") -> str:
    text = f"{value.quantize(Decimal(places), rounding=ROUND_HALF_UP):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def picc_is_new_energy_vehicle(*, energy_type: Any = "", account_type_name: Any = "", is_energy_car: Any = None) -> bool:
    if isinstance(is_energy_car, bool):
        return is_energy_car
    explicit = _text(is_energy_car).lower()
    if explicit in {"1", "true", "yes", "y"}:
        return True
    if explicit in {"0", "false", "no", "n"}:
        return False
    energy = _text(energy_type).lower()
    if energy in {"new_energy", "new-energy", "nev", "新能源"}:
        return True
    if energy in {"fuel", "oil", "燃油", "油车"}:
        return False
    account_type = _text(account_type_name)
    return "新能源" in account_type


def picc_result_kind_name(
    kind_code: Any,
    *,
    platform_name: Any = "",
    fallback_name: Any = "",
    is_new_energy: bool = False,
) -> str:
    """Return the truthful result label, preferring the platform response.

    PICC uses the same kind codes for fuel and new-energy motor products.  The
    platform-provided label is therefore authoritative.  The energy-aware code
    map is used only when that label is absent or the stored label is one of the
    known aliases produced by older normalization code.
    """

    code = _text(kind_code)
    raw_platform_name = _text(platform_name)
    if raw_platform_name and raw_platform_name != code:
        return raw_platform_name

    fallback = _text(fallback_name)
    names = PICC_NEW_ENERGY_KIND_NAMES if is_new_energy else PICC_FUEL_KIND_NAMES
    aliases = _KNOWN_KIND_ALIASES.get(code, set())
    if code in names and (not fallback or fallback == code or fallback in aliases):
        return names[code]
    return fallback or names.get(code, "") or code


def picc_result_amount_text(
    row: Mapping[str, Any],
    *,
    seat_count: Any = "",
    shared_main_limit: Optional[bool] = None,
) -> str:
    """Format the responsibility/amount column without dropping dynamic rows."""

    code = _text(row.get("code") or row.get("kindCode"))
    name = _text(row.get("platform_name") or row.get("name") or row.get("kindName"))
    quantity = _positive_int(row.get("quantity"))
    if (code == "051064" or "道路救援" in name) and quantity > 0:
        return f"{quantity}次"

    explicit = _text(row.get("amount_text") or row.get("amountText"))
    if explicit:
        return explicit

    shared_flag = shared_main_limit
    if shared_flag is None:
        shared_flag = _text(row.get("shared_amount_flag") or row.get("sharedAmountFlag")) == "1"
    if code == "051063" and shared_flag:
        return "共享主险限额"

    amount = _decimal(row.get("amount"))
    if amount <= 0:
        return "-"
    if code == "051050":
        return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}元"

    unit_amount = _decimal(row.get("unit_amount") or row.get("unitAmount"))
    if code == "051053" or "乘客" in name:
        seats = _positive_int(seat_count)
        divisor = max(seats - 1, 1)
        per_seat = unit_amount if unit_amount > 0 else amount / Decimal(divisor)
        if unit_amount <= 0 and seats <= 1:
            return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}元"
        covered_seats = max(
            1,
            int((amount / per_seat).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
        ) if per_seat > 0 else divisor
        return f"{_compact_decimal(per_seat / Decimal('10000'))}万元/座*{covered_seats}"

    if code in {"051051", "051052", "051063", "051074", "051085"} or any(
        marker in name for marker in ("第三者", "医保外", "司机")
    ):
        return f"{_compact_decimal(amount / Decimal('10000'))}万元"
    return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}元"
