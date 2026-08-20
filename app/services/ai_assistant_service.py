# app/services/ai_assistant_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import and_, desc, false as sql_false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, selectinload

from app.core.access_control import normalize_team_names, user_team_match_expr
from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_MARKET, ROLE_SALES, ROLE_SUPER_ADMIN
from app.core.db import async_session_factory, get_db
from app.models.ocr_task import OcrTask
from app.models.order import Order, OrderImage
from app.models.order_info import OrderInfo
from app.models.quote_assistant import QuoteAssistantMessage, QuoteAssistantSession, QuoteTask
from app.models.user import User
from app.services.ai_platforms import get_adapter
from app.services.ai_platforms.base import AiPlatformAdapter, QuoteContext, StubPlatformAdapter, QuoteResult
from app.services.image_slot_classifier import slot_label
from app.services.ocr_cleaner import correct_vehicle_cert_field, norm_id_number
from app.services.quote_assistant_service import (
    _collect_context_images,
    _extract_joint_sales_image_adjustment,
    _extract_quote_product_exclusions,
    _extract_quote_repair_code_command,
    _extract_transfer_vehicle_command,
    _is_explicit_platform_quote_command,
    _looks_like_quote_text_material,
    detect_platform_credential_signal,
    detect_quote_config_override_signal,
    detect_quote_data_override_signal,
    detect_quote_signal,
    extract_quote_fields,
    handle_quote_images_message,
    handle_quote_material_form_message,
    handle_platform_credential_message,
    handle_quote_material_status,
    handle_quote_message,
    handle_quote_text_material_message,
    has_expired_waiting_sms_task,
    has_recent_invalid_sms_task,
    has_waiting_duplicate_quote_confirm_task,
    has_waiting_sms_task,
    looks_like_quote_material_form_command,
    looks_like_duplicate_quote_cancel,
    looks_like_duplicate_quote_confirmation,
    looks_like_short_quote_command,
    looks_like_sms_code,
    redact_quote_sensitive_text,
    sanitize_quote_user_message,
    sanitize_quote_entities,
)
from app.services.quote_result_validation import quote_result_real_data_error
from app.services.quote_result_image import save_quote_result_card_image
from app.services.storage import StorageService

TZ_BJ = timezone(timedelta(hours=8))
storage = StorageService()
logger = logging.getLogger(__name__)
HISTORY_BEFORE_CURSOR_PREFIX = "__before__:"

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


def _now_db() -> datetime:
    return datetime.now(TZ_BJ).replace(tzinfo=None)


def _today_bounds_db() -> Tuple[datetime, datetime]:
    start = _now_db().replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _history_before_cursor(dt: datetime) -> str:
    return HISTORY_BEFORE_CURSOR_PREFIX + dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_history_before_cursor(cursor: str) -> Optional[datetime]:
    raw = _to_str(cursor).strip()
    if not raw.startswith(HISTORY_BEFORE_CURSOR_PREFIX):
        return None
    text = raw[len(HISTORY_BEFORE_CURSOR_PREFIX):].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except Exception:
            continue
    return None


def _to_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        return str(v)
    except Exception:
        return default


def _merge_quote_entities(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Merge intent entities without dropping complementary quote field overrides."""
    if not isinstance(incoming, dict):
        return base

    prev_overrides = base.get("quote_field_overrides")
    next_overrides = incoming.get("quote_field_overrides")
    prev_map = dict(prev_overrides) if isinstance(prev_overrides, dict) else {}
    next_map = dict(next_overrides) if isinstance(next_overrides, dict) else {}

    base.update(incoming)
    if prev_map or next_map:
        base["quote_field_overrides"] = {**prev_map, **next_map}
    return base


def _looks_like_quote_data_override_command(text: Any) -> bool:
    """A field value is not always a correction; initial text materials use the same labels."""

    compact = re.sub(r"\s+", "", _to_str(text))
    if not compact:
        return False
    return bool(
        re.search(
            r"改成|改为|改到|调整成|调整为|调整到|调成|调到|调至|设置为|设为|变成|变为|变到|更正为|修正为|纠正为|调整|修改|更正|修正|纠正|改|变",
            compact,
        )
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return _fmt_dt(value) or value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return _to_str(value)


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


def _looks_like_quote_image_context_hint(text: Any) -> bool:
    compact = re.sub(r"\s+", "", _to_str(text).strip())
    if not compact or len(compact) > 36:
        return False
    if re.search(r"[查找搜]|订单|车主|报价|状态|多少钱|保费", compact):
        return False
    slot_words = (
        "身份证正面",
        "身份证反面",
        "身份证人像面",
        "身份证国徽面",
        "行驶证正本",
        "行驶证主页",
        "行驶证副页",
        "行驶证副本",
        "车辆合格证",
        "合格证",
        "驾驶证",
    )
    if not any(word in compact for word in slot_words):
        return False
    return compact.startswith(("这是", "这个是", "这张是", "图片是", "照片是", "材料是")) or compact in slot_words


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
        "entities": sanitize_quote_entities(entities),
        "payload": payload or {},
    }


def _stable_image_url(storage_key: str, raw_url: str = "") -> str:
    sk = _to_str(storage_key).strip().lstrip("/")
    if sk:
        try:
            return storage.object_public_url(sk)
        except Exception:
            pass
    url = _to_str(raw_url).strip()
    if not url or url.startswith("blob:"):
        return ""
    return url.split("?", 1)[0]


def _safe_image_meta_for_history(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    sk = _to_str(item.get("storage_key") or item.get("key")).strip().lstrip("/")
    stable_url = _stable_image_url(sk, _to_str(item.get("image_url") or item.get("url") or item.get("preview_url")))
    out: Dict[str, Any] = {}
    for key in (
        "id",
        "kind",
        "slot_key",
        "provided_slot_key",
        "predicted_slot_key",
        "confirmed_slot_key",
        "storage_key",
        "md5",
        "etag",
        "size",
        "content_type",
        "provider",
        "width",
        "height",
        "render_scale",
        "original_name",
        "context_hint",
        "upload_batch_id",
        "confidence",
        "method",
        "reason",
        "recalled",
        "recalled_at",
    ):
        if key in item and item.get(key) not in (None, ""):
            out[key] = item.get(key)
    if sk:
        out["storage_key"] = sk
    if stable_url:
        out["url"] = stable_url
        out["preview_url"] = stable_url
        out["image_url"] = stable_url
    return out


def _safe_context_for_history(ctx: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(ctx, dict):
        return {}
    blocked = {"current_user_id", "role_name", "team_names", "session_id"}
    out: Dict[str, Any] = {}
    for key, value in ctx.items():
        if key in blocked:
            continue
        if key in {"images", "uploaded_images"} and isinstance(value, list):
            out[key] = [_safe_image_meta_for_history(x) for x in value]
        elif key == "page_context" and isinstance(value, dict):
            page = {}
            for pk, pv in value.items():
                if pk in blocked:
                    continue
                if pk in {"images", "uploaded_images"} and isinstance(pv, list):
                    page[pk] = [_safe_image_meta_for_history(x) for x in pv]
                else:
                    page[pk] = _json_safe(pv)
            out[key] = page
        else:
            out[key] = _json_safe(value)
    return out


def _context_has_history_images(ctx: Dict[str, Any]) -> bool:
    if not isinstance(ctx, dict):
        return False
    for key in ("images", "uploaded_images", "quote_images"):
        value = ctx.get(key)
        if isinstance(value, list) and value:
            return True
    page = ctx.get("page_context")
    if isinstance(page, dict):
        for key in ("images", "uploaded_images", "quote_images"):
            value = page.get(key)
            if isinstance(value, list) and value:
                return True
    return False


def _message_hidden_from_preview(role: Any, metadata: Any) -> bool:
    """Background organizer messages should not replace the human-visible session preview."""

    if _to_str(role).strip().lower() != "assistant" or not isinstance(metadata, dict):
        return False
    data = metadata.get("data")
    data = data if isinstance(data, dict) else {}
    payload = data.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    intent = _to_str(metadata.get("intent") or data.get("intent")).strip().lower()
    result_status = _to_str(data.get("result_status")).strip().lower()
    if intent == "fallback" or result_status == RESULT_INVALID:
        return True
    if intent == "quote_image_collect":
        return True
    if intent == "quote_config_override":
        return True
    quote_case = payload.get("quote_case")
    quote_case = quote_case if isinstance(quote_case, dict) else {}
    quote_task = payload.get("quote_task")
    quote_task = quote_task if isinstance(quote_task, dict) else {}
    entities = data.get("entities")
    entities = entities if isinstance(entities, dict) else {}
    platform_name = _to_str(
        payload.get("platform_name")
        or entities.get("platform_name")
        or quote_case.get("platform_name")
        or quote_task.get("platform_name")
    ).strip()
    duplicate_waiting = (
        payload.get("duplicate_quote_confirm_required") is True
        or bool(_to_str(payload.get("duplicate_quote_warning")).strip())
        or _to_str(quote_case.get("status")).strip().lower() == "waiting_duplicate_confirm"
        or _to_str(quote_task.get("status")).strip().lower() == "waiting_duplicate_confirm"
    )
    if intent == "quote" and duplicate_waiting:
        return True
    if payload.get("default_config_changed") is True:
        return True
    if "默认参数已更新" in _to_str(data.get("message")) or "默认参数已更新" in _to_str(payload.get("message")):
        return True
    if (
        intent == "quote"
        and payload.get("unsupported_platform") is True
        and "中止重复" in platform_name
    ):
        return True
    for container in (metadata, data, payload):
        if container.get("silent") is True:
            return True
        if _to_str(container.get("silent")).strip().lower() == "true":
            return True
        if container.get("ui_visible") is False:
            return True
        if _to_str(container.get("ui_visible")).strip().lower() == "false":
            return True
    return False


def _message_preview_text(role: Any, content: Any, metadata: Any) -> str:
    if _message_hidden_from_preview(role, metadata):
        return ""
    preview = _safe_chat_text_for_display(content).strip()
    if preview:
        return preview[:120]
    if _context_has_history_images(metadata if isinstance(metadata, dict) else {}):
        return "图片"
    return ""


def _session_preview_needs_recompute(value: Any) -> bool:
    text = _safe_chat_text_for_display(value).strip()
    if not text:
        return True
    hidden_markers = (
        "已完成识别归位",
        "默认参数已更新",
        "我没完全看懂这条指令",
        "等待重复投保确认",
        "平台提示可能重复投保",
    )
    if any(marker in text for marker in hidden_markers):
        return True
    return bool(re.match(r"^已收到\s*\d+\s*张图片", text))


def _looks_like_image_dict(value: Dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    image_keys = {"storage_key", "image_url", "url", "preview_url", "confirmed_slot_key", "predicted_slot_key"}
    return bool(image_keys.intersection(value.keys()))


def _safe_history_value(value: Any, *, depth: int = 0) -> Any:
    """Keep persisted chat history stable, displayable, and free of signed URLs."""

    if depth > 8:
        return _to_str(value)[:500]
    if isinstance(value, dict):
        if _looks_like_image_dict(value):
            return _safe_image_meta_for_history(value)
        out: Dict[str, Any] = {}
        for key, item in value.items():
            skey = _to_str(key)
            low = skey.lower()
            if low in {"authorization", "x-bce-security-token", "secret_access_key", "password", "account_password"}:
                continue
            if low.endswith("_ciphertext") or "password_ciphertext" in low:
                continue
            if skey in {"current_user_id", "role_name", "team_names", "session_id"}:
                continue
            out[skey] = _safe_history_value(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_safe_history_value(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if ("authorization=" in text.lower() or "x-bce-security-token=" in text.lower()) and text.startswith(("http://", "https://")):
            return text.split("?", 1)[0]
        return text[:4000]
    return _json_safe(value)


def _safe_metadata_for_history(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    return _safe_history_value(metadata)


def _safe_chat_text_for_display(text: Any) -> str:
    """Return historical chat text in the same user-facing language as live replies."""

    safe = sanitize_quote_user_message(redact_quote_sensitive_text(text), "")
    return safe or ""


def _client_message_id_for_history(value: Any) -> str:
    text = _to_str(value).strip()
    if not text or len(text) > 64:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", text):
        return ""
    return text


def _public_message_id_from_client_id(session_id: Any, client_msg_id: Any) -> str:
    """Keep frontend retries idempotent without requiring global client id uniqueness."""
    client_id = _client_message_id_for_history(client_msg_id)
    if not client_id:
        return ""
    sid = re.sub(r"[^A-Za-z0-9_-]", "", _to_str(session_id))[:12]
    if not sid:
        return client_id[:64]
    return f"{sid}_{client_id}"[:64]


def _looks_like_garbled_exception_detail(text: str) -> bool:
    detail = _to_str(text).strip()
    if not detail:
        return True
    if re.search(r"[\u4e00-\u9fff]", detail):
        return False
    meaningful = re.sub(r"[\s'\"`.,;:，。；：、!?！？()\[\]{}<>\\/_+=|~-]+", "", detail)
    if len(meaningful) < 8:
        return True
    # The assistant chat should not expose raw English stack/error fragments.
    return bool(re.fullmatch(r"[A-Za-z0-9-]+", meaningful))


def _humanize_exception(e: Exception) -> str:
    raw = (_to_str(e) or e.__class__.__name__).strip()
    lower = raw.lower()
    if "data too long for column" in lower:
        return "数据库字段写入内容过长，请检查上传图片链接或文本长度"
    if "duplicate entry" in lower:
        return "数据库唯一性冲突，可能是重复提交了同一份数据"
    if "can't connect to mysql" in lower or "connect to mysql" in lower:
        return "数据库连接失败，请确认数据库服务已启动且配置正确"
    if "no permission" in lower or "error_code" in lower or "access denied" in lower:
        return "接口暂无访问权限，请检查账号权限或稍后重试"
    if "internal server error" in lower:
        return "服务器处理异常，请稍后重试"
    if "lock timeout" in lower:
        return "会话历史写入锁等待超时，请稍后重试"
    if "timeout" in lower:
        return "外部服务或数据库响应超时，请稍后重试"
    if raw:
        detail = sanitize_quote_user_message(raw[:300], "处理失败，请稍后重试")
        if _looks_like_garbled_exception_detail(detail):
            return "处理失败，请稍后重试"
        return detail
    return "处理失败，请稍后重试"


# =============================
# 轻量 JSON 会话存储（会话/历史）
# =============================
class _Store:
    """
    轻量 JSON 存储（报价助手会话/消息）
    文件：storage/quote_assistant_sessions.json
    """

    _LOCK_TIMEOUT_SECONDS = 10.0
    _STALE_LOCK_SECONDS = 30.0

    def __init__(self) -> None:
        base_dir = Path(os.getenv("STORAGE_DIR", "storage"))
        base_dir.mkdir(parents=True, exist_ok=True)
        self._file = base_dir / "quote_assistant_sessions.json"
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"sessions": {}}
        with self._lock:
            with self._interprocess_lock():
                self._load()

    @contextmanager
    def _interprocess_lock(self):
        lock_file = self._file.with_suffix(self._file.suffix + ".lock")
        fd: Optional[int] = None
        deadline = time.monotonic() + self._LOCK_TIMEOUT_SECONDS
        while fd is None:
            try:
                fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            except FileExistsError:
                try:
                    age = time.time() - lock_file.stat().st_mtime
                    if age > self._STALE_LOCK_SECONDS:
                        lock_file.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"AI assistant session store lock timeout: {lock_file}")
                time.sleep(0.05)
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)
            try:
                lock_file.unlink()
            except FileNotFoundError:
                pass

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
            tmp.write_text(json.dumps(_json_safe(self._data), ensure_ascii=False, indent=2), encoding="utf-8")
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
            with self._interprocess_lock():
                self._load()
                self._data["sessions"][sid] = row
                self._flush()
        return deepcopy(row)

    def get_session(self, *, owner_user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            with self._interprocess_lock():
                self._load()
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
            with self._interprocess_lock():
                self._load()
                rows: List[Dict[str, Any]] = []
                for s in self._data["sessions"].values():
                    if s.get("deleted"):
                        continue
                    if _to_str(s.get("owner_user_id")) != owner:
                        continue
                    msgs = s.get("messages") or []
                    preview = ""
                    for m in reversed(msgs):
                        if not isinstance(m, dict):
                            continue
                        preview = _message_preview_text(
                            m.get("role"),
                            m.get("content"),
                            m.get("metadata") or {},
                        )
                        if preview:
                            break
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
            with self._interprocess_lock():
                self._load()
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

        msgs = [
            m
            for m in (row.get("messages") or [])
            if isinstance(m, dict) and not _message_hidden_from_preview(m.get("role"), m.get("metadata") or {})
        ]
        lim = max(1, min(int(limit or 50), 200))

        def safe_items(values: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for item in values:
                if not isinstance(item, dict):
                    continue
                clean = dict(item)
                clean["content"] = _safe_chat_text_for_display(clean.get("content"))
                clean["metadata"] = _safe_metadata_for_history(clean.get("metadata") or {})
                out.append(clean)
            return out

        if cursor:
            idx = -1
            for i, m in enumerate(msgs):
                if _to_str(m.get("id")) == _to_str(cursor):
                    idx = i
                    break
            if idx >= 0:
                if idx == 0:
                    return {"items": [], "next_cursor": None, "has_more": False}
                start = max(0, idx - lim)
                sliced = msgs[start: idx]
                has_more = start > 0
                next_cursor = sliced[0]["id"] if (has_more and sliced) else None
                return {"items": safe_items(sliced), "next_cursor": next_cursor, "has_more": has_more}

        sliced = msgs[-lim:]
        has_more = len(msgs) > len(sliced)
        next_cursor = sliced[0]["id"] if (has_more and sliced) else None
        return {"items": safe_items(sliced), "next_cursor": next_cursor, "has_more": has_more}

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
            with self._interprocess_lock():
                self._load()
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
                    "metadata": _safe_metadata_for_history(metadata or {}),
                }
                row.setdefault("messages", []).append(msg)

                if (row.get("title") in (None, "", "新会话")) and msg["role"] == "user":
                    row["title"] = (msg["content"].strip() or "新会话")[:24]

                row["updated_at"] = msg["created_at"]
                preview = _message_preview_text(msg["role"], msg["content"], msg.get("metadata") or {})
                if preview:
                    row["last_message_preview"] = preview
                self._flush()
                return deepcopy(msg)

    def recall_images(
            self,
            *,
            owner_user_id: str,
            session_id: str,
            storage_keys: List[str],
            message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        keys = {str(x or "").strip().lstrip("/") for x in (storage_keys or []) if str(x or "").strip()}
        if not keys:
            return {"updated_messages": 0, "updated_images": 0, "storage_keys": []}

        def mark_list(value: Any, recalled_at: str) -> int:
            changed = 0
            if not isinstance(value, list):
                return changed
            for item in value:
                if not isinstance(item, dict):
                    continue
                sk = _to_str(item.get("storage_key") or item.get("key")).strip().lstrip("/")
                if sk and sk in keys and not item.get("recalled"):
                    item["recalled"] = True
                    item["recalled_at"] = recalled_at
                    changed += 1
            return changed

        with self._lock:
            with self._interprocess_lock():
                self._load()
                row = self._data["sessions"].get(session_id)
                if not row or row.get("deleted"):
                    raise ValueError("会话不存在或无权限访问")
                if _to_str(row.get("owner_user_id")) != _to_str(owner_user_id):
                    raise ValueError("会话不存在或无权限访问")

                recalled_at = _now_iso()
                updated_messages = 0
                updated_images = 0
                for msg in row.get("messages") or []:
                    if message_id and _to_str(msg.get("id")) != _to_str(message_id):
                        continue
                    meta = msg.get("metadata")
                    if not isinstance(meta, dict):
                        continue

                    before = updated_images
                    updated_images += mark_list(meta.get("images"), recalled_at)
                    page_context = meta.get("page_context")
                    if isinstance(page_context, dict):
                        updated_images += mark_list(page_context.get("images"), recalled_at)
                        updated_images += mark_list(page_context.get("uploaded_images"), recalled_at)
                    data = meta.get("data")
                    payload = data.get("payload") if isinstance(data, dict) else None
                    if isinstance(payload, dict):
                        updated_images += mark_list(payload.get("attached_images"), recalled_at)

                    if updated_images > before:
                        old_keys = meta.get("recalled_image_storage_keys")
                        if not isinstance(old_keys, list):
                            old_keys = []
                        meta["recalled_image_storage_keys"] = sorted(set([*old_keys, *keys]))
                        meta["recalled_at"] = recalled_at
                        updated_messages += 1

                if updated_images:
                    row["updated_at"] = recalled_at
                    self._flush()
                return {
                    "updated_messages": updated_messages,
                    "updated_images": updated_images,
                    "storage_keys": sorted(keys),
                }


_store = _Store()


# =============================
# DB 会话存储（生产路径）
# =============================
def _session_row_to_dict(row: QuoteAssistantSession) -> Dict[str, Any]:
    return {
        "session_id": _to_str(getattr(row, "session_id", "")),
        "owner_user_id": _to_str(getattr(row, "owner_user_id", "")),
        "title": _to_str(getattr(row, "title", "")) or "新会话",
        "created_at": _fmt_dt(getattr(row, "created_at", None)),
        "updated_at": _fmt_dt(getattr(row, "updated_at", None)),
        "deleted": bool(getattr(row, "deleted", False)),
        "message_count": int(getattr(row, "message_count", 0) or 0),
        "last_message_preview": _safe_chat_text_for_display(getattr(row, "last_message_preview", ""))[:120],
    }


def _message_row_to_dict(row: QuoteAssistantMessage) -> Dict[str, Any]:
    meta = getattr(row, "metadata_json", None)
    if not isinstance(meta, dict):
        meta = {}
    return {
        "id": _to_str(getattr(row, "message_id", "")),
        "role": _to_str(getattr(row, "role", "assistant")) or "assistant",
        "content": _safe_chat_text_for_display(getattr(row, "content", "")),
        "created_at": _fmt_dt(getattr(row, "created_at", None)),
        "metadata": _safe_metadata_for_history(meta),
    }


def _message_row_hidden_from_history(row: QuoteAssistantMessage) -> bool:
    meta = getattr(row, "metadata_json", None)
    if not isinstance(meta, dict):
        meta = {}
    data = meta.get("data")
    data = data if isinstance(data, dict) else {}
    intent = _to_str(meta.get("intent") or data.get("intent")).strip().lower()
    result_status = _to_str(data.get("result_status")).strip().lower()
    # A fallback is not useful as a session-list preview, but it is a direct
    # response to the operator and must remain visible after history reload.
    if intent == "fallback" or result_status == RESULT_INVALID:
        return False
    return _message_hidden_from_preview(getattr(row, "role", ""), meta)


async def db_get_session(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: str,
) -> Optional[Dict[str, Any]]:
    row = await _db_get_session_row(db, owner_user_id=owner_user_id, session_id=session_id)
    return _session_row_to_dict(row) if row else None


async def _db_get_session_row(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: str,
) -> Optional[QuoteAssistantSession]:
    owner = _safe_int(owner_user_id, 0)
    sid = _to_str(session_id).strip()
    if owner <= 0 or not sid:
        return None
    stmt = (
        select(QuoteAssistantSession)
        .where(
            QuoteAssistantSession.owner_user_id == owner,
            QuoteAssistantSession.session_id == sid,
            QuoteAssistantSession.deleted.is_(False),
        )
        .execution_options(populate_existing=True)
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def db_create_session(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        title: Optional[str] = None,
) -> Dict[str, Any]:
    owner = _safe_int(owner_user_id, 0)
    if owner <= 0:
        raise ValueError("无法识别当前用户")
    now = _now_db()
    row = QuoteAssistantSession(
        session_id=_new_id(),
        owner_user_id=owner,
        title=(_to_str(title).strip() or "新会话")[:128],
        deleted=False,
        message_count=0,
        last_message_preview="",
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.flush()
    return _session_row_to_dict(row)


async def db_get_or_create_session(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: Optional[str] = None,
        title: Optional[str] = None,
) -> Dict[str, Any]:
    if session_id:
        found = await db_get_session(db, owner_user_id=owner_user_id, session_id=session_id)
        if found:
            return found
    return await db_create_session(db, owner_user_id=owner_user_id, title=title)


async def _db_session_last_visible_preview(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: str,
        fallback: str = "",
) -> str:
    fallback_preview = _safe_chat_text_for_display(fallback).strip()[:120]
    if fallback_preview and not _session_preview_needs_recompute(fallback_preview):
        return fallback_preview
    try:
        page = await db_list_messages(db, owner_user_id=owner_user_id, session_id=session_id, limit=8)
    except Exception:
        return fallback_preview
    items = page.get("items") if isinstance(page, dict) else []
    if not items:
        return "" if _session_preview_needs_recompute(fallback_preview) else fallback_preview
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        preview = _message_preview_text(item.get("role"), item.get("content", ""), item.get("metadata") or {})
        if preview:
            return preview[:120]
    return "" if _session_preview_needs_recompute(fallback_preview) else fallback_preview


async def db_list_sessions(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        limit: int = 50,
        cursor: Optional[str] = None,
) -> Dict[str, Any]:
    owner = _safe_int(owner_user_id, 0)
    if owner <= 0:
        return {"items": [], "next_cursor": None, "has_more": False}
    lim = max(1, min(int(limit or 50), 200))
    stmt = (
        select(QuoteAssistantSession)
        .where(
            QuoteAssistantSession.owner_user_id == owner,
            QuoteAssistantSession.deleted.is_(False),
        )
    )

    cursor_id = _to_str(cursor).strip()
    if cursor_id:
        cursor_stmt = (
            select(QuoteAssistantSession)
            .where(
                QuoteAssistantSession.owner_user_id == owner,
                QuoteAssistantSession.deleted.is_(False),
                QuoteAssistantSession.session_id == cursor_id,
            )
            .limit(1)
        )
        cursor_row = (await db.execute(cursor_stmt)).scalar_one_or_none()
        if not cursor_row:
            return {"items": [], "next_cursor": None, "has_more": False}
        stmt = stmt.where(
            or_(
                QuoteAssistantSession.updated_at < cursor_row.updated_at,
                and_(
                    QuoteAssistantSession.updated_at == cursor_row.updated_at,
                    QuoteAssistantSession.id < cursor_row.id,
                ),
            )
        )

    stmt = stmt.order_by(desc(QuoteAssistantSession.updated_at), desc(QuoteAssistantSession.id)).limit(lim + 1)
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > lim
    visible_rows = rows[:lim]
    items: List[Dict[str, Any]] = []
    for row in visible_rows:
        item = _session_row_to_dict(row)
        item["last_message_preview"] = await _db_session_last_visible_preview(
            db,
            owner_user_id=owner,
            session_id=_to_str(getattr(row, "session_id", "")),
            fallback=_to_str(getattr(row, "last_message_preview", "")),
        )
        items.append(item)
    return {
        "items": items,
        "next_cursor": items[-1]["session_id"] if has_more and items else None,
        "has_more": has_more,
    }


async def db_delete_session(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: str,
) -> bool:
    row = await _db_get_session_row(db, owner_user_id=owner_user_id, session_id=session_id)
    if not row:
        return False
    row.deleted = True
    row.updated_at = _now_db()
    await db.flush()
    return True


async def db_list_messages(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: str,
        cursor: Optional[str] = None,
        limit: int = 50,
        today_only_initial: bool = False,
) -> Dict[str, Any]:
    owner = _safe_int(owner_user_id, 0)
    row = await _db_get_session_row(db, owner_user_id=owner, session_id=session_id)
    if not row:
        raise ValueError("会话不存在或无权限访问")

    lim = max(1, min(int(limit or 50), 200))
    cursor_pk: Optional[int] = None
    cursor_id = _to_str(cursor).strip()
    before_created_at = _parse_history_before_cursor(cursor_id) if cursor_id else None
    today_start: Optional[datetime] = None
    today_end: Optional[datetime] = None
    if today_only_initial and not cursor_id:
        today_start, today_end = _today_bounds_db()

    if cursor_id and before_created_at is None:
        cursor_stmt = (
            select(QuoteAssistantMessage.id)
            .where(
                QuoteAssistantMessage.owner_user_id == owner,
                QuoteAssistantMessage.session_id == row.session_id,
                QuoteAssistantMessage.message_id == cursor_id,
            )
            .limit(1)
        )
        cursor_pk = (await db.execute(cursor_stmt)).scalar_one_or_none()
        if cursor_pk is None:
            return {"items": [], "next_cursor": None, "has_more": False}

    # 后台归位/状态消息会被隐藏，不能让它们消耗“默认 3 条/上翻 5 条”的可见额度。
    rows_desc: List[QuoteAssistantMessage] = []
    fetch_limit = min(max(lim + 8, lim * 4), 200)
    scan_before_pk: Optional[int] = None
    last_raw_cursor: Optional[str] = None
    raw_exhausted = False

    for _ in range(10):
        stmt = (
            select(QuoteAssistantMessage)
            .where(
                QuoteAssistantMessage.owner_user_id == owner,
                QuoteAssistantMessage.session_id == row.session_id,
            )
        )
        if before_created_at is not None:
            stmt = stmt.where(QuoteAssistantMessage.created_at < before_created_at)
        elif cursor_id and cursor_pk is not None:
            stmt = stmt.where(QuoteAssistantMessage.id < int(cursor_pk))
        elif today_start is not None and today_end is not None:
            stmt = stmt.where(
                QuoteAssistantMessage.created_at >= today_start,
                QuoteAssistantMessage.created_at < today_end,
            )
        if scan_before_pk is not None:
            stmt = stmt.where(QuoteAssistantMessage.id < scan_before_pk)

        batch = (
            await db.execute(stmt.order_by(desc(QuoteAssistantMessage.id)).limit(fetch_limit))
        ).scalars().all()
        if not batch:
            raw_exhausted = True
            break

        last_raw = batch[-1]
        scan_before_pk = int(getattr(last_raw, "id", 0) or 0) or None
        last_raw_cursor = _to_str(getattr(last_raw, "message_id", "")).strip() or None

        for msg_row in batch:
            if not _message_row_hidden_from_history(msg_row):
                rows_desc.append(msg_row)
                if len(rows_desc) > lim:
                    break

        if len(rows_desc) > lim:
            break
        if len(batch) < fetch_limit:
            raw_exhausted = True
            break

    has_more = len(rows_desc) > lim or not raw_exhausted
    page_rows = list(reversed(rows_desc[:lim]))
    items = [_message_row_to_dict(m) for m in page_rows]
    next_cursor = items[0]["id"] if (has_more and items) else (last_raw_cursor if has_more else None)

    if today_start is not None:
        if items and not has_more:
            first_row = page_rows[0]
            older_stmt = (
                select(QuoteAssistantMessage.id)
                .where(
                    QuoteAssistantMessage.owner_user_id == owner,
                    QuoteAssistantMessage.session_id == row.session_id,
                    QuoteAssistantMessage.id < int(getattr(first_row, "id", 0) or 0),
                )
                .limit(1)
            )
            if (await db.execute(older_stmt)).scalar_one_or_none() is not None:
                has_more = True
                next_cursor = items[0]["id"]
        elif not items:
            older_stmt = (
                select(QuoteAssistantMessage.id)
                .where(
                    QuoteAssistantMessage.owner_user_id == owner,
                    QuoteAssistantMessage.session_id == row.session_id,
                    QuoteAssistantMessage.created_at < today_start,
                )
                .limit(1)
            )
            if (await db.execute(older_stmt)).scalar_one_or_none() is not None:
                has_more = True
                next_cursor = _history_before_cursor(today_start)

    if items:
        await _reschedule_pending_quote_result_images_from_page(
            owner_user_id=owner,
            session_id=row.session_id,
            items=items,
        )

    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


async def db_append_message(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        message_id: Optional[str] = None,
) -> Dict[str, Any]:
    owner = _safe_int(owner_user_id, 0)
    row = await _db_get_session_row(db, owner_user_id=owner, session_id=session_id)
    if not row:
        raise ValueError("会话不存在或无权限访问")

    now = _now_db()
    safe_content = _to_str(content)
    msg = QuoteAssistantMessage(
        message_id=_to_str(message_id).strip() or _new_id(),
        session_id=row.session_id,
        owner_user_id=owner,
        role=_to_str(role) or "assistant",
        content=safe_content,
        metadata_json=_safe_metadata_for_history(metadata or {}),
        created_at=now,
        updated_at=now,
    )
    db.add(msg)

    if (row.title in (None, "", "新会话")) and msg.role == "user":
        row.title = (safe_content.strip() or "新会话")[:24]
    row.message_count = int(row.message_count or 0) + 1
    preview = _message_preview_text(msg.role, safe_content, msg.metadata_json)
    if preview:
        row.last_message_preview = preview
    row.updated_at = now

    await db.flush()
    return _message_row_to_dict(msg)


async def _db_get_message_by_public_id(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: str,
        message_id: str,
) -> Optional[QuoteAssistantMessage]:
    owner = _safe_int(owner_user_id, 0)
    msg_id = _to_str(message_id).strip()
    sid = _to_str(session_id).strip()
    if owner <= 0 or not sid or not msg_id:
        return None
    return (
        await db.execute(
            select(QuoteAssistantMessage)
            .where(
                QuoteAssistantMessage.owner_user_id == owner,
                QuoteAssistantMessage.session_id == sid,
                QuoteAssistantMessage.message_id == msg_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _db_cached_response_after_user_message(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: str,
        user_row: QuoteAssistantMessage,
        model: Optional[str],
) -> Optional[Dict[str, Any]]:
    owner = _safe_int(owner_user_id, 0)
    sid = _to_str(session_id).strip()
    if owner <= 0 or not sid or user_row is None:
        return None
    user_meta = user_row.metadata_json if isinstance(user_row.metadata_json, dict) else {}
    cached = user_meta.get("cached_response")
    if isinstance(cached, dict):
        return {
            "session_id": sid,
            "reply": _to_str(cached.get("reply")),
            "intent": _to_str(cached.get("intent"), "chat") or "chat",
            "trace_id": _to_str(cached.get("trace_id"), _new_id()[:16]) or _new_id()[:16],
            "confidence": float(cached.get("confidence") or 0.0),
            "actions": cached.get("actions") if isinstance(cached.get("actions"), list) else [],
            "usage": cached.get("usage") if isinstance(cached.get("usage"), dict) else None,
            "model": _to_str(cached.get("model") or model, "rule-engine") or "rule-engine",
            "data": cached.get("data") if isinstance(cached.get("data"), dict) else None,
            "silent": bool(cached.get("silent") is True or _to_str(cached.get("silent")).strip().lower() == "true"),
            "ui_visible": not (cached.get("ui_visible") is False or _to_str(cached.get("ui_visible")).strip().lower() == "false"),
            "user_message": _message_row_to_dict(user_row),
            "assistant_message": cached.get("assistant_message") if isinstance(cached.get("assistant_message"), dict) else None,
            "stream": None,
            "cached": True,
        }
    next_user_id = (
        await db.execute(
            select(func.min(QuoteAssistantMessage.id))
            .where(
                QuoteAssistantMessage.owner_user_id == owner,
                QuoteAssistantMessage.session_id == sid,
                QuoteAssistantMessage.role == "user",
                QuoteAssistantMessage.id > user_row.id,
            )
        )
    ).scalar_one_or_none()
    stmt = (
        select(QuoteAssistantMessage)
        .where(
            QuoteAssistantMessage.owner_user_id == owner,
            QuoteAssistantMessage.session_id == sid,
            QuoteAssistantMessage.role == "assistant",
            QuoteAssistantMessage.id > user_row.id,
        )
        .order_by(desc(QuoteAssistantMessage.id))
        .limit(1)
    )
    if next_user_id:
        stmt = stmt.where(QuoteAssistantMessage.id < int(next_user_id))
    assistant_row = (await db.execute(stmt)).scalar_one_or_none()
    if assistant_row is None:
        return None

    meta = assistant_row.metadata_json if isinstance(assistant_row.metadata_json, dict) else {}
    return {
        "session_id": sid,
        "reply": _to_str(assistant_row.content),
        "intent": _to_str(meta.get("intent"), "chat") or "chat",
        "trace_id": _to_str(meta.get("trace_id"), _new_id()[:16]) or _new_id()[:16],
        "confidence": float(meta.get("confidence") or 0.0),
        "actions": meta.get("actions") if isinstance(meta.get("actions"), list) else [],
        "usage": None,
        "model": _to_str(model, "rule-engine") or "rule-engine",
        "data": meta.get("data") if isinstance(meta.get("data"), dict) else None,
        "silent": bool(meta.get("silent") is True or _to_str(meta.get("silent")).strip().lower() == "true"),
        "ui_visible": not (meta.get("ui_visible") is False or _to_str(meta.get("ui_visible")).strip().lower() == "false"),
        "user_message": _message_row_to_dict(user_row),
        "assistant_message": _message_row_to_dict(assistant_row),
        "stream": None,
        "cached": True,
    }


async def _db_store_cached_response_on_user_message(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: str,
        message_id: str,
        response: Dict[str, Any],
) -> None:
    owner = _safe_int(owner_user_id, 0)
    sid = _to_str(session_id).strip()
    msg_id = _to_str(message_id).strip()
    if owner <= 0 or not sid or not msg_id or not isinstance(response, dict):
        return
    row = await _db_get_message_by_public_id(
        db,
        owner_user_id=owner,
        session_id=sid,
        message_id=msg_id,
    )
    if row is None:
        return
    meta = dict(row.metadata_json or {})
    meta["cached_response"] = _safe_history_value(response)
    row.metadata_json = meta
    row.updated_at = _now_db()
    await db.flush()


async def db_recall_session_images(
        db: AsyncSession,
        *,
        owner_user_id: str | int,
        session_id: str,
        storage_keys: List[str],
        message_id: Optional[str] = None,
) -> Dict[str, Any]:
    keys = {str(x or "").strip().lstrip("/") for x in (storage_keys or []) if str(x or "").strip()}
    if not keys:
        return {"updated_messages": 0, "updated_images": 0, "storage_keys": []}

    owner = _safe_int(owner_user_id, 0)
    sess = await _db_get_session_row(db, owner_user_id=owner, session_id=session_id)
    if not sess:
        raise ValueError("会话不存在或无权限访问")

    def mark_list(value: Any, recalled_at: str) -> int:
        changed = 0
        if not isinstance(value, list):
            return changed
        for item in value:
            if not isinstance(item, dict):
                continue
            sk = _to_str(item.get("storage_key") or item.get("key")).strip().lstrip("/")
            if sk and sk in keys and not item.get("recalled"):
                item["recalled"] = True
                item["recalled_at"] = recalled_at
                changed += 1
        return changed

    stmt = (
        select(QuoteAssistantMessage)
        .where(
            QuoteAssistantMessage.owner_user_id == owner,
            QuoteAssistantMessage.session_id == sess.session_id,
        )
    )
    msg_id = _to_str(message_id).strip()
    if msg_id:
        stmt = stmt.where(QuoteAssistantMessage.message_id == msg_id)
    stmt = stmt.order_by(QuoteAssistantMessage.id)
    rows = (await db.execute(stmt)).scalars().all()

    recalled_at = _now_iso()
    updated_at = _now_db()
    updated_messages = 0
    updated_images = 0

    for msg in rows:
        meta = deepcopy(msg.metadata_json or {})
        if not isinstance(meta, dict):
            continue

        before = updated_images
        updated_images += mark_list(meta.get("images"), recalled_at)
        page_context = meta.get("page_context")
        if isinstance(page_context, dict):
            updated_images += mark_list(page_context.get("images"), recalled_at)
            updated_images += mark_list(page_context.get("uploaded_images"), recalled_at)
        data = meta.get("data")
        payload = data.get("payload") if isinstance(data, dict) else None
        if isinstance(payload, dict):
            updated_images += mark_list(payload.get("attached_images"), recalled_at)

        if updated_images > before:
            old_keys = meta.get("recalled_image_storage_keys")
            if not isinstance(old_keys, list):
                old_keys = []
            meta["recalled_image_storage_keys"] = sorted(set([*old_keys, *keys]))
            meta["recalled_at"] = recalled_at
            msg.metadata_json = _safe_metadata_for_history(meta)
            msg.updated_at = updated_at
            updated_messages += 1

    if updated_images:
        sess.updated_at = updated_at
        await db.flush()

    return {
        "updated_messages": updated_messages,
        "updated_images": updated_images,
        "storage_keys": sorted(keys),
    }


def _quote_result_from_chat_data(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    result = (
        payload.get("quote_result")
        or payload.get("quoteResult")
        or data.get("quote_result")
        or data.get("quoteResult")
    )
    return result if isinstance(result, dict) else {}


def _set_quote_result_on_chat_data(data: Dict[str, Any], result: Dict[str, Any]) -> None:
    if not isinstance(data, dict) or not isinstance(result, dict):
        return
    payload = data.get("payload")
    if isinstance(payload, dict):
        if isinstance(payload.get("quote_result"), dict):
            payload["quote_result"] = deepcopy(result)
        if isinstance(payload.get("quoteResult"), dict):
            payload["quoteResult"] = deepcopy(result)
    if isinstance(data.get("quote_result"), dict):
        data["quote_result"] = deepcopy(result)
    if isinstance(data.get("quoteResult"), dict):
        data["quoteResult"] = deepcopy(result)


def _quote_result_needs_async_image(result: Mapping[str, Any]) -> bool:
    if not isinstance(result, Mapping):
        return False
    pending = result.get("result_image_pending")
    if pending is not True and _to_str(pending).strip().lower() not in {"true", "1", "yes"}:
        return False
    if result.get("result_image") or result.get("resultImage"):
        return False
    return not quote_result_real_data_error(result)


_ASYNC_QUOTE_RESULT_IMAGE_SCHEDULED_KEYS: set[Tuple[str, str]] = set()
_ASYNC_QUOTE_RESULT_IMAGE_SCHEDULED_KEYS_LOCK = threading.Lock()


def _async_quote_result_image_schedule_key(
    *,
    owner_user_id: Any,
    session_id: Any,
    assistant_message_id: Any,
) -> Optional[Tuple[str, str]]:
    owner = _safe_int(owner_user_id, 0)
    session_text = _to_str(session_id).strip()
    message_text = _to_str(assistant_message_id).strip()
    if owner <= 0 or not session_text or not message_text:
        return None
    return _to_str(owner), f"{session_text}:{message_text}"


def _schedule_async_quote_result_image_completion_once(
    *,
    owner_user_id: int,
    session_id: str,
    assistant_message_id: str,
    quote_task_id: int,
    trace_id: str,
) -> bool:
    key = _async_quote_result_image_schedule_key(
        owner_user_id=owner_user_id,
        session_id=session_id,
        assistant_message_id=assistant_message_id,
    )
    if key is None:
        return False
    with _ASYNC_QUOTE_RESULT_IMAGE_SCHEDULED_KEYS_LOCK:
        if key in _ASYNC_QUOTE_RESULT_IMAGE_SCHEDULED_KEYS:
            return False
        _ASYNC_QUOTE_RESULT_IMAGE_SCHEDULED_KEYS.add(key)
    try:
        asyncio.create_task(
            _complete_async_quote_result_image(
                owner_user_id=int(owner_user_id),
                session_id=_to_str(session_id).strip(),
                assistant_message_id=_to_str(assistant_message_id).strip(),
                quote_task_id=int(quote_task_id or 0),
                trace_id=_to_str(trace_id).strip(),
            )
        )
    except RuntimeError:
        logger.debug(
            "async quote result image scheduling skipped: no running loop owner=%s session=%s message=%s",
            owner_user_id,
            session_id,
            assistant_message_id,
        )
        with _ASYNC_QUOTE_RESULT_IMAGE_SCHEDULED_KEYS_LOCK:
            _ASYNC_QUOTE_RESULT_IMAGE_SCHEDULED_KEYS.discard(key)
        return False
    return True


def _quote_result_mark_async_image_failed(result: Dict[str, Any]) -> None:
    if not isinstance(result, dict):
        return
    result["result_image_pending"] = False
    result["result_image_async_failed"] = True


async def _clear_async_quote_result_image_pending(
    *,
    owner_user_id: int,
    session_id: str,
    assistant_message_id: str,
    quote_task_id: int,
) -> None:
    async with async_session_factory() as db:
        msg = await _db_get_message_by_public_id(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            message_id=assistant_message_id,
        )
        if msg is not None:
            meta = deepcopy(msg.metadata_json or {})
            if isinstance(meta, dict):
                data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
                result = _quote_result_from_chat_data(data)
                if _quote_result_needs_async_image(result):
                    result = deepcopy(result)
                    _quote_result_mark_async_image_failed(result)
                    _set_quote_result_on_chat_data(data, result)
                    meta["data"] = data
                    msg.metadata_json = _safe_metadata_for_history(meta)
                    msg.updated_at = _now_db()

        if quote_task_id > 0:
            task = await db.get(QuoteTask, quote_task_id)
            if task is not None:
                task_result = deepcopy(task.result_payload or {})
                if isinstance(task_result, dict) and _quote_result_needs_async_image(task_result):
                    _quote_result_mark_async_image_failed(task_result)
                    task.result_payload = task_result
                response_payload = deepcopy(task.response_payload or {})
                perf = response_payload.get("perf") if isinstance(response_payload.get("perf"), dict) else {}
                perf["result_image_async_failed"] = True
                response_payload["perf"] = perf
                task.response_payload = response_payload
                task.updated_at = _now_db()

        await db.commit()


async def _reschedule_pending_quote_result_images_from_page(
    *,
    owner_user_id: int,
    session_id: str,
    items: List[Dict[str, Any]],
) -> None:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        data = metadata.get("data") if isinstance(metadata.get("data"), dict) else {}
        result = _quote_result_from_chat_data(data)
        if not _quote_result_needs_async_image(result):
            continue
        assistant_message_id = _to_str(item.get("id")).strip()
        trace_id = _to_str(result.get("trace_id") or metadata.get("trace_id") or data.get("trace_id")).strip()
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        quote_task = payload.get("quote_task") if isinstance(payload.get("quote_task"), dict) else {}
        quote_task_id = _safe_int(quote_task.get("id"), 0)
        if assistant_message_id:
            _schedule_async_quote_result_image_completion_once(
                owner_user_id=owner_user_id,
                session_id=session_id,
                assistant_message_id=assistant_message_id,
                quote_task_id=quote_task_id,
                trace_id=trace_id,
            )


async def _complete_async_quote_result_image(
    *,
    owner_user_id: int,
    session_id: str,
    assistant_message_id: str,
    quote_task_id: int,
    trace_id: str,
) -> None:
    started = time.perf_counter()
    cleanup_needed = False
    try:
        async with async_session_factory() as db:
            msg = await _db_get_message_by_public_id(
                db,
                owner_user_id=owner_user_id,
                session_id=session_id,
                message_id=assistant_message_id,
            )
            if msg is None:
                return
            meta = deepcopy(msg.metadata_json or {})
            if not isinstance(meta, dict):
                return
            data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
            result = _quote_result_from_chat_data(data)
            if not _quote_result_needs_async_image(result):
                return
            card = result.get("result_card") or result.get("resultCard")
            if not isinstance(card, dict) or not card:
                raise ValueError("async quote result image card missing")

            image_payload = await asyncio.to_thread(
                save_quote_result_card_image,
                card,
                trace_id=trace_id,
            )
            if not isinstance(image_payload, dict) or not (
                image_payload.get("image_url") or image_payload.get("url") or image_payload.get("preview_url")
            ):
                raise ValueError("async quote result image payload missing")

            result = deepcopy(result)
            result["result_image"] = dict(image_payload)
            result["result_image_pending"] = False
            result["result_image_async"] = True
            result["result_image_ms"] = int(round((time.perf_counter() - started) * 1000))
            if image_payload.get("render_ms") is not None:
                result["result_image_render_ms"] = _safe_int(image_payload.get("render_ms"), 0)
            if image_payload.get("upload_ms") is not None:
                result["result_image_upload_ms"] = _safe_int(image_payload.get("upload_ms"), 0)
            _set_quote_result_on_chat_data(data, result)
            meta["data"] = data
            msg.metadata_json = _safe_metadata_for_history(meta)
            msg.updated_at = _now_db()

            if quote_task_id > 0:
                task = await db.get(QuoteTask, quote_task_id)
                if task is not None:
                    task_result = deepcopy(task.result_payload or {})
                    if isinstance(task_result, dict) and _quote_result_needs_async_image(task_result):
                        task_result.update(
                            {
                                "result_image": dict(image_payload),
                                "result_image_pending": False,
                                "result_image_async": True,
                                "result_image_ms": result["result_image_ms"],
                                "result_image_render_ms": result.get("result_image_render_ms"),
                                "result_image_upload_ms": result.get("result_image_upload_ms"),
                            }
                        )
                        task.result_payload = task_result
                    response_payload = deepcopy(task.response_payload or {})
                    perf = response_payload.get("perf") if isinstance(response_payload.get("perf"), dict) else {}
                    perf["result_image_async_ms"] = result["result_image_ms"]
                    if result.get("result_image_render_ms") is not None:
                        perf["result_image_render_ms"] = result["result_image_render_ms"]
                    if result.get("result_image_upload_ms") is not None:
                        perf["result_image_upload_ms"] = result["result_image_upload_ms"]
                    response_payload["perf"] = perf
                    task.response_payload = response_payload
                    task.updated_at = _now_db()
            await db.commit()
            logger.info(
                "async quote result image completed owner=%s session=%s message=%s task=%s image_ms=%s render_ms=%s upload_ms=%s",
                owner_user_id,
                session_id,
                assistant_message_id,
                quote_task_id,
                result.get("result_image_ms"),
                result.get("result_image_render_ms"),
                result.get("result_image_upload_ms"),
            )
    except Exception:
        logger.warning(
            "async quote result image completion failed: owner=%s session=%s message=%s task=%s",
            owner_user_id,
            session_id,
            assistant_message_id,
            quote_task_id,
            exc_info=True,
        )
        try:
            await _clear_async_quote_result_image_pending(
                owner_user_id=owner_user_id,
                session_id=session_id,
                assistant_message_id=assistant_message_id,
                quote_task_id=quote_task_id,
            )
        except Exception:
            logger.warning(
                "async quote result image failure cleanup failed: owner=%s session=%s message=%s task=%s",
                owner_user_id,
                session_id,
                assistant_message_id,
                quote_task_id,
                exc_info=True,
            )
    finally:
        key = _async_quote_result_image_schedule_key(
            owner_user_id=owner_user_id,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
        )
        if key is not None:
            with _ASYNC_QUOTE_RESULT_IMAGE_SCHEDULED_KEYS_LOCK:
                _ASYNC_QUOTE_RESULT_IMAGE_SCHEDULED_KEYS.discard(key)


def schedule_async_quote_result_image_completion(
    *,
    owner_user_id: int,
    response: Dict[str, Any],
) -> None:
    if not isinstance(response, dict):
        return
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    result = _quote_result_from_chat_data(data)
    if not _quote_result_needs_async_image(result):
        return
    assistant_message = response.get("assistant_message") if isinstance(response.get("assistant_message"), dict) else {}
    assistant_message_id = _to_str(assistant_message.get("id")).strip()
    session_id = _to_str(response.get("session_id")).strip()
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    quote_task = payload.get("quote_task") if isinstance(payload.get("quote_task"), dict) else {}
    quote_task_id = _safe_int(quote_task.get("id"), 0)
    trace_id = _to_str(response.get("trace_id") or quote_task.get("trace_id") or result.get("trace_id")).strip()
    if owner_user_id <= 0 or not session_id or not assistant_message_id:
        return
    _schedule_async_quote_result_image_completion_once(
        owner_user_id=int(owner_user_id),
        session_id=session_id,
        assistant_message_id=assistant_message_id,
        quote_task_id=quote_task_id,
        trace_id=trace_id,
    )


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
    return None


def _extract_order_id(text: str) -> Optional[int]:
    for p in (r"(?:订单号|订单)\s*[:：#]?\s*(\d{1,12})", r"\border\s*[:：#]?\s*(\d{1,12})\b"):
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            raw = _to_str(m.group(1)).strip()
            if re.fullmatch(r"1\d{10}", raw):
                continue
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
    return correct_vehicle_cert_field("plate_no", m.group(1)).upper() if m else None


def _extract_owner_name(text: str) -> Optional[str]:
    m = re.search(r"(?:车主|姓名)\s*[:： ]\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,40})", text)
    if m:
        s = _to_str(m.group(1)).strip()
        return s or None
    return None


def _extract_loose_owner_name_for_field_query(text: str) -> Optional[str]:
    t = _norm_text(text)
    m = re.search(
        r"(?:查|查询|订单|车主|姓名)?\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,40})\s*的?\s*(?:车牌号|车牌|手机号|电话|身份证|证件号|VIN|车架号|发动机号|车型|保费|应收|应付|利润)",
        t,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    name = re.sub(r"^(?:查|查询|订单|车主|姓名)", "", _to_str(m.group(1)).strip())
    if re.fullmatch(r"\d+", name):
        return None
    if not name or name in {"订单", "车主", "姓名", "车牌号", "车牌", "手机号", "电话", "身份证", "证件号", "车型", "保费"}:
        return None
    return name


def _extract_loose_owner_name_for_order_query(text: str) -> Optional[str]:
    t = _norm_text(text)
    if "报价" in t:
        return None
    m = re.search(r"(?:查订单|查询订单|订单信息|订单详情|订单)\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,40})", t)
    if not m:
        return None
    name = _to_str(m.group(1)).strip()
    if re.fullmatch(r"\d+", name):
        return None
    if not name or name in {"订单", "详情", "信息", "状态", "车主", "姓名", "车牌号", "车牌", "手机号", "电话", "身份证", "保费"}:
        return None
    return name


def _extract_owner_phone(text: str) -> Optional[str]:
    m = re.search(r"\b(1\d{10})\b", text)
    return m.group(1) if m else None


def _extract_id_number(text: str) -> Optional[str]:
    m = re.search(r"\b([0-9A-Za-z\u00d7Xx]{18})\b", text)
    if not m:
        return None
    value = norm_id_number(m.group(1))
    return value.upper() if value else None


def _extract_vin(text: str) -> Optional[str]:
    up = text.upper()
    m = re.search(r"(?:VIN|车架号|车辆识别代号)\s*[:：]?\s*([A-Z0-9]{11,20})", up)
    if not m:
        m = re.search(r"\b([A-Z0-9]{17})\b", up)
    if not m:
        return None
    value = correct_vehicle_cert_field("vin", m.group(1))
    return value.upper() if value else None


def _extract_engine_no(text: str) -> Optional[str]:
    up = text.upper()
    m = re.search(r"(?:发动机号|发动机号码|发动机)\s*[:：]?\s*([A-Z0-9\-]{4,32})", up)
    if not m:
        return None
    value = correct_vehicle_cert_field("engine_no", m.group(1))
    return value.upper() if value else None


def _extract_order_query_fields(text: str) -> List[str]:
    t = _norm_text(text)
    fields: List[str] = []
    rules = (
        ("owner_name", ("车主", "姓名")),
        ("plate_no", ("车牌号", "车牌")),
        ("owner_phone", ("手机号", "电话", "联系方式")),
        ("id_number", ("身份证", "证件号", "证件号码")),
        ("vin", ("车架号", "VIN", "车辆识别代号")),
        ("engine_no", ("发动机号", "发动机")),
        ("vehicle_model", ("车型", "品牌型号")),
        ("first_register_date", ("初登", "初登日期", "注册日期")),
        ("insurance_expire_date", ("保险到期", "保险到期日", "到期日")),
        ("premium_total", ("总保费", "保费", "金额")),
        ("commercial_amount", ("商业金额", "商业险")),
        ("compulsory_amount", ("交强金额", "交强险")),
        ("vehicle_tax_amount", ("车船税", "车船税金额")),
        ("non_vehicle_amount", ("非车", "非车金额")),
        ("finance_record", ("财务", "结算", "付款", "收款", "利润", "实际金额")),
        ("profit", ("利润", "差价")),
        ("channel_total", ("应收", "渠道", "渠道费用", "上游")),
        ("customer_total", ("应付", "客户", "客户费用", "下游")),
        ("remark", ("备注", "说明")),
        ("images", ("图片", "材料", "卡槽", "照片")),
        ("ocr_summary", ("OCR", "识别", "识别状态")),
        ("status", ("状态", "完成", "返点", "支付")),
    )
    for key, words in rules:
        if any(w in t for w in words):
            fields.append(key)
    return fields


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

    if _looks_like_quote_image_context_hint(t):
        return "quote_image_hint", 0.90, entities

    plate_no = _extract_plate_no(t)
    if plate_no:
        entities["plate_no"] = plate_no

    query_fields = _extract_order_query_fields(t)

    owner_name = _extract_owner_name(t)
    if not owner_name and query_fields:
        owner_name = _extract_loose_owner_name_for_field_query(t)
    if not owner_name:
        owner_name = _extract_loose_owner_name_for_order_query(t)
    if owner_name:
        entities["owner_name"] = owner_name

    owner_phone = _extract_owner_phone(t)
    if owner_phone:
        entities["owner_phone"] = owner_phone

    id_number = _extract_id_number(t)
    if id_number:
        entities["id_number"] = id_number

    vin = _extract_vin(t)
    if vin:
        entities["vin"] = vin

    engine_no = _extract_engine_no(t)
    if engine_no:
        entities["engine_no"] = engine_no

    if query_fields:
        entities["query_fields"] = query_fields

    credential_signal = detect_platform_credential_signal(t)
    if credential_signal.get("is_credential"):
        signal_entities = credential_signal.get("entities")
        if isinstance(signal_entities, dict):
            entities.update(signal_entities)
        credentials = credential_signal.get("credentials")
        if isinstance(credentials, dict) and _to_str(credentials.get("login_phone")) == _to_str(entities.get("owner_phone")):
            entities.pop("owner_phone", None)
        return "quote_credential", 0.96, entities

    if _contains_any(low, ["help", "帮助", "怎么用", "能做什么", "指令", "菜单"]):
        return "help", 0.99, entities

    if platform_name and _is_explicit_platform_quote_command(t, entities.get("platform_code"), platform_name):
        return "quote", 0.98, entities

    if _contains_any(t, ["材料状态", "资料状态", "当前材料", "图片状态", "卡槽状态", "上传了哪些"]):
        return "query_material_status", 0.95, entities

    if _contains_any(t, ["ocr任务", "OCR任务", "识别状态", "ocr状态", "任务状态"]) or ("任务" in t and "状态" in t):
        return "query_ocr_task", 0.95 if task_id else 0.82, entities

    if (
        _contains_any(t, ["查订单", "订单信息", "订单详情", "订单状态"])
        or order_id
        or vin
        or engine_no
        or id_number
        or (query_fields and any([plate_no, owner_phone, owner_name]))
    ):
        return "query_order", 0.92 if order_id else 0.76, entities

    if _contains_any(t, ["车主信息", "车主资料", "查车主", "车主"]) or owner_name or plate_no or owner_phone:
        return "query_owner", 0.88, entities

    return "fallback", 0.40, entities


# =============================
# DB 查询（真查库，不炸）
# =============================
def _json_text_col(col, path: str):
    return func.json_unquote(func.json_extract(col, path))


def _ctx_role_name(ctx: Dict[str, Any]) -> str:
    return _to_str((ctx or {}).get("role_name")).strip()


def _ctx_current_user_id(ctx: Dict[str, Any]) -> int:
    return _safe_int((ctx or {}).get("current_user_id"), 0)


def _ctx_team_names(ctx: Dict[str, Any]) -> Tuple[str, ...]:
    raw = (ctx or {}).get("team_names") or ()
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    if not isinstance(raw, (list, tuple, set)):
        raw = []
    return normalize_team_names(tuple(str(x or "").strip() for x in raw if str(x or "").strip()))


def _order_acl_clause_for_ctx(ctx: Dict[str, Any]):
    role_name = _ctx_role_name(ctx)
    if role_name == ROLE_SUPER_ADMIN:
        return None

    if role_name == ROLE_SALES:
        current_user_id = _ctx_current_user_id(ctx)
        if current_user_id <= 0:
            return sql_false()
        return Order.salesperson_id == current_user_id

    if role_name in (ROLE_MANAGER, ROLE_FINANCE, ROLE_MARKET):
        team_names = _ctx_team_names(ctx)
        if not team_names:
            return sql_false()
        if role_name in (ROLE_FINANCE, ROLE_MARKET) and len(team_names) != 1:
            return sql_false()
        team_user_ids = select(User.id).where(user_team_match_expr(team_names))
        return Order.salesperson_id.in_(team_user_ids)

    return sql_false()


async def _db_can_read_order_by_id(
        db: AsyncSession,
        order_id: int,
        *,
        ctx: Optional[Dict[str, Any]] = None,
) -> bool:
    stmt = select(Order.id).where(Order.id == int(order_id)).limit(1)
    acl_clause = _order_acl_clause_for_ctx(ctx or {})
    if acl_clause is not None:
        stmt = stmt.where(acl_clause)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _db_get_order_by_id(
        db: AsyncSession,
        order_id: int,
        *,
        ctx: Optional[Dict[str, Any]] = None,
) -> Optional[Order]:
    stmt = (
        select(Order)
        .where(Order.id == int(order_id))
        .options(
            lazyload("*"),
            selectinload(Order.salesperson).selectinload(User.parent),
            selectinload(Order.customer_group),
            selectinload(Order.channel_group),
            selectinload(Order.order_info),
            selectinload(Order.finance_record),
            selectinload(Order.images).selectinload(OrderImage.image_file),
        )
    )
    acl_clause = _order_acl_clause_for_ctx(ctx or {})
    if acl_clause is not None:
        stmt = stmt.where(acl_clause)
    return (await db.execute(stmt)).scalars().first()

async def _db_find_order(
        db: AsyncSession,
        *,
        order_id: Optional[int],
        plate_no: Optional[str],
        owner_phone: Optional[str],
        owner_name: Optional[str],
        vin: Optional[str] = None,
        engine_no: Optional[str] = None,
        id_number: Optional[str] = None,
        ctx: Optional[Dict[str, Any]] = None,
) -> Optional[Order]:
    if order_id:
        return await _db_get_order_by_id(db, int(order_id), ctx=ctx)

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
    if vin:
        clauses.append(_json_text_col(Order.dynamic_data, "$.vin") == vin.upper())
    if engine_no:
        clauses.append(_json_text_col(Order.dynamic_data, "$.engine_no") == engine_no.upper())
    if id_number:
        clauses.append(_json_text_col(Order.dynamic_data, "$.id_number") == id_number.upper())
    stmt = select(Order).options(
        lazyload("*"),
        selectinload(Order.salesperson).selectinload(User.parent),
        selectinload(Order.customer_group),
        selectinload(Order.channel_group),
        selectinload(Order.order_info),
        selectinload(Order.finance_record),
        selectinload(Order.images).selectinload(OrderImage.image_file),
    )

    if owner_phone:
        stmt = stmt.join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
        clauses.append(OrderInfo.owner_phone == owner_phone)

    if clauses:
        stmt = stmt.where(and_(*clauses))

    acl_clause = _order_acl_clause_for_ctx(ctx or {})
    if acl_clause is not None:
        stmt = stmt.where(acl_clause)

    stmt = stmt.order_by(desc(Order.id)).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def _db_find_orders(
        db: AsyncSession,
        *,
        order_id: Optional[int],
        plate_no: Optional[str],
        owner_phone: Optional[str],
        owner_name: Optional[str],
        vin: Optional[str] = None,
        engine_no: Optional[str] = None,
        id_number: Optional[str] = None,
        ctx: Optional[Dict[str, Any]] = None,
        limit: int = 6,
) -> List[Order]:
    if order_id:
        order = await _db_get_order_by_id(db, int(order_id), ctx=ctx)
        return [order] if order else []

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
    if vin:
        clauses.append(_json_text_col(Order.dynamic_data, "$.vin") == vin.upper())
    if engine_no:
        clauses.append(_json_text_col(Order.dynamic_data, "$.engine_no") == engine_no.upper())
    if id_number:
        clauses.append(_json_text_col(Order.dynamic_data, "$.id_number") == id_number.upper())
    if not clauses and not owner_phone:
        return []

    stmt = select(Order).options(
        lazyload("*"),
        selectinload(Order.salesperson).selectinload(User.parent),
        selectinload(Order.customer_group),
        selectinload(Order.channel_group),
        selectinload(Order.order_info),
        selectinload(Order.finance_record),
        selectinload(Order.images).selectinload(OrderImage.image_file),
    )

    if owner_phone:
        stmt = stmt.join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
        clauses.append(OrderInfo.owner_phone == owner_phone)

    if clauses:
        stmt = stmt.where(and_(*clauses))

    acl_clause = _order_acl_clause_for_ctx(ctx or {})
    if acl_clause is not None:
        stmt = stmt.where(acl_clause)

    stmt = stmt.order_by(desc(Order.id)).limit(max(1, min(int(limit or 6), 20)))
    return list((await db.execute(stmt)).scalars().all())


async def _db_get_latest_ocr_task_for_order(db: AsyncSession, order_id: int) -> Optional[OcrTask]:
    stmt = (
        select(OcrTask)
        .where(and_(OcrTask.scope_type == "order", OcrTask.scope_id == int(order_id)))
        .options(lazyload("*"))
        .order_by(desc(OcrTask.id))
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def _db_get_ocr_task(
        db: AsyncSession,
        task_id: int,
        *,
        ctx: Optional[Dict[str, Any]] = None,
) -> Optional[OcrTask]:
    stmt = select(OcrTask).where(OcrTask.id == int(task_id)).options(lazyload("*"))
    task = (await db.execute(stmt)).scalars().first()
    if not task:
        return None

    scope_type = _to_str(getattr(task, "scope_type", None) or "")
    scope_id = _safe_int(getattr(task, "scope_id", 0), 0)
    if scope_type == "order" and scope_id > 0:
        return task if await _db_can_read_order_by_id(db, scope_id, ctx=ctx) else None

    if _ctx_role_name(ctx or {}) == ROLE_SUPER_ADMIN:
        return task
    return None


def _image_signed_url(storage_key: str) -> Optional[str]:
    sk = _to_str(storage_key).strip()
    if not sk or not getattr(storage, "enabled", False):
        return None
    try:
        return storage.object_url_for_display(
            sk,
            expires_in=900,
            allow_fallback_public=False,
        )
    except TypeError:
        try:
            return storage.object_url_for_display(sk, expires_in=900)
        except Exception:
            return None
    except Exception:
        return None


def _latest_image_url(img: Optional[OrderImage]) -> Optional[str]:
    if not img:
        return None

    signed_url = _image_signed_url(getattr(img, "storage_key", ""))
    if signed_url:
        return signed_url

    imf = getattr(img, "image_file", None)
    if imf is not None:
        signed_url = _image_signed_url(getattr(imf, "storage_key", ""))
        if signed_url:
            return signed_url

    if not getattr(storage, "enabled", False):
        u = _to_str(getattr(img, "image_url", "")).strip()
        if u:
            return u
        if imf is not None:
            u2 = _to_str(getattr(imf, "url", "")).strip()
            if u2:
                return u2

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
    fr = getattr(order, "finance_record", None)

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

    finance_payload: Optional[Dict[str, Any]] = None
    if fr is not None:
        finance_payload = {
            "id": _safe_int(getattr(fr, "id", 0), 0) or None,
            "order_id": _safe_int(getattr(fr, "order_id", 0), 0) or None,
            "supplier_id": getattr(fr, "supplier_id", None),
            "upstream_paid": bool(getattr(fr, "upstream_paid", 0)),
            "downstream_paid": bool(getattr(fr, "downstream_paid", 0)),
            "settle_amount": getattr(fr, "settle_amount", None),
            "actual_amount": getattr(fr, "actual_amount", None),
            "note": _to_str(getattr(fr, "note", None) or "") or None,
            "created_at": _fmt_dt(getattr(fr, "created_at", None)),
            "updated_at": _fmt_dt(getattr(fr, "updated_at", None)),
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
        "finance_record": finance_payload,
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
# 平台 adapter 获取（无 adapter 时返回明确未接入错误）
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
    # 未注册平台不能走成功占位，StubPlatformAdapter 会明确返回失败。
    return _DynamicStubAdapter(code)


# =============================
# 业务回复（人性化 + 结构化 data）
# =============================
def _help_reply() -> Tuple[str, Dict[str, Any]]:
    msg = (
        "我是报价助手，主要负责：材料状态、识别任务状态、订单/车主查询、平台报价指令分发。\n"
        "你可以这样说：\n"
        "1) 太平洋报价（或 人保报价/平安报价）\n"
        "2) 查看当前材料状态\n"
        "3) 识别任务123状态（或 查识别任务 123）\n"
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
            _mk_action("查识别任务 123"),
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
                                 owner_name=owner_name, ctx=ctx)
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
        lines.append("缺少关键材料：" + "、".join(slot_label(k) for k in required_missing))
    else:
        lines.append("关键材料已齐，可以发起报价指令。")

    for k, v in slots.items():
        lines.append(f"- {slot_label(k)}：{'有图' if v.get('has_image') else '无图'}（{int(v.get('count') or 0)}张）")

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
                _mk_action("识别任务状态"),
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
                                     owner_name=owner_name, ctx=ctx)
        if not order:
            return (
                "已识别为识别任务查询，但你没提供任务号，也没定位到订单。你可以发：查识别任务 123 或 查订单123 后再查识别状态。",
                {
                    "status": "success",
                    "intent": "query_ocr_task",
                    "trace_id": _new_id()[:16],
                    "data": _mk_data(
                        result_status=RESULT_NEED_MORE,
                        message="缺少识别任务号或订单定位信息",
                        entities=entities,
                        payload={},
                    ),
                    "actions": [_mk_action("查识别任务 123"), _mk_action("查订单 10086")],
                },
            )
        latest = await _db_get_latest_ocr_task_for_order(db, int(getattr(order, "id")))
        if not latest:
            return (
                "当前订单还没有识别任务。你可以先上传图片并触发识别或报价。",
                {
                    "status": "success",
                    "intent": "query_ocr_task",
                    "trace_id": _new_id()[:16],
                    "data": _mk_data(
                        result_status=RESULT_EMPTY,
                        message="该订单暂无识别任务",
                        entities={**entities, "order_id": _safe_int(getattr(order, "id", 0), 0) or None},
                        payload={},
                    ),
                    "actions": [_mk_action("查看当前材料状态"), _mk_action("太平洋报价")],
                },
            )
        task_id = int(getattr(latest, "id"))

    task = await _db_get_ocr_task(db, int(task_id), ctx=ctx)
    if not task:
        return (
            f"没找到识别任务{task_id}。请确认任务号是否正确。",
            {
                "status": "success",
                "intent": "query_ocr_task",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_EMPTY,
                    message="识别任务不存在",
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
        order = await _db_get_order_by_id(db, scope_id, ctx=ctx)
        if order:
            order_brief = _order_brief_from_order(order)
            slot_statuses = _ocr_slot_statuses_from_order(order)

    lines = [
        f"识别任务状态：{status_cn}（{progress}%）",
        f"任务号：{_safe_int(getattr(task, 'id', 0), 0) or '未知'}",
    ]
    if scope_type and scope_id:
        scope_label = {
            "order": "关联订单",
            "image": "关联图片",
            "quote_case": "关联报价草稿",
            "quote": "关联报价",
        }.get(scope_type, "关联业务")
        lines.append(f"{scope_label}：{scope_id}")
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
                message="已返回识别任务状态",
                entities={**entities, "task_id": task_id},
                payload=payload,
            ),
            "actions": [_mk_action("查看当前材料状态"), _mk_action("太平洋报价")],
        },
    )


def _short_json(value: Any, *, max_len: int = 360) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = _to_str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _display_text(value: Any) -> str:
    text = _to_str(value).strip()
    return text if text else "-"


def _fmt_ymd(value: Any) -> str:
    text = _fmt_dt(value) or ""
    text = text.strip()
    if not text:
        return "-"
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    return text


def _fmt_money_text(value: Any) -> str:
    if value is None:
        return "-"
    text = _to_str(value).strip()
    if text == "":
        return "-"
    try:
        dec = value if isinstance(value, Decimal) else Decimal(text)
        return f"{dec.quantize(Decimal('0.01'))}"
    except Exception:
        return text


def _fmt_point_text(value: Any) -> str:
    if value is None or _to_str(value).strip() == "":
        return "-"
    try:
        dec = value if isinstance(value, Decimal) else Decimal(_to_str(value).strip())
        return f"{dec.normalize()}%"
    except Exception:
        return f"{_to_str(value).strip()}%"


def _yes_no(value: Any) -> str:
    return "是" if bool(value) else "否"


def _relation_text(obj: Any, *attrs: str) -> str:
    for attr in attrs:
        val = getattr(obj, attr, None) if obj is not None else None
        text = _to_str(val).strip()
        if text:
            return text
    return "-"


def _order_list_style_values(order: Order, payload: Dict[str, Any]) -> Dict[str, str]:
    dd = payload.get("dynamic_data") if isinstance(payload.get("dynamic_data"), dict) else {}
    oi = payload.get("order_info") if isinstance(payload.get("order_info"), dict) else {}
    sp = getattr(order, "salesperson", None)
    manager = getattr(sp, "parent", None) if sp is not None else None
    customer_group = getattr(order, "customer_group", None)
    channel_group = getattr(order, "channel_group", None)
    team_name = _display_text(
        getattr(sp, "team_names", None)
        or getattr(sp, "team_name", None)
        or getattr(customer_group, "team_name", None)
        or getattr(channel_group, "team_name", None)
    )

    return {
        "订单号": _display_text(_safe_int(getattr(order, "id", 0), 0) or None),
        "创建日期": _fmt_ymd(getattr(order, "created_at", None)),
        "客户": _relation_text(customer_group, "customer_name"),
        "渠道": _relation_text(channel_group, "channel_name"),
        "市场": _relation_text(customer_group, "market"),
        "业务员": _relation_text(sp, "real_name", "username"),
        "车主": _display_text(dd.get("owner_name") or dd.get("id_name")),
        "车牌": _display_text(dd.get("plate_no")),
        "保险到期日": _fmt_ymd(oi.get("insurance_expire_date")),
        "车架号": _display_text(dd.get("vin")),
        "发动机号": _display_text(dd.get("engine_no")),
        "车型": _display_text(dd.get("vehicle_model")),
        "初登日期": _fmt_ymd(dd.get("first_register_date")),
        "身份证号": _display_text(dd.get("id_number")),
        "电话": _display_text(oi.get("owner_phone")),
        "商业金额": _fmt_money_text(oi.get("commercial_amount")),
        "交强金额": _fmt_money_text(oi.get("compulsory_amount")),
        "车船税金额": _fmt_money_text(oi.get("vehicle_tax_amount")),
        "非车金额": _fmt_money_text(oi.get("non_vehicle_amount")),
        "保费金额": _fmt_money_text(oi.get("premium_total")),
        "应收": _fmt_money_text(oi.get("channel_total")),
        "应付": _fmt_money_text(oi.get("customer_total")),
        "利润": _fmt_money_text(oi.get("profit")),
        "所属经理": _relation_text(manager, "real_name", "username"),
        "所属团队": team_name,
        "是否完成": _yes_no(getattr(order, "is_finished", False)),
        "是否回款": _yes_no(getattr(order, "is_paid", False)),
        "是否返点": _yes_no(getattr(order, "is_rebate", False)),
    }


def _order_list_style_lines(order: Order, payload: Dict[str, Any]) -> List[str]:
    values = _order_list_style_values(order, payload)
    labels = (
        "订单号",
        "创建日期",
        "客户",
        "渠道",
        "市场",
        "业务员",
        "车主",
        "车牌",
        "保险到期日",
        "车架号",
        "发动机号",
        "车型",
        "初登日期",
        "身份证号",
        "电话",
        "商业金额",
        "交强金额",
        "车船税金额",
        "非车金额",
        "保费金额",
        "应收",
        "应付",
        "利润",
        "所属经理",
        "所属团队",
        "是否完成",
        "是否回款",
        "是否返点",
    )
    return ["订单查询结果：", *[f"{label}：{values.get(label) or '-'}" for label in labels]]


def _order_multi_brief_values(order: Order, payload: Dict[str, Any]) -> Dict[str, str]:
    values = _order_list_style_values(order, payload)
    labels = (
        "订单号",
        "创建日期",
        "客户",
        "渠道",
        "业务员",
        "车主",
        "车牌",
        "车型",
        "保费金额",
        "应收",
        "应付",
        "利润",
        "是否完成",
        "是否回款",
        "是否返点",
    )
    return {label: values.get(label) or "-" for label in labels}


def _order_multi_summary_lines(
        orders: List[Order],
        *,
        query_fields: List[str],
        truncated: bool,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    shown = orders[:5]
    total_text = f"{len(shown)}条" if not truncated else "超过5条"
    if query_fields:
        lines = [
            f"找到{total_text}匹配订单，按最新订单优先展示前5条。",
            "因为命中多条，我只列出你指定的字段，并保留订单号/车主/车牌方便区分：",
        ]
    else:
        lines = [
            f"找到{total_text}匹配订单，按最新订单优先展示前5条。",
            "下面按订单列表常用字段做摘要。要看完整详情，请继续输入：查订单 订单号。",
        ]

    display_rows: List[Dict[str, Any]] = []
    for idx, order in enumerate(shown, start=1):
        payload = _order_payload_from_order(order)
        brief = _order_multi_brief_values(order, payload)
        display_rows.append(brief)

        if query_fields:
            fields = _order_field_lines(order, payload, query_fields) or ["未识别到可展示字段：-"]
            lines.append(
                f"{idx}. 订单 {brief.get('订单号', '-')} | 车主 {brief.get('车主', '-')} | 车牌 {brief.get('车牌', '-')}"
            )
            lines.append("   " + "；".join(fields))
        else:
            lines.extend(
                [
                    f"{idx}. 订单 {brief.get('订单号', '-')} | 创建 {brief.get('创建日期', '-')}",
                    f"   客户/渠道/业务员：{brief.get('客户', '-')} / {brief.get('渠道', '-')} / {brief.get('业务员', '-')}",
                    f"   车主/车牌/车型：{brief.get('车主', '-')} / {brief.get('车牌', '-')} / {brief.get('车型', '-')}",
                    f"   金额：保费 {brief.get('保费金额', '-')}，应收 {brief.get('应收', '-')}，应付 {brief.get('应付', '-')}，利润 {brief.get('利润', '-')}",
                    f"   状态：完成 {brief.get('是否完成', '-')}，回款 {brief.get('是否回款', '-')}，返点 {brief.get('是否返点', '-')}",
                ]
            )
    if truncated:
        lines.append("结果超过5条，建议补充车牌、手机号或订单号进一步缩小范围。")
    return lines, display_rows


def _order_field_lines(order: Order, payload: Dict[str, Any], query_fields: List[str]) -> List[str]:
    if not query_fields:
        return []
    dd = payload.get("dynamic_data") if isinstance(payload.get("dynamic_data"), dict) else {}
    oi = payload.get("order_info") if isinstance(payload.get("order_info"), dict) else {}
    fr = payload.get("finance_record") if isinstance(payload.get("finance_record"), dict) else None
    images = payload.get("images") if isinstance(payload.get("images"), dict) else {}
    ocr = payload.get("ocr_summary") if isinstance(payload.get("ocr_summary"), dict) else {}
    order_payload = payload.get("order") if isinstance(payload.get("order"), dict) else {}

    out: List[str] = []
    seen = set()
    for key in query_fields:
        if key in seen:
            continue
        seen.add(key)
        if key == "owner_name":
            out.append(f"车主：{dd.get('owner_name') or dd.get('id_name') or '-'}")
        elif key == "plate_no":
            out.append(f"车牌号：{dd.get('plate_no') or '-'}")
        elif key == "owner_phone":
            out.append(f"手机号：{oi.get('owner_phone') or '-'}")
        elif key == "id_number":
            out.append(f"身份证号：{dd.get('id_number') or '-'}")
        elif key == "vin":
            out.append(f"车架号：{dd.get('vin') or '-'}")
        elif key == "engine_no":
            out.append(f"发动机号：{dd.get('engine_no') or '-'}")
        elif key == "vehicle_model":
            out.append(f"车型：{dd.get('vehicle_model') or '-'}")
        elif key == "first_register_date":
            out.append(f"初登日期：{_fmt_ymd(dd.get('first_register_date'))}")
        elif key == "insurance_expire_date":
            out.append(f"保险到期日：{_fmt_ymd(oi.get('insurance_expire_date'))}")
        elif key == "premium_total":
            out.append(f"保费金额：{_fmt_money_text(oi.get('premium_total'))}")
        elif key == "commercial_amount":
            out.append(f"商业金额：{_fmt_money_text(oi.get('commercial_amount'))}")
        elif key == "compulsory_amount":
            out.append(f"交强金额：{_fmt_money_text(oi.get('compulsory_amount'))}")
        elif key == "vehicle_tax_amount":
            out.append(f"车船税金额：{_fmt_money_text(oi.get('vehicle_tax_amount'))}")
        elif key == "non_vehicle_amount":
            out.append(f"非车金额：{_fmt_money_text(oi.get('non_vehicle_amount'))}")
        elif key == "profit":
            out.append(f"利润：{_fmt_money_text(oi.get('profit'))}")
        elif key == "channel_total":
            out.append(f"应收：{_fmt_money_text(oi.get('channel_total'))}")
        elif key == "customer_total":
            out.append(f"应付：{_fmt_money_text(oi.get('customer_total'))}")
        elif key == "remark":
            out.append(f"备注：{oi.get('remark') or '-'}")
        elif key == "finance_record":
            out.append(f"财务记录：{_short_json(fr) if fr else '-'}")
        elif key == "images":
            ready = [f"{k}:{v.get('count')}" for k, v in images.items() if isinstance(v, dict) and v.get("count")]
            out.append(f"图片卡槽：{', '.join(ready) if ready else '-'}")
        elif key == "ocr_summary":
            out.append(f"图片识别状态：{sanitize_quote_user_message(_short_json(ocr), '-')}")
        elif key == "status":
            out.append(
                f"状态：完成={_yes_no(order_payload.get('is_finished'))}，回款={_yes_no(order_payload.get('is_paid'))}，返点={_yes_no(order_payload.get('is_rebate'))}"
            )
    return out


async def _reply_order(db: AsyncSession, ctx: Dict[str, Any], entities: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    order_id = _safe_int(entities.get("order_id"), 0) or None
    plate_no = _to_str(entities.get("plate_no") or "").strip() or None
    owner_phone = _to_str(entities.get("owner_phone") or "").strip() or None
    owner_name = _to_str(entities.get("owner_name") or "").strip() or None
    vin = _to_str(entities.get("vin") or "").strip() or None
    engine_no = _to_str(entities.get("engine_no") or "").strip() or None
    id_number = _to_str(entities.get("id_number") or "").strip() or None
    query_fields = entities.get("query_fields") if isinstance(entities.get("query_fields"), list) else []

    if not any([order_id, plate_no, owner_phone, owner_name, vin, engine_no, id_number]):
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

    orders = await _db_find_orders(
        db,
        order_id=order_id,
        plate_no=plate_no,
        owner_phone=owner_phone,
        owner_name=owner_name,
        vin=vin,
        engine_no=engine_no,
        id_number=id_number,
        ctx=ctx,
        limit=6,
    )
    if not orders:
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

    if len(orders) > 1:
        truncated = len(orders) > 5
        lines, display_rows = _order_multi_summary_lines(
            orders,
            query_fields=[_to_str(x) for x in query_fields],
            truncated=truncated,
        )
        return (
            "\n".join(lines),
            {
                "status": "success",
                "intent": "query_order",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_SUCCESS,
                    message="订单查询命中多条结果",
                    entities={**entities, "matched_count": len(orders), "truncated": truncated},
                    payload={
                        "multiple": True,
                        "truncated": truncated,
                        "display_rows": display_rows,
                    },
                ),
                "actions": [_mk_action("请补充订单号查看完整详情"), _mk_action("查看当前材料状态")],
            },
        )

    order = orders[0]
    payload = _order_payload_from_order(order)
    brief = _order_brief_from_order(order)
    display = _order_list_style_values(order, payload)
    field_lines = _order_field_lines(order, payload, [_to_str(x) for x in query_fields])
    lines = field_lines if field_lines else _order_list_style_lines(order, payload)

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
                payload={
                    "multiple": False,
                    "display": display if not field_lines else {line.split("：", 1)[0]: line.split("：", 1)[1] for line in field_lines if "：" in line},
                },
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

    order = await _db_find_order(db, order_id=None, plate_no=plate_no, owner_phone=owner_phone, owner_name=owner_name,
                                 ctx=ctx)
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
        f"- 车架号：{owner_profile.get('vin') or '-'}\n"
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
                                 owner_name=owner_name, ctx=ctx)
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
            f"已识别报价指令：{platform_name}报价。材料已齐，但图片识别还在处理中（{_safe_int(getattr(task, 'progress', 0), 0)}%），稍后可重试。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": _new_id()[:16],
                "data": _mk_data(
                    result_status=RESULT_NOT_READY,
                    message="图片识别处理中，暂不能报价",
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
                "actions": [_mk_action("识别任务状态"), _mk_action("查看当前材料状态"),
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

    result_validation_error = quote_result_real_data_error(res.quote_result) if res.ok else ""
    if not res.ok or result_validation_error:
        # 不炸：人性化失败回显
        error_code = res.error_code or "quote_result_invalid"
        error_message = res.error_message or result_validation_error or "平台报价失败"
        return (
            f"{platform_name}报价未成功：{error_message}",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_FAILED if error_code not in ("platform_disabled",) else RESULT_NOT_READY,
                    message=error_message,
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
                            "error_code": error_code,
                            "error_message": error_message,
                            "quote_result": None,
                            "raw_request": res.raw_request,
                            "raw_response": res.raw_response,
                            "cached": bool(res.cached),
                        },
                    },
                ),
                "actions": [_mk_action("查看当前材料状态"), _mk_action(f"{platform_name}报价")],
            },
        )

    # 只有真实适配器返回成功时才统一回显
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
    msg = "指令错误：这条命令不在当前支持范围内，请使用已支持的报价、调参、手工、补资料或查询指令。"
    return msg, {
        "status": "success",
        "intent": "fallback",
        "trace_id": _new_id()[:16],
        "data": _mk_data(result_status=RESULT_INVALID, message=msg),
        "actions": [_mk_action("查看当前材料状态"), _mk_action("人保报价"), _mk_action("补资料")],
    }


def _quote_image_hint_reply() -> Tuple[str, Dict[str, Any]]:
    return "请把这句说明留在输入框里，再直接拖入对应图片。", {
        "status": "success",
        "intent": "quote_image_hint",
        "trace_id": _new_id()[:16],
        "data": _mk_data(result_status=RESULT_SUCCESS, message="等待图片上传"),
        "actions": [],
    }


# =============================
# 分发入口（真查库）
# =============================
def _dispatch_error_reply(
        error: Exception,
        entities: Dict[str, Any],
        ctx: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    detail = _humanize_exception(error)
    role_name = _ctx_role_name(ctx or {})
    can_use_quote = role_name in {ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_SALES, ROLE_FINANCE, ROLE_MARKET}
    actions = [_mk_action("查看当前材料状态"), _mk_action("人保报价")] if can_use_quote else [_mk_action("查订单")]
    fallback_hint = "查看当前材料状态" if can_use_quote else "查订单 车牌号或车主姓名"
    first_line = "这次处理没成功。"
    if detail and detail != "处理失败，请稍后重试":
        first_line = f"这次处理没成功：{detail}。"
    return (
        f"{first_line}\n请重试一次；如果还不行，先发“{fallback_hint}”。",
        {
            "status": "failed",
            "intent": "system_error",
            "trace_id": _new_id()[:16],
            "confidence": 0.0,
            "data": _mk_data(
                result_status=RESULT_FAILED,
                message=f"处理失败：{detail}",
                entities=entities,
                payload={"error": detail},
            ),
            "actions": actions,
        },
    )


def _log_dispatch_exception(error: Exception, *, intent: str, ctx: Optional[Dict[str, Any]] = None) -> None:
    safe_ctx = ctx or {}
    try:
        logger.exception(
            "ai assistant dispatch failed: intent=%s user_id=%s session_id=%s error_type=%s",
            intent,
            safe_ctx.get("current_user_id"),
            safe_ctx.get("session_id"),
            error.__class__.__name__,
        )
    except Exception:
        pass


async def _dispatch_rule_with_db(
        text: str,
        ctx: Dict[str, Any],
        *,
        db: AsyncSession,
        intent: str,
        confidence: float,
        entities: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    waiting_sms_active = False
    if intent != "quote":
        waiting_sms_active = await has_waiting_sms_task(db, ctx)
    if (
        intent != "quote"
        and looks_like_duplicate_quote_confirmation(text)
        and await has_waiting_duplicate_quote_confirm_task(db, ctx)
    ):
        intent = "quote"
        confidence = max(float(confidence or 0.0), 0.94)
    if intent != "quote" and looks_like_sms_code(text) and waiting_sms_active:
        intent = "quote"
        confidence = max(float(confidence or 0.0), 0.93)
    sms_code_like = looks_like_sms_code(text)
    if intent != "quote" and sms_code_like and await has_expired_waiting_sms_task(db, ctx):
        intent = "quote"
        confidence = max(float(confidence or 0.0), 0.91)
    if intent != "quote" and sms_code_like and await has_recent_invalid_sms_task(db, ctx):
        intent = "quote"
        confidence = max(float(confidence or 0.0), 0.9)
    if intent != "quote_credential" and looks_like_quote_material_form_command(text):
        form_result = await handle_quote_material_form_message(db, ctx=ctx, entities=entities, text=text)
        if form_result:
            reply, meta = form_result
            meta["intent"] = _to_str(meta.get("intent"), "quote_material_form") or "quote_material_form"
            meta["confidence"] = max(float(confidence or 0.0), 0.96)
            data = meta.get("data")
            if isinstance(data, dict):
                data.setdefault("entities", entities)
            return reply, meta

    image_result = None
    has_context_images = bool(_collect_context_images(ctx))
    # Only actual uploads enter the quote material organizer. Plain fallback
    # text must not create/update cases or cancel an in-flight quote task.
    should_collect_images = has_context_images and intent != "quote_credential"
    if should_collect_images:
        image_result = await handle_quote_images_message(db, ctx=ctx, entities=entities, text=text)

    text_material_result = None
    text_material_candidate = _looks_like_quote_text_material(
        text,
        extract_quote_fields(text),
    )
    if (
        not image_result
        and intent not in {"quote", "quote_credential", "query_material_status"}
        and not has_context_images
        and (intent != "fallback" or text_material_candidate)
    ):
        text_material_result = await handle_quote_text_material_message(db, ctx=ctx, entities=entities, text=text)

    if image_result:
        reply, meta = image_result
    elif text_material_result:
        reply, meta = text_material_result
    elif intent == "help":
        reply, meta = _help_reply()
    elif intent == "query_material_status":
        quote_status = await handle_quote_material_status(db, ctx=ctx, entities=entities)
        if quote_status:
            reply, meta = quote_status
        else:
            reply, meta = await _reply_material_status(db, ctx, entities)
    elif intent == "query_ocr_task":
        reply, meta = await _reply_ocr_task(db, ctx, entities)
    elif intent == "query_order":
        reply, meta = await _reply_order(db, ctx, entities)
    elif intent == "query_owner":
        reply, meta = await _reply_owner(db, ctx, entities)
    elif intent == "quote_credential":
        reply, meta = await handle_platform_credential_message(db, ctx=ctx, entities=entities, text=text)
    elif intent == "quote":
        reply, meta = await handle_quote_message(db, ctx=ctx, entities=entities, text=text)
    elif intent == "quote_image_hint":
        reply, meta = _quote_image_hint_reply()
    else:
        reply, meta = _fallback_reply()

    meta["intent"] = _to_str(meta.get("intent"), intent) or intent
    meta["confidence"] = float(confidence)
    data = meta.get("data")
    if isinstance(data, dict):
        data.setdefault("entities", entities)
    return reply, meta


async def _dispatch_rule(text: str, ctx: Dict[str, Any], db: Optional[AsyncSession] = None) -> Tuple[str, Dict[str, Any]]:
    intent, confidence, entities = _detect_intent(text)
    quote_signal = detect_quote_signal(text)
    signal_entities = quote_signal.get("entities")
    if isinstance(signal_entities, dict):
        _merge_quote_entities(entities, signal_entities)
    quote_platform_code = _to_str(entities.get("platform_code")).strip().upper()
    quote_platform_name = _to_str(entities.get("platform_name")).strip()
    if quote_signal.get("is_quote") and (
        _is_explicit_platform_quote_command(text, quote_platform_code, quote_platform_name)
        or looks_like_short_quote_command(text)
    ):
        intent = "quote"
        confidence = max(float(confidence or 0.0), 0.96)
    quote_override_signal = detect_quote_config_override_signal(text)
    if quote_override_signal.get("is_override"):
        intent = "quote"
        confidence = max(float(confidence or 0.0), 0.93)
        override_entities = quote_override_signal.get("entities")
        if isinstance(override_entities, dict):
            _merge_quote_entities(entities, override_entities)
    quote_data_override_signal = detect_quote_data_override_signal(text)
    if (
        intent != "quote_credential"
        and quote_data_override_signal.get("is_override")
        and _looks_like_quote_data_override_command(text)
    ):
        intent = "quote"
        confidence = max(float(confidence or 0.0), 0.93)
        data_override_entities = quote_data_override_signal.get("entities")
        if isinstance(data_override_entities, dict):
            _merge_quote_entities(entities, data_override_entities)
    transfer_vehicle_command = _extract_transfer_vehicle_command(text)
    if transfer_vehicle_command:
        intent = "quote"
        confidence = max(float(confidence or 0.0), 0.93)
        for key in ("is_transfer_vehicle", "transfer_date", "transfer_vehicle_override"):
            if transfer_vehicle_command.get(key) not in (None, ""):
                entities[key] = transfer_vehicle_command.get(key)
    if (
        _extract_quote_product_exclusions(text)
        or _extract_joint_sales_image_adjustment(text)
        or _extract_quote_repair_code_command(text)
    ):
        intent = "quote"
        confidence = max(float(confidence or 0.0), 0.93)
    if looks_like_duplicate_quote_confirmation(text) or looks_like_duplicate_quote_cancel(text):
        intent = "quote"
        confidence = max(float(confidence or 0.0), 0.94)

    if db is not None:
        try:
            # Quote flows create/update long-running task state and may commit
            # before calling the platform. Wrapping them in begin_nested() can
            # close the transaction mid-context, so let the quote service own
            # its transaction boundary.
            quote_material_candidate = bool(_collect_context_images(ctx)) or _looks_like_quote_text_material(
                text,
                extract_quote_fields(text),
            )
            if intent in {"quote", "quote_credential"} or quote_material_candidate:
                return await _dispatch_rule_with_db(
                    text,
                    ctx,
                    db=db,
                    intent=intent,
                    confidence=confidence,
                    entities=entities,
                )
            # Keep normal assistant writes in the same API transaction while
            # allowing a failed dispatch to preserve the user's message.
            async with db.begin_nested():
                return await _dispatch_rule_with_db(
                    text,
                    ctx,
                    db=db,
                    intent=intent,
                    confidence=confidence,
                    entities=entities,
                )
        except Exception as e:
            _log_dispatch_exception(e, intent=intent, ctx=ctx)
            return _dispatch_error_reply(e, entities, ctx=ctx)

    async for db in get_db():
        try:
            result = await _dispatch_rule_with_db(
                text,
                ctx,
                db=db,
                intent=intent,
                confidence=confidence,
                entities=entities,
            )
            await db.commit()
            return result

        except Exception as e:
            try:
                await db.rollback()
            except Exception:
                pass
            _log_dispatch_exception(e, intent=intent, ctx=ctx)
            return _dispatch_error_reply(e, entities, ctx=ctx)

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


def recall_session_images(
        *,
        owner_user_id: str,
        session_id: str,
        storage_keys: List[str],
        message_id: Optional[str] = None,
) -> Dict[str, Any]:
    return _store.recall_images(
        owner_user_id=owner_user_id,
        session_id=session_id,
        storage_keys=storage_keys,
        message_id=message_id,
    )


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
        db: Optional[AsyncSession] = None,
) -> Dict[str, Any]:
    del history, system_prompt, temperature, max_tokens

    final_text = _norm_text(message if message is not None else text)
    final_context = context if isinstance(context, dict) else (page_context if isinstance(page_context, dict) else {})
    final_stream = bool(stream if stream is not None else use_stream)

    if not final_text:
        raise ValueError("消息内容不能为空")

    if db is not None:
        sess = await db_get_or_create_session(db, owner_user_id=owner_user_id, session_id=session_id)
    else:
        sess = _store.get_or_create_session(owner_user_id=_to_str(owner_user_id), session_id=session_id)
    real_session_id = _to_str(sess.get("session_id"))

    # 给平台入口一个 session_id 也能用（不强绑）
    if isinstance(final_context, dict):
        final_context["session_id"] = real_session_id
    suppress_user_message = bool(
        isinstance(final_context, dict)
        and (final_context.get("suppress_user_message") or final_context.get("auto_followup"))
    )
    has_history_images = _context_has_history_images(final_context)

    hide_unlabeled_sms_code = False
    if looks_like_sms_code(final_text):
        if db is not None:
            try:
                hide_unlabeled_sms_code = bool(
                    await has_waiting_sms_task(db, final_context)
                    or await has_expired_waiting_sms_task(db, final_context)
                    or await has_recent_invalid_sms_task(db, final_context)
                )
            except Exception:
                hide_unlabeled_sms_code = False
        else:
            try:
                async for sms_db in get_db():
                    hide_unlabeled_sms_code = bool(
                        await has_waiting_sms_task(sms_db, final_context)
                        or await has_expired_waiting_sms_task(sms_db, final_context)
                        or await has_recent_invalid_sms_task(sms_db, final_context)
                    )
                    break
            except Exception:
                hide_unlabeled_sms_code = False

    user_metadata = {
        "status": "success",
        "intent": "user_input",
        "client_msg_id": client_msg_id,
        "page_context": _safe_context_for_history(final_context),
        "use_stream": final_stream,
        "model": _to_str(model, default="rule-engine") or "rule-engine",
    }
    if isinstance(final_context, dict) and "display_user_content" in final_context:
        display_text = _norm_text(final_context.get("display_user_content"))
    else:
        display_text = final_text
    user_content = redact_quote_sensitive_text(
        display_text,
        hide_unlabeled_sms_code=hide_unlabeled_sms_code,
    )
    user_msg = None
    stable_user_message_id = _public_message_id_from_client_id(real_session_id, client_msg_id)
    if not suppress_user_message or has_history_images:
        existing_user_row = None
        if db is not None:
            existing_user_row = (
                await _db_get_message_by_public_id(
                    db,
                    owner_user_id=owner_user_id,
                    session_id=real_session_id,
                    message_id=stable_user_message_id,
                )
                if stable_user_message_id
                else None
            )
            if existing_user_row is not None:
                cached_result = await _db_cached_response_after_user_message(
                    db,
                    owner_user_id=owner_user_id,
                    session_id=real_session_id,
                    user_row=existing_user_row,
                    model=model,
                )
                if cached_result is not None:
                    return cached_result
                user_msg = _message_row_to_dict(existing_user_row)
            else:
                user_msg = await db_append_message(
                    db,
                    owner_user_id=owner_user_id,
                    session_id=real_session_id,
                    role="user",
                    content=user_content,
                    metadata=user_metadata,
                    message_id=stable_user_message_id,
                )
        else:
            user_msg = _store.append_message(
                owner_user_id=owner_user_id,
                session_id=real_session_id,
                role="user",
                content=user_content,
                metadata=user_metadata,
            )

    reply_text, reply_meta = await _dispatch_rule(final_text, final_context, db=db)

    assistant_content = redact_quote_sensitive_text(reply_text)
    reply_meta = reply_meta if isinstance(reply_meta, dict) else {}
    reply_data = reply_meta.get("data") if isinstance(reply_meta.get("data"), dict) else {}
    reply_payload = reply_data.get("payload") if isinstance(reply_data.get("payload"), dict) else {}
    hidden_assistant_response = (
        reply_meta.get("silent") is True
        or _to_str(reply_meta.get("silent")).strip().lower() == "true"
        or reply_meta.get("ui_visible") is False
        or _to_str(reply_meta.get("ui_visible")).strip().lower() == "false"
        or reply_data.get("silent") is True
        or _to_str(reply_data.get("silent")).strip().lower() == "true"
        or reply_data.get("ui_visible") is False
        or _to_str(reply_data.get("ui_visible")).strip().lower() == "false"
        or reply_payload.get("silent") is True
        or _to_str(reply_payload.get("silent")).strip().lower() == "true"
        or reply_payload.get("ui_visible") is False
        or _to_str(reply_payload.get("ui_visible")).strip().lower() == "false"
        or _to_str(reply_meta.get("intent") or reply_data.get("intent")).strip().lower() == "quote_image_collect"
    )
    if hidden_assistant_response:
        assistant_msg = {
            "id": None,
            "role": "assistant",
            "content": "",
            "metadata": reply_meta,
            "hidden": True,
        }
    elif db is not None:
        assistant_msg = await db_append_message(
            db,
            owner_user_id=owner_user_id,
            session_id=real_session_id,
            role="assistant",
            content=assistant_content,
            metadata=reply_meta,
        )
    else:
        assistant_msg = _store.append_message(
            owner_user_id=owner_user_id,
            session_id=real_session_id,
            role="assistant",
            content=assistant_content,
            metadata=reply_meta,
        )

    meta = assistant_msg.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    result_payload = {
        "session_id": real_session_id,
        "reply": _to_str(assistant_msg.get("content")),
        "intent": _to_str(meta.get("intent"), "chat") or "chat",
        "trace_id": _to_str(meta.get("trace_id"), _new_id()[:16]) or _new_id()[:16],
        "confidence": float(meta.get("confidence") or 0.0),
        "actions": meta.get("actions") if isinstance(meta.get("actions"), list) else [],
        "usage": None,
        "model": _to_str(model, "rule-engine") or "rule-engine",
        "data": meta.get("data") if isinstance(meta.get("data"), dict) else None,
        "silent": bool(meta.get("silent") is True or _to_str(meta.get("silent")).strip().lower() == "true"),
        "ui_visible": not (meta.get("ui_visible") is False or _to_str(meta.get("ui_visible")).strip().lower() == "false"),
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        "stream": None,
    }
    if hidden_assistant_response and db is not None and stable_user_message_id:
        await _db_store_cached_response_on_user_message(
            db,
            owner_user_id=owner_user_id,
            session_id=real_session_id,
            message_id=stable_user_message_id,
            response=result_payload,
        )
    return result_payload


__all__ = [
    "get_or_create_session",
    "create_session",
    "list_sessions",
    "delete_session",
    "recall_session_images",
    "get_session_messages",
    "list_messages",
    "db_create_session",
    "db_delete_session",
    "db_get_or_create_session",
    "db_get_session",
    "db_list_messages",
    "db_list_sessions",
    "db_recall_session_images",
    "schedule_async_quote_result_image_completion",
    "send_message",
]
