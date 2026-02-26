# app/core/slot_field_config.py
# encoding: utf-8
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ==========================
# 卡槽字段配置（统一口径）
# ==========================
# key:        接口返回给前端的字段 key（展示层）
# label:      前端展示标签（展示层）
# source_key: dynamic_data 实际取值 key（存储层）
SLOT_FIELD_CONFIGS: Dict[str, Dict[str, Any]] = {
    "vehicle_cert": {
        "slot_key": "vehicle_cert",
        "title": "车辆合格证",
        "multi_image": False,
        "fields": [
            {"key": "vehicle_brand_name", "label": "品牌", "source_key": "vehicle_brand_name"},
            {"key": "vehicle_name", "label": "车辆名称", "source_key": "vehicle_name"},
            {"key": "vehicle_model", "label": "车辆型号", "source_key": "vehicle_model"},
            {"key": "vin", "label": "车架号", "source_key": "vin"},
            {"key": "engine_no", "label": "发动机号", "source_key": "engine_no"},
            {"key": "plate_no", "label": "车牌号", "source_key": "plate_no"},
            {"key": "register_date", "label": "注册日期", "source_key": "register_date"},
            {"key": "first_register_date", "label": "初登日期", "source_key": "first_register_date"},
            {"key": "owner_name", "label": "车主", "source_key": "owner_name"},
            {"key": "owner_phone", "label": "车主电话", "source_key": "owner_phone"},
        ],
    },
    "idcard_front": {
        "slot_key": "idcard_front",
        "title": "身份证正面",
        "multi_image": False,
        "fields": [
            {"key": "id_name", "label": "姓名", "source_key": "id_name"},
            {"key": "id_number", "label": "身份证号", "source_key": "id_number"},
            {"key": "id_address", "label": "住址", "source_key": "id_address"},
            {"key": "id_nation", "label": "民族", "source_key": "id_nation"},
            {"key": "id_gender", "label": "性别", "source_key": "id_gender"},
            {"key": "id_birth", "label": "出生日期", "source_key": "id_birth"},
        ],
    },
    "idcard_back": {
        "slot_key": "idcard_back",
        "title": "身份证背面",
        "multi_image": False,
        "fields": [
            {"key": "id_issue_authority", "label": "签发机关", "source_key": "id_issue_authority"},
            {"key": "id_valid_from", "label": "有效期起", "source_key": "id_valid_from"},
            {"key": "id_valid_to", "label": "有效期止", "source_key": "id_valid_to"},
            {"key": "id_valid_period", "label": "有效期", "source_key": "id_valid_period"},
        ],
    },
    "driving_license_main": {
        "slot_key": "driving_license_main",
        "title": "行驶证主页",
        "multi_image": False,
        "fields": [
            {"key": "dl_owner", "label": "车主", "source_key": "dl_owner"},
            {"key": "id_name", "label": "证件姓名", "source_key": "id_name"},
            {"key": "dl_plate_no", "label": "车牌号", "source_key": "dl_plate_no"},
            {"key": "dl_vin", "label": "车架号", "source_key": "dl_vin"},
            {"key": "dl_engine_no", "label": "发动机号", "source_key": "dl_engine_no"},
            # 统一口径：车型使用 dl_vehicle_model（不再使用 dl_brand_model）
            {"key": "dl_vehicle_model", "label": "车型", "source_key": "dl_vehicle_model"},
            {"key": "dl_register_date", "label": "注册日期", "source_key": "dl_register_date"},
            {"key": "dl_issue_date", "label": "发证日期", "source_key": "dl_issue_date"},
            # 历史脏 key 兼容：dynamic_data 存的是 dl_use性质
            {"key": "dl_use_nature", "label": "使用性质", "source_key": "dl_use性质"},
            {"key": "dl_id_number", "label": "证件号码", "source_key": "dl_id_number"},
        ],
    },
    "driving_license_sub": {
        "slot_key": "driving_license_sub",
        "title": "行驶证副页",
        "multi_image": False,
        "fields": [
            {"key": "dl_owner", "label": "车主", "source_key": "dl_owner"},
            {"key": "id_name", "label": "证件姓名", "source_key": "id_name"},
            {"key": "dl_plate_no", "label": "车牌号", "source_key": "dl_plate_no"},
            {"key": "dl_vin", "label": "车架号", "source_key": "dl_vin"},
            {"key": "dl_engine_no", "label": "发动机号", "source_key": "dl_engine_no"},
            # 统一口径：车型使用 dl_vehicle_model（不再使用 dl_brand_model）
            {"key": "dl_vehicle_model", "label": "车型", "source_key": "dl_vehicle_model"},
            {"key": "dl_register_date", "label": "注册日期", "source_key": "dl_register_date"},
            {"key": "dl_issue_date", "label": "发证日期", "source_key": "dl_issue_date"},
            {"key": "dl_use_nature", "label": "使用性质", "source_key": "dl_use性质"},
            {"key": "dl_id_number", "label": "证件号码", "source_key": "dl_id_number"},
        ],
    },
    "related": {
        "slot_key": "related",
        "title": "相关图片",
        "multi_image": True,
        "fields": [],
    },
}

# 固定输出顺序（详情页 slots 展示顺序）
DEFAULT_SLOT_ORDER: List[str] = [
    "vehicle_cert",
    "idcard_front",
    "idcard_back",
    "driving_license_main",
    "driving_license_sub",
    "related",
]


def slot_title(slot_key: str) -> str:
    """
    返回卡槽标题；未知槽兜底返回 slot_key 本身。
    """
    sk = str(slot_key or "").strip()
    conf = SLOT_FIELD_CONFIGS.get(sk)
    if conf:
        t = conf.get("title")
        if t is not None and str(t).strip():
            return str(t)
    return sk


def slot_is_multi_image(slot_key: str) -> bool:
    """
    返回卡槽是否多图。
    """
    sk = str(slot_key or "").strip()
    conf = SLOT_FIELD_CONFIGS.get(sk) or {}
    return bool(conf.get("multi_image", False))


def slot_field_defs(slot_key: str) -> List[Dict[str, str]]:
    """
    返回某卡槽字段定义（配置本身，不含值）。
    未知槽返回空数组。
    """
    sk = str(slot_key or "").strip()
    conf = SLOT_FIELD_CONFIGS.get(sk) or {}
    rows = conf.get("fields") or []
    out: List[Dict[str, str]] = []
    for row in rows:
        key = str(row.get("key") or "").strip()
        label = str(row.get("label") or "").strip()
        source_key = str(row.get("source_key") or "").strip()
        if not key or not label or not source_key:
            continue
        out.append(
            {
                "key": key,
                "label": label,
                "source_key": source_key,
            }
        )
    return out


def ordered_slot_keys(extra_slot_keys: Optional[List[str]] = None) -> List[str]:
    """
    返回稳定卡槽顺序；未知槽会追加到末尾（用于图片里出现新 slot 的兜底）。
    """
    base = list(DEFAULT_SLOT_ORDER)
    extras = [str(x or "").strip() for x in (extra_slot_keys or [])]
    extras = [x for x in extras if x]
    for sk in extras:
        if sk not in base:
            base.append(sk)
    return base


def build_slot_fields_from_dynamic(slot_key: str, dynamic_data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    按卡槽配置从 dynamic_data 取值，返回前端 fields[]:
    [
      {"key": "...", "label": "...", "value": ...},
      ...
    ]

    规则（统一为稳定返回口径）：
    - 按配置字段固定顺序返回
    - 即使 dynamic_data 缺少 source_key，也返回 value=None
    - 不做业务推导，不做跨槽兜底，不猜字段
    """
    d = dynamic_data or {}
    if not isinstance(d, dict):
        d = {}

    out: List[Dict[str, Any]] = []
    for fd in slot_field_defs(slot_key):
        source_key = fd["source_key"]
        out.append(
            {
                "key": fd["key"],
                "label": fd["label"],
                "value": d.get(source_key),
            }
        )
    return out


# ==========================
# 向后统一导出名（给 orders.py / 其它模块直接 import）
# ==========================
# 你当前 orders.py 使用的是这组名字，这里直接给出稳定导出，避免 ImportError。
ORDERED_SLOT_KEYS: List[str] = list(DEFAULT_SLOT_ORDER)


def get_slot_title(slot_key: str) -> str:
    return slot_title(slot_key)


def get_slot_field_defs(slot_key: str) -> List[Dict[str, str]]:
    return slot_field_defs(slot_key)
