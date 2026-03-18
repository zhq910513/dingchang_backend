# encoding: utf-8
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import func, or_

OWNER_FUZZY_DIRECT_KEYS: List[str] = [
    "owner_name",
    "id_name",
    "name",
    "person_name",
    "vehicle_cert_owner_name",
    "vehicle_owner_name",
    "certificate_owner_name",
    "driving_license_owner_name",
    "license_owner_name",
    "idcard_name",
    "id_card_name",
    "identity_name",
]

OWNER_FUZZY_DYNAMIC_DATA_PATHS: List[str] = [
    "vehicle_cert.owner_name",
    "vehicle_cert.vehicle_cert_owner_name",
    "vehicle_cert.vehicle_owner_name",
    "vehicle_cert.certificate_owner_name",
    "vehicle_cert.name",
    "driving_license.owner_name",
    "driving_license.driving_license_owner_name",
    "driving_license.license_owner_name",
    "driving_license.name",
    "driving_license_main.owner_name",
    "driving_license_main.driving_license_owner_name",
    "driving_license_main.license_owner_name",
    "driving_license_main.name",
    "driving_license_sub.owner_name",
    "driving_license_sub.driving_license_owner_name",
    "driving_license_sub.license_owner_name",
    "driving_license_sub.name",
    "idcard.name",
    "idcard.id_name",
    "idcard.owner_name",
    "idcard.idcard_name",
    "idcard.id_card_name",
    "idcard.identity_name",
    "idcard_front.name",
    "idcard_front.id_name",
    "idcard_front.owner_name",
    "idcard_front.idcard_name",
    "idcard_front.id_card_name",
    "idcard_front.identity_name",
    "idcard_back.name",
    "idcard_back.id_name",
    "idcard_back.owner_name",
    "idcard_back.idcard_name",
    "idcard_back.id_card_name",
    "idcard_back.identity_name",
]

_EMPTY_DICT: Dict[str, Any] = {}


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else _EMPTY_DICT


def _trim_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pick_first_non_empty(data: Dict[str, Any], *keys: str) -> Optional[str]:
    if not data:
        return None
    for key in keys:
        value = data.get(key)
        text = _trim_or_none(value)
        if text:
            return text
    return None



def resolve_owner_name(dynamic_data: Any, ocr_raw_json: Any = None) -> Optional[str]:
    """
    车主统一新口径：只基于 dynamic_data 规范字段与多卡槽字段兜底。
    保留 ocr_raw_json 形参仅为兼容旧调用点，但不再参与正式解析。
    """
    dynamic_data_dict = _safe_dict(dynamic_data)

    owner_name = _pick_first_non_empty(
        dynamic_data_dict,
        "owner_name",
        "id_name",
        "name",
        "person_name",
    )
    if owner_name:
        return owner_name

    vehicle_cert = dynamic_data_dict.get("vehicle_cert")
    if isinstance(vehicle_cert, dict):
        owner_name = _pick_first_non_empty(
            vehicle_cert,
            "owner_name",
            "vehicle_cert_owner_name",
            "vehicle_owner_name",
            "certificate_owner_name",
            "name",
        )
        if owner_name:
            return owner_name

    owner_name = _pick_first_non_empty(
        dynamic_data_dict,
        "vehicle_cert_owner_name",
        "vehicle_owner_name",
        "certificate_owner_name",
    )
    if owner_name:
        return owner_name

    for slot_name in ("driving_license", "driving_license_main", "driving_license_sub"):
        slot_value = dynamic_data_dict.get(slot_name)
        if isinstance(slot_value, dict):
            owner_name = _pick_first_non_empty(
                slot_value,
                "owner_name",
                "driving_license_owner_name",
                "license_owner_name",
                "name",
            )
            if owner_name:
                return owner_name

    owner_name = _pick_first_non_empty(
        dynamic_data_dict,
        "driving_license_owner_name",
        "license_owner_name",
    )
    if owner_name:
        return owner_name

    for slot_name in ("idcard", "idcard_front", "idcard_back"):
        slot_value = dynamic_data_dict.get(slot_name)
        if isinstance(slot_value, dict):
            owner_name = _pick_first_non_empty(
                slot_value,
                "name",
                "id_name",
                "owner_name",
                "idcard_name",
                "id_card_name",
                "identity_name",
            )
            if owner_name:
                return owner_name

    owner_name = _pick_first_non_empty(
        dynamic_data_dict,
        "idcard_name",
        "id_card_name",
        "identity_name",
    )
    if owner_name:
        return owner_name

    return None


def append_owner_name_fuzzy_clause(
    clauses: List[Any],
    *,
    value: Optional[str],
    flat_text_getter: Callable[[str], Any],
    path_text_getter: Callable[[str], Any],
) -> None:
    needle_source = (value or "").strip()
    if not needle_source:
        return

    needle = f"%{needle_source.lower()}%"
    or_terms = []

    for key in OWNER_FUZZY_DIRECT_KEYS:
        or_terms.append(func.lower(flat_text_getter(key)).like(needle))

    for path in OWNER_FUZZY_DYNAMIC_DATA_PATHS:
        or_terms.append(func.lower(path_text_getter(path)).like(needle))

    if or_terms:
        clauses.append(or_(*or_terms))
