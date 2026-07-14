# encoding: utf-8
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional


SLOT_KEYS = {
    "vehicle_cert",
    "idcard_front",
    "idcard_back",
    "driving_license_main",
    "driving_license_sub",
    "related",
}

SINGLE_SLOTS = {
    "vehicle_cert",
    "idcard_front",
    "idcard_back",
    "driving_license_main",
    "driving_license_sub",
}

MULTI_SLOTS = {"related"}

SLOT_LABELS = {
    "vehicle_cert": "合格证",
    "idcard_front": "身份证正面",
    "idcard_back": "身份证反面",
    "driving_license_main": "行驶证主页",
    "driving_license_sub": "行驶证副页",
    "related": "相关图片",
}

_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "vehicle_cert": (
        "合格证",
        "车辆合格证",
        "整车出厂合格证",
        "机动车整车出厂合格证",
        "车辆识别代号",
        "底盘合格证",
        "发动机型号",
        "制造厂名称",
    ),
    "idcard_front": (
        "居民身份证",
        "姓名",
        "性别",
        "民族",
        "出生",
        "住址",
        "公民身份号码",
    ),
    "idcard_back": (
        "签发机关",
        "签发日期",
        "失效日期",
        "有效期限",
        "有效期",
        "中华人民共和国",
        "居民身份证",
    ),
    "driving_license_main": (
        "机动车行驶证",
        "号牌号码",
        "车辆类型",
        "所有人",
        "住址",
        "使用性质",
        "品牌型号",
        "车辆识别代号",
        "发动机号码",
        "注册日期",
        "发证日期",
    ),
    "driving_license_sub": (
        "中华人民共和国机动车行驶证",
        "核定载人数",
        "核定载质量",
        "总质量",
        "整备质量",
        "外廓尺寸",
        "档案编号",
        "证芯编号",
        "燃油类型",
        "准牵引总质量",
        "备注",
        "检验记录",
    ),
    "related": (
        "保单",
        "发票",
        "报价",
        "其他",
        "微信",
        "聊天",
        "截图",
    ),
}

_FILENAME_HINTS: Dict[str, tuple[str, ...]] = {
    "vehicle_cert": ("hege", "cert", "certificate", "hgz", "合格", "合格证"),
    "idcard_front": ("idcard_front", "idcard-front", "sfz_front", "sfz-front", "id_front", "id-front", "身份证正", "身份证人像", "正面"),
    "idcard_back": ("idcard_back", "idcard-back", "sfz_back", "sfz-back", "id_back", "id-back", "身份证反", "国徽", "反面"),
    "driving_license_main": ("drive_main", "drive-main", "driving_main", "driving-main", "xsz_main", "xsz-main", "行驶证主页", "行驶证正", "主页"),
    "driving_license_sub": ("drive_sub", "drive-sub", "driving_sub", "driving-sub", "xsz_sub", "xsz-sub", "行驶证副页", "行驶证反", "副页"),
    "related": ("related", "backup", "other", "其他", "截图"),
}

_STRONG_FILENAME_HINTS: Dict[str, tuple[str, ...]] = {
    "vehicle_cert": ("vehicle_cert", "vehicle-cert", "vehicle_certificate", "vehicle-certificate", "cert", "certificate", "hgz", "合格证"),
    "idcard_front": ("idcard_front", "idcard-front", "sfz_front", "sfz-front", "id_front", "id-front", "身份证正面", "身份证人像面"),
    "idcard_back": ("idcard_back", "idcard-back", "sfz_back", "sfz-back", "id_back", "id-back", "身份证反面", "身份证国徽面"),
    "driving_license_main": ("driving_license_main", "driving-license-main", "drive_main", "drive-main", "xsz_main", "xsz-main", "行驶证主页"),
    "driving_license_sub": ("driving_license_sub", "driving-license-sub", "drive_sub", "drive-sub", "xsz_sub", "xsz-sub", "行驶证副页"),
}

_CONTEXT_HINTS: Dict[str, tuple[str, ...]] = {
    "vehicle_cert": ("合格证", "车辆合格证", "整车出厂合格证", "机动车整车出厂合格证"),
    "idcard_front": ("身份证正面", "身份证人像面", "身份证头像面", "身份证正页", "身份证人像页"),
    "idcard_back": ("身份证反面", "身份证国徽面", "身份证背面", "身份证反页", "身份证国徽页"),
    "driving_license_main": ("行驶证主页", "行驶证正页", "行驶证正本", "行驶证主页照片"),
    "driving_license_sub": ("行驶证副页", "行驶证反页", "行驶证副本", "行驶证副页照片"),
    "related": ("相关图片", "其他材料", "补充材料", "聊天截图"),
}

_PREFIX_HINTS: Dict[str, str] = {
    "/cert/": "vehicle_cert",
    "cert/": "vehicle_cert",
    "/backup/": "related",
    "backup/": "related",
}


@dataclass(frozen=True)
class SlotClassification:
    predicted_slot_key: str
    confidence: float
    method: str
    reason: str
    text_features: Dict[str, Any] = field(default_factory=dict)
    ocr_text_sample: str = ""


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def _norm_text(value: Any) -> str:
    text = _to_str(value).replace("\u3000", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _flatten_text(value: Any, *, limit: int = 8000) -> str:
    parts: list[str] = []

    def walk(v: Any) -> None:
        if len(" ".join(parts)) > limit:
            return
        if isinstance(v, str):
            s = _norm_text(v)
            if s:
                parts.append(s)
            return
        if isinstance(v, dict):
            for key, item in v.items():
                if isinstance(key, str):
                    parts.append(key)
                walk(item)
            return
        if isinstance(v, (list, tuple, set)):
            for item in v:
                walk(item)
            return
        if v is not None and not isinstance(v, (int, float, bool)):
            parts.append(_norm_text(v))

    walk(value)
    return _norm_text(" ".join(parts))[:limit]


def _score_keywords(text: str, keywords: Iterable[str]) -> int:
    score = 0
    for keyword in keywords:
        if keyword and keyword in text:
            score += 1
    return score


def _storage_prefix_slot(storage_key: str) -> Optional[str]:
    key = "/" + _to_str(storage_key).strip().lstrip("/").lower()
    for prefix, slot_key in _PREFIX_HINTS.items():
        if key.startswith(prefix) or prefix in key:
            return slot_key
    if "/idcard/" in key:
        return None
    if "/dl/" in key:
        return None
    return None


def classify_image_slot(
    *,
    provided_slot_key: Optional[str] = None,
    original_name: Optional[str] = None,
    storage_key: Optional[str] = None,
    ocr_text: Optional[Any] = None,
    raw_payload: Optional[Any] = None,
) -> SlotClassification:
    """Classify an uploaded image into the quote-assistant slot taxonomy.

    The function is deterministic and safe to run before vision-model access is
    available. It uses OCR text first, then file/storage hints, then the user
    provided slot as a low-risk fallback.
    """

    provided = _to_str(provided_slot_key).strip()
    if provided not in SLOT_KEYS:
        provided = ""

    text = _flatten_text(ocr_text if ocr_text is not None else raw_payload)
    name = _norm_text(original_name).lower()
    storage = _norm_text(storage_key).lower()

    features: Dict[str, Any] = {
        "provided_slot_key": provided or None,
        "has_ocr_text": bool(text),
        "storage_prefix_slot": None,
        "keyword_scores": {},
        "filename_hits": {},
    }

    if text:
        context_hits = {
            slot: _score_keywords(text, hints)
            for slot, hints in _CONTEXT_HINTS.items()
        }
        features["context_hint_hits"] = context_hits
        hint_slot, hint_score = max(context_hits.items(), key=lambda item: item[1])
        if hint_score > 0:
            return SlotClassification(
                predicted_slot_key=hint_slot,
                confidence=0.86,
                method="context_hint_rule",
                reason="图片上下文说明命中明确材料类型",
                text_features=features,
                ocr_text_sample=text[:2000],
            )

        scores = {slot: _score_keywords(text, words) for slot, words in _KEYWORDS.items()}
        features["keyword_scores"] = scores
        best_slot, best_score = max(scores.items(), key=lambda item: item[1])
        second_score = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
        if best_score >= 2 and best_score > second_score:
            confidence = min(0.98, 0.70 + best_score * 0.06)
            return SlotClassification(
                predicted_slot_key=best_slot,
                confidence=confidence,
                method="ocr_rule",
                reason=f"OCR关键词命中 {best_score} 个，明显高于其他槽位",
                text_features=features,
                ocr_text_sample=text[:2000],
            )
        if best_score >= 2 and provided == best_slot:
            return SlotClassification(
                predicted_slot_key=best_slot,
                confidence=0.86,
                method="ocr_rule",
                reason="OCR关键词与用户选择槽位一致",
                text_features=features,
                ocr_text_sample=text[:2000],
            )

    strong_filename_scores = {
        slot: _score_keywords(name + " " + storage, hints)
        for slot, hints in _STRONG_FILENAME_HINTS.items()
    }
    features["strong_filename_hits"] = strong_filename_scores
    strong_file_slot, strong_file_score = max(strong_filename_scores.items(), key=lambda item: item[1])
    if strong_file_score > 0:
        return SlotClassification(
            predicted_slot_key=strong_file_slot,
            confidence=0.82,
            method="strong_filename_rule",
            reason="文件名或路径包含明确材料类型",
            text_features=features,
            ocr_text_sample=text[:2000],
        )

    filename_scores = {
        slot: _score_keywords(name + " " + storage, hints)
        for slot, hints in _FILENAME_HINTS.items()
    }
    features["filename_hits"] = filename_scores
    file_slot, file_score = max(filename_scores.items(), key=lambda item: item[1])
    if file_score > 0:
        confidence = 0.76 if file_slot == provided else 0.70
        return SlotClassification(
            predicted_slot_key=file_slot,
            confidence=confidence,
            method="filename_rule",
            reason="文件名或路径包含槽位关键词",
            text_features=features,
            ocr_text_sample=text[:2000],
        )

    prefix_slot = _storage_prefix_slot(storage)
    features["storage_prefix_slot"] = prefix_slot
    if prefix_slot:
        return SlotClassification(
            predicted_slot_key=prefix_slot,
            confidence=0.68,
            method="storage_prefix",
            reason="对象存储目录前缀可确定大类",
            text_features=features,
            ocr_text_sample=text[:2000],
        )

    if provided:
        confidence = 0.58 if provided != "related" else 0.52
        return SlotClassification(
            predicted_slot_key=provided,
            confidence=confidence,
            method="provided_slot_fallback",
            reason="暂无OCR/文件名强特征，保留上传时槽位作为候选归位",
            text_features=features,
            ocr_text_sample=text[:2000],
        )

    return SlotClassification(
        predicted_slot_key="related",
        confidence=0.35,
        method="unknown_fallback",
        reason="暂无可用识别特征，归入相关图片候选池",
        text_features=features,
        ocr_text_sample=text[:2000],
    )


def is_single_slot(slot_key: str) -> bool:
    return _to_str(slot_key).strip() in SINGLE_SLOTS


def slot_label(slot_key: str) -> str:
    return SLOT_LABELS.get(_to_str(slot_key).strip(), _to_str(slot_key).strip() or "未知槽位")
