# app/services/ocr_cleaner.py
# encoding: utf-8
from __future__ import annotations

import re
from typing import Any, Dict, Optional

_YMD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_8DIGITS_RE = re.compile(r"^\d{8}$")


def _s(v: Any) -> str:
    if v is None:
        return ""
    try:
        return str(v).strip()
    except Exception:
        return ""


def _is_empty(v: Any) -> bool:
    s = _s(v).lower()
    return s in ("", "-", "—", "null", "none")


def _digits_only(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")


def norm_text(v: Any) -> Optional[str]:
    if _is_empty(v):
        return None
    return _s(v) or None


def norm_ymd(v: Any) -> Optional[str]:
    """统一日期：只允许 YYYY-MM-DD 或 None。"""
    if _is_empty(v):
        return None

    s = _s(v)
    if not s:
        return None

    s2 = s.replace("/", "-").replace(".", "-")
    s2 = re.sub(r"\s+", "", s2)

    if _YMD_RE.match(s2):
        return s2

    digits = _digits_only(s2)
    if _8DIGITS_RE.match(digits):
        yyyy = digits[0:4]
        mm = digits[4:6]
        dd = digits[6:8]
        try:
            mi = int(mm)
            di = int(dd)
            if mi < 1 or mi > 12:
                return None
            if di < 1 or di > 31:
                return None
        except Exception:
            return None
        return f"{yyyy}-{mm}-{dd}"

    return None


def clean_dynamic_data_for_ocr(dyn: Dict[str, Any]) -> Dict[str, Any]:
    """OCR 入库前清洗 dynamic_data（新表唯一口径）。

    - 删除所有 dl_* 历史键（禁止再出现）
    - 日期字段两态化：YYYY-MM-DD / None
    - 文本字段：空占位 -> None
    """
    d = dict(dyn or {})

    # 1) 删除历史键
    for k in list(d.keys()):
        if str(k).startswith("dl_"):
            d.pop(k, None)

    # 2) 日期字段
    for k in ("first_register_date", "issue_date", "id_birth_date", "id_valid_from", "id_valid_to"):
        if k in d:
            d[k] = norm_ymd(d.get(k))

    # 3) 文本字段
    for k in (
            "owner_name",
            "plate_no",
            "vin",
            "engine_no",
            "vehicle_model",
            "vehicle_type",
            "use_nature",
            "issuer_org",
            "id_name",
            "id_number",
            "id_address",
            "id_gender",
            "id_ethnicity",
            "id_issuer",
            "id_validity",
            "approved_passenger_count",
            "vehicle_brand_name",
            "manufacturer_name",
    ):
        if k in d:
            d[k] = norm_text(d.get(k))

    return d
