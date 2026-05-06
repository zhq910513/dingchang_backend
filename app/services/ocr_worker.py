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

from app.core.db import get_db
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

    out: Dict[str, Any] = {
        "vehicle_model": vehicle_model,
        "vin": vin,
        "engine_no": engine_no,
        "approved_passenger_count": getter("SeatingCapacity") or getter("LimitPassenger"),
        "vehicle_brand_name": getter("CarBrand") or getter("BrandModel"),
        "manufacturer_name": getter("Manufacturer"),
    }
    return {key: value for key, value in out.items() if _safe_str(value)}


def _extract_by_type(api_type: str, resp: Dict[str, Any]) -> Dict[str, Any]:
    if api_type == "idcard":
        return _extract_idcard(resp)
    if api_type == "vehicle_license":
        return _extract_vehicle_license(resp)
    if api_type == "vehicle_certificate":
        return _extract_vehicle_certificate(resp)
    return {}


def _is_baidu_error(resp: Dict[str, Any]) -> bool:
    return isinstance(resp, dict) and resp.get("error_code") not in (None, "", 0, "0")


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

        slot_to_image: Dict[str, OrderImage] = {}
        for image in (getattr(order, "images", None) or []):
            slot_key = getattr(image, "slot_key", None)
            if not slot_key or slot_key not in SLOT_TO_OCR:
                continue

            storage_key = (getattr(image, "storage_key", "") or "").strip()
            if not storage_key:
                image_file = getattr(image, "image_file", None)
                storage_key = (getattr(image_file, "storage_key", "") or "").strip()
            if not storage_key:
                continue

            old_image = slot_to_image.get(slot_key)
            if not old_image:
                slot_to_image[slot_key] = image
                continue

            old_id = int(getattr(old_image, "id", 0) or 0)
            new_id = int(getattr(image, "id", 0) or 0)
            if new_id >= old_id:
                slot_to_image[slot_key] = image

        if not slot_to_image:
            await _set_task(db, task, status="skipped", progress=100, error_message="没有可识别的 OCR 图片")
            return

        ocr_raw_json: Dict[str, Any] = dict(getattr(order, "ocr_raw_json", None) or {})
        extracted_all: Dict[str, Any] = {}

        total = len(slot_to_image)
        done = 0
        errors: Dict[str, str] = {}

        logger.info("[ocr_worker] start task_id=%s order_id=%s total=%s", task_id, order_id, total)

        for slot_key in sorted(slot_to_image.keys()):
            image = slot_to_image[slot_key]
            api_type, side0 = SLOT_TO_OCR[slot_key]
            side = (side0 or "").strip()
            provider = "baidu"

            storage_key = (getattr(image, "storage_key", "") or "").strip()
            if not storage_key:
                image_file0 = getattr(image, "image_file", None)
                storage_key = (getattr(image_file0, "storage_key", "") or "").strip()

            image_file: Optional[ImageFile] = getattr(image, "image_file", None)
            image_file_id: Optional[int] = getattr(image, "image_file_id", None) or getattr(image_file, "id", None)

            try:
                cached = await _cache_get(
                    db,
                    storage_key=storage_key,
                    api_type=api_type,
                    side=side,
                    provider=provider,
                )
                if cached is not None:
                    resp = cached
                else:
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

                ocr_raw_json[slot_key] = resp

                if _is_baidu_error(resp):
                    error_message = _safe_str(resp.get("error_msg")) or "baidu_error"
                    errors[slot_key] = error_message
                else:
                    extracted = _extract_by_type(api_type, resp)
                    extracted_all.update(extracted)

                if image_file_id:
                    await _image_result_upsert(
                        db,
                        image_file_id=int(image_file_id),
                        provider=provider,
                        api_type=api_type,
                        side=side,
                        raw_result=resp,
                    )

            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                if len(message) > 300:
                    message = message[:300] + "..."
                errors[slot_key] = message
                ocr_raw_json[slot_key] = {"error_code": "worker_error", "error_msg": message}

            done += 1
            await _set_task(
                db,
                task,
                status="processing",
                progress=int(done * 90 / total),
                error_message=None,
            )

        dyn = clean_dynamic_data_for_ocr(dict(getattr(order, "dynamic_data", None) or {}))
        extracted_clean = clean_dynamic_data_for_ocr(extracted_all)
        dyn = _merge_if_empty(dyn, extracted_clean)
        dyn = clean_dynamic_data_for_ocr(dyn)

        order.dynamic_data = dyn
        order.ocr_raw_json = ocr_raw_json
        await sync_order_fact_from_dynamic_data(db, order_id=order_id, dynamic_data=dyn)
        await db.commit()

        if errors:
            await _set_task(
                db,
                task,
                status="finished_with_errors",
                progress=100,
                error_message=json.dumps(errors, ensure_ascii=False),
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
