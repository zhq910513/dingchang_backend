# app/core/slot_field_config.py
# encoding: utf-8
from __future__ import annotations

"""
卡槽配置（唯一真源）

本文件职责：
1) 统一定义卡槽顺序、标题、多图属性（multi）
2) 统一定义哪些卡槽属于 OCR 卡槽（ocr）
3) 统一定义“卡槽图片输出”的标准字段契约（固定字段集合）

硬规则：
- 任何地方需要“卡槽图片结构”输出，必须使用 utils 下的组装器按本配置产出
- 禁止业务层/路由层自行拼 slot 图片结构或 url list
"""

from typing import Any, Dict, List

FACT_CONFIG_VERSION = 1

# =========================
# 1) 卡槽顺序（标准骨架顺序）
# =========================
SLOT_ORDER: List[str] = [
    "vehicle_cert",
    "idcard_front",
    "idcard_back",
    "driving_license_main",
    "driving_license_sub",
    "related",
    "unknown",
]

# ✅ 兼容常量名（历史代码引用）——仍以 SLOT_ORDER 为真源
ORDERED_SLOT_KEYS: List[str] = SLOT_ORDER

# =========================
# 2) OCR 卡槽集合（唯一真源）
# =========================
OCR_SLOTS = {
    "vehicle_cert",
    "idcard_front",
    "idcard_back",
    "driving_license_main",
    "driving_license_sub",
}

# =========================
# 3) 卡槽元信息（title/multi）
# =========================
SLOT_META: Dict[str, Dict[str, Any]] = {
    "vehicle_cert": {"title": "车辆合格证", "multi": False},
    "idcard_front": {"title": "身份证正面", "multi": False},
    "idcard_back": {"title": "身份证背面", "multi": False},
    "driving_license_main": {"title": "行驶证正页", "multi": False},
    "driving_license_sub": {"title": "行驶证副页", "multi": False},
    "related": {"title": "相关图片", "multi": True},
    "unknown": {"title": "未知图片", "multi": True},
}

# =========================
# 4) 卡槽图片输出契约（字段固定）
# =========================
SLOT_IMAGE_NODE_FIELDS: List[str] = ["slot_key", "title", "multi", "ocr", "images"]
SLOT_IMAGE_ITEM_FIELDS: List[str] = [
    "order_image_id",
    "image_file_id",
    "storage_key",
    "url",
    "created_at",
    "updated_at",
]


# =========================
# 5) 对外函数（被全项目调用）
# =========================
def ordered_slot_keys() -> List[str]:
    """标准卡槽顺序（用于产出固定骨架）"""
    return list(SLOT_ORDER)


def slot_title(slot_key: str) -> str:
    sk = str(slot_key or "").strip()
    meta = SLOT_META.get(sk) or {}
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return sk or "未知卡槽"


# ✅ 兼容函数名（历史代码引用）
def slot_is_multi_image(slot_key: str) -> bool:
    sk = str(slot_key or "").strip()
    meta = SLOT_META.get(sk) or {}
    return bool(meta.get("multi", False))


def slot_is_ocr(slot_key: str) -> bool:
    sk = str(slot_key or "").strip()
    return sk in OCR_SLOTS
