# app/services/ocr_worker.py
# encoding: utf-8
from __future__ import annotations

import asyncio
import json
import logging
import re
import traceback
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.core.db import get_db
from app.core.slot_fact_config import COMPOSE_RULES, SLOT_FIELDS
from app.models.image_file import ImageFile
from app.models.image_ocr_result import ImageOcrResult
from app.models.ocr_image_cache import OcrImageCache
from app.models.ocr_task import OcrTask
from app.models.order import Order, OrderImage
from app.services.baidu_ocr import OcrNotConfigured, call_ocr
from app.services.ocr_cleaner import clean_dynamic_data_for_ocr
from app.services.order_fact_service import sync_order_fact_from_dynamic_data
from app.services.storage import StorageService

logger = logging.getLogger(__name__)

storage = StorageService()
BJ_TZ = ZoneInfo("Asia/Shanghai")

# ✅ 只做这三类卡证 OCR：行驶证 / 车辆合格证 / 身份证
SLOT_TO_OCR: Dict[str, Tuple[str, Optional[str]]] = {
    "idcard_front": ("idcard", "front"),
    "idcard_back": ("idcard", "back"),
    "driving_license_main": ("vehicle_license", "front"),
    "driving_license_sub": ("vehicle_license", "back"),
    "vehicle_cert": ("vehicle_certificate", None),
}
CORE_OCR_SLOTS = {"vehicle_cert", "idcard_front", "driving_license_main"}

# 主模型失败、字段为空或用户放错槽位时的兜底顺序。先试强模板，再用通用文字 OCR 做保守提取。
SLOT_OCR_CANDIDATES: Dict[str, Tuple[Tuple[str, Optional[str]], ...]] = {
    "idcard_front": (
        ("idcard", "front"),
        ("idcard", "back"),
        ("vehicle_license", "front"),
        ("vehicle_certificate", None),
        ("accurate_basic", None),
    ),
    "idcard_back": (
        ("idcard", "back"),
        ("idcard", "front"),
        ("accurate_basic", None),
    ),
    "driving_license_main": (
        ("vehicle_license", "front"),
        ("vehicle_license", "back"),
        ("vehicle_certificate", None),
        ("idcard", "front"),
        ("accurate_basic", None),
    ),
    "driving_license_sub": (
        ("vehicle_license", "back"),
        ("vehicle_license", "front"),
        ("accurate_basic", None),
    ),
    "vehicle_cert": (
        ("vehicle_certificate", None),
        ("vehicle_license", "front"),
        ("accurate_basic", None),
    ),
    "related": (
        ("vehicle_license", "front"),
        ("vehicle_certificate", None),
        ("idcard", "front"),
        ("vehicle_license", "back"),
        ("idcard", "back"),
        ("accurate_basic", None),
    ),
}

HIGH_VALUE_FIELDS = {
    "id_number",
    "vin",
    "plate_no",
    "engine_no",
    "owner_name",
    "id_name",
    "vehicle_model",
}

def _now() -> datetime:
    """
    ✅ 全局时间口径对齐：
    - DB 存北京时间 naive DATETIME（timezone=False）
    - 显式按 Asia/Shanghai 取当前时间，再去掉 tzinfo
    """
    return datetime.now(BJ_TZ).replace(tzinfo=None)


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_ymd(value: Any) -> str:
    """
    统一日期格式为 YYYY-MM-DD：
    - 支持 YYYYMMDD -> YYYY-MM-DD
    - 支持 YYYY-MM-DD
    - '-', 空串, 非法值 -> ''
    """
    text = _safe_str(value)
    if not text or text == "-":
        return ""

    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return text
        except ValueError:
            return ""

    return ""


def _clamp_progress(value: Any) -> int:
    try:
        number = int(value)
    except Exception:
        number = 0
    if number < 0:
        return 0
    if number > 100:
        return 100
    return number


def _merge_if_empty(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    """
    仅在目标字段为空时回填 OCR 提取值，避免覆盖人工修正值。
    其中 '-' 视为空占位符，允许被正常值覆盖。
    """
    for key, value in (src or {}).items():
        if value is None:
            continue
        if key not in dst or _safe_str(dst.get(key)) in ("", "-"):
            dst[key] = value
    return dst


def _candidate_slot_for_result(base_slot_key: str, api_type: str, side: str, extracted: Optional[Dict[str, Any]] = None) -> str:
    if api_type == "idcard":
        return "idcard_back" if side == "back" else "idcard_front"
    if api_type == "vehicle_license":
        return "driving_license_sub" if side == "back" else "driving_license_main"
    if api_type == "vehicle_certificate":
        return "vehicle_cert"
    if api_type == "accurate_basic":
        data = extracted or {}
        if any(_safe_str(data.get(k)) for k in ("plate_no", "owner_name", "use_nature", "first_register_date", "issuer_org")):
            return "driving_license_main"
        if any(_safe_str(data.get(k)) for k in ("id_number", "id_name", "id_address", "id_gender", "id_ethnicity")):
            return "idcard_front"
        if any(_safe_str(data.get(k)) for k in ("id_issuer", "id_validity", "id_valid_from", "id_valid_to")):
            return "idcard_back"
        if any(_safe_str(data.get(k)) for k in ("approved_passenger_count", "manufacturer_name", "vehicle_brand_name", "vin", "engine_no", "vehicle_model")):
            return "vehicle_cert"
    return base_slot_key


def _merge_slot_result(slot_data: Dict[str, Dict[str, Any]], slot_key: str, extracted: Dict[str, Any]) -> None:
    sk = _safe_str(slot_key)
    if not sk or not extracted:
        return
    cur = dict(slot_data.get(sk) or {})
    _merge_if_empty(cur, extracted)
    slot_data[sk] = cur


def _compose_extracted_from_slots(slot_data: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    for target_key, rules in COMPOSE_RULES.items():
        for rule in rules:
            src = slot_data.get(rule.from_slot) or {}
            value = src.get(rule.from_key)
            if _safe_str(value):
                out[target_key] = value
                break

    for slot_key, fields in SLOT_FIELDS.items():
        src = slot_data.get(slot_key) or {}
        for field_name in fields:
            if _safe_str(src.get(field_name)) and not _safe_str(out.get(field_name)):
                out[field_name] = src.get(field_name)

    # 身份证姓名不等同于车主，但前端会展示，保留为独立字段。
    for slot_key in ("idcard_front", "idcard_back"):
        src = slot_data.get(slot_key) or {}
        for field_name, value in src.items():
            if _safe_str(value) and not _safe_str(out.get(field_name)):
                out[field_name] = value

    return out


def _base_slot_key(slot_key: str) -> str:
    return _safe_str(slot_key).split("#", 1)[0]


def _filter_blocking_ocr_errors(
    errors: Dict[str, str],
    *,
    slot_extracted: Dict[str, Dict[str, Any]],
    extracted_clean: Dict[str, Any],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Split hard OCR failures from warnings after fallback composition.

    A misplaced image can fail in its original slot while another related image
    successfully recovers the same canonical material. In that case the order
    should be considered recognized, while the per-image failure stays in
    ocr_raw_json for diagnosis.
    """
    if not errors:
        return {}, {}

    if not extracted_clean:
        return dict(errors), {}

    recovered_slots = {
        _base_slot_key(slot_key)
        for slot_key, data in (slot_extracted or {}).items()
        if data
    }
    blocking: Dict[str, str] = {}
    non_blocking: Dict[str, str] = {}

    for slot_key, message in errors.items():
        base_slot = _base_slot_key(slot_key)

        if base_slot == "related":
            non_blocking[slot_key] = message
            continue

        if base_slot in recovered_slots:
            non_blocking[slot_key] = message
            continue

        blocking[slot_key] = message

    return blocking, non_blocking


def _order_image_signature_from_images(images: Any) -> tuple[tuple[int, str, str], ...]:
    rows: list[tuple[int, str, str]] = []
    for image in images or []:
        image_id = int(getattr(image, "id", 0) or 0)
        slot_key = _safe_str(getattr(image, "slot_key", None))
        storage_key = _safe_str(getattr(image, "storage_key", None))
        if image_id and slot_key and storage_key:
            rows.append((image_id, slot_key, storage_key))
    return tuple(sorted(rows))


async def _load_order_image_signature(db: AsyncSession, order_id: int) -> tuple[tuple[int, str, str], ...]:
    stmt = (
        select(OrderImage.id, OrderImage.slot_key, OrderImage.storage_key)
        .where(OrderImage.order_id == int(order_id))
        .order_by(OrderImage.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    return tuple(
        (int(row[0]), _safe_str(row[1]), _safe_str(row[2]))
        for row in rows
        if int(row[0] or 0) and _safe_str(row[1]) and _safe_str(row[2])
    )


def _extract_idcard(resp: Dict[str, Any]) -> Dict[str, Any]:
    """从身份证 OCR 结果提取标准字段（不写任何 dl_* / 历史别名）。"""
    words_result = resp.get("words_result") or {}

    def getter(name: str) -> str:
        node = words_result.get(name) or {}
        if isinstance(node, dict):
            return _safe_str(node.get("words"))
        return _safe_str(node)

    id_name = getter("姓名")
    id_number = getter("公民身份号码")
    id_address = getter("住址")
    id_birth_date = _normalize_ymd(getter("出生")) or getter("出生")
    id_gender = getter("性别")
    id_ethnicity = getter("民族")

    id_issuer = getter("签发机关")
    id_validity = getter("有效期限") or getter("有效期") or getter("失效日期")
    id_valid_from = _normalize_ymd(getter("签发日期"))
    id_valid_to = _normalize_ymd(getter("失效日期"))

    out: Dict[str, Any] = {
        "id_name": id_name,
        "id_number": id_number,
        "id_address": id_address,
        "id_birth_date": id_birth_date,
        "id_gender": id_gender,
        "id_ethnicity": id_ethnicity,
        "id_issuer": id_issuer,
        "id_valid_from": id_valid_from,
        "id_valid_to": id_valid_to,
        "id_validity": id_validity,
    }
    return {key: value for key, value in out.items() if _safe_str(value)}


def _extract_vehicle_license(resp: Dict[str, Any]) -> Dict[str, Any]:
    """从行驶证 OCR 结果提取标准字段（不写任何 dl_* / 历史别名）。"""
    words_result = resp.get("words_result") or {}

    def getter(name: str) -> str:
        node = words_result.get(name) or {}
        return _safe_str(node.get("words") if isinstance(node, dict) else node)

    plate_no = getter("号牌号码")
    owner_name = getter("所有人")
    vin = getter("车辆识别代号")
    engine_no = getter("发动机号码")
    vehicle_model = getter("品牌型号")
    vehicle_type = getter("车辆类型")
    use_nature = getter("使用性质")
    first_register_date = _normalize_ymd(getter("注册日期"))
    issue_date = _normalize_ymd(getter("发证日期"))
    issuer_org = getter("发证机关") or getter("发证单位")

    out: Dict[str, Any] = {
        "plate_no": plate_no,
        "owner_name": owner_name,
        "vin": vin,
        "engine_no": engine_no,
        "vehicle_model": vehicle_model,
        "vehicle_type": vehicle_type,
        "use_nature": use_nature,
        "first_register_date": first_register_date,
        "issue_date": issue_date,
        "issuer_org": issuer_org,
    }
    return {key: value for key, value in out.items() if _safe_str(value)}


def _extract_vehicle_certificate(resp: Dict[str, Any]) -> Dict[str, Any]:
    """从车辆合格证 OCR 结果提取标准字段（不写任何 dl_* 镜像）。"""
    words_result = resp.get("words_result") or {}

    def getter(name: str) -> str:
        return _safe_str(words_result.get(name))

    vehicle_model = getter("CarModel") or getter("VehicleModel")
    vin = getter("VinNo") or getter("VIN")
    engine_no = getter("EngineNo")
    vehicle_type = (
        getter("VehicleType")
        or getter("CarType")
        or getter("车辆类型")
        or getter("VehicleClass")
    )
    fuel_type = (
        getter("FuelType")
        or getter("Fuel")
        or getter("燃料种类")
        or getter("能源种类")
        or getter("EnergyType")
    )
    energy_type = ""
    fuel_type_compact = re.sub(r"\s+", "", fuel_type)
    if fuel_type_compact == "电" or re.search(
        r"新能源|纯电|插电|混动|电动|油电|BEV|PHEV|EV|增程",
        fuel_type_compact,
        flags=re.IGNORECASE,
    ):
        energy_type = "new_energy"
    elif fuel_type:
        energy_type = "fuel"

    out: Dict[str, Any] = {
        "vehicle_model": vehicle_model,
        "car_name": getter("CarName") or getter("VehicleName"),
        "vehicle_type": vehicle_type,
        "vin": vin,
        "engine_no": engine_no,
        "approved_passenger_count": getter("SeatingCapacity") or getter("LimitPassenger"),
        "vehicle_brand_name": getter("CarBrand") or getter("BrandModel"),
        "manufacturer_name": getter("Manufacturer"),
        "fuel_type": fuel_type,
        "vehicle_energy_type": energy_type,
    }
    return {key: value for key, value in out.items() if _safe_str(value)}


def _ocr_word_lines(resp: Dict[str, Any]) -> list[str]:
    words_result = resp.get("words_result") if isinstance(resp, dict) else None
    lines: list[str] = []

    if isinstance(words_result, list):
        for item in words_result:
            if isinstance(item, dict):
                text = _safe_str(item.get("words"))
            else:
                text = _safe_str(item)
            if text:
                lines.append(text)
        return lines

    if isinstance(words_result, dict):
        for key, item in words_result.items():
            if isinstance(item, dict):
                text = _safe_str(item.get("words"))
            else:
                text = _safe_str(item)
            if text:
                label = _safe_str(key)
                lines.append(f"{label}{text}" if label and label not in text else text)
        return lines

    return lines


def _compact_ocr_text(lines: list[str]) -> str:
    text = "\n".join(_safe_str(x) for x in lines if _safe_str(x))
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return text.strip()


def _compact_for_regex(text: str) -> str:
    return re.sub(r"[\s:：;；,，。·•\-_/\\|]+", "", _safe_str(text).upper())


def _extract_line_value(lines: list[str], labels: tuple[str, ...], *, max_len: int = 80) -> str:
    if not lines:
        return ""

    all_labels = labels + (
        "号牌号码",
        "车辆类型",
        "所有人",
        "住址",
        "使用性质",
        "品牌型号",
        "车辆识别代号",
        "车辆识别代码",
        "发动机号码",
        "发动机号",
        "注册日期",
        "发证日期",
        "姓名",
        "性别",
        "民族",
        "出生",
        "公民身份号码",
        "签发机关",
        "有效期限",
        "车辆型号",
        "制造厂名称",
        "额定载客",
    )

    for idx, raw in enumerate(lines):
        line = _safe_str(raw)
        compact_line = _compact_for_regex(line)
        for label in labels:
            label_compact = _compact_for_regex(label)
            if not label_compact or label_compact not in compact_line:
                continue
            after = compact_line.split(label_compact, 1)[1].strip()
            if after:
                cuts = [
                    after.find(_compact_for_regex(other))
                    for other in all_labels
                    if other not in labels and _compact_for_regex(other) and after.find(_compact_for_regex(other)) > 0
                ]
                if cuts:
                    after = after[: min(cuts)]
                return after[:max_len]
            for nxt in lines[idx + 1: idx + 3]:
                candidate = _compact_for_regex(nxt)
                if not candidate:
                    continue
                if any(_compact_for_regex(x) in candidate for x in all_labels):
                    continue
                return candidate[:max_len]
    return ""


def _extract_accurate_basic(resp: Dict[str, Any]) -> Dict[str, Any]:
    """通用文字 OCR 的保守兜底提取：只抽确定性较高的订单字段。"""
    lines = _ocr_word_lines(resp)
    text = _compact_ocr_text(lines)
    compact = _compact_for_regex(text)
    out: Dict[str, Any] = {}

    id_match = re.search(r"\d{17}[\dX]", compact)
    if id_match:
        out["id_number"] = id_match.group(0)
    else:
        credit_match = re.search(r"(?=[0-9A-Z]*[A-Z])[0-9A-Z]{18}", compact)
        if credit_match:
            out["id_number"] = credit_match.group(0)

    plate_match = re.search(r"[\u4e00-\u9fff][A-Z][A-Z0-9]{4,6}", compact)
    if plate_match:
        out["plate_no"] = plate_match.group(0)

    vin_labeled = re.search(
        r"(?:车辆识别代号|车辆识别代码|车辆识别码|车架号|VIN)([A-HJ-NPR-Z0-9]{11,22})",
        compact,
        flags=re.IGNORECASE,
    )
    if vin_labeled:
        out["vin"] = vin_labeled.group(1)
    else:
        for candidate in re.findall(r"[A-HJ-NPR-Z0-9]{17}", compact):
            if any(ch.isalpha() for ch in candidate):
                out["vin"] = candidate
                break

    engine = _extract_line_value(lines, ("发动机号码", "发动机号", "发动机编号"), max_len=40)
    if engine:
        out["engine_no"] = engine

    vehicle_model = _extract_line_value(lines, ("品牌型号", "车辆型号", "型号"), max_len=80)
    if vehicle_model:
        out["vehicle_model"] = vehicle_model

    owner_name = _extract_line_value(lines, ("所有人", "车主"), max_len=80)
    if owner_name:
        out["owner_name"] = owner_name

    id_name = _extract_line_value(lines, ("姓名",), max_len=30)
    if id_name:
        out["id_name"] = id_name

    manufacturer = _extract_line_value(lines, ("制造厂名称", "生产企业", "制造企业"), max_len=100)
    if manufacturer:
        out["manufacturer_name"] = manufacturer

    passenger_count = _extract_line_value(lines, ("额定载客", "核定载人数"), max_len=20)
    if passenger_count:
        out["approved_passenger_count"] = passenger_count

    return {key: value for key, value in out.items() if _safe_str(value)}


def _extract_by_type(api_type: str, resp: Dict[str, Any]) -> Dict[str, Any]:
    if api_type == "idcard":
        return _extract_idcard(resp)
    if api_type == "vehicle_license":
        return _extract_vehicle_license(resp)
    if api_type == "vehicle_certificate":
        return _extract_vehicle_certificate(resp)
    if api_type == "accurate_basic":
        return _extract_accurate_basic(resp)
    return {}


def _is_baidu_error(resp: Dict[str, Any]) -> bool:
    return isinstance(resp, dict) and resp.get("error_code") not in (None, "", 0, "0")


def _extracted_score(api_type: str, extracted: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    cleaned = clean_dynamic_data_for_ocr(extracted or {})
    if not cleaned:
        return 0, {}

    score = len(cleaned)
    high_value_count = sum(1 for key in HIGH_VALUE_FIELDS if _safe_str(cleaned.get(key)))
    score += high_value_count * 4

    if api_type == "vehicle_license" and any(_safe_str(cleaned.get(k)) for k in ("plate_no", "vin", "engine_no")):
        score += 5
    elif api_type == "vehicle_certificate" and any(_safe_str(cleaned.get(k)) for k in ("vin", "engine_no", "vehicle_model")):
        score += 5
    elif api_type == "idcard" and any(_safe_str(cleaned.get(k)) for k in ("id_number", "id_name")):
        score += 5
    elif api_type == "accurate_basic":
        score = max(1, score - 1)

    return score, cleaned


def _is_low_quality_idcard_result(resp: Dict[str, Any], extracted: Dict[str, Any]) -> bool:
    image_status = _safe_str((resp or {}).get("image_status")).lower()
    if image_status and image_status not in {"normal", "reversed_side"}:
        return True
    return not any(_safe_str((extracted or {}).get(k)) for k in ("id_number", "id_name", "id_issuer", "id_validity"))


async def _set_task(
    db: AsyncSession,
    task: OcrTask,
    *,
    status: str,
    progress: int,
    error_message: Optional[str] = None,
) -> None:
    task.status = str(status or "").strip() or "failed"
    task.progress = _clamp_progress(progress)
    task.error_message = error_message

    if task.status in ("finished", "failed", "skipped", "finished_with_errors"):
        task.active_scope_id = None
        if hasattr(task, "finished_at"):
            task.finished_at = _now()
    else:
        if hasattr(task, "finished_at"):
            task.finished_at = None

    await db.commit()


async def _cache_get(
    db: AsyncSession,
    *,
    storage_key: str,
    api_type: str,
    side: str,
    provider: str = "baidu",
) -> Optional[Dict[str, Any]]:
    stmt = select(OcrImageCache).where(
        and_(
            OcrImageCache.storage_key == storage_key,
            OcrImageCache.api_type == api_type,
            OcrImageCache.side == side,
            OcrImageCache.provider == provider,
        )
    )
    obj = (await db.execute(stmt)).scalar_one_or_none()
    return obj.result if obj else None


async def _cache_put(
    db: AsyncSession,
    *,
    storage_key: str,
    api_type: str,
    side: str,
    provider: str,
    result: Dict[str, Any],
) -> None:
    stmt = select(OcrImageCache).where(
        and_(
            OcrImageCache.storage_key == storage_key,
            OcrImageCache.api_type == api_type,
            OcrImageCache.side == side,
            OcrImageCache.provider == provider,
        )
    )
    obj = (await db.execute(stmt)).scalar_one_or_none()
    if obj:
        obj.result = result
        await db.flush()
        return

    obj = OcrImageCache(
        storage_key=storage_key,
        sha256=None,
        api_type=api_type,
        side=side,
        provider=provider,
        result=result,
    )
    db.add(obj)

    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        obj2 = (await db.execute(stmt)).scalar_one_or_none()
        if obj2:
            obj2.result = result
            await db.flush()
            return
        raise


async def _image_result_upsert(
    db: AsyncSession,
    *,
    image_file_id: int,
    provider: str,
    api_type: str,
    side: str,
    raw_result: Dict[str, Any],
) -> None:
    stmt = select(ImageOcrResult).where(
        and_(
            ImageOcrResult.image_file_id == image_file_id,
            ImageOcrResult.provider == provider,
            ImageOcrResult.api_type == api_type,
            ImageOcrResult.side == side,
        )
    )
    obj = (await db.execute(stmt)).scalar_one_or_none()
    if obj:
        obj.raw_result = raw_result
        obj.usage_count = int(obj.usage_count or 0) + 1
        obj.last_used_at = _now()
        await db.flush()
        return

    obj = ImageOcrResult(
        image_file_id=image_file_id,
        provider=provider,
        api_type=api_type,
        side=side,
        raw_result=raw_result,
        usage_count=1,
        last_used_at=_now(),
    )
    db.add(obj)

    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        obj2 = (await db.execute(stmt)).scalar_one_or_none()
        if obj2:
            obj2.raw_result = raw_result
            obj2.usage_count = int(obj2.usage_count or 0) + 1
            obj2.last_used_at = _now()
            await db.flush()
            return
        raise


def _build_ocr_fetch_url(storage_key: str, image_file: Optional[ImageFile]) -> str:
    """
    给百度 OCR 用的“服务端可访问 URL”（严格模式）：
    - 优先：BOS 新签名 URL
    - 仅本地/非 BOS 图片才使用 ImageFile.url
    - 不再回退 BOS 公网 URL，避免敏感图片绕过签名链路

    ✅ 兼容：storage.object_url_for_display 是否支持 allow_fallback_public 参数（不支持也不炸）
    """
    normalized_storage_key = (storage_key or "").strip()

    if normalized_storage_key and getattr(storage, "enabled", False):
        try:
            try:
                return storage.object_url_for_display(
                    normalized_storage_key,
                    expires_in=3600,
                    allow_fallback_public=False,
                )
            except TypeError:
                return storage.object_url_for_display(normalized_storage_key, expires_in=3600)
        except Exception:
            pass

    if normalized_storage_key and getattr(storage, "enabled", False):
        return ""

    if image_file and (getattr(image_file, "url", "") or "").strip():
        return (getattr(image_file, "url", "") or "").strip()

    return ""


async def _call_ocr_candidate(
    db: AsyncSession,
    *,
    storage_key: str,
    image_file: Optional[ImageFile],
    image_file_id: Optional[int],
    api_type: str,
    side: str,
    provider: str = "baidu",
) -> tuple[Dict[str, Any], bool]:
    cached = await _cache_get(
        db,
        storage_key=storage_key,
        api_type=api_type,
        side=side,
        provider=provider,
    )
    if cached is not None:
        if image_file_id:
            await _image_result_upsert(
                db,
                image_file_id=int(image_file_id),
                provider=provider,
                api_type=api_type,
                side=side,
                raw_result=cached,
            )
        return cached, True

    public_url = _build_ocr_fetch_url(storage_key, image_file)
    if not public_url:
        raise RuntimeError("无法生成 OCR 可访问 URL（请检查 BOS 配置/桶权限/ImageFile.url）")

    resp = await asyncio.to_thread(
        call_ocr,
        api_type=api_type,
        image_url=public_url,
        side=(side or None),
        detect_direction=True,
    )

    await _cache_put(
        db,
        storage_key=storage_key,
        api_type=api_type,
        side=side,
        provider=provider,
        result=resp,
    )

    if image_file_id:
        await _image_result_upsert(
            db,
            image_file_id=int(image_file_id),
            provider=provider,
            api_type=api_type,
            side=side,
            raw_result=resp,
        )

    return resp, False


async def run_ocr_task(task_id: int) -> None:
    try:
        async for db in get_db():
            await _run_ocr_task_in_db(db, task_id)
            return
    except Exception:
        logger.exception("[ocr_worker] fatal error task_id=%s", task_id)


async def _claim_task(db: AsyncSession, task_id: int) -> bool:
    values: Dict[str, Any] = {
        "status": "processing",
        "progress": 1,
        "error_message": None,
    }
    if hasattr(OcrTask, "started_at"):
        values["started_at"] = _now()

    result = await db.execute(
        update(OcrTask)
        .where(
            and_(
                OcrTask.id == task_id,
                OcrTask.status == "pending",
                OcrTask.active_scope_id.isnot(None),
            )
        )
        .values(**values)
    )
    await db.commit()
    return bool(getattr(result, "rowcount", 0) or 0)


async def _run_ocr_task_in_db(db: AsyncSession, task_id: int) -> None:
    task = (await db.execute(select(OcrTask).where(OcrTask.id == task_id))).scalar_one_or_none()
    if not task:
        logger.warning("[ocr_worker] task not found task_id=%s", task_id)
        return

    if task.status != "pending":
        logger.info("[ocr_worker] ignore task_id=%s status=%s", task_id, task.status)
        return

    claimed = await _claim_task(db, task_id)
    if not claimed:
        logger.info("[ocr_worker] task already claimed task_id=%s", task_id)
        return

    task = (await db.execute(select(OcrTask).where(OcrTask.id == task_id))).scalar_one()

    try:
        if task.scope_type != "order":
            await _set_task(
                db,
                task,
                status="skipped",
                progress=100,
                error_message=f"不支持的 scope_type: {task.scope_type}",
            )
            return

        order_id = int(task.scope_id)

        stmt = (
            select(Order)
            .where(Order.id == order_id)
            .options(lazyload("*"), selectinload(Order.images).selectinload(OrderImage.image_file))
        )
        order = (await db.execute(stmt)).scalars().first()
        if not order:
            await _set_task(db, task, status="failed", progress=100, error_message=f"订单不存在: {order_id}")
            return

        initial_image_signature = _order_image_signature_from_images(getattr(order, "images", None) or [])
        slot_to_image: Dict[str, OrderImage] = {}
        related_fallback_images: list[OrderImage] = []
        for image in (getattr(order, "images", None) or []):
            slot_key = getattr(image, "slot_key", None)
            if not slot_key:
                continue

            storage_key = (getattr(image, "storage_key", "") or "").strip()
            if not storage_key:
                image_file = getattr(image, "image_file", None)
                storage_key = (getattr(image_file, "storage_key", "") or "").strip()
            if not storage_key:
                continue

            if slot_key == "related":
                related_fallback_images.append(image)
                continue

            if slot_key not in SLOT_TO_OCR:
                continue

            old_image = slot_to_image.get(slot_key)
            if not old_image:
                slot_to_image[slot_key] = image
                continue

            old_id = int(getattr(old_image, "id", 0) or 0)
            new_id = int(getattr(image, "id", 0) or 0)
            if new_id >= old_id:
                slot_to_image[slot_key] = image

        missing_core_slots = CORE_OCR_SLOTS - {k for k in slot_to_image.keys() if k in CORE_OCR_SLOTS}
        if missing_core_slots and related_fallback_images:
            # 用户只放对部分卡槽、其余卡证误放到“相关图片”时，也保守选最近 5 张做 OCR 兜底。
            for idx, image in enumerate(
                sorted(
                    related_fallback_images,
                    key=lambda x: int(getattr(x, "id", 0) or 0),
                    reverse=True,
                )[:5]
            ):
                slot_to_image[f"related#{idx + 1}"] = image

        if not slot_to_image:
            await _set_task(db, task, status="skipped", progress=100, error_message="没有可识别的 OCR 图片")
            return

        ocr_raw_json: Dict[str, Any] = dict(getattr(order, "ocr_raw_json", None) or {})
        slot_extracted: Dict[str, Dict[str, Any]] = {}

        total = len(slot_to_image)
        done = 0
        errors: Dict[str, str] = {}

        logger.info("[ocr_worker] start task_id=%s order_id=%s total=%s", task_id, order_id, total)

        for slot_key in sorted(slot_to_image.keys()):
            image = slot_to_image[slot_key]
            provider = "baidu"
            base_slot_key = slot_key.split("#", 1)[0]

            storage_key = (getattr(image, "storage_key", "") or "").strip()
            if not storage_key:
                image_file0 = getattr(image, "image_file", None)
                storage_key = (getattr(image_file0, "storage_key", "") or "").strip()

            image_file: Optional[ImageFile] = getattr(image, "image_file", None)
            image_file_id: Optional[int] = getattr(image, "image_file_id", None) or getattr(image_file, "id", None)

            attempts: list[Dict[str, Any]] = []
            best_resp: Optional[Dict[str, Any]] = None
            best_api_type = ""
            best_side = ""
            best_effective_slot_key = base_slot_key
            best_extracted: Dict[str, Any] = {}
            best_score = 0

            try:
                for idx, (api_type, side0) in enumerate(
                    SLOT_OCR_CANDIDATES.get(base_slot_key) or (SLOT_TO_OCR[base_slot_key],)
                ):
                    side = (side0 or "").strip()
                    attempt: Dict[str, Any] = {
                        "api_type": api_type,
                        "side": side,
                        "primary": idx == 0,
                    }

                    try:
                        resp, cached = await _call_ocr_candidate(
                            db,
                            storage_key=storage_key,
                            image_file=image_file,
                            image_file_id=image_file_id,
                            api_type=api_type,
                            side=side,
                            provider=provider,
                        )
                        attempt["cached"] = cached

                        if _is_baidu_error(resp):
                            attempt["error"] = _safe_str(resp.get("error_msg")) or "baidu_error"
                            attempts.append(attempt)
                            continue

                        extracted = _extract_by_type(api_type, resp)
                        score, extracted_clean = _extracted_score(api_type, extracted)
                        attempt["score"] = score
                        attempt["fields"] = sorted(extracted_clean.keys())

                        if score > best_score:
                            best_resp = resp
                            best_api_type = api_type
                            best_side = side
                            best_effective_slot_key = _candidate_slot_for_result(
                                base_slot_key,
                                api_type,
                                side,
                                extracted_clean,
                            )
                            best_extracted = extracted_clean
                            best_score = score

                        attempts.append(attempt)

                        if api_type == "idcard" and _is_low_quality_idcard_result(resp, extracted_clean):
                            continue
                        if idx == 0 and score >= 12:
                            break
                        if score >= 18:
                            break

                    except OcrNotConfigured:
                        raise
                    except Exception as exc:
                        message = str(exc) or exc.__class__.__name__
                        if len(message) > 220:
                            message = message[:220] + "..."
                        attempt["error"] = message
                        attempts.append(attempt)
                        continue

                if best_resp is not None and best_score > 0:
                    ocr_raw_json[slot_key] = best_resp
                    ocr_raw_json[f"{slot_key}__fallback"] = {
                        "selected_api_type": best_api_type,
                        "selected_side": best_side,
                        "effective_slot_key": best_effective_slot_key,
                        "score": best_score,
                        "attempts": attempts,
                    }
                    _merge_slot_result(slot_extracted, best_effective_slot_key, best_extracted)
                else:
                    error_message = "; ".join(
                        f"{x.get('api_type')}:{x.get('error') or 'empty_result'}"
                        for x in attempts
                    ) or "OCR 未提取到有效字段"
                    errors[slot_key] = error_message[:300]
                    ocr_raw_json[slot_key] = {
                        "error_code": "ocr_no_fields",
                        "error_msg": errors[slot_key],
                    }
                    ocr_raw_json[f"{slot_key}__fallback"] = {
                        "selected_api_type": "",
                        "score": 0,
                        "attempts": attempts,
                    }

            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                if len(message) > 300:
                    message = message[:300] + "..."
                errors[slot_key] = message
                ocr_raw_json[slot_key] = {"error_code": "worker_error", "error_msg": message}
                ocr_raw_json[f"{slot_key}__fallback"] = {
                    "selected_api_type": "",
                    "score": 0,
                    "attempts": attempts,
                }

            done += 1
            await _set_task(
                db,
                task,
                status="processing",
                progress=int(done * 90 / total),
                error_message=None,
            )

        current_image_signature = await _load_order_image_signature(db, order_id)
        if current_image_signature != initial_image_signature:
            ocr_raw_json = dict(ocr_raw_json)
            ocr_raw_json["_discarded_due_to_image_change"] = {
                "task_id": int(task_id),
                "reason": "order images changed while OCR was running",
            }
            order.ocr_raw_json = ocr_raw_json
            flag_modified(order, "ocr_raw_json")
            await db.commit()
            await _set_task(
                db,
                task,
                status="pending",
                progress=0,
                error_message="图片在识别过程中已更新，已自动重新排队识别",
            )
            logger.warning("[ocr_worker] image set changed; requeued task_id=%s order_id=%s", task_id, order_id)
            return

        dyn = clean_dynamic_data_for_ocr(dict(getattr(order, "dynamic_data", None) or {}))
        extracted_clean = clean_dynamic_data_for_ocr(_compose_extracted_from_slots(slot_extracted))
        dyn = _merge_if_empty(dyn, extracted_clean)
        dyn = clean_dynamic_data_for_ocr(dyn)

        order.dynamic_data = dyn
        order.ocr_raw_json = ocr_raw_json
        await sync_order_fact_from_dynamic_data(db, order_id=order_id, dynamic_data=dyn)
        await db.commit()

        blocking_errors, non_blocking_errors = _filter_blocking_ocr_errors(
            errors,
            slot_extracted=slot_extracted,
            extracted_clean=extracted_clean,
        )
        if non_blocking_errors:
            ocr_raw_json = dict(ocr_raw_json)
            ocr_raw_json["_non_blocking_errors"] = non_blocking_errors
            order.ocr_raw_json = ocr_raw_json
            flag_modified(order, "ocr_raw_json")
            await db.commit()

        if blocking_errors:
            await _set_task(
                db,
                task,
                status="finished_with_errors",
                progress=100,
                error_message=json.dumps(blocking_errors, ensure_ascii=False),
            )
        else:
            await _set_task(db, task, status="finished", progress=100, error_message=None)

        logger.info("[ocr_worker] done task_id=%s status=%s", task_id, task.status)

    except OcrNotConfigured as exc:
        await _set_task(db, task, status="skipped", progress=100, error_message=str(exc))

    except Exception as exc:
        trace_text = traceback.format_exc(limit=8)
        logger.error("[ocr_worker] failed task_id=%s err=%s\n%s", task_id, exc, trace_text)
        message = str(exc) or exc.__class__.__name__
        if len(message) > 500:
            message = message[:500] + "..."
        await _set_task(db, task, status="failed", progress=100, error_message=message)
