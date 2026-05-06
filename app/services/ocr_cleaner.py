# encoding: utf-8
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Optional


CLEANING_RULE_VERSION = "ocr-cleaner-2026-05-03-v2"

_YMD_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_YM_RE = re.compile(r"^(\d{4})-(\d{1,2})$")
_8DIGITS_RE = re.compile(r"^\d{8}$")
_6DIGITS_RE = re.compile(r"^\d{6}$")
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_PLATE_RE = re.compile(r"[\u4e00-\u9fff][A-Z][A-Z0-9]{4,6}")
_RESIDENT_ID_RE = re.compile(r"\d{17}[\dXx]")
_SOCIAL_CREDIT_RE = re.compile(r"[0-9A-Z]{18}")

_EMPTY_MARKERS = {
    "",
    "-",
    "--",
    "---",
    "\u2014",
    "\uff0d",
    "null",
    "none",
    "nan",
    "undefined",
    "n/a",
    "na",
    "nil",
    "\u65e0",
    "\u6682\u65e0",
    "\u672a\u77e5",
    "\u4e0d\u8be6",
    "\u672a\u63d0\u4f9b",
    "\u672a\u8bc6\u522b",
    "\u8bc6\u522b\u5931\u8d25",
    "\u7a7a",
}

_FIELD_EMPTY_MARKERS: Dict[str, set[str]] = {
    "plate_no": {
        "\u65e0\u724c",
        "\u672a\u4e0a\u724c",
        "\u6682\u672a\u4e0a\u724c",
        "\u65b0\u8f66\u672a\u4e0a\u724c",
        "\u65b0\u8f66",
    },
    "vin": {
        "\u672a\u89c1",
        "\u672a\u62d3\u5370",
    },
}

_LABEL_PREFIXES: Dict[str, tuple[str, ...]] = {
    "owner_name": ("\u6240\u6709\u4eba", "\u8f66\u4e3b", "\u59d3\u540d"),
    "id_name": ("\u59d3\u540d", "\u540d\u79f0"),
    "id_number": (
        "\u516c\u6c11\u8eab\u4efd\u53f7\u7801",
        "\u8eab\u4efd\u8bc1\u53f7\u7801",
        "\u8eab\u4efd\u8bc1\u53f7",
        "\u7edf\u4e00\u793e\u4f1a\u4fe1\u7528\u4ee3\u7801",
        "\u8bc1\u4ef6\u53f7\u7801",
        "\u53f7\u7801",
    ),
    "plate_no": (
        "\u53f7\u724c\u53f7\u7801",
        "\u8f66\u724c\u53f7\u7801",
        "\u8f66\u724c\u53f7",
        "\u53f7\u724c",
    ),
    "vin": (
        "\u8f66\u8f86\u8bc6\u522b\u4ee3\u53f7",
        "\u8f66\u8f86\u8bc6\u522b\u4ee3\u7801",
        "\u8f66\u67b6\u53f7",
        "VIN",
        "vin",
    ),
    "engine_no": (
        "\u53d1\u52a8\u673a\u53f7\u7801",
        "\u53d1\u52a8\u673a\u53f7",
        "\u53d1\u52a8\u673a\u7f16\u53f7",
    ),
    "vehicle_model": ("\u54c1\u724c\u578b\u53f7", "\u8f66\u8f86\u578b\u53f7", "\u578b\u53f7"),
    "vehicle_type": ("\u8f66\u8f86\u7c7b\u578b",),
    "use_nature": ("\u4f7f\u7528\u6027\u8d28",),
    "issuer_org": ("\u53d1\u8bc1\u5355\u4f4d", "\u53d1\u8bc1\u673a\u5173"),
    "id_issuer": ("\u7b7e\u53d1\u673a\u5173", "\u53d1\u8bc1\u673a\u5173"),
}

_ALIASES: Dict[str, str] = {
    "id_birth": "id_birth_date",
    "id_nation": "id_ethnicity",
    "id_valid_period": "id_validity",
    "id_issue_authority": "id_issuer",
    "register_date": "first_register_date",
    "vehicle_name": "vehicle_model",
    "dla_approved_passengers": "approved_passenger_count",
    "dla_passenger_count": "approved_passenger_count",
}

_TEXT_FIELDS = (
    "owner_name",
    "vehicle_model",
    "vehicle_type",
    "use_nature",
    "issuer_org",
    "id_name",
    "id_address",
    "id_gender",
    "id_ethnicity",
    "id_issuer",
    "id_validity",
)

_BAD_MANUFACTURER_TOKENS = (
    "\u8054\u7cfb\u4eba",
    "\u8054\u7cfb\u7535\u8bdd",
    "\u7535\u8bdd",
    "\u624b\u673a",
    "\u6700\u5927\u51c0\u529f\u7387",
    "\u70df\u5ea6",
    "\u5438\u6536\u7cfb\u6570",
    "\u989d\u5b9a\u8f7d\u5ba2",
    "\u68c0\u9a8c\u8bb0\u5f55",
    "\u8bc1\u82af\u7f16\u53f7",
    "\u6863\u6848\u7f16\u53f7",
)

_MANUFACTURER_KEEP_TOKENS = (
    "\u516c\u53f8",
    "\u5382",
    "\u96c6\u56e2",
    "\u6709\u9650",
    "\u6c7d\u8f66",
    "\u5236\u9020",
)

_GENERIC_BRAND_VALUES = {
    "\u4e2d\u56fd",
    "\u56fd\u4ea7",
    "\u8f7f\u8f66",
    "\u5ba2\u8f66",
    "\u8d27\u8f66",
    "\u5c0f\u578b\u8f7f\u8f66",
    "\u5c0f\u578b\u666e\u901a\u5ba2\u8f66",
    "\u767d",
    "\u9ed1",
    "\u7ea2",
    "\u84dd",
    "\u7070",
    "\u94f6",
    "\u767d\u8272",
    "\u9ed1\u8272",
    "\u7ea2\u8272",
    "\u84dd\u8272",
    "\u7070\u8272",
    "\u94f6\u8272",
}

_CN_NUMBERS = {
    "\u96f6": 0,
    "\u3007": 0,
    "\u4e00": 1,
    "\u58f9": 1,
    "\u4e8c": 2,
    "\u4e24": 2,
    "\u8d30": 2,
    "\u4e09": 3,
    "\u53c1": 3,
    "\u56db": 4,
    "\u8086": 4,
    "\u4e94": 5,
    "\u4f0d": 5,
    "\u516d": 6,
    "\u9646": 6,
    "\u4e03": 7,
    "\u67d2": 7,
    "\u516b": 8,
    "\u634c": 8,
    "\u4e5d": 9,
    "\u7396": 9,
    "\u5341": 10,
    "\u62fe": 10,
}


def describe_cleaning_rules() -> Dict[str, Any]:
    return {
        "version": CLEANING_RULE_VERSION,
        "fields": {
            "text": "NFKC, strip zero-width chars, collapse whitespace, strip known labels",
            "date": "YYYYMMDD/YYYMM/YYYY-M-D/YYYY-MM-DD/YYYY year-month-day -> normalized text",
            "vin": "extract a 17-char VIN candidate, uppercase, OCR O/I/Q -> 0/1/0",
            "plate_no": "strip punctuation/spaces and extract Chinese plate shapes",
            "id_number": "resident ID with valid birth date, or 18-char social credit code with letters",
            "passengers": "Arabic or simple Chinese numerals; plus expressions are summed",
            "manufacturer": "drop contact/phone/testing/power noise; keep company-like names",
            "aliases": "move known legacy aliases to canonical keys, then remove aliases and dl_* keys",
        },
    }


def _s(value: Any) -> str:
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\ufeff", "")
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_empty(value: Any, *, field_name: str = "") -> bool:
    text = _s(value)
    if text.lower() in _EMPTY_MARKERS:
        return True
    if field_name and text in _FIELD_EMPTY_MARKERS.get(field_name, set()):
        return True
    return False


def _digits_only(text: str) -> str:
    return re.sub(r"[^\d]", "", text or "")


def _alnum_only(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", text or "")


def _strip_label_prefix(value: Any, field_name: str) -> str:
    text = _s(value)
    if not text:
        return ""
    for label in _LABEL_PREFIXES.get(field_name, ()):
        text = re.sub(rf"^\s*{re.escape(label)}\s*[:;\-_\s]*", "", text, flags=re.IGNORECASE)
    return text.strip()


def _valid_ymd(year: str, month: str, day: str) -> Optional[str]:
    text = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except Exception:
        return None
    return text


def _valid_ym(year: str, month: str) -> Optional[str]:
    text = f"{int(year):04d}-{int(month):02d}"
    try:
        datetime.strptime(text + "-01", "%Y-%m-%d")
    except Exception:
        return None
    return text


def _normalize_date_text(value: Any) -> str:
    text = _s(value)
    text = text.replace("\u5e74", "-").replace("\u6708", "-").replace("\u65e5", "")
    text = text.replace("/", "-").replace(".", "-")
    text = re.sub(r"\s+", "", text)
    return text.strip("-")


def norm_text(value: Any, *, field_name: str = "") -> Optional[str]:
    if _is_empty(value, field_name=field_name):
        return None
    text = _strip_label_prefix(value, field_name) if field_name else _s(value)
    if _is_empty(text, field_name=field_name):
        return None
    return text


def norm_fuzzy_date_text(value: Any) -> Optional[str]:
    if _is_empty(value):
        return None

    text = _normalize_date_text(value)
    if not text:
        return None

    match = _YMD_RE.match(text)
    if match:
        return _valid_ymd(match.group(1), match.group(2), match.group(3))

    match = _YM_RE.match(text)
    if match:
        return _valid_ym(match.group(1), match.group(2))

    digits = _digits_only(text)
    if _8DIGITS_RE.match(digits):
        return _valid_ymd(digits[0:4], digits[4:6], digits[6:8])
    if _6DIGITS_RE.match(digits):
        return _valid_ym(digits[0:4], digits[4:6])

    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text)
    if match:
        return _valid_ymd(match.group(1), match.group(2), match.group(3))

    match = re.search(r"(\d{4})\D+(\d{1,2})(?!\d)", text)
    if match:
        return _valid_ym(match.group(1), match.group(2))

    return None


def norm_ymd(value: Any) -> Optional[str]:
    text = norm_fuzzy_date_text(value)
    return text if text and len(text) == 10 else None


def norm_id_valid_to(value: Any) -> Optional[str]:
    text = _s(value)
    if not text:
        return None
    if "\u957f\u671f" in text:
        return "\u957f\u671f"
    return norm_ymd(text)


def _looks_like_resident_id(candidate: str) -> bool:
    if not re.fullmatch(r"\d{17}[\dX]", candidate):
        return False
    return _valid_ymd(candidate[6:10], candidate[10:12], candidate[12:14]) is not None


def norm_id_number(value: Any) -> Optional[str]:
    if _is_empty(value):
        return None
    text = _strip_label_prefix(value, "id_number").upper()
    compact = _alnum_only(text).upper()
    if not compact:
        return None

    resident_match = _RESIDENT_ID_RE.search(compact)
    if resident_match:
        candidate = resident_match.group(0).upper()
        if _looks_like_resident_id(candidate):
            return candidate

    credit_match = _SOCIAL_CREDIT_RE.search(compact)
    if credit_match:
        candidate = credit_match.group(0).upper()
        if any(ch.isalpha() for ch in candidate):
            return candidate

    return None


def _birth_from_resident_id(id_number: Optional[str]) -> Optional[str]:
    if not id_number or not _looks_like_resident_id(id_number):
        return None
    return f"{id_number[6:10]}-{id_number[10:12]}-{id_number[12:14]}"


def norm_vin(value: Any) -> Optional[str]:
    if _is_empty(value, field_name="vin"):
        return None
    text = _strip_label_prefix(value, "vin").upper()
    compact = _alnum_only(text).upper()
    if not compact:
        return None
    compact = compact.replace("O", "0").replace("I", "1").replace("Q", "0")

    for i in range(0, max(1, len(compact) - 16)):
        candidate = compact[i : i + 17]
        if len(candidate) < 17:
            continue
        if _VIN_RE.match(candidate) and any(ch.isdigit() for ch in candidate) and any(ch.isalpha() for ch in candidate):
            return candidate
    return None


def norm_engine_no(value: Any) -> Optional[str]:
    if _is_empty(value):
        return None
    text = _strip_label_prefix(value, "engine_no").upper()
    compact = _alnum_only(text).upper()
    if 3 <= len(compact) <= 32:
        return compact
    return None


def norm_plate_no(value: Any) -> Optional[str]:
    if _is_empty(value, field_name="plate_no"):
        return None
    text = _strip_label_prefix(value, "plate_no").upper()
    if _is_empty(text, field_name="plate_no"):
        return None
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^\u4e00-\u9fffA-Z0-9]", "", text)
    match = _PLATE_RE.search(text)
    if match:
        return match.group(0)
    if 2 <= len(text) <= 12 and any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return text
    return None


def _simple_cn_number(text: str) -> Optional[int]:
    chars = [ch for ch in text if ch in _CN_NUMBERS]
    if not chars:
        return None
    if len(chars) == 1:
        value = _CN_NUMBERS[chars[0]]
        return value if 1 <= value <= 99 else None
    if "\u5341" in chars or "\u62fe" in chars:
        ten_index = next((i for i, ch in enumerate(chars) if ch in {"\u5341", "\u62fe"}), -1)
        if ten_index < 0:
            return None
        high = 1 if ten_index == 0 else _CN_NUMBERS.get(chars[ten_index - 1], 0)
        low = _CN_NUMBERS.get(chars[ten_index + 1], 0) if ten_index + 1 < len(chars) else 0
        value = high * 10 + low
        return value if 1 <= value <= 99 else None
    return None


def norm_passenger_count(value: Any) -> Optional[str]:
    if _is_empty(value):
        return None
    text = _s(value)
    numbers = [int(x) for x in re.findall(r"\d+", text)]
    if numbers:
        count = sum(numbers) if "+" in text else numbers[0]
    else:
        count = _simple_cn_number(text)
    if count is None:
        return None
    if 1 <= int(count) <= 99:
        return str(int(count))
    return None


def norm_manufacturer_name(value: Any) -> Optional[str]:
    text = norm_text(value, field_name="manufacturer_name")
    if not text:
        return None
    if any(token in text for token in _BAD_MANUFACTURER_TOKENS):
        return None
    if re.search(r"(?:1[3-9]\d{9}|0\d{2,3}-?\d{7,8})", text):
        return None
    digits = sum(1 for ch in text if ch.isdigit())
    if digits and digits / max(1, len(text)) > 0.45 and not any(token in text for token in _MANUFACTURER_KEEP_TOKENS):
        return None
    return text


def norm_vehicle_brand_name(value: Any) -> Optional[str]:
    text = norm_text(value, field_name="vehicle_brand_name")
    if not text:
        return None
    if text in _GENERIC_BRAND_VALUES:
        return None
    return text


def _merge_aliases(data: Dict[str, Any]) -> None:
    for alias, canonical in _ALIASES.items():
        if alias not in data:
            continue
        if canonical not in data or _is_empty(data.get(canonical)):
            data[canonical] = data.get(alias)
        data.pop(alias, None)


def _find_ymd_tokens(text: str) -> list[str]:
    src = _s(text)
    if not src:
        return []
    patterns = (
        r"\d{4}\s*[\u5e74./-]\s*\d{1,2}\s*[\u6708./-]\s*\d{1,2}\s*\u65e5?",
        r"\d{8}",
    )
    out: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, src):
            value = norm_ymd(match.group(0))
            if value and value not in out:
                out.append(value)
    return out


def _parse_id_validity_range(data: Dict[str, Any]) -> None:
    validity = norm_text(data.get("id_validity"), field_name="id_validity")
    if not validity:
        return
    data["id_validity"] = validity

    date_parts = _find_ymd_tokens(validity)
    if date_parts and ("id_valid_from" not in data or _is_empty(data.get("id_valid_from"))):
        data["id_valid_from"] = date_parts[0]
    if len(date_parts) >= 2 and ("id_valid_to" not in data or _is_empty(data.get("id_valid_to"))):
        data["id_valid_to"] = date_parts[1]
    elif "\u957f\u671f" in validity and ("id_valid_to" not in data or _is_empty(data.get("id_valid_to"))):
        data["id_valid_to"] = "\u957f\u671f"


def _apply_normalizer(data: Dict[str, Any], key: str, fn: Callable[[Any], Optional[str]]) -> None:
    if key in data:
        data[key] = fn(data.get(key))


def _remove_legacy_keys(data: Dict[str, Any], prefixes: Iterable[str] = ("dl_",)) -> None:
    for key in list(data.keys()):
        text_key = str(key)
        if any(text_key.startswith(prefix) for prefix in prefixes):
            data.pop(key, None)


def clean_dynamic_data_for_ocr(dynamic_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize OCR/order dynamic data before persistence.

    Raw OCR JSON is intentionally not changed by this function. The cleaned
    dynamic_data is the canonical payload used by order detail and order_fact.
    """

    data = dict(dynamic_data or {})

    _remove_legacy_keys(data)
    _merge_aliases(data)
    _parse_id_validity_range(data)

    for key in _TEXT_FIELDS:
        if key in data:
            data[key] = norm_text(data.get(key), field_name=key)

    _apply_normalizer(data, "plate_no", norm_plate_no)
    _apply_normalizer(data, "vin", norm_vin)
    _apply_normalizer(data, "engine_no", norm_engine_no)
    _apply_normalizer(data, "id_number", norm_id_number)
    _apply_normalizer(data, "first_register_date", norm_fuzzy_date_text)
    _apply_normalizer(data, "issue_date", norm_ymd)
    _apply_normalizer(data, "id_birth_date", norm_ymd)
    _apply_normalizer(data, "id_valid_from", norm_ymd)
    _apply_normalizer(data, "id_valid_to", norm_id_valid_to)
    _apply_normalizer(data, "approved_passenger_count", norm_passenger_count)
    _apply_normalizer(data, "vehicle_brand_name", norm_vehicle_brand_name)
    _apply_normalizer(data, "manufacturer_name", norm_manufacturer_name)

    if _is_empty(data.get("id_birth_date")):
        derived_birth = _birth_from_resident_id(data.get("id_number"))
        if derived_birth:
            data["id_birth_date"] = derived_birth

    return data


def diff_dynamic_data_for_ocr(dynamic_data: Dict[str, Any]) -> Dict[str, Any]:
    before = dict(dynamic_data or {})
    after = clean_dynamic_data_for_ocr(before)
    changed: Dict[str, Dict[str, Any]] = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed[key] = {"before": before.get(key), "after": after.get(key)}
    return {
        "version": CLEANING_RULE_VERSION,
        "changed": bool(changed),
        "changes": changed,
        "after": after,
    }
