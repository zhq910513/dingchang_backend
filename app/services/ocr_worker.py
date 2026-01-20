# app/services/ocr_worker.py
# encoding: utf-8
from __future__ import annotations

import asyncio
import json
import logging
import traceback
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.models.ocr_task import OcrTask
from app.models.order import Order, OrderImage
from app.models.image_file import ImageFile
from app.models.image_ocr_result import ImageOcrResult
from app.models.ocr_image_cache import OcrImageCache
from app.services.baidu_ocr import call_ocr, OcrNotConfigured
from app.services.storage import StorageService

logger = logging.getLogger(__name__)

storage = StorageService()

# ✅ 只做这三类卡证 OCR：行驶证 / 车辆合格证 / 身份证
SLOT_TO_OCR: Dict[str, Tuple[str, Optional[str]]] = {
    "idcard_front": ("idcard", "front"),
    "idcard_back": ("idcard", "back"),
    "driving_license_main": ("vehicle_license", "front"),
    "driving_license_sub": ("vehicle_license", "back"),
    "vehicle_cert": ("vehicle_certificate", None),
}


def _now() -> datetime:
    # 统一北京时间写入（DB timezone=True）
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _merge_if_empty(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in (src or {}).items():
        if v is None:
            continue
        if k not in dst or dst.get(k) in ("", None):
            dst[k] = v
    return dst


def _extract_idcard(resp: Dict[str, Any]) -> Dict[str, Any]:
    wr = resp.get("words_result") or {}

    def g(name: str) -> str:
        x = wr.get(name) or {}
        return _safe_str(x.get("words"))

    out: Dict[str, Any] = {}
    out["id_name"] = g("姓名")
    out["id_number"] = g("公民身份号码")
    out["id_address"] = g("住址")
    out["id_birth_date"] = g("出生")
    out["id_gender"] = g("性别")
    out["id_ethnicity"] = g("民族")
    return {k: v for k, v in out.items() if v}


def _extract_vehicle_license(resp: Dict[str, Any]) -> Dict[str, Any]:
    wr = resp.get("words_result") or {}

    def g(name: str) -> str:
        x = wr.get(name) or {}
        return _safe_str(x.get("words") if isinstance(x, dict) else x)

    out: Dict[str, Any] = {}
    out["dl_plate_no"] = g("号牌号码")
    out["dl_owner"] = g("所有人")
    out["dl_vin"] = g("车辆识别代号")
    out["dl_engine_no"] = g("发动机号码")
    out["dl_brand_model"] = g("品牌型号")
    out["dl_vehicle_type"] = g("车辆类型")
    out["dl_use_nature"] = g("使用性质")
    out["dl_register_date"] = g("注册日期")
    out["dl_issue_date"] = g("发证日期")
    out["dl_issuer_org"] = g("发证机关") or g("发证单位")
    return {k: v for k, v in out.items() if v}


def _extract_vehicle_certificate(resp: Dict[str, Any]) -> Dict[str, Any]:
    wr = resp.get("words_result") or {}

    def g(name: str) -> str:
        return _safe_str(wr.get(name))

    out: Dict[str, Any] = {}
    out["vehicle_model"] = g("CarModel") or g("VehicleModel")
    out["vin"] = g("VinNo") or g("VIN")
    out["engine_no"] = g("EngineNo")
    out["approved_passenger_count"] = g("SeatingCapacity") or g("LimitPassenger")
    out["vehicle_brand_name"] = g("CarBrand") or g("BrandModel")
    out["manufacturer_name"] = g("Manufacturer")
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
    # 百度常见：error_code 为数字或字符串，成功一般没有 error_code
    return isinstance(resp, dict) and resp.get("error_code") not in (None, "", 0, "0")


async def _set_task(
    db: AsyncSession,
    task: OcrTask,
    *,
    status: str,
    progress: int,
    error_message: Optional[str] = None,
) -> None:
    task.status = status
    task.progress = int(progress)
    task.error_message = error_message

    if status in ("finished", "failed", "skipped", "finished_with_errors"):
        task.active_scope_id = None
        task.finished_at = _now()

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
    await db.flush()


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
    await db.flush()


def _build_ocr_fetch_url(storage_key: str, image_file: Optional[ImageFile]) -> str:
    """
    给百度 OCR 用的“服务端可访问 URL”：
    - 优先：ImageFile.url（如果你存的是可公网访问或带签名的 URL）
    - 其次：BOS 签名 URL（最稳，适配私有 bucket）
    - 最后兜底：BOS 公网 URL（仅当 bucket 是 public 才能用）
    ⚠️ 绝不剥掉 query（签名通常在 query 里）
    """
    if image_file and (getattr(image_file, "url", "") or "").strip():
        return (getattr(image_file, "url", "") or "").strip()

    if getattr(storage, "enabled", False):
        try:
            # 给百度抓取留足时间，避免过期
            return storage.object_url_for_display(storage_key, expires_in=3600)
        except Exception:
            try:
                return storage.object_public_url(storage_key)
            except Exception:
                return ""

    return ""


async def run_ocr_task(task_id: int) -> None:
    """
    供 BackgroundTasks / poller 调用的入口。
    """
    try:
        async for db in get_db():
            await _run_ocr_task_in_db(db, task_id)
            return
    except Exception:
        logger.exception("[ocr_worker] fatal error task_id=%s", task_id)


async def _claim_task(db: AsyncSession, task_id: int) -> bool:
    """
    ✅ DB 级抢占：只有 pending 且 active_scope_id 非空的任务能被抢到。
    防止同一任务被重复执行。
    """
    values: Dict[str, Any] = {"status": "processing", "progress": 1, "error_message": None}
    # 不臆造字段：模型有 started_at 才写
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
            await _set_task(db, task, status="skipped", progress=100, error_message=f"不支持的 scope_type: {task.scope_type}")
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

        # ✅ 同一 slot 多张图：取最新（OrderImage.id 最大）
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

        for slot, img in slot_to_img.items():
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
                cached = await _cache_get(db, storage_key=storage_key, api_type=api_type, side=side, provider=provider)
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
            await _set_task(db, task, status="processing", progress=int(done * 90 / total), error_message=None)

        dyn = dict(getattr(order, "dynamic_data", None) or {})
        dyn = _merge_if_empty(dyn, extracted_all)
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
        msg = str(e)
        if len(msg) > 500:
            msg = msg[:500] + "..."
        await _set_task(db, task, status="failed", progress=100, error_message=msg)
