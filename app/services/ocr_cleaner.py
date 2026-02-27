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
    """
    “空”的严格定义：None / '' / '-' / '—' / 'null' / 'none'
    """
    s = _s(v).lower()
    return s in ("", "-", "—", "null", "none")


def _digits_only(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")


def norm_text(v: Any) -> Optional[str]:
    """
    文本字段：空 -> None；非空 -> 去两侧空格
    """
    if _is_empty(v):
        return None
    return _s(v) or None


def norm_ymd(v: Any) -> Optional[str]:
    """
    统一日期：只允许 YYYY-MM-DD 或 None
    - 支持：YYYY-MM-DD / YYYYMMDD / YYYY/MM/DD / YYYY.MM.DD / 含空格
    - 非法：一律 None
    """
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


def _fill_if_empty(dst: Dict[str, Any], key: str, val: Any) -> None:
    """
    不覆盖人工值：只有 dst[key] 为空时才回填
    """
    if key not in dst or _is_empty(dst.get(key)):
        if not _is_empty(val):
            dst[key] = val


def _sync_pair_ymd(d: Dict[str, Any], a: str, b: str) -> None:
    """
    两字段同步：只保留两态（ymd / None），且同一单 a==b
    """
    av = norm_ymd(d.get(a))
    bv = norm_ymd(d.get(b))
    chosen = av or bv
    d[a] = chosen
    d[b] = chosen


def _sync_text_two_way(d: Dict[str, Any], std_key: str, dl_key: str) -> None:
    """
    字段同步（双向补空，不互相覆盖非空）：
    - std 为空 -> 从 dl 回填
    - dl 为空 -> 从 std 回填
    """
    std_v = norm_text(d.get(std_key))
    dl_v = norm_text(d.get(dl_key))

    if std_v is None and dl_v is not None:
        d[std_key] = dl_v
        std_v = dl_v

    if dl_v is None and std_v is not None:
        d[dl_key] = std_v


def clean_dynamic_data_for_ocr(dyn: Dict[str, Any]) -> Dict[str, Any]:
    """
    ✅ OCR 入库前“强制清洗 + 同步”总入口（只改 dynamic_data，不动 ocr_raw_json）

    规则（与你的验收口径一致）：
    1) dl_issue_date：只允许 YYYY-MM-DD 或 None
    2) dl_register_date 与 first_register_date：两态化且同值（ymd / None）
    3) 标准字段与 dl_* 同步（不覆盖人工值，双向补空）：
       owner_name/plate_no/vin/engine_no/vehicle_model/id_number ↔ dl_owner/dl_plate_no/dl_vin/dl_engine_no/dl_vehicle_model/dl_id_number
       id_number：标准优先，dl_id_number 为空时可从 id_number 回填；标准为空时可用 dl_id_number 回填
    """
    d = dict(dyn or {})

    # --- 1) 日期两态化 ---
    d["dl_issue_date"] = norm_ymd(d.get("dl_issue_date"))
    _sync_pair_ymd(d, "dl_register_date", "first_register_date")

    # --- 2) 文本字段同步（双向补空，不覆盖） ---
    _sync_text_two_way(d, "owner_name", "dl_owner")
    _sync_text_two_way(d, "plate_no", "dl_plate_no")
    _sync_text_two_way(d, "vin", "dl_vin")
    _sync_text_two_way(d, "engine_no", "dl_engine_no")
    _sync_text_two_way(d, "vehicle_model", "dl_vehicle_model")

    # --- 3) 身份证号（标准优先，但允许双向补空） ---
    std_id = norm_text(d.get("id_number"))
    dl_id = norm_text(d.get("dl_id_number"))

    if std_id is None and dl_id is not None:
        d["id_number"] = dl_id
        std_id = dl_id

    if dl_id is None and std_id is not None:
        d["dl_id_number"] = std_id

    # --- 4) 额外：把“明显脏的空占位”统一成 None（只对我们关心的字段，避免误伤其它业务键） ---
    for k in (
        "owner_name",
        "plate_no",
        "vin",
        "engine_no",
        "vehicle_model",
        "id_number",
        "dl_owner",
        "dl_plate_no",
        "dl_vin",
        "dl_engine_no",
        "dl_vehicle_model",
        "dl_id_number",
    ):
        if k in d and _is_empty(d.get(k)):
            d[k] = None

    return d