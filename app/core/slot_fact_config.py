# app/core/slot_fact_config.py  （示意：你可以放进你已有的 slot_field_config 旁边）
# encoding: utf-8
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


# =========================
# 0) 版本（用于审计/回溯）
# =========================
FACT_CONFIG_VERSION = 1


# =========================
# 1) 卡槽级字段定义（recognized_json 使用）
#    - 每个 slot_key 只定义“该槽可产出的规范字段”
#    - 禁止出现 dl_* / 历史别名键
# =========================
SLOT_FIELDS: Dict[str, List[str]] = {
    # 车辆合格证（vehicle_certificate）
    "vehicle_cert": [
        "vin",
        "engine_no",
        "vehicle_model",
        "approved_passenger_count",
        "vehicle_brand_name",
        "manufacturer_name",
    ],

    # 身份证正面（idcard front）
    "idcard_front": [
        "id_name",
        "id_number",
        "id_address",
        "id_birth_date",
        "id_gender",
        "id_ethnicity",
    ],

    # 身份证背面（idcard back）
    "idcard_back": [
        "id_issuer",
        "id_valid_from",
        "id_valid_to",
        "id_validity",
    ],

    # 行驶证正页（vehicle_license front）
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

    # 行驶证副页（vehicle_license back）
    # 你当前 extractor 对 back 没额外产出独有字段也没关系，先保留结构位
    "driving_license_sub": [
        # 预留：未来如果你从副页抽年检记录、核载、档案编号等，直接加字段即可
    ],

    # 相关图片：不跑 OCR，不产生字段
    "related": [],

    # 未知/脏数据兜底：不跑 OCR，不产生字段
    "unknown": [],
}


# =========================
# 2) 订单级固定字段定义（order_fact / dynamic_data 的“定死字段集”）
#    - 这里就是你要“定死 dynamic_data”里允许存在的键集合
# =========================
ORDER_FIELDS: List[str] = [
    "vin",
    "plate_no",
    "owner_name",
    "engine_no",
    "vehicle_model",
    "first_register_date",
    "id_number",
]


# =========================
# 3) 组合规则：从 slot recognized -> order_fact
#    - 每个订单字段给出候选来源列表（按优先级排序）
#    - merge_mode：fill_if_empty / always_override（默认 fill_if_empty，更符合“人工优先”）
#    - transform：可选转换（日期格式化等）
# =========================
@dataclass(frozen=True)
class SourceRule:
    from_slot: str
    from_key: str
    transform: Optional[str] = None          # 例如 "ymd"
    merge_mode: str = "fill_if_empty"        # fill_if_empty | always_override


COMPOSE_RULES: Dict[str, List[SourceRule]] = {
    # vin：优先行驶证正页，其次合格证
    "vin": [
        SourceRule("driving_license_main", "vin"),
        SourceRule("vehicle_cert", "vin"),
    ],

    # plate_no：只应来自行驶证
    "plate_no": [
        SourceRule("driving_license_main", "plate_no"),
    ],

    # owner_name：只应来自行驶证（身份证姓名 id_name 是另一字段语义，不混用）
    "owner_name": [
        SourceRule("driving_license_main", "owner_name"),
    ],

    # engine_no：优先行驶证，其次合格证
    "engine_no": [
        SourceRule("driving_license_main", "engine_no"),
        SourceRule("vehicle_cert", "engine_no"),
    ],

    # vehicle_model：优先行驶证，其次合格证
    "vehicle_model": [
        SourceRule("driving_license_main", "vehicle_model"),
        SourceRule("vehicle_cert", "vehicle_model"),
    ],

    # first_register_date：只应来自行驶证注册日期（必须 ymd 规范化）
    "first_register_date": [
        SourceRule("driving_license_main", "first_register_date", transform="ymd"),
    ],

    # id_number：身份证正面优先（如果你未来从行驶证也能抽出，不建议混用）
    "id_number": [
        SourceRule("idcard_front", "id_number"),
    ],
}