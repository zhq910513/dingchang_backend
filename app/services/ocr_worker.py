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
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.models.image_file import ImageFile
from app.models.image_ocr_result import ImageOcrResult
from app.models.ocr_image_cache import OcrImageCache
from app.models.ocr_task import OcrTask
from app.models.order import Order, OrderImage
from app.services.baidu_ocr import call_ocr, OcrNotConfigured
from app.services.ocr_cleaner import clean_dynamic_data_for_ocr
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


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _normalize_ymd(v: Any) -> str:
    """
    统一日期格式为 YYYY-MM-DD：
    - 支持 YYYYMMDD -> YYYY-MM-DD
    - 支持 YYYY-MM-DD
    - '-', 空串, 非法值 -> ''
    """
    s = _safe_str(v)
    if not s or s == "-":
        return ""

    if re.fullmatch(r"\d{8}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:8]}"

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return s
        except ValueError:
            return ""

    return ""


def _clamp_progress(v: Any) -> int:
    try:
        n = int(v)
    except Exception:
        n = 0
    if n < 0:
        return 0
    if n > 100:
        return 100
    return n


def _merge_if_empty(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    """
    仅在目标字段为空时回填 OCR 提取值，避免覆盖人工修正值。
    其中 '-' 视为空占位符，允许被正常值覆盖。
    """
    for k, v in (src or {}).items():
        if v is None:
            continue
        if k not in dst or _safe_str(dst.get(k)) in ("", "-"):
            dst[k] = v
    return dst


def _extract_idcard(resp: Dict[str, Any]) -> Dict[str, Any]:
    wr = resp.get("words_result") or {}

    def g(name: str) -> str:
        x = wr.get(name) or {}
        if isinstance(x, dict):
            return _safe_str(x.get("words"))
        return _safe_str(x)

    id_name = g("姓名")
    id_number = g("公民身份号码")
    id_address = g("住址")
    id_birth_date = _normalize_ymd(g("出生")) or g("出生")
    id_gender = g("性别")
    id_ethnicity = g("民族")

    id_issuer = g("签发机关")
    id_validity = g("失效日期") or g("有效期限") or g("有效期")
    id_valid_from = _normalize_ymd(g("签发日期")) or g("签发日期")
    id_valid_to = _normalize_ymd(g("失效日期")) or g("失效日期")

    out: Dict[str, Any] = {
        # ===== 标准字段 =====
        "id_name": id_name,
        "id_number": id_number,
        "id_address": id_address,
        "id_birth_date": id_birth_date,
        "id_gender": id_gender,
        "id_ethnicity": id_ethnicity,
        "id_issuer": id_issuer,
        "id_validity": id_validity,

        # ===== 财务口径（严格：接口只读 dl_*，这里同步镜像写入，避免新增数据再脏）=====
        "dl_id_number": id_number,

        # ===== 兼容 slot_field_config 历史 key（用于详情卡槽展示）=====
        "id_nation": id_ethnicity,
        "id_birth": id_birth_date,
        "id_issue_authority": id_issuer,
        "id_valid_from": id_valid_from,
        "id_valid_to": id_valid_to,
        "id_valid_period": id_validity,
    }
    return {k: v for k, v in out.items() if v}


def _extract_vehicle_license(resp: Dict[str, Any]) -> Dict[str, Any]:
    wr = resp.get("words_result") or {}

    def g(name: str) -> str:
        x = wr.get(name) or {}
        return _safe_str(x.get("words") if isinstance(x, dict) else x)

    dl_plate_no = g("号牌号码")
    dl_owner = g("所有人")
    dl_vin = g("车辆识别代号")
    dl_engine_no = g("发动机号码")
    dl_vehicle_model = g("品牌型号")
    dl_vehicle_type = g("车辆类型")
    dl_use_nature = g("使用性质")
    dl_register_date = _normalize_ymd(g("注册日期"))
    dl_issue_date = _normalize_ymd(g("发证日期"))
    dl_issuer_org = g("发证机关") or g("发证单位")

    out: Dict[str, Any] = {
        # ===== 原始行驶证 OCR 字段（保留）=====
        "dl_plate_no": dl_plate_no,
        "dl_owner": dl_owner,
        "dl_vin": dl_vin,
        "dl_engine_no": dl_engine_no,
        "dl_vehicle_model": dl_vehicle_model,
        "dl_brand_model": dl_vehicle_model,
        "dl_vehicle_type": dl_vehicle_type,
        "dl_use_nature": dl_use_nature,
        "dl_use性质": dl_use_nature,
        "dl_register_date": dl_register_date,
        "dl_issue_date": dl_issue_date,
        "dl_issuer_org": dl_issuer_org,

        # ===== 标准化字段（给订单/财务/前端统一消费）=====
        "plate_no": dl_plate_no,
        "owner_name": dl_owner,
        "vin": dl_vin,
        "engine_no": dl_engine_no,
        "vehicle_model": dl_vehicle_model,
        "first_register_date": dl_register_date,
    }
    return {k: v for k, v in out.items() if v}


def _extract_vehicle_certificate(resp: Dict[str, Any]) -> Dict[str, Any]:
    wr = resp.get("words_result") or {}

    def g(name: str) -> str:
        return _safe_str(wr.get(name))

    vehicle_model = g("CarModel") or g("VehicleModel")
    vin = g("VinNo") or g("VIN")
    engine_no = g("EngineNo")

    out: Dict[str, Any] = {
        "vehicle_model": vehicle_model,
        "vin": vin,
        "engine_no": engine_no,
        "approved_passenger_count": g("SeatingCapacity") or g("LimitPassenger"),
        "vehicle_brand_name": g("CarBrand") or g("BrandModel"),
        "manufacturer_name": g("Manufacturer"),

        # 财务口径镜像
        "dl_vehicle_model": vehicle_model,
        "dl_vin": vin,
        "dl_engine_no": engine_no,
    }
    return {k: v for k, v in out.items() if v}


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
    - 其次：ImageFile.url（若已有）
    - 最后：BOS 公网 URL（仅当确实可用）

    ✅ 兼容：storage.object_url_for_display 是否支持 allow_fallback_public 参数（不支持也不炸）
    """
    sk = (storage_key or "").strip()

    if sk and getattr(storage, "enabled", False):
        try:
            # 先尝试严格参数
            try:
                return storage.object_url_for_display(sk, expires_in=3600, allow_fallback_public=False)
            except TypeError:
                # 旧版本不支持 allow_fallback_public
                return storage.object_url_for_display(sk, expires_in=3600)
        except Exception:
            pass

    if image_file and (getattr(image_file, "url", "") or "").strip():
        return (getattr(image_file, "url", "") or "").strip()

    if sk and getattr(storage, "enabled", False):
        try:
            return storage.object_public_url(sk)
        except Exception:
            return ""

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

    res = await db.execute(
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
    return bool(getattr(res, "rowcount", 0) or 0)


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
            .options(selectinload(Order.images).selectinload(OrderImage.image_file))
        )
        order = (await db.execute(stmt)).scalars().first()
        if not order:
            await _set_task(db, task, status="failed", progress=100, error_message=f"订单不存在: {order_id}")
            return

        slot_to_img: Dict[str, OrderImage] = {}
        for img in (getattr(order, "images", None) or []):
            slot = getattr(img, "slot_key", None)
            if not slot or slot not in SLOT_TO_OCR:
                continue

            sk = (getattr(img, "storage_key", "") or "").strip()
            if not sk:
                imf = getattr(img, "image_file", None)
                sk = (getattr(imf, "storage_key", "") or "").strip()
            if not sk:
                continue

            old = slot_to_img.get(slot)
            if not old:
                slot_to_img[slot] = img
                continue

            old_id = int(getattr(old, "id", 0) or 0)
            new_id = int(getattr(img, "id", 0) or 0)
            if new_id >= old_id:
                slot_to_img[slot] = img

        if not slot_to_img:
            await _set_task(db, task, status="skipped", progress=100, error_message="没有可识别的 OCR 图片")
            return

        ocr_raw: Dict[str, Any] = dict(getattr(order, "ocr_raw_json", None) or {})
        extracted_all: Dict[str, Any] = {}

        total = len(slot_to_img)
        done = 0
        errors: Dict[str, str] = {}

        logger.info("[ocr_worker] start task_id=%s order_id=%s total=%s", task_id, order_id, total)

        for slot in sorted(slot_to_img.keys()):
            img = slot_to_img[slot]
            api_type, side0 = SLOT_TO_OCR[slot]
            side = (side0 or "").strip()
            provider = "baidu"

            storage_key = (getattr(img, "storage_key", "") or "").strip()
            if not storage_key:
                imf0 = getattr(img, "image_file", None)
                storage_key = (getattr(imf0, "storage_key", "") or "").strip()

            image_file: Optional[ImageFile] = getattr(img, "image_file", None)
            image_file_id: Optional[int] = getattr(img, "image_file_id", None) or getattr(image_file, "id", None)

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

                ocr_raw[slot] = resp

                if _is_baidu_error(resp):
                    emsg = _safe_str(resp.get("error_msg")) or "baidu_error"
                    errors[slot] = emsg
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

            except Exception as e:
                msg = str(e) or e.__class__.__name__
                if len(msg) > 300:
                    msg = msg[:300] + "..."
                errors[slot] = msg
                ocr_raw[slot] = {"error_code": "worker_error", "error_msg": msg}

            done += 1
            await _set_task(
                db,
                task,
                status="processing",
                progress=int(done * 90 / total),
                error_message=None,
            )

        # =========================
        # ✅ 写库前：合并 + 强制清洗
        # =========================
        dyn = dict(getattr(order, "dynamic_data", None) or {})
        dyn = _merge_if_empty(dyn, extracted_all)
        dyn = clean_dynamic_data_for_ocr(dyn)

        order.dynamic_data = dyn
        order.ocr_raw_json = ocr_raw
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

    except OcrNotConfigured as e:
        await _set_task(db, task, status="skipped", progress=100, error_message=str(e))

    except Exception as e:
        tb = traceback.format_exc(limit=8)
        logger.error("[ocr_worker] failed task_id=%s err=%s\n%s", task_id, e, tb)
        msg = str(e) or e.__class__.__name__
        if len(msg) > 500:
            msg = msg[:500] + "..."
        await _set_task(db, task, status="failed", progress=100, error_message=msg)
