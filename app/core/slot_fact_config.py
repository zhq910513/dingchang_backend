# app/core/slot_fact_config.py
# encoding: utf-8
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

FACT_CONFIG_VERSION = 1

SLOT_FIELDS: Dict[str, List[str]] = {
    "vehicle_cert": [
        "vin",
        "engine_no",
        "vehicle_model",
        "approved_passenger_count",
        "vehicle_brand_name",
        "manufacturer_name",
    ],
    "idcard_front": [
        "id_name",
        "id_number",
        "id_address",
        "id_birth_date",
        "id_gender",
        "id_ethnicity",
    ],
    "idcard_back": [
        "id_issuer",
        "id_valid_from",
        "id_valid_to",
        "id_validity",
    ],
    "driving_license_main": [
        "plate_no",
        "owner_name",
        "vin",
        "engine_no",
        "vehicle_model",
        "vehicle_type",
        "use_nature",
        "first_register_date",
        "issue_date",
        "issuer_org",
    ],
    "driving_license_sub": [],
    "related": [],
    "unknown": [],
}

ORDER_FIELDS: List[str] = [
    "vin",
    "plate_no",
    "owner_name",
    "engine_no",
    "vehicle_model",
    "first_register_date",
    "id_number",
]


@dataclass(frozen=True)
class SourceRule:
    from_slot: str
    from_key: str
    transform: Optional[str] = None
    merge_mode: str = "fill_if_empty"  # fill_if_empty | always_override


COMPOSE_RULES: Dict[str, List[SourceRule]] = {
    "vin": [
        SourceRule("driving_license_main", "vin"),
        SourceRule("vehicle_cert", "vin"),
    ],
    "plate_no": [
        SourceRule("driving_license_main", "plate_no"),
    ],
    "owner_name": [
        SourceRule("driving_license_main", "owner_name"),
    ],
    "engine_no": [
        SourceRule("driving_license_main", "engine_no"),
        SourceRule("vehicle_cert", "engine_no"),
    ],
    "vehicle_model": [
        SourceRule("driving_license_main", "vehicle_model"),
        SourceRule("vehicle_cert", "vehicle_model"),
    ],
    "first_register_date": [
        SourceRule("driving_license_main", "first_register_date", transform="ymd"),
    ],
    "id_number": [
        SourceRule("idcard_front", "id_number"),
    ],
}
