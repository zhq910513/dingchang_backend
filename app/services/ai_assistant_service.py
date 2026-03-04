# app/services/ai_assistant_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.models.ocr_task import OcrTask
from app.models.order import Order, OrderImage
from app.models.order_info import OrderInfo
from app.services.ai_platforms import get_adapter
from app.services.ai_platforms.base import AiPlatformAdapter, QuoteContext, StubPlatformAdapter, QuoteResult
from app.services.storage import StorageService

TZ_BJ = timezone(timedelta(hours=8))
storage = StorageService()

# =============================
# 卡槽配置（报价助手口径）
# =============================
SLOT_CONFIG: Dict[str, Dict[str, Any]] = {
    "vehicle_cert": {"multi": False, "ocr": True, "required": True},
    "idcard_front": {"multi": False, "ocr": True, "required": True},
    "idcard_back": {"multi": False, "ocr": True, "required": False},
    "driving_license_main": {"multi": False, "ocr": True, "required": True},
    "driving_license_sub": {"multi": False, "ocr": True, "required": False},
    "related": {"multi": True, "ocr": False, "required": False},
}

OCR_SLOTS = {"idcard_front", "idcard_back", "driving_license_main", "driving_license_sub", "vehicle_cert"}

# =============================
# 结果状态（不炸）
# =============================
RESULT_SUCCESS = "success"
RESULT_EMPTY = "empty"
RESULT_INVALID = "invalid_command"
RESULT_NEED_MORE = "need_more_info"
RESULT_NOT_READY = "not_ready"
RESULT_FAILED = "failed"


# =============================
# 基础工具
# =============================
def _now_iso() -> str:
    return datetime.now(TZ_BJ).isoformat()


def _to_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        return str(v)
    except Exception:
        return default


def _new_id() -> str:
    return uuid.uuid4().hex


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _fmt_dt(dt: Any) -> Optional[str]:
    if not dt:
        return None
    if isinstance(dt, datetime):
        try:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return _to_str(dt)
    return _to_str(dt) or None


def _norm_text(s: Any) -> str:
    t = _to_str(s).replace("\u3000", " ").strip()
    t = t.replace("：", ":").replace("（", "(").replace("）", ")")
    t = re.sub(r"\s+", " ", t)
    return t


def _contains_any(text: str, keys: List[str]) -> bool:
    return any(k in text for k in keys if k)


def _mk_action(label: str, type_: str = "suggest", target: Optional[str] = None, **extra) -> Dict[str, Any]:
    item: Dict[str, Any] = {"type": type_, "label": label}
    if target:
        item["target"] = target
    if extra:
        item["extra"] = extra
    return item


def _mk_data(
        *,
        result_status: str,
        message: str,
        entities: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "result_status": result_status,
        "message": message,
        "entities": entities or {},
        "payload": payload or {},
    }


# =============================
# 轻量 JSON 会话存储（会话/历史）
# =============================
class _Store:
    """
    轻量 JSON 存储（报价助手会话/消息）
    文件：storage/quote_assistant_sessions.json
    """

    def __init__(self) -> None:
        base_dir = Path(os.getenv("STORAGE_DIR", "storage"))
        base_dir.mkdir(parents=True, exist_ok=True)
        self._file = base_dir / "quote_assistant_sessions.json"
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"sessions": {}}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self._file.exists():
                self._flush()
                return
            try:
                text = self._file.read_text(encoding="utf-8")
                obj = json.loads(text) if text.strip() else {}
                if not isinstance(obj, dict):
                    obj = {}
                if not isinstance(obj.get("sessions"), dict):
                    obj["sessions"] = {}
                self._data = obj
            except Exception:
                self._data = {"sessions": {}}
                self._flush()

    def _flush(self) -> None:
        with self._lock:
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._file)

    def create_session(self, *, owner_user_id: str, title: Optional[str] = None) -> Dict[str, Any]:
        now = _now_iso()
        sid = _new_id()
        row = {
            "session_id": sid,
            "owner_user_id": _to_str(owner_user_id),
            "title": (_to_str(title).strip() or "新会话"),
            "created_at": now,
            "updated_at": now,
            "deleted": False,
            "messages": [],
        }
        with self._lock:
            self._data["sessions"][sid] = row
            self._flush()
        return deepcopy(row)

    def get_session(self, *, owner_user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._data["sessions"].get(session_id)
            if not row or row.get("deleted"):
                return None
            if _to_str(row.get("owner_user_id")) != _to_str(owner_user_id):
                return None
            return deepcopy(row)

    def get_or_create_session(
            self,
            *,
            owner_user_id: str,
            session_id: Optional[str] = None,
            title: Optional[str] = None,
    ) -> Dict[str, Any]:
        if session_id:
            found = self.get_session(owner_user_id=owner_user_id, session_id=session_id)
            if found:
                return found
        return self.create_session(owner_user_id=owner_user_id, title=title)

    def list_sessions(self, *, owner_user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        owner = _to_str(owner_user_id)
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for s in self._data["sessions"].values():
                if s.get("deleted"):
                    continue
                if _to_str(s.get("owner_user_id")) != owner:
                    continue
                msgs = s.get("messages") or []
                preview = _to_str((msgs[-1] or {}).get("content"))[:120] if msgs else ""
                rows.append(
                    {
                        "session_id": s.get("session_id"),
                        "title": s.get("title") or "新会话",
                        "created_at": s.get("created_at"),
                        "updated_at": s.get("updated_at"),
                        "message_count": len(msgs),
                        "last_message_preview": preview,
                    }
                )
            rows.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
            return deepcopy(rows[: max(1, min(int(limit or 50), 200))])

    def delete_session(self, *, owner_user_id: str, session_id: str) -> bool:
        with self._lock:
            row = self._data["sessions"].get(session_id)
            if not row or row.get("deleted"):
                return False
            if _to_str(row.get("owner_user_id")) != _to_str(owner_user_id):
                return False
            row["deleted"] = True
            row["updated_at"] = _now_iso()
            self._flush()
        return True

    def list_messages(
            self,
            *,
            owner_user_id: str,
            session_id: str,
            cursor: Optional[str] = None,
            limit: int = 50,
    ) -> Dict[str, Any]:
        row = self.get_session(owner_user_id=owner_user_id, session_id=session_id)
        if not row:
            raise ValueError("会话不存在或无权限访问")

        msgs = row.get("messages") or []
        lim = max(1, min(int(limit or 50), 200))

        if cursor:
            idx = -1
            for i, m in enumerate(msgs):
                if _to_str(m.get("id")) == _to_str(cursor):
                    idx = i
                    break
            if idx > 0:
                sliced = msgs[max(0, idx - lim): idx]
                has_more = (idx - lim) > 0
                next_cursor = sliced[0]["id"] if (has_more and sliced) else None
                return {"items": sliced, "next_cursor": next_cursor, "has_more": has_more}

        sliced = msgs[-lim:]
        has_more = len(msgs) > len(sliced)
        next_cursor = sliced[0]["id"] if (has_more and sliced) else None
        return {"items": sliced, "next_cursor": next_cursor, "has_more": has_more}

    def append_message(
            self,
            *,
            owner_user_id: str,
            session_id: str,
            role: str,
            content: str,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            row = self._data["sessions"].get(session_id)
            if not row or row.get("deleted"):
                raise ValueError("会话不存在")
            if _to_str(row.get("owner_user_id")) != _to_str(owner_user_id):
                raise ValueError("无权限访问该会话")

            msg = {
                "id": _new_id(),
                "role": _to_str(role),
                "content": _to_str(content),
                "created_at": _now_iso(),
                "metadata": metadata or {},
            }
            row.setdefault("messages", []).append(msg)

            if (row.get("title") in (None, "", "新会话")) and msg["role"] == "user":
                row["title"] = (msg["content"].strip() or "新会话")[:24]

            row["updated_at"] = msg["created_at"]
            self._flush()
            return deepcopy(msg)


_store = _Store()

# =============================
# 指令理解（规则引擎）
# =============================
_PLATFORM_ALIASES = {
    "太平洋": ["太平洋", "太保", "太平洋保险"],
    "人保": ["人保", "picc", "中国人保", "人保财险"],
    "平安": ["平安", "平安保险"],
    "国寿财": ["国寿", "国寿财", "人寿", "中国人寿"],
    "大地": ["大地", "大地保险"],
    "阳光": ["阳光", "阳光保险"],
    "中华联合": ["中华联合", "中华"],
    "华安": ["华安"],
    "天安": ["天安"],
    "永安": ["永安"],
    "太平": ["太平"],
}

# ✅ 平台显示名 -> 平台 code（用于开关 & registry）
# 注意：这是“工程约定”，不是业务猜测；你后续想改 code 不影响前端/服务层结构。
PLATFORM_NAME_TO_CODE: Dict[str, str] = {
    "太平洋": "TP",
    "人保": "PICC",
    "平安": "PA",
    "国寿财": "CL",
    "大地": "DD",
    "阳光": "YG",
    "中华联合": "ZH",
    "华安": "HA",
    "天安": "TA",
    "永安": "YA",
    "太平": "TPIC",
}


def _detect_platform_name(text: str) -> Optional[str]:
    low = text.lower()
    for name, aliases in _PLATFORM_ALIASES.items():
        for a in aliases:
            if a.lower() in low:
                return name
    m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]{1,12})报价", text)
    if m:
        return m.group(1)
    return None


def _extract_order_id(text: str) -> Optional[int]:
    for p in (r"(?:订单号|订单)\s*[:：#]?\s*(\d{1,12})", r"\border\s*[:：#]?\s*(\d{1,12})\b"):
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            x = _safe_int(m.group(1), 0)
            return x if x > 0 else None
    return None


def _extract_task_id(text: str) -> Optional[int]:
    for p in (r"(?:任务号|任务|ocr任务|OCR任务)\s*[:：#]?\s*(\d{1,12})", r"\btask\s*[:：#]?\s*(\d{1,12})\b"):
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            x = _safe_int(m.group(1), 0)
            return x if x > 0 else None
    return None


def _extract_plate_no(text: str) -> Optional[str]:
    m = re.search(r"([京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{4,6})", text.upper())
    return m.group(1).upper() if m else None


def _extract_owner_name(text: str) -> Optional[str]:
    m = re.search(r"(?:车主|姓名)\s*[:： ]\s*([\u4e00-\u9fa5]{2,8})", text)
    if m:
        s = _to_str(m.group(1)).strip()
        return s or None
    return None


def _extract_owner_phone(text: str) -> Optional[str]:
    m = re.search(r"\b(1\d{10})\b", text)
    return m.group(1) if m else None


def _detect_intent(text: str) -> Tuple[str, float, Dict[str, Any]]:
    """
    intent:
    - quote
    - query_ocr_task
    - query_material_status
    - query_order
    - query_owner
    - help
    - fallback
    """
    t = _norm_text(text)
    low = t.lower()

    entities: Dict[str, Any] = {}
    platform_name = _detect_platform_name(t)
    if platform_name:
        entities["platform_name"] = platform_name
        entities["platform_code"] = PLATFORM_NAME_TO_CODE.get(platform_name, "STUB")

    order_id = _extract_order_id(t)
    if order_id:
        entities["order_id"] = order_id

    task_id = _extract_task_id(t)
    if task_id:
        entities["task_id"] = task_id

    plate_no = _extract_plate_no(t)
    if plate_no:
        entities["plate_no"] = plate_no

    owner_name = _extract_owner_name(t)
    if owner_name:
        entities["owner_name"] = owner_name

    owner_phone = _extract_owner_phone(t)
    if owner_phone:
        entities["owner_phone"] = owner_phone

    if _contains_any(low, ["help", "帮助", "怎么用", "能做什么", "指令", "菜单"]):
        return "help", 0.99, entities

    if "报价" in t:
        return ("quote", 0.98, entities) if platform_name else ("quote", 0.78, entities)

    if _contains_any(t, ["材料状态", "资料状态", "当前材料", "图片状态", "卡槽状态", "上传了哪些"]):
        return "query_material_status", 0.95, entities

    if _contains_any(t, ["ocr任务", "OCR任务", "识别状态", "ocr状态", "任务状态"]) or ("任务" in t and "状态" in t):
        return "query_ocr_task", 0.95 if task_id else 0.82, entities

    if _contains_any(t, ["查订单", "订单信息", "订单详情", "订单状态"]) or order_id:
        return "query_order", 0.92 if order_id else 0.76, entities

    if _contains_any(t, ["车主信息", "车主资料", "查车主", "车主"]) or owner_name or plate_no or owner_phone:
        return "query_owner", 0.88, entities

    return "fallback", 0.40, entities


# =============================
# DB 查询（真查库，不炸）
# =============================
def _json_text_col(col, path: str):
    return func.json_unquote(func.json_extract(col, path))


async def _db_get_order_by_id(db: AsyncSession, order_id: int) -> Optional[Order]:
    stmt = (
        select(Order)
        .where(Order.id == int(order_id))
        .options(
            selectinload(Order.order_info),
            selectinload(Order.images).selectinload(OrderImage.image_file),
        )
    )
    return (await db.execute(stmt)).scalars().first()


async def _db_find_order(
        db: AsyncSession,
        *,
        order_id: Optional[int],
        plate_no: Optional[str],
        owner_phone: Optional[str],
        owner_name: Optional[str],
) -> Optional[Order]:
    if order_id:
        return await _db_get_order_by_id(db, int(order_id))

    clauses = []
    if plate_no:
        clauses.append(_json_text_col(Order.dynamic_data, "$.plate_no") == plate_no.upper())
    if owner_name:
        clauses.append(
            or_(
                _json_text_col(Order.dynamic_data, "$.owner_name") == owner_name,
                _json_text_col(Order.dynamic_data, "$.id_name") == owner_name,
            )
        )

    stmt = select(Order).options(
        selectinload(Order.order_info),
        selectinload(Order.images).selectinload(OrderImage.image_file),
    )

    if owner_phone:
        stmt = stmt.join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
        clauses.append(OrderInfo.owner_phone == owner_phone)

    if clauses:
        stmt = stmt.where(and_(*clauses))

    stmt = stmt.order_by(desc(Order.id)).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def _db_get_latest_ocr_task_for_order(db: AsyncSession, order_id: int) -> Optional[OcrTask]:
    stmt = (
        select(OcrTask)
        .where(and_(OcrTask.scope_type == "order", OcrTask.scope_id == int(order_id)))
        .order_by(desc(OcrTask.id))
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def _db_get_ocr_task(db: AsyncSession, task_id: int) -> Optional[OcrTask]:
    stmt = select(OcrTask).where(OcrTask.id == int(task_id))
    return (await db.execute(stmt)).scalars().first()


def _latest_image_url(img: Optional[OrderImage]) -> Optional[str]:
    if not img:
        return None
    u = _to_str(getattr(img, "image_url", "")).strip()
    if u:
        return u
    sk = _to_str(getattr(img, "storage_key", "")).strip()
    if sk:
        try:
            return storage.object_url_for_display(sk, expires_in=900)
        except Exception:
            return None
    imf = getattr(img, "image_file", None)
    if imf is not None:
        u2 = _to_str(getattr(imf, "url", "")).strip()
        if u2:
            return u2
        sk2 = _to_str(getattr(imf, "storage_key", "")).strip()
        if sk2:
            try:
                return storage.object_url_for_display(sk2, expires_in=900)
            except Exception:
                return None
    return None


def _build_material_slots_from_order(order: Order) -> Dict[str, Any]:
    buckets: Dict[str, List[OrderImage]] = {k: [] for k in SLOT_CONFIG.keys()}
    for img in (getattr(order, "images", None) or []):
        sk = _to_str(getattr(img, "slot_key", "")).strip()
        if sk in buckets:
            buckets[sk].append(img)

    out: Dict[str, Any] = {}
    for slot_key, conf in SLOT_CONFIG.items():
        imgs = buckets.get(slot_key) or []
        imgs_sorted = sorted(imgs, key=lambda x: _safe_int(getattr(x, "id", 0), 0))
        latest = imgs_sorted[-1] if imgs_sorted else None
        out[slot_key] = {
            "slot_key": slot_key,
            "multi": bool(conf.get("multi", False)),
            "ocr": bool(conf.get("ocr", False)),
            "required": bool(conf.get("required", False)),
            "count": len(imgs_sorted),
            "has_image": bool(imgs_sorted),
            "latest_url": _latest_image_url(latest),
            "latest_storage_key": _to_str(getattr(latest, "storage_key", "")).strip() or None if latest else None,
        }
    return out


def _ocr_slot_statuses_from_order(order: Order) -> List[Dict[str, Any]]:
    slot_has_image = {k: False for k in OCR_SLOTS}
    for img in (getattr(order, "images", None) or []):
        sk = _to_str(getattr(img, "slot_key", "")).strip()
        if sk in slot_has_image:
            slot_has_image[sk] = True

    ocr_raw = getattr(order, "ocr_raw_json", None) or {}
    out: List[Dict[str, Any]] = []
    for slot_key in sorted(OCR_SLOTS):
        resp = ocr_raw.get(slot_key)
        status = "none"
        last_error = None
        if not slot_has_image.get(slot_key):
            status = "none"
        else:
            if resp is None:
                status = "pending"
            elif isinstance(resp, dict) and resp.get("error_code") not in (None, "", 0, "0"):
                status = "failed"
                last_error = _to_str(resp.get("error_msg") or resp.get("error_message") or "ocr_error")
            else:
                status = "finished"
        out.append(
            {
                "slot_key": slot_key,
                "ocr_required": True,
                "has_image": bool(slot_has_image.get(slot_key)),
                "ocr_status": status,
                "last_error": last_error,
            }
        )
    return out


def _order_brief_from_order(order: Order) -> Dict[str, Any]:
    dd = getattr(order, "dynamic_data", None) or {}
    return {
        "id": _safe_int(getattr(order, "id", 0), 0) or None,
        "plate_no": _to_str(dd.get("plate_no")).strip() or None,
        "owner_name": _to_str(dd.get("owner_name") or dd.get("id_name")).strip() or None,
        "vin": _to_str(dd.get("vin")).strip() or None,
        "engine_no": _to_str(dd.get("engine_no")).strip() or None,
    }


def _order_payload_from_order(order: Order) -> Dict[str, Any]:
    dd = getattr(order, "dynamic_data", None) or {}
    oi = getattr(order, "order_info", None)

    order_info_payload: Dict[str, Any] = {}
    if oi is not None:
        order_info_payload = {
            "insurance_expire_date": _to_str(getattr(oi, "insurance_expire_date", None) or "") or None,
            "owner_phone": _to_str(getattr(oi, "owner_phone", None) or "") or None,
            "remark": _to_str(getattr(oi, "remark", None) or "") or None,
            "commercial_amount": getattr(oi, "commercial_amount", None),
            "compulsory_amount": getattr(oi, "compulsory_amount", None),
            "vehicle_tax_amount": getattr(oi, "vehicle_tax_amount", None),
            "non_vehicle_amount": getattr(oi, "non_vehicle_amount", None),
            "premium_total": getattr(oi, "premium_total", None),
            "channel_total": getattr(oi, "channel_total", None),
            "customer_total": getattr(oi, "customer_total", None),
            "profit": getattr(oi, "profit", None),
        }

    slot_statuses = _ocr_slot_statuses_from_order(order)

    return {
        "order": {
            "id": _safe_int(getattr(order, "id", 0), 0) or None,
            "module": _to_str(getattr(order, "module", None) or "") or None,
            "created_by": _safe_int(getattr(order, "created_by", 0), 0) or None,
            "salesperson_id": _safe_int(getattr(order, "salesperson_id", 0), 0) or None,
            "customer_group_id": getattr(order, "customer_group_id", None),
            "channel_group_id": getattr(order, "channel_group_id", None),
            "is_finished": bool(getattr(order, "is_finished", False)),
            "is_rebate": bool(getattr(order, "is_rebate", False)),
            "is_paid": bool(getattr(order, "is_paid", False)),
            "created_at": _fmt_dt(getattr(order, "created_at", None)),
            "updated_at": _fmt_dt(getattr(order, "updated_at", None)),
        },
        "dynamic_data": dd,
        "order_info": order_info_payload or None,
        "images": _build_material_slots_from_order(order),
        "ocr_summary": {
            "slot_statuses": slot_statuses,
            "recognized_slots": [x["slot_key"] for x in slot_statuses if x["ocr_status"] == "finished"],
            "failed_slots": [x["slot_key"] for x in slot_statuses if x["ocr_status"] == "failed"],
        },
    }


# =============================
# material_payload 统一组装（平台公共入口用）
# =============================
def _build_material_payload_for_platform(order: Order) -> Dict[str, Any]:
    """
    ✅ 统一入参：基于 OCR/卡槽数据组装 material_payload
    - 不发明字段：只用现有 order/dynamic_data/order_info/images/ocr_raw_json
    """
    dd = getattr(order, "dynamic_data", None) or {}
    oi = getattr(order, "order_info", None)

    slots = _build_material_slots_from_order(order)
    # 为平台提供更可用的 slots：每个槽提供 storage_key/url/count
    slot_payload: Dict[str, Any] = {}
    for k, v in slots.items():
        slot_payload[k] = {
            "slot_key": k,
            "required": bool(v.get("required")),
            "ocr": bool(v.get("ocr")),
            "multi": bool(v.get("multi")),
            "count": int(v.get("count") or 0),
            "latest_url": v.get("latest_url"),
            "latest_storage_key": v.get("latest_storage_key"),
        }

    order_info_payload = None
    if oi is not None:
        order_info_payload = {
            "insurance_expire_date": _to_str(getattr(oi, "insurance_expire_date", None) or "") or None,
            "owner_phone": _to_str(getattr(oi, "owner_phone", None) or "") or None,
            "remark": _to_str(getattr(oi, "remark", None) or "") or None,
        }

    # OCR 原始结果（平台若需要可直接用）
    ocr_raw = getattr(order, "ocr_raw_json", None) or {}

    return {
        "order_id": _safe_int(getattr(order, "id", 0), 0) or None,
        "dynamic_data": dd,
        "order_info": order_info_payload,
        "slots": slot_payload,
        "ocr_raw_json": ocr_raw,
    }


# =============================
# 平台 adapter 获取（无 adapter 时 fallback stub）
# =============================
class _DynamicStubAdapter(StubPlatformAdapter):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.platform_code = (code or "STUB").strip().upper() or "STUB"


def _get_platform_adapter(platform_code: str) -> AiPlatformAdapter:
    code = (platform_code or "").strip().upper() or "STUB"
    a = get_adapter(code)
    if a:
        return a
    # 没注册任何平台时，仍然可以跑通“公共入口占位”
    return _DynamicStubAdapter(code)


# =============================
# 业务回复（人性化 + 结构化 data）
# =============================
def _help_reply() -> Tuple[str, Dict[str, Any]]:
    msg = (
        "我是报价助手（规则引擎版），主要负责：材料状态、OCR任务状态、订单/车主查询、平台报价指令分发。\n"
        "你可以这样说：\n"
        "1) 太平洋报价（或 人保报价/平安报价）\n"
        "2) 查看当前材料状态\n"
        "3) OCR任务123状态（或 查OCR任务 123）\n"
        "4) 查订单123（或 查订单 赣B12345 / 查订单 13800138000）\n"
        "5) 查车主 赣B12345（或 姓名:张三）"
    )
    return msg, {
        "status": "success",
        "intent": "help",
        "trace_id": _new_id()[:16],
        "data": _mk_data(result_status=RESULT_SUCCESS, message="已返回可用指令示例"),
        "actions": [
            _mk_action("查看当前材料状态"),
            _mk_action("太平洋报价"),
            _mk_action("查OCR任务 123"),
            _mk_action("查订单 10086"),
        ],
    }


async def _reply_material_status(db: AsyncSession, ctx: Dict[str, Any], entities: Dict[str, Any]) -> Tuple[
    str, Dict[str, Any]]:
    order_id = _safe_int(ctx.get("order_id"), 0) or _safe_int(entities.get("order_id"), 0) or None
    plate_no = _to_str(ctx.get("plate_no") or entities.get("plate_no")).strip() or None
    owner_phone = _to_str(ctx.get("owner_phone") or entities.get("owner_phone")).strip() or None
    owner_name = _to_str(ctx.get("owner_name") or entities.get("owner_name")).strip() or None

    order = await _db_find_order(db, order_id=order_id, plate_no=plate_no, owner_phone=owner_phone,
                                 owner_name=owner_name)
    if not order:
        return (
            "当前没有可展示的材料状态（未定位到订单）。你可以先发：查订单123 或 查订单 赣B12345，然后再查看材料状态。",
            {
                "status": "success",
                "intent": "material_status",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_EMPTY,
                    message="未定位到订单，无法展示材料状态",
                    entities=entities,
                    payload={"slots": {}},
                ),
                "actions": [_mk_action("查订单 10086"), _mk_action("查看当前材料状态")],
            },
        )

    slots = _build_material_slots_from_order(order)
    required_missing = [k for k, v in slots.items() if v.get("required") and not v.get("has_image")]

    total_slots = len(slots)
    ready_slots = len([1 for _, v in slots.items() if v.get("has_image")])

    lines = [f"材料状态：已覆盖 {ready_slots}/{total_slots} 个槽位。"]
    if required_missing:
        lines.append("缺少关键材料：" + "、".join(required_missing))
    else:
        lines.append("关键材料已齐，可以发起报价指令。")

    for k, v in slots.items():
        lines.append(f"- {k}: {'有图' if v.get('has_image') else '无图'}（{int(v.get('count') or 0)}张）")

    return (
        "\n".join(lines),
        {
            "status": "success",
            "intent": "material_status",
            "trace_id": _new_id()[:16],
            "data": _mk_data(
                result_status=RESULT_SUCCESS,
                message="已返回材料状态",
                entities={**entities, "order_id": _safe_int(getattr(order, "id", 0), 0) or None},
                payload={
                    "summary": {
                        "order_id": _safe_int(getattr(order, "id", 0), 0) or None,
                        "total_slots": total_slots,
                        "ready_slots": ready_slots,
                        "required_missing_slots": required_missing,
                    },
                    "slots": slots,
                },
            ),
            "actions": [
                _mk_action("太平洋报价"),
                _mk_action("人保报价"),
                _mk_action("OCR任务状态"),
            ],
        },
    )


async def _reply_ocr_task(db: AsyncSession, ctx: Dict[str, Any], entities: Dict[str, Any]) -> Tuple[
    str, Dict[str, Any]]:
    task_id = _safe_int(entities.get("task_id"), 0) or None

    if not task_id:
        order_id = _safe_int(ctx.get("order_id"), 0) or _safe_int(entities.get("order_id"), 0) or None
        plate_no = _to_str(ctx.get("plate_no") or entities.get("plate_no")).strip() or None
        owner_phone = _to_str(ctx.get("owner_phone") or entities.get("owner_phone")).strip() or None
        owner_name = _to_str(ctx.get("owner_name") or entities.get("owner_name")).strip() or None

        order = await _db_find_order(db, order_id=order_id, plate_no=plate_no, owner_phone=owner_phone,
                                     owner_name=owner_name)
        if not order:
            return (
                "已识别为OCR任务查询，但你没提供任务号，也没定位到订单。你可以发：查OCR任务 123 或 查订单123 再查OCR状态。",
                {
                    "status": "success",
                    "intent": "query_ocr_task",
                    "trace_id": _new_id()[:16],
                    "data": _mk_data(
                        result_status=RESULT_NEED_MORE,
                        message="缺少 task_id 或订单定位信息",
                        entities=entities,
                        payload={},
                    ),
                    "actions": [_mk_action("查OCR任务 123"), _mk_action("查订单 10086")],
                },
            )
        latest = await _db_get_latest_ocr_task_for_order(db, int(getattr(order, "id")))
        if not latest:
            return (
                "当前订单还没有OCR任务。你可以先上传图片并触发识别/报价。",
                {
                    "status": "success",
                    "intent": "query_ocr_task",
                    "trace_id": _new_id()[:16],
                    "data": _mk_data(
                        result_status=RESULT_EMPTY,
                        message="该订单暂无OCR任务",
                        entities={**entities, "order_id": _safe_int(getattr(order, "id", 0), 0) or None},
                        payload={},
                    ),
                    "actions": [_mk_action("查看当前材料状态"), _mk_action("太平洋报价")],
                },
            )
        task_id = int(getattr(latest, "id"))

    task = await _db_get_ocr_task(db, int(task_id))
    if not task:
        return (
            f"没找到 OCR任务{task_id}。请确认任务号是否正确。",
            {
                "status": "success",
                "intent": "query_ocr_task",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_EMPTY,
                    message="OCR任务不存在",
                    entities={**entities, "task_id": task_id},
                    payload={"task": None},
                ),
                "actions": [_mk_action("查看当前材料状态"), _mk_action("查订单 10086")],
            },
        )

    status = _to_str(getattr(task, "status", None) or "unknown")
    progress = _safe_int(getattr(task, "progress", 0), 0)
    error_message = _to_str(getattr(task, "error_message", None) or "").strip()
    scope_type = _to_str(getattr(task, "scope_type", None) or "")
    scope_id = _safe_int(getattr(task, "scope_id", 0), 0)

    status_cn_map = {
        "pending": "排队中",
        "processing": "识别中",
        "finished": "已完成",
        "finished_with_errors": "完成（部分异常）",
        "failed": "失败",
        "skipped": "跳过",
    }
    status_cn = status_cn_map.get(status, status)

    order_brief = None
    slot_statuses = []
    if scope_type == "order" and scope_id > 0:
        order = await _db_get_order_by_id(db, scope_id)
        if order:
            order_brief = _order_brief_from_order(order)
            slot_statuses = _ocr_slot_statuses_from_order(order)

    lines = [
        f"OCR任务状态：{status_cn}（{progress}%）",
        f"任务号：{_safe_int(getattr(task, 'id', 0), 0) or '未知'}",
    ]
    if scope_type and scope_id:
        lines.append(f"关联范围：{scope_type} / {scope_id}")
    if error_message:
        lines.append(f"提示：{error_message[:200]}")

    result_status = RESULT_SUCCESS
    if status in ("failed",):
        result_status = RESULT_FAILED
    elif status in ("pending", "processing"):
        result_status = RESULT_NOT_READY

    payload = {
        "task": {
            "id": _safe_int(getattr(task, "id", 0), 0) or None,
            "scope_type": scope_type or None,
            "scope_id": scope_id or None,
            "status": status,
            "progress": progress,
            "error_message": error_message or None,
            "created_at": _fmt_dt(getattr(task, "created_at", None)),
            "updated_at": _fmt_dt(getattr(task, "updated_at", None)),
            "finished_at": _fmt_dt(getattr(task, "finished_at", None)),
        },
        "order_brief": order_brief,
        "slot_statuses": slot_statuses,
    }

    return (
        "\n".join(lines),
        {
            "status": "success",
            "intent": "query_ocr_task",
            "trace_id": _new_id()[:16],
            "data": _mk_data(
                result_status=result_status,
                message="已返回OCR任务状态",
                entities={**entities, "task_id": task_id},
                payload=payload,
            ),
            "actions": [_mk_action("查看当前材料状态"), _mk_action("太平洋报价")],
        },
    )


async def _reply_order(db: AsyncSession, ctx: Dict[str, Any], entities: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    order_id = _safe_int(entities.get("order_id"), 0) or None
    plate_no = _to_str(entities.get("plate_no") or "").strip() or None
    owner_phone = _to_str(entities.get("owner_phone") or "").strip() or None
    owner_name = _to_str(entities.get("owner_name") or "").strip() or None

    if not any([order_id, plate_no, owner_phone, owner_name]):
        return (
            "已识别为订单查询，但你还没给查询条件。请补充订单号、车牌或手机号，例如：查订单 10086 / 查订单 赣B12345 / 查订单 13800138000",
            {
                "status": "success",
                "intent": "query_order",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="缺少订单查询条件",
                    entities=entities,
                    payload={},
                ),
                "actions": [_mk_action("查订单 10086"), _mk_action("查订单 赣B12345")],
            },
        )

    order = await _db_find_order(db, order_id=order_id, plate_no=plate_no, owner_phone=owner_phone,
                                 owner_name=owner_name)
    if not order:
        return (
            "没查到符合条件的订单。你可以换个条件再试试（订单号/车牌/手机号）。",
            {
                "status": "success",
                "intent": "query_order",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_EMPTY,
                    message="订单未命中",
                    entities=entities,
                    payload={},
                ),
                "actions": [_mk_action("查看当前材料状态"), _mk_action("太平洋报价")],
            },
        )

    payload = _order_payload_from_order(order)
    brief = _order_brief_from_order(order)

    lines = [
        f"订单查询结果：订单{brief.get('id') or '-'}",
        f"车主：{brief.get('owner_name') or '-'}",
        f"车牌：{brief.get('plate_no') or '-'}",
        f"VIN：{brief.get('vin') or '-'}",
        f"发动机号：{brief.get('engine_no') or '-'}",
    ]
    oi = (payload.get("order_info") or {}) if isinstance(payload.get("order_info"), dict) else {}
    remark = _to_str(oi.get("remark") or "").strip()
    if remark:
        lines.append(f"备注：{remark}")

    return (
        "\n".join(lines),
        {
            "status": "success",
            "intent": "query_order",
            "trace_id": _new_id()[:16],
            "data": _mk_data(
                result_status=RESULT_SUCCESS,
                message="订单查询成功",
                entities={**entities, "order_id": brief.get("id")},
                payload=payload,
            ),
            "actions": [_mk_action("查看当前材料状态"), _mk_action("太平洋报价")],
        },
    )


async def _reply_owner(db: AsyncSession, ctx: Dict[str, Any], entities: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    plate_no = _to_str(entities.get("plate_no") or "").strip() or None
    owner_phone = _to_str(entities.get("owner_phone") or "").strip() or None
    owner_name = _to_str(entities.get("owner_name") or "").strip() or None

    if not any([plate_no, owner_phone, owner_name]):
        return (
            "已识别为车主查询，请补充车牌号、手机号或姓名，例如：查车主 赣B12345 / 查车主 13800138000 / 查车主 姓名:张三",
            {
                "status": "success",
                "intent": "query_owner",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="车主查询缺少条件",
                    entities=entities,
                    payload={},
                ),
                "actions": [_mk_action("查车主 赣B12345"), _mk_action("查车主 13800138000")],
            },
        )

    order = await _db_find_order(db, order_id=None, plate_no=plate_no, owner_phone=owner_phone, owner_name=owner_name)
    if not order:
        return (
            "没查到对应车主信息（可能条件不匹配或暂无订单）。你可以换车牌/手机号/姓名再试一次。",
            {
                "status": "success",
                "intent": "query_owner",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_EMPTY,
                    message="车主未命中",
                    entities=entities,
                    payload={},
                ),
                "actions": [_mk_action("查订单 赣B12345"), _mk_action("查看当前材料状态")],
            },
        )

    dd = getattr(order, "dynamic_data", None) or {}
    oi = getattr(order, "order_info", None)

    owner_profile = {
        "owner_name": _to_str(dd.get("owner_name") or dd.get("id_name")).strip() or None,
        "id_name": _to_str(dd.get("id_name")).strip() or None,
        "id_number": _to_str(dd.get("id_number")).strip() or None,
        "owner_phone": _to_str(getattr(oi, "owner_phone", None) or "").strip() or None if oi else None,
        "plate_no": _to_str(dd.get("plate_no")).strip() or None,
        "vin": _to_str(dd.get("vin")).strip() or None,
        "engine_no": _to_str(dd.get("engine_no")).strip() or None,
        "vehicle_model": _to_str(dd.get("vehicle_model")).strip() or None,
        "first_register_date": _to_str(dd.get("first_register_date")).strip() or None,
    }

    task = await _db_get_latest_ocr_task_for_order(db, int(getattr(order, "id")))
    task_status = _to_str(getattr(task, "status", None) or "") if task else None

    slots = _build_material_slots_from_order(order)
    required_missing = [k for k, v in slots.items() if v.get("required") and not v.get("has_image")]
    platform_quote_ready = (not required_missing) and (task_status in (None, "", "finished", "finished_with_errors"))

    remark = _to_str(getattr(oi, "remark", None) or "") if oi else ""

    recent_orders = [
        {
            "order_id": _safe_int(getattr(order, "id", 0), 0) or None,
            "created_at": _fmt_dt(getattr(order, "created_at", None)),
            "is_finished": bool(getattr(order, "is_finished", False)),
            "task_status": task_status,
            "platform_quote_ready": bool(platform_quote_ready),
            "remark": remark or None,
        }
    ]

    payload = {
        "owner_profile": owner_profile,
        "matched_by": {"plate_no": plate_no, "owner_phone": owner_phone, "owner_name": owner_name},
        "recent_orders": recent_orders,
    }

    reply = (
        "车主信息查询结果：\n"
        f"- 车主：{owner_profile.get('owner_name') or '-'}\n"
        f"- 手机号：{owner_profile.get('owner_phone') or '-'}\n"
        f"- 车牌：{owner_profile.get('plate_no') or '-'}\n"
        f"- VIN：{owner_profile.get('vin') or '-'}\n"
        f"- 最近订单：{recent_orders[0].get('order_id') or '-'}（可报价：{'是' if platform_quote_ready else '否'}）"
    )

    return (
        reply,
        {
            "status": "success",
            "intent": "query_owner",
            "trace_id": _new_id()[:16],
            "data": _mk_data(
                result_status=RESULT_SUCCESS,
                message="已返回车主信息",
                entities={**entities, "order_id": _safe_int(getattr(order, "id", 0), 0) or None},
                payload=payload,
            ),
            "actions": [_mk_action(f"查订单 {_safe_int(getattr(order, 'id', 0), 0)}"), _mk_action("太平洋报价")],
        },
    )


async def _reply_quote(db: AsyncSession, ctx: Dict[str, Any], entities: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    platform_name = _to_str(entities.get("platform_name")).strip()
    platform_code = _to_str(entities.get("platform_code")).strip().upper() or "STUB"

    if not platform_name:
        return (
            "我识别到你要报价，但还没识别出平台。请直接说“太平洋报价”或“人保报价”。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="报价缺少平台信息",
                    entities=entities,
                    payload={},
                ),
                "actions": [_mk_action("太平洋报价"), _mk_action("人保报价"), _mk_action("平安报价")],
            },
        )

    # 定位订单
    order_id = _safe_int(ctx.get("order_id"), 0) or _safe_int(entities.get("order_id"), 0) or None
    plate_no = _to_str(ctx.get("plate_no") or entities.get("plate_no")).strip() or None
    owner_phone = _to_str(ctx.get("owner_phone") or entities.get("owner_phone")).strip() or None
    owner_name = _to_str(ctx.get("owner_name") or entities.get("owner_name")).strip() or None

    order = await _db_find_order(db, order_id=order_id, plate_no=plate_no, owner_phone=owner_phone,
                                 owner_name=owner_name)
    if not order:
        return (
            f"已识别报价指令：{platform_name}报价，但当前未定位到订单。你可以先发：查订单123 / 查订单 赣B12345，再执行报价。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="报价缺少订单定位信息",
                    entities=entities,
                    payload={"quote_request": {"platform_name": platform_name, "platform_code": platform_code,
                                               "accepted": False, "reason": "order_not_found"}},
                ),
                "actions": [_mk_action("查订单 10086"), _mk_action("查看当前材料状态")],
            },
        )

    # 材料检查
    slots = _build_material_slots_from_order(order)
    required_missing = [k for k, v in slots.items() if v.get("required") and not v.get("has_image")]
    if required_missing:
        return (
            f"已识别报价指令：{platform_name}报价。\n但关键材料不完整，暂不能报价。\n缺少：{'、'.join(required_missing)}",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_NOT_READY,
                    message="材料不完整，无法报价",
                    entities={**entities, "order_id": _safe_int(getattr(order, "id", 0), 0) or None},
                    payload={
                        "quote_request": {
                            "platform_name": platform_name,
                            "platform_code": platform_code,
                            "order_id": _safe_int(getattr(order, "id", 0), 0) or None,
                            "accepted": False,
                            "reason": "required_material_missing",
                            "required_missing_slots": required_missing,
                        }
                    },
                ),
                "actions": [_mk_action("查看当前材料状态")],
            },
        )

    # OCR 检查（仅阻塞 processing/pending）
    task = await _db_get_latest_ocr_task_for_order(db, int(getattr(order, "id")))
    task_status = _to_str(getattr(task, "status", None) or "") if task else ""
    if task_status in ("pending", "processing"):
        return (
            f"已识别报价指令：{platform_name}报价。材料已齐，但OCR还在处理中（{_safe_int(getattr(task, 'progress', 0), 0)}%），稍后可重试。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_NOT_READY,
                    message="OCR处理中，暂不能报价",
                    entities={**entities, "order_id": _safe_int(getattr(order, "id", 0), 0) or None},
                    payload={
                        "quote_request": {
                            "platform_name": platform_name,
                            "platform_code": platform_code,
                            "order_id": _safe_int(getattr(order, "id", 0), 0) or None,
                            "accepted": False,
                            "reason": "ocr_processing",
                            "ocr_task_id": _safe_int(getattr(task, "id", 0), 0) or None,
                            "progress": _safe_int(getattr(task, "progress", 0), 0),
                        }
                    },
                ),
                "actions": [_mk_action("OCR任务状态"), _mk_action("查看当前材料状态"),
                            _mk_action(f"{platform_name}报价")],
            },
        )

    # ✅ 平台公共入口：adapter + cache + 统一返回
    trace_id = _new_id()[:16]
    qc = QuoteContext(
        owner_user_id=None,
        session_id=_to_str(ctx.get("session_id") or "") or None,
        order_id=_safe_int(getattr(order, "id", 0), 0) or None,
        draft_id=_to_str(ctx.get("draft_id") or "") or None,
        trace_id=trace_id,
        account_id=_to_str(ctx.get("account_id") or "") or None,
        extra=ctx if isinstance(ctx, dict) else None,
    )

    adapter = _get_platform_adapter(platform_code)
    material_payload = _build_material_payload_for_platform(order)

    # 让平台知道用户看到的“平台名”
    material_payload["platform_name"] = platform_name
    material_payload["platform_code"] = platform_code

    res: QuoteResult = await adapter.quote(ctx=qc, material_payload=material_payload, use_cache=True)

    if not res.ok:
        # 不炸：人性化失败回显
        return (
            f"{platform_name}报价未成功：{res.error_message or '未知错误'}",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_FAILED if res.error_code not in ("platform_disabled",) else RESULT_NOT_READY,
                    message=res.error_message or "平台报价失败",
                    entities={**entities, "order_id": _safe_int(getattr(order, "id", 0), 0) or None},
                    payload={
                        "quote_request": {
                            "platform_name": platform_name,
                            "platform_code": platform_code,
                            "order_id": _safe_int(getattr(order, "id", 0), 0) or None,
                            "accepted": False,
                        },
                        "quote_result": {
                            "ok": False,
                            "error_code": res.error_code,
                            "error_message": res.error_message,
                            "quote_result": res.quote_result,
                            "raw_request": res.raw_request,
                            "raw_response": res.raw_response,
                            "cached": bool(res.cached),
                        },
                    },
                ),
                "actions": [_mk_action("查看当前材料状态"), _mk_action(f"{platform_name}报价")],
            },
        )

    # 成功（或 stub 成功）：统一回显
    brief = _order_brief_from_order(order)
    reply = (
        f"{platform_name}报价已返回（{'命中缓存' if res.cached else '实时计算'}）。\n"
        f"- 订单：{brief.get('id') or '-'} / {brief.get('plate_no') or '-'}\n"
        f"- 车主：{brief.get('owner_name') or '-'}"
    )

    return (
        reply,
        {
            "status": "success",
            "intent": "quote",
            "trace_id": trace_id,
            "data": _mk_data(
                result_status=RESULT_SUCCESS,
                message="报价结果已返回",
                entities={**entities, "order_id": brief.get("id")},
                payload={
                    "quote_request": {
                        "platform_name": platform_name,
                        "platform_code": platform_code,
                        "session_id": qc.session_id,
                        "order_id": qc.order_id,
                        "draft_id": qc.draft_id,
                        "trace_id": trace_id,
                    },
                    "quote_result": {
                        "ok": True,
                        "error_code": None,
                        "error_message": None,
                        "quote_result": res.quote_result,
                        "raw_request": res.raw_request,
                        "raw_response": res.raw_response,
                        "cached": bool(res.cached),
                    },
                },
            ),
            "actions": [_mk_action("查看当前材料状态"), _mk_action("查订单 " + _to_str(brief.get("id") or ""))],
        },
    )


def _fallback_reply() -> Tuple[str, Dict[str, Any]]:
    msg = (
        "我没完全看懂这条指令。\n"
        "你可以试试：\n"
        "- 太平洋报价\n"
        "- 查看当前材料状态\n"
        "- OCR任务123状态\n"
        "- 查订单123\n"
        "- 查车主 赣B12345"
    )
    return msg, {
        "status": "success",
        "intent": "fallback",
        "trace_id": _new_id()[:16],
        "data": _mk_data(result_status=RESULT_INVALID, message="无法识别指令"),
        "actions": [_mk_action("查看当前材料状态"), _mk_action("太平洋报价"), _mk_action("OCR任务状态")],
    }


# =============================
# 分发入口（真查库）
# =============================
async def _dispatch_rule(text: str, ctx: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    intent, confidence, entities = _detect_intent(text)

    async for db in get_db():
        try:
            if intent == "help":
                reply, meta = _help_reply()
            elif intent == "query_material_status":
                reply, meta = await _reply_material_status(db, ctx, entities)
            elif intent == "query_ocr_task":
                reply, meta = await _reply_ocr_task(db, ctx, entities)
            elif intent == "query_order":
                reply, meta = await _reply_order(db, ctx, entities)
            elif intent == "query_owner":
                reply, meta = await _reply_owner(db, ctx, entities)
            elif intent == "quote":
                reply, meta = await _reply_quote(db, ctx, entities)
            else:
                reply, meta = _fallback_reply()

            meta["intent"] = intent
            meta["confidence"] = float(confidence)
            data = meta.get("data")
            if isinstance(data, dict):
                data.setdefault("entities", entities)
            return reply, meta

        except Exception as e:
            return (
                "这次处理没成功（系统繁忙）。请重试一次；如果还不行，先发“查看当前材料状态”。",
                {
                    "status": "failed",
                    "intent": "system_error",
                    "trace_id": _new_id()[:16],
                    "confidence": 0.0,
                    "data": _mk_data(
                        result_status=RESULT_FAILED,
                        message="系统繁忙",
                        entities=entities,
                        payload={"error": (_to_str(e) or "unknown")[:300]},
                    ),
                    "actions": [_mk_action("查看当前材料状态"), _mk_action("太平洋报价")],
                },
            )

    return _fallback_reply()


# =============================
# 对外导出函数（给 API 层 import）
# =============================
def get_or_create_session(
        *,
        owner_user_id: str,
        session_id: Optional[str] = None,
        title: Optional[str] = None,
) -> Dict[str, Any]:
    return _store.get_or_create_session(owner_user_id=owner_user_id, session_id=session_id, title=title)


def create_session(*, owner_user_id: str, title: Optional[str] = None) -> Dict[str, Any]:
    return _store.create_session(owner_user_id=owner_user_id, title=title)


def list_sessions(*, owner_user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    return _store.list_sessions(owner_user_id=owner_user_id, limit=limit)


def delete_session(*, owner_user_id: str, session_id: str) -> bool:
    return _store.delete_session(owner_user_id=owner_user_id, session_id=session_id)


def get_session_messages(
        *,
        owner_user_id: str,
        session_id: str,
        cursor: Optional[str] = None,
        limit: int = 50,
) -> Dict[str, Any]:
    return _store.list_messages(owner_user_id=owner_user_id, session_id=session_id, cursor=cursor, limit=limit)


def list_messages(
        *,
        owner_user_id: str,
        session_id: str,
        cursor: Optional[str] = None,
        limit: int = 50,
) -> List[Dict[str, Any]]:
    res = _store.list_messages(owner_user_id=owner_user_id, session_id=session_id, cursor=cursor, limit=limit)
    items = res.get("items") if isinstance(res, dict) else None
    if not isinstance(items, list):
        return []
    return items


async def send_message(
        *,
        owner_user_id: str,
        session_id: Optional[str] = None,
        message: Optional[str] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: Optional[bool] = None,
        context: Optional[Dict[str, Any]] = None,
        text: Optional[str] = None,
        client_msg_id: Optional[str] = None,
        page_context: Optional[Dict[str, Any]] = None,
        use_stream: Optional[bool] = None,
) -> Dict[str, Any]:
    del history, system_prompt, temperature, max_tokens

    final_text = _norm_text(message if message is not None else text)
    final_context = context if isinstance(context, dict) else (page_context if isinstance(page_context, dict) else {})
    final_stream = bool(stream if stream is not None else use_stream)

    if not final_text:
        raise ValueError("消息内容不能为空")

    sess = _store.get_or_create_session(owner_user_id=_to_str(owner_user_id), session_id=session_id)
    real_session_id = _to_str(sess.get("session_id"))

    user_msg = _store.append_message(
        owner_user_id=owner_user_id,
        session_id=real_session_id,
        role="user",
        content=final_text,
        metadata={
            "status": "success",
            "intent": "user_input",
            "client_msg_id": client_msg_id,
            "page_context": final_context,
            "use_stream": final_stream,
            "model": _to_str(model, default="rule-engine") or "rule-engine",
        },
    )

    # 给平台入口一个 session_id 也能用（不强绑）
    if isinstance(final_context, dict) and "session_id" not in final_context:
        final_context["session_id"] = real_session_id

    reply_text, reply_meta = await _dispatch_rule(final_text, final_context)

    assistant_msg = _store.append_message(
        owner_user_id=owner_user_id,
        session_id=real_session_id,
        role="assistant",
        content=reply_text,
        metadata=reply_meta,
    )

    meta = assistant_msg.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    return {
        "session_id": real_session_id,
        "reply": _to_str(assistant_msg.get("content")),
        "intent": _to_str(meta.get("intent"), "chat") or "chat",
        "trace_id": _to_str(meta.get("trace_id"), _new_id()[:16]) or _new_id()[:16],
        "confidence": float(meta.get("confidence") or 0.0),
        "actions": meta.get("actions") if isinstance(meta.get("actions"), list) else [],
        "usage": None,
        "model": _to_str(model, "rule-engine") or "rule-engine",
        "data": meta.get("data") if isinstance(meta.get("data"), dict) else None,
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        "stream": None,
    }


__all__ = [
    "get_or_create_session",
    "create_session",
    "list_sessions",
    "delete_session",
    "get_session_messages",
    "list_messages",
    "send_message",
]
