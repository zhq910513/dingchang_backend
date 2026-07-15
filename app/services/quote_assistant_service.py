# encoding: utf-8
from __future__ import annotations

import hashlib
import asyncio
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, desc, false as sql_false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, selectinload

from app.core.access_control import normalize_team_names, user_team_match_expr
from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_MARKET, ROLE_SALES, ROLE_SUPER_ADMIN
from app.models.image_file import ImageFile
from app.models.order import Order, OrderImage
from app.models.order_info import OrderInfo
from app.models.quote_assistant import (
    QuoteCase,
    QuoteCaseEvent,
    QuoteCaseImage,
    QuotePlatformAccountEvent,
    QuotePlatformAccountLoginTask,
    QuotePlatformAccountProfile,
    QuotePlatformAccountType,
    QuoteTask,
)
from app.models.user import User
from app.services.image_slot_classifier import (
    SLOT_KEYS,
    classify_image_slot,
    is_single_slot,
    slot_label,
)
from app.services.baidu_ocr import OcrCallError, OcrNotConfigured, call_ocr
from app.services.ocr_worker import _extract_by_type
from app.services.quote_platforms import runtime as quote_platform_runtime
from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.quote_secret_box import encrypt_json, encrypt_text
from app.services.storage import StorageService

TZ_BJ = timezone(timedelta(hours=8))
storage = StorageService()

RESULT_SUCCESS = "success"
RESULT_NEED_MORE = "need_more_info"
RESULT_NOT_READY = "not_ready"
RESULT_FAILED = "failed"

ACTIVE_CASE_STATUSES = ("collecting", "ready", "waiting_sms", "failed", "quoted")
ACTIVE_IMAGE_STATUS = "active"
SINGLE_REQUIRED_SLOTS = ("vehicle_cert", "idcard_front", "driving_license_main")
QUOTE_IMAGE_OCR_CLASSIFY_ENABLED = os.getenv("QUOTE_IMAGE_OCR_CLASSIFY_ENABLED", "1") == "1"
try:
    QUOTE_IMAGE_OCR_CALL_TIMEOUT_SECONDS = max(1.0, float(os.getenv("QUOTE_IMAGE_OCR_CALL_TIMEOUT_SECONDS", "2.5") or "2.5"))
except Exception:
    QUOTE_IMAGE_OCR_CALL_TIMEOUT_SECONDS = 2.5
try:
    QUOTE_IMAGE_OCR_TOTAL_TIMEOUT_SECONDS = max(1.0, float(os.getenv("QUOTE_IMAGE_OCR_TOTAL_TIMEOUT_SECONDS", "4") or "4"))
except Exception:
    QUOTE_IMAGE_OCR_TOTAL_TIMEOUT_SECONDS = 4.0
try:
    QUOTE_SMS_CODE_TTL_SECONDS = max(60, int(os.getenv("QUOTE_SMS_CODE_TTL_SECONDS", "600") or "600"))
except Exception:
    QUOTE_SMS_CODE_TTL_SECONDS = 600

OCR_SLOT_CANDIDATES: Tuple[Tuple[str, str, Optional[str]], ...] = (
    ("vehicle_cert", "vehicle_certificate", None),
    ("idcard_front", "idcard", "front"),
    ("idcard_back", "idcard", "back"),
    ("driving_license_main", "vehicle_license", "front"),
    ("driving_license_sub", "vehicle_license", "back"),
)

PLATFORM_ALIASES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "TP": ("太平洋", ("太平洋", "太保", "太平洋保险", "cpic", "tp")),
    "PICC": ("人保", ("人保", "中国人保", "人保财险", "picc")),
    "PA": ("平安", ("平安", "平安保险", "pingan", "pa")),
    "CL": ("国寿财", ("国寿财", "国寿", "人寿财", "中国人寿", "china life")),
    "DD": ("大地", ("大地", "大地保险")),
    "YG": ("阳光", ("阳光", "阳光保险")),
    "ZH": ("中华联合", ("中华联合", "中华")),
    "HA": ("华安", ("华安",)),
    "TA": ("天安", ("天安",)),
    "YA": ("永安", ("永安",)),
    "TPIC": ("太平", ("太平", "太平保险")),
}

DEFAULT_PLATFORM_CREDENTIAL_FIELDS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "login_phone",
        "label": "接收验证码手机号",
        "type": "phone",
        "required": True,
        "secret": False,
        "placeholder": "请输入平台接收短信验证码的手机号",
    },
    {
        "key": "account_username",
        "label": "平台账号",
        "type": "text",
        "required": False,
        "secret": False,
        "placeholder": "如平台需要账号密码登录，请填写",
    },
    {
        "key": "account_password",
        "label": "平台密码",
        "type": "password",
        "required": False,
        "secret": True,
        "placeholder": "如平台需要账号密码登录，请填写",
    },
)

PLATFORM_CREDENTIAL_FIELD_OVERRIDES: Dict[str, Tuple[Dict[str, Any], ...]] = {
    "TP": DEFAULT_PLATFORM_CREDENTIAL_FIELDS,
    "PICC": (
        DEFAULT_PLATFORM_CREDENTIAL_FIELDS[0],
        {**DEFAULT_PLATFORM_CREDENTIAL_FIELDS[1], "required": True},
        {**DEFAULT_PLATFORM_CREDENTIAL_FIELDS[2], "required": True},
    ),
    "PA": (
        DEFAULT_PLATFORM_CREDENTIAL_FIELDS[0],
        {**DEFAULT_PLATFORM_CREDENTIAL_FIELDS[1], "required": True},
        {**DEFAULT_PLATFORM_CREDENTIAL_FIELDS[2], "required": True},
    ),
}

ACCOUNT_LOGIN_NOT_LOGGED_IN = "not_logged_in"
ACCOUNT_LOGIN_LOGGING_IN = "logging_in"
ACCOUNT_LOGIN_NEEDS_CODE = "needs_code"
ACCOUNT_LOGIN_AUTHENTICATED = "authenticated"
ACCOUNT_LOGIN_EXPIRED = "expired"
ACCOUNT_LOGIN_FAILED = "failed"
ACCOUNT_LOGIN_DISABLED = "disabled"

ACCOUNT_QUOTA_UNKNOWN = "unknown"
ACCOUNT_QUOTA_AVAILABLE = "available"
ACCOUNT_QUOTA_WARNING = "warning"
ACCOUNT_QUOTA_FULL = "full"
ACCOUNT_QUOTA_RESET = "reset"

LOGIN_TASK_PENDING = "pending"
LOGIN_TASK_RUNNING = "running"
LOGIN_TASK_NEEDS_CODE = "needs_code"
LOGIN_TASK_SUCCESS = "success"
LOGIN_TASK_FAILED = "failed"
LOGIN_TASK_EXPIRED = "expired"

RUNTIME_LOGIN_SUCCESS_STATUSES = {"success", "ok", "authenticated"}
RUNTIME_LOGIN_CHALLENGE_STATUSES = {
    "needs_code",
    "need_code",
    "sms_required",
    "requires_sms",
    "challenge_required",
    "requires_challenge",
}
RUNTIME_QUOTE_SUCCESS_STATUSES = {"success", "ok", "quoted"}
RUNTIME_QUOTA_FULL_STATUSES = {"quota_full", "quota_exceeded", "limit_exceeded"}

ACCOUNT_SENSITIVE_FIELDS = {
    "platform_code",
    "account_type_name",
    "account_username",
    "account_password",
    "login_phone",
    "email",
    "account_owner_name",
    "auto_login",
}


def _platform_display_name(platform_code: str, platform_name: Optional[str] = None) -> str:
    code = _to_str(platform_code).strip().upper()
    if platform_name:
        return _to_str(platform_name).strip()
    return PLATFORM_ALIASES.get(code, (code, ()))[0] if code else ""


def _platform_credential_fields(platform_code: str) -> List[Dict[str, Any]]:
    code = _to_str(platform_code).strip().upper()
    fields = PLATFORM_CREDENTIAL_FIELD_OVERRIDES.get(code) or DEFAULT_PLATFORM_CREDENTIAL_FIELDS
    return [dict(item) for item in fields]


def _platform_schema(platform_code: str, platform_name: Optional[str] = None) -> Dict[str, Any]:
    code = _to_str(platform_code).strip().upper()
    return {
        "platform_code": code,
        "platform_name": _platform_display_name(code, platform_name),
        "fields": _platform_credential_fields(code),
    }

def _alias_matches_text(alias: str, low_text: str) -> bool:
    token = _to_str(alias).strip().lower()
    if not token:
        return False
    # Short ASCII aliases must stand alone; otherwise they can hit passwords or usernames.
    if re.search(r"[a-z0-9]", token):
        pattern = rf"(?<![a-z0-9_.@/\\-]){re.escape(token)}(?![a-z0-9_.@/\\-])"
        return bool(re.search(pattern, low_text, flags=re.IGNORECASE))
    return token in low_text


REQUIRED_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("owner_name", "车主姓名"),
    ("owner_phone", "车主手机号"),
    ("id_number", "身份证号"),
    ("plate_no", "车牌号"),
    ("vin", "车架号/VIN"),
    ("engine_no", "发动机号"),
    ("vehicle_model", "品牌型号/车型"),
)


def _now() -> datetime:
    return datetime.now(TZ_BJ).replace(tzinfo=None)


def _new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def _to_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, Decimal):
            return float(value)
        return float(value)
    except Exception:
        return default


def _fmt_dt(value: Any) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, datetime):
        try:
            return value.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return _to_str(value) or None
    return _to_str(value) or None


def _loaded_value(obj: Any, key: str, default: Any = None) -> Any:
    values = getattr(obj, "__dict__", None)
    if isinstance(values, dict):
        if key in values:
            return values.get(key)
        if "_sa_instance_state" in values:
            return default
    return getattr(obj, key, default)


def _norm_text(value: Any) -> str:
    text = _to_str(value).replace("\u3000", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _json_obj(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _mk_action(label: str, type_: str = "suggest", target: Optional[str] = None, **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "label": label}
    if target:
        out["target"] = target
    if extra:
        out["extra"] = extra
    return out


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


def _case_no() -> str:
    return "QA" + datetime.now(TZ_BJ).strftime("%Y%m%d") + uuid.uuid4().hex[:8].upper()


def _mask_phone(phone: Any) -> str:
    s = re.sub(r"\D+", "", _to_str(phone))
    if len(s) == 11:
        return f"{s[:3]}****{s[-4:]}"
    if len(s) >= 4:
        return "*" * max(0, len(s) - 4) + s[-4:]
    return "业务员手机号"


def _credential_aad(owner_user_id: int, platform_code: str) -> str:
    return f"quote_platform_account:{int(owner_user_id)}:{_to_str(platform_code).strip().upper()}"


def _clean_secret_value(value: Any, max_len: int = 256) -> str:
    text = _to_str(value).strip().strip("，,。.;；")
    text = re.sub(r"\s+", "", text)
    return text[:max_len]


def _extract_labeled_value(text: str, labels: Tuple[str, ...], *, max_len: int = 128) -> Optional[str]:
    label_expr = "|".join(re.escape(x) for x in sorted(labels, key=len, reverse=True) if x)
    if not label_expr:
        return None
    pattern = rf"(?:{label_expr})\s*[:：=]?\s*([^\s,，,;；。]+)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    value = _clean_secret_value(match.group(1), max_len=max_len)
    return value or None


_LOGIN_PHONE_HINTS = (
    "登录手机号",
    "登陆手机号",
    "验证码手机号",
    "短信手机号",
    "平台手机号",
    "业务员手机号",
    "业务员手机",
    "登录手机",
    "登陆手机",
    "login_phone",
    "phone",
    "mobile",
)

_ACCOUNT_USERNAME_HINTS = ("登录账号", "登陆账号", "平台账号", "账号", "账户", "用户名", "user", "username", "account")
_PASSWORD_HINTS = ("登录密码", "登陆密码", "平台密码", "密码", "口令", "password", "pwd")


_OWNER_PHONE_HINTS = (
    "车主手机号",
    "车主手机",
    "车主电话",
    "客户手机号",
    "客户手机",
    "被保险人手机号",
    "被保人手机号",
    "投保人手机号",
    "联系电话",
)

_OWNER_NAME_HINTS = (
    "被保险人姓名",
    "投保人姓名",
    "联系人姓名",
    "车主姓名",
    "车主名称",
    "姓名",
)


def _has_login_phone_hint(text: str) -> bool:
    low = text.lower()
    return any(h.lower() in low for h in _LOGIN_PHONE_HINTS) or any(
        h in text for h in ("登录", "登陆", "验证码", "短信", "平台账号", "平台密码")
    )


def redact_quote_sensitive_text(text: Any, *, hide_unlabeled_sms_code: bool = False) -> str:
    """Return chat-display/audit-safe text without leaking quote credentials."""

    raw = _to_str(text)
    if not raw:
        return ""

    out = raw
    if hide_unlabeled_sms_code and re.fullmatch(r"\s*\d{4,8}\s*", out):
        return "[短信验证码已隐藏]"
    out = re.sub(
        r"((?:登录密码|登陆密码|平台密码|密码|口令|password|pwd)\s*[:：=]?\s*)([^\s,，,;；。]+)",
        lambda m: m.group(1) + "[已隐藏]",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"((?:短信验证码|验证码|校验码|code)\s*[:：=]?\s*)(\d{4,8})",
        lambda m: m.group(1) + "[已隐藏]",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"((?:登录手机号|登陆手机号|验证码手机号|短信手机号|平台手机号|业务员手机号|业务员手机|登录手机|登陆手机|login_phone|phone|mobile)\s*[:：=]?\s*)(1\d{10})",
        lambda m: m.group(1) + _mask_phone(m.group(2)),
        out,
        flags=re.IGNORECASE,
    )
    return out


def sanitize_quote_entities(entities: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    safe = dict(entities or {})
    safe.pop("account_password", None)
    login_phone = _to_str(safe.pop("login_phone", "")).strip()
    if login_phone:
        safe["login_phone_mask"] = _mask_phone(login_phone)
    return safe


def _redact_platform_credentials_for_signal(text: Any) -> str:
    out = _to_str(text)
    for labels in (_LOGIN_PHONE_HINTS, _ACCOUNT_USERNAME_HINTS, _PASSWORD_HINTS):
        label_expr = "|".join(re.escape(x) for x in labels if x)
        if not label_expr:
            continue
        out = re.sub(
            rf"((?:{label_expr})\s*[:：=]?\s*)([^\s,，,;；。]+)",
            lambda m: m.group(1) + "[VALUE]",
            out,
            flags=re.IGNORECASE,
        )
    return out


def _extract_platform_credentials(text: Any, *, allow_loose_phone: bool = False) -> Dict[str, str]:
    t = _norm_text(text)
    low = t.lower()
    has_owner_phone_hint = any(h in t for h in _OWNER_PHONE_HINTS)
    has_login_hint = any(
        key in low
        for key in (
            "登录",
            "登陆",
            "验证码",
            "短信",
            "平台手机号",
            "业务员手机号",
            "业务员手机",
            "平台账号",
            "登录账号",
            "登陆账号",
            "用户名",
            "密码",
            "password",
            "phone",
            "mobile",
        )
    )
    out: Dict[str, str] = {}

    phone = _extract_labeled_value(t, _LOGIN_PHONE_HINTS, max_len=32)
    if not phone and (has_login_hint or allow_loose_phone) and not has_owner_phone_hint:
        m = re.search(r"\b(1\d{10})\b", t)
        phone = m.group(1) if m else None
    if phone:
        digits = re.sub(r"\D+", "", phone)
        if len(digits) == 11:
            out["login_phone"] = digits

    username = _extract_labeled_value(
        t,
        _ACCOUNT_USERNAME_HINTS,
        max_len=128,
    )
    if username:
        out["account_username"] = username

    password = _extract_labeled_value(
        t,
        _PASSWORD_HINTS,
        max_len=256,
    )
    if password:
        out["account_password"] = password

    return out


def detect_platform_credential_signal(text: Any) -> Dict[str, Any]:
    signal = detect_quote_signal(_redact_platform_credentials_for_signal(text))
    credentials = _extract_platform_credentials(text)
    t = _norm_text(text)
    has_keyword = any(
        key in t.lower()
        for key in ("登录", "登陆", "验证码", "短信", "手机号", "手机", "账号", "账户", "用户名", "密码", "password")
    )
    entities = _json_obj(signal.get("entities")).copy()
    entities.update(credentials)
    return {
        "is_credential": bool(credentials and has_keyword),
        "entities": entities,
        "credentials": credentials,
    }


def _normalize_platform_code_name(platform_code: Any, platform_name: Optional[Any] = None) -> Tuple[str, str]:
    code = _to_str(platform_code).strip().upper()
    if not code:
        raise ValueError("请选择平台")
    name = _platform_display_name(code, _to_str(platform_name).strip() or None)
    return code, name or code


def _normalize_account_type_name(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value).strip())
    return text[:64]


def _normalize_login_phone(value: Any) -> Tuple[Optional[str], Optional[str]]:
    raw = _to_str(value).strip()
    if not raw:
        return None, None
    digits = re.sub(r"\D+", "", raw)
    if len(digits) != 11:
        raise ValueError("绑定手机号格式不正确，请填写 11 位手机号")
    return digits, _mask_phone(digits)


def _normalize_email(value: Any) -> Optional[str]:
    text = _to_str(value).strip()
    if not text:
        return None
    if len(text) > 128 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        raise ValueError("邮箱格式不正确")
    return text


def _account_profile_aad(row: QuotePlatformAccountProfile) -> str:
    return (
        f"quote_platform_account_profile:{int(row.owner_user_id or 0)}:"
        f"{_to_str(row.platform_code).strip().upper()}:{_to_str(row.account_username).strip()}"
    )


def _account_type_payload(row: QuotePlatformAccountType) -> Dict[str, Any]:
    return {
        "id": _loaded_value(row, "id"),
        "platform_code": _loaded_value(row, "platform_code"),
        "platform_name": _loaded_value(row, "platform_name"),
        "type_name": _loaded_value(row, "type_name"),
        "description": _loaded_value(row, "description") or "",
        "match_rules": _json_obj(_loaded_value(row, "match_rules_json")),
        "is_default": bool(_loaded_value(row, "is_default")),
        "enabled": bool(_loaded_value(row, "enabled")),
        "created_at": _fmt_dt(_loaded_value(row, "created_at")),
        "updated_at": _fmt_dt(_loaded_value(row, "updated_at")),
    }


def _credential_public_payload(row: Optional[QuotePlatformAccountProfile]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    login_phone = _loaded_value(row, "login_phone")
    password_ciphertext = _loaded_value(row, "password_ciphertext")
    return {
        "id": _loaded_value(row, "id"),
        "platform_code": _loaded_value(row, "platform_code"),
        "platform_name": _loaded_value(row, "platform_name"),
        "account_type_id": _loaded_value(row, "account_type_id"),
        "account_type_name": _loaded_value(row, "account_type_name") or "",
        "account_username": _loaded_value(row, "account_username"),
        "has_password": bool(_to_str(password_ciphertext).strip()),
        "login_phone_mask": _loaded_value(row, "login_phone_mask") or _mask_phone(login_phone),
        "has_login_phone": bool(_to_str(login_phone).strip()),
        "email": _loaded_value(row, "email") or "",
        "account_owner_user_id": _loaded_value(row, "account_owner_user_id"),
        "account_owner_name": _loaded_value(row, "account_owner_name") or "",
        "auto_login": bool(_loaded_value(row, "auto_login")),
        "enabled": bool(_loaded_value(row, "enabled")),
        "login_status": _loaded_value(row, "login_status") or ACCOUNT_LOGIN_NOT_LOGGED_IN,
        "quota_status": _loaded_value(row, "quota_status") or ACCOUNT_QUOTA_UNKNOWN,
        "quota_reset_at": _fmt_dt(_loaded_value(row, "quota_reset_at")),
        "browser_env_key": _loaded_value(row, "browser_env_key"),
        "last_login_at": _fmt_dt(_loaded_value(row, "last_login_at")),
        "last_check_at": _fmt_dt(_loaded_value(row, "last_check_at")),
        "last_used_at": _fmt_dt(_loaded_value(row, "last_used_at")),
        "last_error": _loaded_value(row, "last_error") or "",
        "created_at": _fmt_dt(_loaded_value(row, "created_at")),
        "updated_at": _fmt_dt(_loaded_value(row, "updated_at")),
    }


def _account_event_snapshot(row: Optional[QuotePlatformAccountProfile]) -> Dict[str, Any]:
    return _credential_public_payload(row) or {}


async def _add_account_event(
    db: AsyncSession,
    *,
    account: QuotePlatformAccountProfile,
    event_type: str,
    operator_user_id: Optional[int],
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
    message: Optional[str] = None,
) -> None:
    db.add(
        QuotePlatformAccountEvent(
            account_id=int(account.id),
            event_type=_to_str(event_type).strip()[:32] or "update",
            operator_user_id=operator_user_id,
            before_json=before or {},
            after_json=after or _account_event_snapshot(account),
            message=_to_str(message).strip()[:1024] or None,
        )
    )


def list_quote_platforms() -> List[Dict[str, Any]]:
    return [
        {"platform_code": code, "platform_name": name, "aliases": list(aliases)}
        for code, (name, aliases) in PLATFORM_ALIASES.items()
    ]


async def list_platform_account_types(
    db: AsyncSession,
    *,
    owner_user_id: int,
    platform_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if owner_user_id <= 0:
        return []
    stmt = select(QuotePlatformAccountType).where(QuotePlatformAccountType.owner_user_id == int(owner_user_id))
    code = _to_str(platform_code).strip().upper()
    if code:
        stmt = stmt.where(QuotePlatformAccountType.platform_code == code)
    rows = (
        await db.execute(
            stmt.order_by(
                QuotePlatformAccountType.platform_code.asc(),
                QuotePlatformAccountType.is_default.desc(),
                QuotePlatformAccountType.type_name.asc(),
                QuotePlatformAccountType.id.desc(),
            )
        )
    ).scalars().all()
    return [_account_type_payload(row) for row in rows]


async def _get_or_create_account_type(
    db: AsyncSession,
    *,
    owner_user_id: int,
    platform_code: str,
    platform_name: str,
    type_name: str,
) -> Optional[QuotePlatformAccountType]:
    name = _normalize_account_type_name(type_name)
    if not name:
        return None
    row = (
        await db.execute(
            select(QuotePlatformAccountType)
            .where(
                QuotePlatformAccountType.owner_user_id == int(owner_user_id),
                QuotePlatformAccountType.platform_code == platform_code,
                QuotePlatformAccountType.type_name == name,
            )
            .limit(1)
        )
    ).scalars().first()
    if row:
        if not bool(row.enabled):
            row.enabled = True
            row.updated_at = _now()
        if not row.platform_name:
            row.platform_name = platform_name
        return row
    row = QuotePlatformAccountType(
        owner_user_id=int(owner_user_id),
        platform_code=platform_code,
        platform_name=platform_name,
        type_name=name,
        description=None,
        match_rules_json={},
        is_default=False,
        enabled=True,
    )
    db.add(row)
    await db.flush()
    return row


async def save_platform_account_type(
    db: AsyncSession,
    *,
    owner_user_id: int,
    values: Dict[str, Any],
    type_id: Optional[int] = None,
) -> QuotePlatformAccountType:
    if owner_user_id <= 0:
        raise ValueError("无法识别当前用户")
    code, platform_name = _normalize_platform_code_name(values.get("platform_code"), values.get("platform_name"))
    type_name = _normalize_account_type_name(values.get("type_name"))
    if not type_name:
        raise ValueError("账号类型不能为空")
    row = None
    if type_id:
        row = (
            await db.execute(
                select(QuotePlatformAccountType)
                .where(
                    QuotePlatformAccountType.id == int(type_id),
                    QuotePlatformAccountType.owner_user_id == int(owner_user_id),
                )
                .limit(1)
            )
        ).scalars().first()
        if not row:
            raise ValueError("账号类型不存在或无权修改")
    else:
        row = await _get_or_create_account_type(
            db,
            owner_user_id=owner_user_id,
            platform_code=code,
            platform_name=platform_name,
            type_name=type_name,
        )
    row.platform_code = code
    row.platform_name = platform_name
    row.type_name = type_name
    row.description = _to_str(values.get("description")).strip()[:255] or None
    row.match_rules_json = values.get("match_rules") if isinstance(values.get("match_rules"), dict) else {}
    row.enabled = bool(values.get("enabled", True))
    row.is_default = bool(values.get("is_default", False))
    row.updated_at = _now()
    if row.is_default:
        await db.execute(
            update(QuotePlatformAccountType)
            .where(
                QuotePlatformAccountType.owner_user_id == int(owner_user_id),
                QuotePlatformAccountType.platform_code == code,
                QuotePlatformAccountType.id != int(row.id),
            )
            .values(is_default=False)
        )
    await db.flush()
    return row


async def get_platform_account_profile(
    db: AsyncSession,
    *,
    owner_user_id: int,
    account_id: int,
) -> Optional[QuotePlatformAccountProfile]:
    if owner_user_id <= 0 or account_id <= 0:
        return None
    return (
        await db.execute(
            select(QuotePlatformAccountProfile)
            .where(
                QuotePlatformAccountProfile.id == int(account_id),
                QuotePlatformAccountProfile.owner_user_id == int(owner_user_id),
            )
            .limit(1)
        )
    ).scalars().first()


async def list_platform_account_profiles(
    db: AsyncSession,
    *,
    owner_user_id: int,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if owner_user_id <= 0:
        return {"total": 0, "items": []}
    filters = filters or {}
    stmt = select(QuotePlatformAccountProfile).where(QuotePlatformAccountProfile.owner_user_id == int(owner_user_id))
    code = _to_str(filters.get("platform_code")).strip().upper()
    if code:
        stmt = stmt.where(QuotePlatformAccountProfile.platform_code == code)
    account_type = _normalize_account_type_name(filters.get("account_type_name") or filters.get("account_type"))
    if account_type:
        stmt = stmt.where(QuotePlatformAccountProfile.account_type_name == account_type)
    if filters.get("enabled") is not None:
        stmt = stmt.where(QuotePlatformAccountProfile.enabled == bool(filters.get("enabled")))
    login_status = _to_str(filters.get("login_status")).strip()
    if login_status:
        stmt = stmt.where(QuotePlatformAccountProfile.login_status == login_status)
    quota_status = _to_str(filters.get("quota_status")).strip()
    if quota_status:
        stmt = stmt.where(QuotePlatformAccountProfile.quota_status == quota_status)
    keyword = _to_str(filters.get("keyword")).strip()
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                QuotePlatformAccountProfile.platform_name.like(like),
                QuotePlatformAccountProfile.account_type_name.like(like),
                QuotePlatformAccountProfile.account_username.like(like),
                QuotePlatformAccountProfile.login_phone.like(like),
                QuotePlatformAccountProfile.email.like(like),
                QuotePlatformAccountProfile.account_owner_name.like(like),
            )
        )
    total = (await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(
                QuotePlatformAccountProfile.platform_code.asc(),
                QuotePlatformAccountProfile.account_type_name.asc(),
                QuotePlatformAccountProfile.enabled.desc(),
                desc(QuotePlatformAccountProfile.updated_at),
                desc(QuotePlatformAccountProfile.id),
            )
        )
    ).scalars().all()
    return {"total": int(total or 0), "items": [_credential_public_payload(row) for row in rows]}


def _normalize_account_profile_input(values: Dict[str, Any], *, is_create: bool) -> Dict[str, Any]:
    raw = values if isinstance(values, dict) else {}
    code, platform_name = _normalize_platform_code_name(raw.get("platform_code"), raw.get("platform_name"))
    username = _to_str(raw.get("account_username")).strip()
    if not username:
        raise ValueError("账号不能为空")
    if len(username) > 128:
        raise ValueError("账号不能超过 128 个字符")
    password = _to_str(raw.get("account_password")).strip()
    if is_create and not password:
        raise ValueError("密码不能为空")
    if password and len(password) > 256:
        raise ValueError("密码不能超过 256 个字符")
    login_phone, login_phone_mask = _normalize_login_phone(raw.get("login_phone"))
    return {
        "platform_code": code,
        "platform_name": platform_name,
        "account_type_name": _normalize_account_type_name(raw.get("account_type_name") or raw.get("account_type")) or None,
        "account_username": username,
        "account_password": password or None,
        "login_phone": login_phone,
        "login_phone_mask": login_phone_mask,
        "email": _normalize_email(raw.get("email")),
        "account_owner_user_id": _safe_int(raw.get("account_owner_user_id"), 0) or None,
        "account_owner_name": _to_str(raw.get("account_owner_name")).strip()[:64] or None,
        "auto_login": bool(raw.get("auto_login", True)),
        "enabled": bool(raw.get("enabled", True)),
    }


def _enabled_account_sensitive_changes(row: QuotePlatformAccountProfile, incoming: Dict[str, Any]) -> List[str]:
    changes: List[str] = []
    for key, old_value, new_value in (
        ("platform_code", row.platform_code, incoming["platform_code"]),
        ("account_type_name", row.account_type_name or None, incoming["account_type_name"]),
        ("account_username", row.account_username, incoming["account_username"]),
        ("login_phone", row.login_phone or None, incoming["login_phone"]),
        ("email", row.email or None, incoming["email"]),
        ("account_owner_name", row.account_owner_name or None, incoming["account_owner_name"]),
        ("auto_login", bool(row.auto_login), bool(incoming["auto_login"])),
    ):
        if old_value != new_value:
            changes.append(key)
    if incoming.get("account_password"):
        changes.append("account_password")
    return sorted(set(changes))


async def create_platform_account_profile(
    db: AsyncSession,
    *,
    owner_user_id: int,
    values: Dict[str, Any],
    operator_user_id: Optional[int] = None,
) -> QuotePlatformAccountProfile:
    if owner_user_id <= 0:
        raise ValueError("无法识别当前用户")
    incoming = _normalize_account_profile_input(values, is_create=True)
    account_type = await _get_or_create_account_type(
        db,
        owner_user_id=owner_user_id,
        platform_code=incoming["platform_code"],
        platform_name=incoming["platform_name"],
        type_name=incoming["account_type_name"] or "",
    )
    row = QuotePlatformAccountProfile(
        owner_user_id=int(owner_user_id),
        platform_code=incoming["platform_code"],
        platform_name=incoming["platform_name"],
        account_type_id=account_type.id if account_type else None,
        account_type_name=incoming["account_type_name"],
        account_username=incoming["account_username"],
        password_ciphertext="pending",
        login_phone=incoming["login_phone"],
        login_phone_mask=incoming["login_phone_mask"],
        email=incoming["email"],
        account_owner_user_id=incoming["account_owner_user_id"],
        account_owner_name=incoming["account_owner_name"],
        auto_login=incoming["auto_login"],
        enabled=incoming["enabled"],
        login_status=ACCOUNT_LOGIN_NOT_LOGGED_IN if incoming["enabled"] else ACCOUNT_LOGIN_DISABLED,
        quota_status=ACCOUNT_QUOTA_UNKNOWN,
        browser_env_key=f"{incoming['platform_code'].lower()}_{int(owner_user_id)}_{uuid.uuid4().hex[:12]}",
        credential_payload={"schema_version": 2, "secret_storage": "encrypted"},
        secret_payload_ciphertext=None,
    )
    db.add(row)
    await db.flush()
    row.password_ciphertext = encrypt_text(incoming["account_password"], aad=_account_profile_aad(row))
    await _add_account_event(
        db,
        account=row,
        event_type="create",
        operator_user_id=operator_user_id,
        before={},
        after=_account_event_snapshot(row),
        message="创建平台账号",
    )
    await db.flush()
    return row


async def update_platform_account_profile(
    db: AsyncSession,
    *,
    owner_user_id: int,
    account_id: int,
    values: Dict[str, Any],
    operator_user_id: Optional[int] = None,
    confirm_enabled_edit: bool = False,
) -> QuotePlatformAccountProfile:
    row = await get_platform_account_profile(db, owner_user_id=owner_user_id, account_id=account_id)
    if not row:
        raise ValueError("平台账号不存在或无权修改")
    incoming = _normalize_account_profile_input(values, is_create=False)
    if "login_phone" not in (values or {}):
        incoming["login_phone"] = row.login_phone
        incoming["login_phone_mask"] = row.login_phone_mask
    before = _account_event_snapshot(row)
    sensitive_changes = _enabled_account_sensitive_changes(row, incoming)
    if bool(row.enabled) and sensitive_changes and not confirm_enabled_edit:
        raise ValueError("该账号当前已启用，修改平台、类型、账号、密码、手机号、邮箱、归属人或自动登录前需要确认")
    account_type = await _get_or_create_account_type(
        db,
        owner_user_id=owner_user_id,
        platform_code=incoming["platform_code"],
        platform_name=incoming["platform_name"],
        type_name=incoming["account_type_name"] or "",
    )
    row.platform_code = incoming["platform_code"]
    row.platform_name = incoming["platform_name"]
    row.account_type_id = account_type.id if account_type else None
    row.account_type_name = incoming["account_type_name"]
    row.account_username = incoming["account_username"]
    if incoming.get("account_password"):
        row.password_ciphertext = encrypt_text(incoming["account_password"], aad=_account_profile_aad(row))
        row.login_status = ACCOUNT_LOGIN_NOT_LOGGED_IN
        row.last_error = None
    row.login_phone = incoming["login_phone"]
    row.login_phone_mask = incoming["login_phone_mask"]
    row.email = incoming["email"]
    row.account_owner_user_id = incoming["account_owner_user_id"]
    row.account_owner_name = incoming["account_owner_name"]
    row.auto_login = incoming["auto_login"]
    row.enabled = incoming["enabled"]
    if not row.enabled:
        row.login_status = ACCOUNT_LOGIN_DISABLED
    elif row.login_status == ACCOUNT_LOGIN_DISABLED:
        row.login_status = ACCOUNT_LOGIN_NOT_LOGGED_IN
    row.credential_payload = {**_json_obj(row.credential_payload), "schema_version": 2, "secret_storage": "encrypted"}
    row.updated_at = _now()
    await _add_account_event(
        db,
        account=row,
        event_type="update",
        operator_user_id=operator_user_id,
        before=before,
        after=_account_event_snapshot(row),
        message="更新平台账号",
    )
    await db.flush()
    return row


def _platform_account_context(account: QuotePlatformAccountProfile) -> PlatformAccountContext:
    return PlatformAccountContext(
        platform_code=_to_str(account.platform_code).strip().upper() or "STUB",
        platform_name=_to_str(account.platform_name).strip() or _to_str(account.platform_code).strip().upper() or "STUB",
        account_id=int(account.id or 0),
        account_username=_to_str(account.account_username).strip(),
        account_type_name=_to_str(account.account_type_name).strip(),
        browser_env_key=_to_str(account.browser_env_key).strip(),
        payload={
            "login_phone": _to_str(account.login_phone).strip(),
            "login_phone_mask": account.login_phone_mask or _mask_phone(account.login_phone),
            "email": _to_str(account.email).strip(),
            "account_owner_name": _to_str(account.account_owner_name).strip(),
            "credential_payload": _json_obj(account.credential_payload),
        },
    )


def _runtime_result_payload(result: Optional[PlatformRuntimeResult]) -> Dict[str, Any]:
    if result is None:
        return {}
    return {
        "status": result.status,
        "message": result.message,
        "data": _json_obj(result.data),
        "challenge_type": result.challenge_type,
        "challenge_prompt": result.challenge_prompt,
    }


def _runtime_status(result: Optional[PlatformRuntimeResult]) -> str:
    if result is None:
        return ""
    return _to_str(result.status).strip().lower()


def _runtime_detail(result: Optional[PlatformRuntimeResult], default_message: str) -> str:
    status = _runtime_status(result)
    message = _to_str(getattr(result, "message", "") if result is not None else "").strip()
    if message and status:
        return f"{message}（平台返回状态：{status}）"
    if message:
        return message
    if status:
        return f"{default_message}：平台返回状态 {status}"
    return f"{default_message}：平台未返回状态"


def _is_runtime_login_success(status: str) -> bool:
    return status in RUNTIME_LOGIN_SUCCESS_STATUSES


def _is_runtime_challenge(status: str) -> bool:
    return status in RUNTIME_LOGIN_CHALLENGE_STATUSES


def _is_runtime_quote_success(status: str) -> bool:
    return status in RUNTIME_QUOTE_SUCCESS_STATUSES


def _is_runtime_quota_full(status: str) -> bool:
    return status in RUNTIME_QUOTA_FULL_STATUSES


def _platform_context_from_public_payload(
    payload: Dict[str, Any],
    *,
    platform_code: str,
    platform_name: str,
) -> PlatformAccountContext:
    safe = _json_obj(payload)
    return PlatformAccountContext(
        platform_code=_to_str(safe.get("platform_code") or platform_code).strip().upper() or "STUB",
        platform_name=_to_str(safe.get("platform_name") or platform_name).strip() or platform_code or "STUB",
        account_id=_safe_int(safe.get("id"), 0),
        account_username=_to_str(safe.get("account_username")).strip(),
        account_type_name=_to_str(safe.get("account_type_name")).strip(),
        browser_env_key=_to_str(safe.get("browser_env_key")).strip(),
        payload={
            "login_phone_mask": _to_str(safe.get("login_phone_mask")).strip(),
            "email": _to_str(safe.get("email")).strip(),
            "account_owner_name": _to_str(safe.get("account_owner_name")).strip(),
        },
    )


def _login_task_payload(row: QuotePlatformAccountLoginTask) -> Dict[str, Any]:
    return {
        "id": _loaded_value(row, "id"),
        "account_id": _loaded_value(row, "account_id"),
        "platform_code": _loaded_value(row, "platform_code"),
        "platform_name": _loaded_value(row, "platform_name"),
        "status": _loaded_value(row, "status"),
        "challenge_type": _loaded_value(row, "challenge_type"),
        "challenge_prompt": _loaded_value(row, "challenge_prompt"),
        "challenge_payload": _json_obj(_loaded_value(row, "challenge_payload")),
        "trace_id": _loaded_value(row, "trace_id"),
        "error_detail": _loaded_value(row, "error_detail") or "",
        "started_at": _fmt_dt(_loaded_value(row, "started_at")),
        "finished_at": _fmt_dt(_loaded_value(row, "finished_at")),
        "expires_at": _fmt_dt(_loaded_value(row, "expires_at")),
        "created_at": _fmt_dt(_loaded_value(row, "created_at")),
        "updated_at": _fmt_dt(_loaded_value(row, "updated_at")),
    }


async def start_platform_account_login(
    db: AsyncSession,
    *,
    owner_user_id: int,
    account_id: int,
    operator_user_id: Optional[int] = None,
) -> Dict[str, Any]:
    account = await get_platform_account_profile(db, owner_user_id=owner_user_id, account_id=account_id)
    if not account:
        raise ValueError("平台账号不存在或无权登录")
    if not bool(account.enabled):
        raise ValueError("该平台账号已停用，请先启用后再登录")
    trace_id = _new_trace_id()
    now = _now()
    task = QuotePlatformAccountLoginTask(
        account_id=int(account.id),
        owner_user_id=int(owner_user_id),
        platform_code=account.platform_code,
        platform_name=account.platform_name,
        status=LOGIN_TASK_RUNNING,
        challenge_type=None,
        challenge_prompt=None,
        challenge_payload={},
        trace_id=trace_id,
        started_at=now,
        expires_at=now + timedelta(seconds=QUOTE_SMS_CODE_TTL_SECONDS),
    )
    db.add(task)
    account.login_status = ACCOUNT_LOGIN_LOGGING_IN
    account.last_error = None
    account.last_check_at = now
    account.updated_at = now
    await db.flush()

    runtime_result = await quote_platform_runtime.login(_platform_account_context(account))
    status = _runtime_status(runtime_result)
    if _is_runtime_challenge(status):
        task.status = LOGIN_TASK_NEEDS_CODE
        task.challenge_type = runtime_result.challenge_type or "sms"
        phone_mask = account.login_phone_mask or _mask_phone(account.login_phone)
        prompt = runtime_result.challenge_prompt or f"请输入{account.platform_name or account.platform_code}的验证码"
        if phone_mask and phone_mask not in prompt:
            prompt = f"{prompt}（发送至 {phone_mask}）"
        task.challenge_prompt = prompt
        task.challenge_payload = {
            "phone_mask": phone_mask or "",
            "code_length": "4-8",
            "platform_runtime": _runtime_result_payload(runtime_result),
        }
        account.login_status = ACCOUNT_LOGIN_NEEDS_CODE
    elif _is_runtime_login_success(status):
        task.status = LOGIN_TASK_SUCCESS
        task.finished_at = _now()
        account.login_status = ACCOUNT_LOGIN_AUTHENTICATED
        account.last_login_at = task.finished_at
        account.quota_status = ACCOUNT_QUOTA_AVAILABLE if account.quota_status == ACCOUNT_QUOTA_UNKNOWN else account.quota_status
    else:
        task.status = LOGIN_TASK_FAILED
        task.error_detail = _runtime_detail(runtime_result, "平台登录失败")
        task.finished_at = _now()
        account.login_status = ACCOUNT_LOGIN_FAILED
        account.last_error = task.error_detail
    account.last_check_at = _now()
    account.updated_at = _now()
    await _add_account_event(
        db,
        account=account,
        event_type="login",
        operator_user_id=operator_user_id,
        before={},
        after=_account_event_snapshot(account),
        message="启动平台登录流程",
    )
    await db.flush()
    return {"account": _credential_public_payload(account), "login_task": _login_task_payload(task)}


async def submit_platform_account_login_challenge(
    db: AsyncSession,
    *,
    owner_user_id: int,
    task_id: int,
    code: str,
    operator_user_id: Optional[int] = None,
) -> Dict[str, Any]:
    clean_code = re.sub(r"\s+", "", _to_str(code))
    if not re.fullmatch(r"\d{4,8}", clean_code):
        raise ValueError("验证码格式不正确，请输入 4-8 位数字")
    task = (
        await db.execute(
            select(QuotePlatformAccountLoginTask)
            .where(
                QuotePlatformAccountLoginTask.id == int(task_id),
                QuotePlatformAccountLoginTask.owner_user_id == int(owner_user_id),
            )
            .limit(1)
        )
    ).scalars().first()
    if not task:
        raise ValueError("登录任务不存在或无权操作")
    account = await get_platform_account_profile(db, owner_user_id=owner_user_id, account_id=int(task.account_id))
    if not account:
        raise ValueError("登录账号不存在或无权操作")
    if task.status != LOGIN_TASK_NEEDS_CODE:
        raise ValueError("当前登录任务不在等待验证码状态")
    if task.expires_at and _now() > task.expires_at:
        task.status = LOGIN_TASK_EXPIRED
        task.error_detail = "验证码已过期"
        task.finished_at = _now()
        account.login_status = ACCOUNT_LOGIN_EXPIRED
        account.last_error = task.error_detail
        await db.flush()
        raise ValueError("验证码已过期，请重新点击登录")
    before = _account_event_snapshot(account)

    runtime_result = await quote_platform_runtime.submit_challenge(_platform_account_context(account), clean_code)
    status = _runtime_status(runtime_result)
    task.challenge_payload = {
        **_json_obj(task.challenge_payload),
        "platform_runtime": _runtime_result_payload(runtime_result),
    }
    if _is_runtime_login_success(status):
        task.status = LOGIN_TASK_SUCCESS
        task.error_detail = None
        account.login_status = ACCOUNT_LOGIN_AUTHENTICATED
        account.last_error = None
        account.last_login_at = _now()
        account.quota_status = ACCOUNT_QUOTA_AVAILABLE if account.quota_status == ACCOUNT_QUOTA_UNKNOWN else account.quota_status
    elif _is_runtime_challenge(status):
        task.status = LOGIN_TASK_NEEDS_CODE
        task.challenge_type = runtime_result.challenge_type or task.challenge_type or "sms"
        task.challenge_prompt = runtime_result.challenge_prompt or runtime_result.message or task.challenge_prompt
        task.error_detail = task.challenge_prompt
        account.login_status = ACCOUNT_LOGIN_NEEDS_CODE
        account.last_error = task.error_detail
    else:
        task.status = LOGIN_TASK_FAILED
        task.error_detail = _runtime_detail(runtime_result, "验证码校验失败")
        account.login_status = ACCOUNT_LOGIN_FAILED
        account.last_error = task.error_detail
    if task.status != LOGIN_TASK_NEEDS_CODE:
        task.finished_at = _now()
    task.updated_at = _now()
    account.last_check_at = _now()
    account.updated_at = _now()
    await _add_account_event(
        db,
        account=account,
        event_type="login",
        operator_user_id=operator_user_id,
        before=before,
        after=_account_event_snapshot(account),
        message="提交平台登录验证码",
    )
    await db.flush()
    return {"account": _credential_public_payload(account), "login_task": _login_task_payload(task)}


async def _select_quote_platform_account(
    db: AsyncSession,
    *,
    owner_user_id: int,
    platform_code: str,
    account_type_name: Optional[str] = None,
) -> Optional[QuotePlatformAccountProfile]:
    code = _to_str(platform_code).strip().upper()
    if owner_user_id <= 0 or not code:
        return None
    stmt = select(QuotePlatformAccountProfile).where(
        QuotePlatformAccountProfile.owner_user_id == int(owner_user_id),
        QuotePlatformAccountProfile.platform_code == code,
        QuotePlatformAccountProfile.enabled == True,  # noqa: E712
        QuotePlatformAccountProfile.quota_status != ACCOUNT_QUOTA_FULL,
    )
    type_name = _normalize_account_type_name(account_type_name)
    if type_name:
        stmt = stmt.where(QuotePlatformAccountProfile.account_type_name == type_name)
    rows = (
        await db.execute(
            stmt.order_by(
                (QuotePlatformAccountProfile.login_status == ACCOUNT_LOGIN_AUTHENTICATED).desc(),
                QuotePlatformAccountProfile.auto_login.desc(),
                QuotePlatformAccountProfile.last_used_at.asc(),
                QuotePlatformAccountProfile.id.asc(),
            )
        )
    ).scalars().all()
    return rows[0] if rows else None


async def _mark_platform_account_used(
    db: AsyncSession,
    *,
    account_id: Optional[int],
    owner_user_id: int,
    login_state: str,
    sms_at: bool = False,
) -> None:
    if not account_id:
        return
    row = await get_platform_account_profile(db, owner_user_id=owner_user_id, account_id=int(account_id))
    if not row:
        return
    row.login_status = login_state or row.login_status
    row.last_used_at = _now()
    if sms_at:
        row.last_check_at = _now()
    if login_state == ACCOUNT_LOGIN_AUTHENTICATED:
        row.last_login_at = _now()
        row.last_error = None
        if row.quota_status == ACCOUNT_QUOTA_UNKNOWN:
            row.quota_status = ACCOUNT_QUOTA_AVAILABLE
    row.updated_at = _now()
    await db.flush()


def _extract_account_type_from_quote_text(text: Any, platform_name: str) -> Optional[str]:
    t = _norm_text(text)
    platform = _to_str(platform_name).strip()
    if not t or not platform:
        return None
    body = t.replace(" ", "")
    body = body.replace(platform, "")
    body = re.sub(r"报价.*$", "", body)
    body = body.strip("，。；;:")
    return _normalize_account_type_name(body) or None


def detect_quote_signal(text: Any) -> Dict[str, Any]:
    t = _norm_text(text)
    low = t.lower()
    platform_low = _redact_platform_credentials_for_signal(t).lower()
    entities: Dict[str, Any] = {}

    for code, (name, aliases) in PLATFORM_ALIASES.items():
        if any(_alias_matches_text(alias, platform_low) for alias in aliases):
            entities["platform_code"] = code
            entities["platform_name"] = name
            break

    order_id = _extract_order_id(t)
    if order_id:
        entities["order_id"] = order_id

    extracted = extract_quote_fields(t)
    entities.update({k: v for k, v in extracted.items() if v})

    is_quote = bool(re.search(r"报价|\bquote\b", low))
    return {"is_quote": bool(is_quote), "entities": entities}


def looks_like_sms_code(text: Any) -> bool:
    t = _norm_text(text)
    if re.fullmatch(r"\d{4,8}", t):
        return True
    return bool(re.search(r"(?:验证码|短信|校验码|code)\D{0,8}(\d{4,8})", t, flags=re.IGNORECASE))


def _extract_sms_code(text: Any) -> Optional[str]:
    t = _norm_text(text)
    if re.fullmatch(r"\d{4,8}", t):
        return t
    m = re.search(r"(?:验证码|短信|校验码|code)\D{0,8}(\d{4,8})", t, flags=re.IGNORECASE)
    return m.group(1) if m else None


def _extract_order_id(text: str) -> Optional[int]:
    for pattern in (
        r"(?:订单号|订单|order)\s*[:：]?\s*(\d{1,12})",
        r"\border\s*[:：]?\s*(\d{1,12})\b",
    ):
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            value = _safe_int(m.group(1), 0)
            if value > 0:
                return value
    return None


def extract_quote_fields(text: Any) -> Dict[str, Any]:
    t = _norm_text(text)
    up = t.upper()
    out: Dict[str, Any] = {}

    owner_phone = _extract_labeled_value(t, _OWNER_PHONE_HINTS, max_len=32)
    if owner_phone:
        digits = re.sub(r"\D+", "", owner_phone)
        if len(digits) == 11:
            out["owner_phone"] = digits
    elif not _has_login_phone_hint(t):
        phone = re.search(r"\b(1\d{10})\b", t)
        if phone:
            out["owner_phone"] = phone.group(1)

    id_no = re.search(r"\b(\d{17}[\dXx])\b", t)
    if id_no:
        out["id_number"] = id_no.group(1).upper()

    plate = re.search(r"([\u4e00-\u9fa5][A-Z][A-Z0-9]{4,7})", up)
    if plate:
        out["plate_no"] = plate.group(1)

    vin = re.search(r"(?:VIN|车架号|车辆识别代号)\s*[:：]?\s*([A-HJ-NPR-Z0-9]{11,20})", up, flags=re.IGNORECASE)
    if not vin:
        vin = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", up)
    if vin:
        out["vin"] = vin.group(1).upper()

    engine = re.search(r"(?:发动机号|发动机号码|发动机)\s*[:：]?\s*([A-Z0-9\-]{4,32})", up, flags=re.IGNORECASE)
    if engine:
        out["engine_no"] = engine.group(1).upper()

    owner_name = _extract_labeled_value(t, _OWNER_NAME_HINTS, max_len=64)
    if not owner_name:
        name = re.search(r"(?:车主|姓名|被保人|被保险人|投保人|联系人)\s*[:：]\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,40})", t)
        if not name:
            name = re.search(r"(?:车主|姓名)\s+([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,40})(?=\s|$)", t)
        owner_name = name.group(1).strip() if name else None
    if owner_name and owner_name not in {"姓名", "车主", "手机号", "电话", "车牌号", "身份证号"}:
        out["owner_name"] = owner_name.strip()

    model = re.search(r"(?:车型|品牌型号|车辆型号)\s*[:：]?\s*([^\s,，,;；。]{2,60})", t)
    if model:
        out["vehicle_model"] = model.group(1).strip()

    exp_date = re.search(r"(?:保险到期|到期日|保险止期)\s*[:：]?\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})", t)
    if exp_date:
        out["insurance_expire_date"] = exp_date.group(1).replace("年", "-").replace("月", "-").replace("日", "")

    return out


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


def _json_text_col(col, path: str):
    from sqlalchemy import func

    return func.json_unquote(func.json_extract(col, path))


async def _find_order(
    db: AsyncSession,
    *,
    ctx: Dict[str, Any],
    order_id: Optional[int] = None,
    plate_no: Optional[str] = None,
    owner_phone: Optional[str] = None,
    owner_name: Optional[str] = None,
) -> Optional[Order]:
    stmt = (
        select(Order)
        .options(
            lazyload("*"),
            selectinload(Order.order_info),
            selectinload(Order.images).selectinload(OrderImage.image_file),
        )
        .limit(1)
    )

    if order_id:
        stmt = stmt.where(Order.id == int(order_id))
    else:
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
        if owner_phone:
            stmt = stmt.join(OrderInfo, OrderInfo.order_id == Order.id, isouter=True)
            clauses.append(OrderInfo.owner_phone == owner_phone)
        if not clauses:
            return None
        stmt = stmt.where(and_(*clauses)).order_by(desc(Order.id))

    acl = _order_acl_clause_for_ctx(ctx)
    if acl is not None:
        stmt = stmt.where(acl)

    return (await db.execute(stmt)).scalars().first()


async def _get_or_create_case(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    order: Optional[Order],
    platform_code: str,
    platform_name: str,
) -> QuoteCase:
    order_id = _safe_int(getattr(order, "id", 0), 0) or None

    base_stmt = select(QuoteCase).where(
        QuoteCase.owner_user_id == owner_user_id,
        QuoteCase.status.in_(ACTIVE_CASE_STATUSES),
    )

    case = None
    if order_id:
        exact_stmt = base_stmt.where(QuoteCase.order_id == order_id)
        case = (await db.execute(exact_stmt.order_by(desc(QuoteCase.id)).limit(1))).scalars().first()
        if not case and session_id:
            draft_stmt = base_stmt.where(QuoteCase.session_id == session_id, QuoteCase.order_id.is_(None))
            case = (await db.execute(draft_stmt.order_by(desc(QuoteCase.id)).limit(1))).scalars().first()
    elif session_id:
        stmt = base_stmt.where(QuoteCase.session_id == session_id, QuoteCase.order_id.is_(None))
        case = (await db.execute(stmt.order_by(desc(QuoteCase.id)).limit(1))).scalars().first()
    else:
        stmt = base_stmt.where(QuoteCase.order_id.is_(None))
        case = (await db.execute(stmt.order_by(desc(QuoteCase.id)).limit(1))).scalars().first()

    if case:
        changed = False
        if platform_code and case.platform_code != platform_code:
            case.platform_code = platform_code
            changed = True
        if platform_name and case.platform_name != platform_name:
            case.platform_name = platform_name
            changed = True
        if session_id and case.session_id != session_id:
            case.session_id = session_id
            changed = True
        if order_id and not case.order_id:
            case.order_id = order_id
            case.source_type = "existing_order"
            changed = True
        if changed:
            case.updated_at = _now()
            await db.flush()
        return case

    case = QuoteCase(
        case_no=_case_no(),
        owner_user_id=owner_user_id,
        session_id=session_id,
        order_id=order_id,
        source_type="existing_order" if order_id else "new_order_draft",
        platform_code=platform_code or None,
        platform_name=platform_name or None,
        status="collecting",
        quote_count=0,
        draft_order_data={},
        normalized_data={},
        missing_requirements=[],
    )
    db.add(case)
    await db.flush()
    return case


async def _latest_active_case(db: AsyncSession, *, owner_user_id: int, session_id: Optional[str]) -> Optional[QuoteCase]:
    stmt = select(QuoteCase).where(QuoteCase.owner_user_id == owner_user_id)
    if session_id:
        stmt = stmt.where(QuoteCase.session_id == session_id)
    stmt = stmt.order_by(desc(QuoteCase.id)).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def _add_event(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    event_type: str,
    role: Optional[str] = None,
    content: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    safe_content = redact_quote_sensitive_text(content) if content else None
    db.add(
        QuoteCaseEvent(
            quote_case_id=case.id,
            owner_user_id=owner_user_id,
            session_id=case.session_id,
            event_type=event_type,
            role=role,
            content=safe_content,
            payload=payload or {},
        )
    )


async def _ensure_image_file(db: AsyncSession, image: Dict[str, Any]) -> Optional[int]:
    storage_key = _to_str(image.get("storage_key")).strip().lstrip("/")
    if not storage_key:
        return None

    row = (await db.execute(select(ImageFile).where(ImageFile.storage_key == storage_key).limit(1))).scalars().first()
    if row:
        return _safe_int(row.id, 0) or None

    row = ImageFile(
        sha256=None,
        md5=_to_str(image.get("md5")).strip().lower() or None,
        original_name=_to_str(image.get("original_name")).strip() or None,
        content_type=_to_str(image.get("content_type")).strip() or None,
        storage_key=storage_key,
        url=_db_safe_image_url(image=image, storage_key=storage_key),
        etag=_to_str(image.get("etag")).strip() or None,
        size=max(0, _safe_int(image.get("size"), 0)),
    )
    db.add(row)
    await db.flush()
    return _safe_int(row.id, 0) or None


def _db_safe_image_url(*, image: Optional[Dict[str, Any]] = None, storage_key: str = "", fallback: str = "") -> str:
    """
    Keep DB URLs short and stable.

    Frontend upload responses can carry temporary signed BOS URLs. Those include
    authorization/security-token query params and easily exceed VARCHAR(512), so
    they must never be persisted as the quote image display URL.
    """
    k = _to_str(storage_key).strip().lstrip("/")
    if k:
        try:
            url = storage.object_public_url(k)
            if 0 < len(url) <= 512:
                return url
        except Exception:
            pass

    img = image or {}
    raw = _to_str(img.get("image_url") or img.get("url") or img.get("preview_url") or fallback).strip()
    if not raw or raw.startswith("blob:"):
        return ""
    raw = raw.split("?", 1)[0]
    return raw[:512]


async def _set_single_active(db: AsyncSession, *, case_id: int, slot_key: str, keep_image_id: int) -> None:
    if not is_single_slot(slot_key):
        return
    await db.execute(
        update(QuoteCaseImage)
        .where(
            QuoteCaseImage.quote_case_id == case_id,
            QuoteCaseImage.confirmed_slot_key == slot_key,
            QuoteCaseImage.status == ACTIVE_IMAGE_STATUS,
            QuoteCaseImage.id != keep_image_id,
        )
        .values(status="replaced", updated_at=_now())
    )


def _image_meta_key(image: Dict[str, Any]) -> str:
    return _to_str(image.get("storage_key")).strip().lstrip("/")


def _collect_context_images(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    images: List[Dict[str, Any]] = []
    seen = set()

    for key in ("images", "uploaded_images", "quote_images"):
        raw = (ctx or {}).get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            storage_key = _image_meta_key(item)
            if not storage_key or storage_key in seen:
                continue
            seen.add(storage_key)
            images.append(item)

    page_ctx = (ctx or {}).get("page_context")
    if isinstance(page_ctx, dict):
        for item in page_ctx.get("uploaded_images") or []:
            if not isinstance(item, dict):
                continue
            storage_key = _image_meta_key(item)
            if not storage_key or storage_key in seen:
                continue
            seen.add(storage_key)
            images.append(item)

    return images


def _image_url_for_ocr(image: Dict[str, Any], storage_key: str) -> str:
    url = _to_str(image.get("url") or image.get("preview_url") or image.get("image_url")).strip()
    if url:
        return url
    if not storage_key:
        return ""
    try:
        return storage.object_url_for_display(storage_key, signed=True, expires_in=900, allow_fallback_public=True)
    except Exception:
        try:
            return storage.object_public_url(storage_key)
        except Exception:
            return ""


def _ocr_candidates_for_image(provided_slot: str, storage_key: str) -> Tuple[Tuple[str, str, Optional[str]], ...]:
    provided = _to_str(provided_slot).strip()
    key = "/" + _to_str(storage_key).strip().lstrip("/").lower()
    if provided == "vehicle_cert" or "/cert/" in key:
        return (("vehicle_cert", "vehicle_certificate", None),)
    if provided in ("idcard_front", "idcard_back") or "/idcard/" in key:
        return (
            ("idcard_front", "idcard", "front"),
            ("idcard_back", "idcard", "back"),
        )
    if provided in ("driving_license_main", "driving_license_sub") or "/dl/" in key:
        return (
            ("driving_license_main", "vehicle_license", "front"),
            ("driving_license_sub", "vehicle_license", "back"),
        )
    return OCR_SLOT_CANDIDATES


async def _classify_image_with_optional_ocr(
    *,
    image: Dict[str, Any],
    provided_slot: str,
    storage_key: str,
):
    context_hint = _to_str(image.get("context_hint")).strip()
    raw_ocr_text = image.get("ocr_text") or image.get("ocr_text_sample")
    text_for_classification = raw_ocr_text or context_hint
    has_actual_ocr_text = bool(raw_ocr_text) and (
        not context_hint or _to_str(raw_ocr_text).strip() != context_hint
    )
    classification = classify_image_slot(
        provided_slot_key=provided_slot,
        original_name=image.get("original_name"),
        storage_key=storage_key,
        ocr_text=text_for_classification,
        raw_payload=image.get("raw") or image.get("ocr_raw"),
    )
    if (
        not QUOTE_IMAGE_OCR_CLASSIFY_ENABLED
        or classification.confidence >= 0.78
        or has_actual_ocr_text
        or image.get("ocr_raw")
    ):
        return classification, None, {}

    image_url = _image_url_for_ocr(image, storage_key)
    if not image_url:
        return classification, None, {}

    best = classification
    best_raw: Optional[Dict[str, Any]] = None
    best_extracted: Dict[str, Any] = {}
    best_score = float(classification.confidence or 0.0)
    deadline = asyncio.get_running_loop().time() + float(QUOTE_IMAGE_OCR_TOTAL_TIMEOUT_SECONDS)

    for slot_key, api_type, side in _ocr_candidates_for_image(provided_slot, storage_key):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(call_ocr, api_type, image_url, side, True),
                timeout=min(float(QUOTE_IMAGE_OCR_CALL_TIMEOUT_SECONDS), remaining),
            )
        except (OcrNotConfigured, OcrCallError, ValueError, RuntimeError):
            continue
        except asyncio.TimeoutError:
            break
        except Exception:
            continue

        candidate = classify_image_slot(
            provided_slot_key=provided_slot,
            original_name=image.get("original_name"),
            storage_key=storage_key,
            ocr_text=raw,
        )
        score = float(candidate.confidence or 0.0)
        if candidate.predicted_slot_key == slot_key:
            score += 0.04
        if score > best_score:
            best = candidate
            best_raw = raw
            best_score = score
            try:
                best_extracted = _extract_by_type(api_type, raw)
            except Exception:
                best_extracted = {}
        if best.predicted_slot_key == slot_key and best.confidence >= 0.82:
            break

    if best_raw is not None:
        features = dict(best.text_features or {})
        features["ocr_classify"] = {
            "enabled": True,
            "api_type": next((api for slot, api, _ in OCR_SLOT_CANDIDATES if slot == best.predicted_slot_key), None),
            "used": True,
        }
        object.__setattr__(best, "text_features", features)
    return best, best_raw, best_extracted


async def _attach_uploaded_images(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    images: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    attached: List[Dict[str, Any]] = []

    for image in images:
        storage_key = _image_meta_key(image)
        if not storage_key:
            continue

        provided_slot = _to_str(image.get("provided_slot_key") or image.get("slot_key")).strip()
        classification, ocr_raw, extracted_fields = await _classify_image_with_optional_ocr(
            image=image,
            provided_slot=provided_slot,
            storage_key=storage_key,
        )
        predicted = classification.predicted_slot_key
        confirmed = predicted
        if classification.confidence < 0.65 and provided_slot in SLOT_KEYS:
            confirmed = provided_slot
        if confirmed not in SLOT_KEYS:
            confirmed = "related"

        image_file_id = await _ensure_image_file(db, {**image, "storage_key": storage_key})
        existing = (
            await db.execute(
                select(QuoteCaseImage)
                .where(QuoteCaseImage.quote_case_id == case.id, QuoteCaseImage.storage_key == storage_key)
                .limit(1)
            )
        ).scalars().first()

        if existing:
            existing.provided_slot_key = provided_slot or existing.provided_slot_key
            existing.predicted_slot_key = predicted
            existing.confirmed_slot_key = confirmed
            existing.confidence = Decimal(str(round(classification.confidence, 4)))
            existing.method = classification.method
            existing.reason = classification.reason[:512]
            existing.status = ACTIVE_IMAGE_STATUS
            existing.image_file_id = image_file_id or existing.image_file_id
            existing.image_url = _db_safe_image_url(image=image, storage_key=storage_key, fallback=existing.image_url)
            existing.original_name = _to_str(image.get("original_name") or existing.original_name) or None
            existing.content_type = _to_str(image.get("content_type") or existing.content_type) or None
            existing.md5 = _to_str(image.get("md5") or existing.md5).strip().lower() or None
            existing.size = max(_safe_int(image.get("size"), 0), _safe_int(existing.size, 0))
            existing.ocr_text_sample = classification.ocr_text_sample
            existing.text_features = classification.text_features or {}
            existing.updated_at = _now()
            await db.flush()
            await _set_single_active(db, case_id=case.id, slot_key=confirmed, keep_image_id=existing.id)
            row = existing
        else:
            row = QuoteCaseImage(
                quote_case_id=case.id,
                image_file_id=image_file_id,
                provided_slot_key=provided_slot or None,
                predicted_slot_key=predicted,
                confirmed_slot_key=confirmed,
                confidence=Decimal(str(round(classification.confidence, 4))),
                method=classification.method,
                reason=classification.reason[:512],
                status=ACTIVE_IMAGE_STATUS,
                storage_key=storage_key,
                image_url=_db_safe_image_url(image=image, storage_key=storage_key),
                original_name=_to_str(image.get("original_name")).strip() or None,
                content_type=_to_str(image.get("content_type")).strip() or None,
                md5=_to_str(image.get("md5")).strip().lower() or None,
                size=max(0, _safe_int(image.get("size"), 0)),
                ocr_text_sample=classification.ocr_text_sample,
                text_features=classification.text_features or {},
                created_by=owner_user_id,
            )
            db.add(row)
            await db.flush()
            await _set_single_active(db, case_id=case.id, slot_key=confirmed, keep_image_id=row.id)

        attached.append(
            {
                "id": row.id,
                "storage_key": storage_key,
                "image_url": row.image_url,
                "url": row.image_url,
                "preview_url": row.image_url,
                "provided_slot_key": provided_slot or None,
                "predicted_slot_key": predicted,
                "confirmed_slot_key": confirmed,
                "confidence": round(classification.confidence, 4),
                "method": classification.method,
                "reason": classification.reason,
                "ocr_used": bool(ocr_raw),
                "extracted_fields": extracted_fields or {},
            }
        )

    if attached:
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="image",
            role="system",
            payload={"attached_images": attached},
        )
    return attached


async def _sync_order_images_to_case(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    order: Optional[Order],
) -> None:
    if not order:
        return

    images = sorted(getattr(order, "images", None) or [], key=lambda item: _safe_int(getattr(item, "id", 0), 0))
    latest_by_slot: Dict[str, int] = {}
    for img in images:
        slot_key = _to_str(getattr(img, "slot_key", "")).strip()
        storage_key = _to_str(getattr(img, "storage_key", "")).strip().lstrip("/")
        if not slot_key or not storage_key or slot_key not in SLOT_KEYS:
            continue

        latest_by_slot[slot_key] = _safe_int(getattr(img, "id", 0), 0)
        existing = (
            await db.execute(
                select(QuoteCaseImage)
                .where(QuoteCaseImage.quote_case_id == case.id, QuoteCaseImage.storage_key == storage_key)
                .limit(1)
            )
        ).scalars().first()
        image_file = getattr(img, "image_file", None)
        image_url = _db_safe_image_url(
            image={
                "image_url": getattr(img, "image_url", ""),
                "url": getattr(image_file, "url", "") if image_file is not None else "",
            },
            storage_key=storage_key,
        )

        if existing:
            existing.provided_slot_key = slot_key
            existing.predicted_slot_key = slot_key
            existing.confirmed_slot_key = slot_key
            existing.confidence = Decimal("1.0000")
            existing.method = "order_slot"
            existing.reason = "来源于已绑定订单槽位"
            existing.status = ACTIVE_IMAGE_STATUS
            existing.image_file_id = _safe_int(getattr(img, "image_file_id", 0), 0) or existing.image_file_id
            existing.image_url = image_url
            existing.updated_at = _now()
        else:
            db.add(
                QuoteCaseImage(
                    quote_case_id=case.id,
                    image_file_id=_safe_int(getattr(img, "image_file_id", 0), 0) or None,
                    provided_slot_key=slot_key,
                    predicted_slot_key=slot_key,
                    confirmed_slot_key=slot_key,
                    confidence=Decimal("1.0000"),
                    method="order_slot",
                    reason="来源于已绑定订单槽位",
                    status=ACTIVE_IMAGE_STATUS,
                    storage_key=storage_key,
                    image_url=image_url,
                    original_name=_to_str(getattr(image_file, "original_name", "")).strip() or None,
                    content_type=_to_str(getattr(image_file, "content_type", "")).strip() or None,
                    md5=_to_str(getattr(image_file, "md5", "")).strip().lower() or None,
                    size=max(0, _safe_int(getattr(image_file, "size", 0), 0)),
                    ocr_text_sample="",
                    text_features={"source": "order_image", "order_image_id": _safe_int(getattr(img, "id", 0), 0)},
                    created_by=owner_user_id,
                )
            )
    await db.flush()

    # Existing order may contain historical duplicated single-slot rows. Keep
    # them in the case image pool, but expose only the latest as active.
    for slot_key in SINGLE_REQUIRED_SLOTS + ("idcard_back", "driving_license_sub"):
        active_rows = (
            await db.execute(
                select(QuoteCaseImage)
                .where(
                    QuoteCaseImage.quote_case_id == case.id,
                    QuoteCaseImage.confirmed_slot_key == slot_key,
                    QuoteCaseImage.status == ACTIVE_IMAGE_STATUS,
                )
                .order_by(desc(QuoteCaseImage.id))
            )
        ).scalars().all()
        if len(active_rows) <= 1:
            continue
        keep_id = active_rows[0].id
        await _set_single_active(db, case_id=case.id, slot_key=slot_key, keep_image_id=keep_id)


def _order_data(order: Optional[Order]) -> Dict[str, Any]:
    if not order:
        return {}
    dd = _json_obj(getattr(order, "dynamic_data", None)).copy()
    info = getattr(order, "order_info", None)
    if info is not None:
        phone = _to_str(getattr(info, "owner_phone", "")).strip()
        if phone:
            dd.setdefault("owner_phone", phone)
        expire = getattr(info, "insurance_expire_date", None)
        if expire:
            dd.setdefault("insurance_expire_date", _to_str(expire))
    if dd.get("id_name") and not dd.get("owner_name"):
        dd["owner_name"] = dd.get("id_name")
    return dd


def _merge_data(*items: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items:
        for key, value in (item or {}).items():
            if value not in (None, ""):
                out[key] = value
    return out


async def _active_images_by_slot(db: AsyncSession, case_id: int) -> Dict[str, List[Dict[str, Any]]]:
    rows = (
        await db.execute(
            select(QuoteCaseImage)
            .where(QuoteCaseImage.quote_case_id == case_id, QuoteCaseImage.status == ACTIVE_IMAGE_STATUS)
            .order_by(QuoteCaseImage.confirmed_slot_key, desc(QuoteCaseImage.id))
        )
    ).scalars().all()

    out: Dict[str, List[Dict[str, Any]]] = {key: [] for key in SLOT_KEYS}
    for row in rows:
        slot_key = _to_str(row.confirmed_slot_key).strip()
        if slot_key not in out:
            slot_key = "related"
        image_url = _db_safe_image_url(storage_key=row.storage_key, fallback=row.image_url)
        out[slot_key].append(
            {
                "id": row.id,
                "storage_key": row.storage_key,
                "image_url": image_url,
                "url": image_url,
                "preview_url": image_url,
                "provided_slot_key": row.provided_slot_key,
                "predicted_slot_key": row.predicted_slot_key,
                "confirmed_slot_key": row.confirmed_slot_key,
                "confidence": _safe_float(row.confidence),
                "method": row.method,
                "reason": row.reason,
                "original_name": row.original_name,
            }
        )
    return out


def _missing_requirements(normalized_data: Dict[str, Any], images_by_slot: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    missing: List[Dict[str, Any]] = []

    for key, label in REQUIRED_FIELDS:
        if not _to_str(normalized_data.get(key)).strip():
            missing.append({"type": "field", "key": key, "label": label})

    for slot_key in SINGLE_REQUIRED_SLOTS:
        if not images_by_slot.get(slot_key):
            missing.append({"type": "image", "key": slot_key, "label": slot_label(slot_key)})

    return missing


def _snapshot_payload(
    *,
    case: QuoteCase,
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    return {
        "quote_case": {
            "id": case.id,
            "case_no": case.case_no,
            "source_type": case.source_type,
            "order_id": case.order_id,
            "session_id": case.session_id,
            "platform_code": case.platform_code,
            "platform_name": case.platform_name,
        },
        "normalized_data": normalized_data,
        "images_by_slot": images_by_slot,
    }


def _case_payload(
    *,
    case: QuoteCase,
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
    missing: List[Dict[str, Any]],
    attached_images: Optional[List[Dict[str, Any]]] = None,
    task: Optional[QuoteTask] = None,
    platform_account: Optional[QuotePlatformAccountProfile] = None,
) -> Dict[str, Any]:
    ready_slots = {key: len(value) for key, value in images_by_slot.items() if value}
    return {
        "quote_case": {
            "id": case.id,
            "case_no": case.case_no,
            "source_type": case.source_type,
            "status": case.status,
            "order_id": case.order_id,
            "session_id": case.session_id,
            "platform_code": case.platform_code,
            "platform_name": case.platform_name,
            "quote_count": case.quote_count,
            "current_task_id": case.current_task_id,
        },
        "normalized_data": normalized_data,
        "images_by_slot": images_by_slot,
        "ready_slots": ready_slots,
        "missing_requirements": missing,
        "attached_images": attached_images or [],
        "platform_account": _credential_public_payload(platform_account),
        "current_task": {
            "id": task.id,
            "status": task.status,
            "login_state": task.login_state,
            "sms_phone_mask": task.sms_phone_mask,
            "trace_id": task.trace_id,
        }
        if task
        else None,
    }


async def _find_waiting_task(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    include_expired: bool = False,
) -> Optional[Tuple[QuoteCase, QuoteTask]]:
    stmt = (
        select(QuoteCase, QuoteTask)
        .join(QuoteTask, QuoteTask.quote_case_id == QuoteCase.id)
        .where(
            QuoteCase.owner_user_id == owner_user_id,
            QuoteCase.status == "waiting_sms",
            QuoteTask.status == "waiting_sms",
            QuoteTask.login_state == "sms_required",
        )
    )
    if session_id:
        stmt = stmt.where(QuoteCase.session_id == session_id)
    stmt = stmt.order_by(desc(QuoteTask.id)).limit(1)
    row = (await db.execute(stmt)).first()
    if not row:
        return None
    task = row[1]
    if not include_expired and _is_sms_task_expired(task):
        return None
    return row[0], row[1]


def _is_sms_task_expired(task: QuoteTask) -> bool:
    base = getattr(task, "started_at", None) or getattr(task, "created_at", None)
    if not isinstance(base, datetime):
        return False
    return (_now() - base).total_seconds() > QUOTE_SMS_CODE_TTL_SECONDS


async def _cancel_waiting_tasks_for_case(
    db: AsyncSession,
    *,
    case: QuoteCase,
    reason: str,
    now: Optional[datetime] = None,
) -> int:
    ts = now or _now()
    active_waiting_tasks = (
        await db.execute(
            select(QuoteTask).where(
                QuoteTask.quote_case_id == case.id,
                QuoteTask.status == "waiting_sms",
                QuoteTask.login_state == "sms_required",
            )
        )
    ).scalars().all()

    cancelled = 0
    for task in active_waiting_tasks:
        task.status = "cancelled"
        task.login_state = "failed"
        task.error_detail = reason
        task.finished_at = ts
        task.updated_at = ts
        cancelled += 1

    if cancelled:
        case.current_task_id = None
        case.updated_at = ts
    return cancelled


async def has_waiting_sms_task(db: AsyncSession, ctx: Dict[str, Any]) -> bool:
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        return False
    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    return await _find_waiting_task(db, owner_user_id=owner_user_id, session_id=session_id) is not None


async def has_expired_waiting_sms_task(db: AsyncSession, ctx: Dict[str, Any]) -> bool:
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        return False
    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    pair = await _find_waiting_task(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        include_expired=True,
    )
    return bool(pair and _is_sms_task_expired(pair[1]))


async def has_quote_case_waiting_for_login_phone(db: AsyncSession, ctx: Dict[str, Any]) -> bool:
    return False


async def _start_sms_task(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    snapshot: Dict[str, Any],
    trace_id: str,
    platform_account: QuotePlatformAccountProfile,
) -> QuoteTask:
    waiting = (
        await db.execute(
            select(QuoteTask)
            .where(
                QuoteTask.quote_case_id == case.id,
                QuoteTask.platform_code == (case.platform_code or "STUB"),
                QuoteTask.status == "waiting_sms",
                QuoteTask.login_state == "sms_required",
            )
            .order_by(desc(QuoteTask.id))
            .limit(1)
        )
    ).scalars().first()
    if waiting:
        if _is_sms_task_expired(waiting):
            ts = _now()
            waiting.status = "failed"
            waiting.login_state = "failed"
            waiting.error_detail = "sms_code_expired"
            waiting.finished_at = ts
            waiting.updated_at = ts
            if case.current_task_id == waiting.id:
                case.current_task_id = None
            case.status = "ready"
            case.updated_at = ts
            await _add_event(
                db,
                case=case,
                owner_user_id=owner_user_id,
                event_type="task",
                role="system",
                payload={"task_id": waiting.id, "status": "failed", "reason": "sms_code_expired"},
            )
            await db.flush()
        else:
            case.status = "waiting_sms"
            case.current_task_id = waiting.id
            case.updated_at = _now()
            await db.flush()
            return waiting

    phone = _to_str(platform_account.login_phone).strip()
    account_snapshot = _credential_public_payload(platform_account) or {}
    task = QuoteTask(
        quote_case_id=case.id,
        platform_code=case.platform_code or "STUB",
        platform_name=case.platform_name,
        status="waiting_sms",
        login_state="sms_required",
        sms_phone_mask=_mask_phone(phone),
        trace_id=trace_id,
        request_payload={
            "mode": "stub",
            "login": "sms_required",
            "owner_user_id": owner_user_id,
            "platform_account": account_snapshot,
        },
        response_payload={},
        result_payload={},
        submitted_snapshot=snapshot,
        started_at=_now(),
    )
    db.add(task)
    await db.flush()
    await _mark_platform_account_used(
        db,
        account_id=platform_account.id,
        owner_user_id=owner_user_id,
        login_state=ACCOUNT_LOGIN_NEEDS_CODE,
        sms_at=True,
    )

    case.status = "waiting_sms"
    case.current_task_id = task.id
    case.updated_at = _now()
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={"task_id": task.id, "status": task.status, "trace_id": trace_id},
    )
    await db.flush()
    return task


def _fake_quote_result(snapshot: Dict[str, Any], *, platform_code: str, platform_name: str, trace_id: str) -> Dict[str, Any]:
    data = snapshot.get("normalized_data") if isinstance(snapshot, dict) else {}
    if not isinstance(data, dict):
        data = {}
    seed = "|".join(
        [
            _to_str(platform_code),
            _to_str(data.get("plate_no")),
            _to_str(data.get("vin")),
            _to_str(data.get("engine_no")),
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    base = int(digest[:8], 16)
    commercial = Decimal(1800 + base % 2400) / Decimal("1.00")
    compulsory = Decimal(760 + base % 260)
    vehicle_tax = Decimal(300 + base % 720)
    total = commercial + compulsory + vehicle_tax
    return {
        "mode": "stub",
        "status": "quoted",
        "platform_code": platform_code,
        "platform_name": platform_name,
        "trace_id": trace_id,
        "plate_no": data.get("plate_no"),
        "owner_name": data.get("owner_name"),
        "price_items": [
            {"name": "商业险", "amount": float(commercial)},
            {"name": "交强险", "amount": float(compulsory)},
            {"name": "车船税", "amount": float(vehicle_tax)},
        ],
        "premium_total": float(total),
        "remark": "本地联调假报价结果，后续替换为真实平台适配器返回。",
    }


async def _complete_waiting_task(
    db: AsyncSession,
    *,
    case: QuoteCase,
    task: QuoteTask,
    owner_user_id: int,
    sms_code: str,
) -> Tuple[str, Dict[str, Any]]:
    trace_id = task.trace_id or _new_trace_id()
    platform_code = task.platform_code or case.platform_code or "STUB"
    platform_name = task.platform_name or case.platform_name or platform_code
    snapshot = _json_obj(task.submitted_snapshot)
    account_payload = _json_obj(_json_obj(task.request_payload).get("platform_account"))
    account_id = _safe_int(account_payload.get("id"), 0) or None
    platform_account = (
        await get_platform_account_profile(db, owner_user_id=owner_user_id, account_id=account_id)
        if account_id
        else None
    )
    platform_ctx = (
        _platform_account_context(platform_account)
        if platform_account
        else _platform_context_from_public_payload(account_payload, platform_code=platform_code, platform_name=platform_name)
    )

    challenge_result = await quote_platform_runtime.submit_challenge(platform_ctx, sms_code)
    challenge_status = _runtime_status(challenge_result)
    if _is_runtime_challenge(challenge_status):
        task.status = "waiting_sms"
        task.login_state = "sms_required"
        task.error_detail = challenge_result.challenge_prompt or challenge_result.message or "平台要求继续验证码校验"
        task.response_payload = {"platform_challenge": _runtime_result_payload(challenge_result)}
        task.updated_at = _now()
        case.status = "waiting_sms"
        case.current_task_id = task.id
        case.updated_at = _now()
        if platform_account:
            platform_account.login_status = ACCOUNT_LOGIN_NEEDS_CODE
            platform_account.last_error = task.error_detail
            platform_account.last_check_at = _now()
            platform_account.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={"task_id": task.id, "status": "waiting_sms", "trace_id": trace_id, "reason": task.error_detail},
        )
        await db.commit()
        return (
            f"{platform_name}还需要继续验证：{task.error_detail}。请在聊天框输入新的 4-8 位验证码。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_NOT_READY,
                    message=task.error_detail,
                    entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                    payload={"quote_task": {"id": task.id, "status": task.status, "trace_id": trace_id}},
                ),
                "actions": [_mk_action("输入短信验证码")],
            },
        )

    if not _is_runtime_login_success(challenge_status):
        task.status = "failed"
        task.login_state = "failed"
        task.error_detail = _runtime_detail(challenge_result, "验证码校验失败")
        task.response_payload = {"platform_challenge": _runtime_result_payload(challenge_result)}
        task.finished_at = _now()
        task.updated_at = _now()
        case.status = "ready"
        case.current_task_id = None
        case.updated_at = _now()
        if platform_account:
            platform_account.login_status = ACCOUNT_LOGIN_FAILED
            platform_account.last_error = task.error_detail
            platform_account.last_check_at = _now()
            platform_account.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={"task_id": task.id, "status": "failed", "trace_id": trace_id, "reason": task.error_detail},
        )
        await db.commit()
        return (
            f"{platform_name}验证码校验失败：{task.error_detail}。请重新点击报价或账号登录后再试。",
            {
                "status": "failed",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_FAILED,
                    message=task.error_detail,
                    entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                    payload={"quote_task": {"id": task.id, "status": task.status, "trace_id": trace_id}},
                ),
            },
        )

    if platform_account:
        platform_account.login_status = ACCOUNT_LOGIN_AUTHENTICATED
        platform_account.last_error = None
        platform_account.last_login_at = _now()
        platform_account.last_check_at = _now()
        platform_account.updated_at = _now()
        if platform_account.quota_status == ACCOUNT_QUOTA_UNKNOWN:
            platform_account.quota_status = ACCOUNT_QUOTA_AVAILABLE

    quote_runtime_result = await quote_platform_runtime.quote(platform_ctx, snapshot)
    quote_status = _runtime_status(quote_runtime_result)
    if not _is_runtime_quote_success(quote_status):
        task.status = "failed"
        task.login_state = "authenticated"
        task.error_detail = _runtime_detail(quote_runtime_result, "平台报价失败")
        task.response_payload = {
            "stub": True,
            "sms_code_length": len(sms_code),
            "challenge": _runtime_result_payload(challenge_result),
            "quote": _runtime_result_payload(quote_runtime_result),
        }
        task.result_payload = {}
        task.finished_at = _now()
        task.updated_at = _now()
        case.status = "ready"
        case.current_task_id = task.id
        case.updated_at = _now()
        if platform_account:
            platform_account.last_error = task.error_detail
            platform_account.last_check_at = _now()
            platform_account.updated_at = _now()
            if _is_runtime_quota_full(quote_status):
                platform_account.quota_status = ACCOUNT_QUOTA_FULL
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={"task_id": task.id, "status": "failed", "trace_id": trace_id, "reason": task.error_detail},
        )
        await db.commit()
        return (
            f"{platform_name}报价失败：{task.error_detail}。请检查平台账号状态或报价资料后重试。",
            {
                "status": "failed",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_FAILED,
                    message=task.error_detail,
                    entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                    payload={"quote_task": {"id": task.id, "status": task.status, "trace_id": trace_id}},
                ),
                "actions": [_mk_action("查看当前材料状态"), _mk_action(f"{platform_name}报价")],
            },
        )
    result = _fake_quote_result(snapshot, platform_code=platform_code, platform_name=platform_name, trace_id=trace_id)

    task.status = "success"
    task.login_state = "authenticated"
    task.response_payload = {
        "stub": True,
        "sms_code_length": len(sms_code),
        "challenge": _runtime_result_payload(challenge_result),
        "quote": _runtime_result_payload(quote_runtime_result),
    }
    task.result_payload = result
    task.finished_at = _now()
    task.updated_at = _now()

    case.status = "quoted"
    case.quote_count = _safe_int(case.quote_count, 0) + 1
    case.current_task_id = task.id
    case.updated_at = _now()
    await _mark_platform_account_used(
        db,
        account_id=account_id,
        owner_user_id=owner_user_id,
        login_state=ACCOUNT_LOGIN_AUTHENTICATED,
    )

    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={"task_id": task.id, "status": "success", "trace_id": trace_id, "result": result},
    )
    await db.commit()

    reply = (
        f"{platform_name}报价流程已跑通（本地假数据）。\n"
        f"- 车牌：{result.get('plate_no') or '-'}\n"
        f"- 车主：{result.get('owner_name') or '-'}\n"
        f"- 商业险：{result['price_items'][0]['amount']:.2f}\n"
        f"- 交强险：{result['price_items'][1]['amount']:.2f}\n"
        f"- 车船税：{result['price_items'][2]['amount']:.2f}\n"
        f"- 合计：{result['premium_total']:.2f}"
    )
    payload = {
        "quote_case": {
            "id": case.id,
            "case_no": case.case_no,
            "status": case.status,
            "order_id": case.order_id,
            "source_type": case.source_type,
        },
        "quote_task": {
            "id": task.id,
            "status": task.status,
            "login_state": task.login_state,
            "trace_id": trace_id,
        },
        "quote_result": result,
    }
    return reply, {
        "status": "success",
        "intent": "quote",
        "trace_id": trace_id,
        "data": _mk_data(
            result_status=RESULT_SUCCESS,
            message="报价流程已完成（当前为平台假数据联调）",
            entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
            payload=payload,
        ),
        "actions": [
            _mk_action("查看当前材料状态"),
            _mk_action(f"{platform_name}报价"),
        ],
    }


async def _complete_quote_without_sms(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    snapshot: Dict[str, Any],
    trace_id: str,
    platform_account: QuotePlatformAccountProfile,
    login_mode: str,
) -> Tuple[str, Dict[str, Any]]:
    platform_code = case.platform_code or platform_account.platform_code or "STUB"
    platform_name = case.platform_name or platform_account.platform_name or platform_code
    platform_ctx = _platform_account_context(platform_account)
    quote_runtime_result = await quote_platform_runtime.quote(platform_ctx, snapshot)
    quote_status = _runtime_status(quote_runtime_result)
    if not _is_runtime_quote_success(quote_status):
        error_detail = _runtime_detail(quote_runtime_result, "平台报价失败")
        task = QuoteTask(
            quote_case_id=case.id,
            platform_code=platform_code,
            platform_name=platform_name,
            status="failed",
            login_state="authenticated",
            sms_phone_mask=platform_account.login_phone_mask,
            trace_id=trace_id,
            request_payload={
                "mode": "stub",
                "login": login_mode,
                "owner_user_id": owner_user_id,
                "platform_account": _credential_public_payload(platform_account),
            },
            response_payload={
                "stub": True,
                "login": login_mode,
                "quote": _runtime_result_payload(quote_runtime_result),
            },
            result_payload={},
            submitted_snapshot=snapshot,
            error_detail=error_detail,
            started_at=_now(),
            finished_at=_now(),
        )
        db.add(task)
        await db.flush()

        case.status = "ready"
        case.current_task_id = task.id
        case.updated_at = _now()
        platform_account.last_error = error_detail
        platform_account.last_check_at = _now()
        platform_account.updated_at = _now()
        if _is_runtime_quota_full(quote_status):
            platform_account.quota_status = ACCOUNT_QUOTA_FULL
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={"task_id": task.id, "status": "failed", "trace_id": trace_id, "reason": error_detail, "login_mode": login_mode},
        )
        await db.commit()

        payload = {
            "quote_case": {
                "id": case.id,
                "case_no": case.case_no,
                "status": case.status,
                "order_id": case.order_id,
                "source_type": case.source_type,
            },
            "quote_task": {
                "id": task.id,
                "status": task.status,
                "login_state": task.login_state,
                "trace_id": trace_id,
                "error_detail": error_detail,
            },
            "platform_account": _credential_public_payload(platform_account),
        }
        return (
            f"{platform_name}报价失败：{error_detail}。请检查平台账号状态或报价资料后重试。",
            {
                "status": "failed",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_FAILED,
                    message=error_detail,
                    entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                    payload=payload,
                ),
                "actions": [_mk_action("查看当前材料状态"), _mk_action(f"{platform_name}报价")],
            },
        )

    result = _fake_quote_result(snapshot, platform_code=platform_code, platform_name=platform_name, trace_id=trace_id)
    task = QuoteTask(
        quote_case_id=case.id,
        platform_code=platform_code,
        platform_name=platform_name,
        status="success",
        login_state="authenticated",
        sms_phone_mask=platform_account.login_phone_mask,
        trace_id=trace_id,
        request_payload={
            "mode": "stub",
            "login": login_mode,
            "owner_user_id": owner_user_id,
            "platform_account": _credential_public_payload(platform_account),
        },
        response_payload={
            "stub": True,
            "login": login_mode,
            "quote": _runtime_result_payload(quote_runtime_result),
        },
        result_payload=result,
        submitted_snapshot=snapshot,
        started_at=_now(),
        finished_at=_now(),
    )
    db.add(task)
    await db.flush()

    case.status = "quoted"
    case.quote_count = _safe_int(case.quote_count, 0) + 1
    case.current_task_id = task.id
    case.updated_at = _now()
    await _mark_platform_account_used(
        db,
        account_id=platform_account.id,
        owner_user_id=owner_user_id,
        login_state=ACCOUNT_LOGIN_AUTHENTICATED,
    )
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={"task_id": task.id, "status": "success", "trace_id": trace_id, "result": result, "login_mode": login_mode},
    )
    await db.commit()

    account_label = platform_account.account_type_name or platform_account.account_username or "默认账号"
    reply = (
        f"{platform_name}报价流程已跑通（本地假数据）。\n"
        f"使用账号：{account_label}\n"
        f"车牌：{result.get('plate_no') or '-'}\n"
        f"车主：{result.get('owner_name') or '-'}\n"
        f"商业险：{result['price_items'][0]['amount']:.2f}\n"
        f"交强险：{result['price_items'][1]['amount']:.2f}\n"
        f"车船税：{result['price_items'][2]['amount']:.2f}\n"
        f"合计：{result['premium_total']:.2f}"
    )
    payload = {
        "quote_case": {
            "id": case.id,
            "case_no": case.case_no,
            "status": case.status,
            "order_id": case.order_id,
            "source_type": case.source_type,
        },
        "quote_task": {
            "id": task.id,
            "status": task.status,
            "login_state": task.login_state,
            "trace_id": trace_id,
        },
        "platform_account": _credential_public_payload(platform_account),
        "quote_result": result,
    }
    return reply, {
        "status": "success",
        "intent": "quote",
        "trace_id": trace_id,
        "data": _mk_data(
            result_status=RESULT_SUCCESS,
            message="报价流程已完成（当前为平台假数据联调）",
            entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
            payload=payload,
        ),
        "actions": [_mk_action(f"{platform_name}报价")],
    }


async def handle_quote_images_message(
    db: AsyncSession,
    *,
    ctx: Dict[str, Any],
    entities: Dict[str, Any],
    text: str,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        return None

    images = _collect_context_images(ctx)
    if not images:
        return None

    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    merged_entities = dict(entities or {})
    signal = detect_quote_signal(text)
    if isinstance(signal.get("entities"), dict):
        merged_entities.update(signal["entities"])

    platform_code = _to_str(merged_entities.get("platform_code")).strip().upper()
    platform_name = _to_str(merged_entities.get("platform_name")).strip()
    if not platform_code and platform_name:
        platform_code = "STUB"
    if not platform_name and platform_code:
        platform_name = _platform_display_name(platform_code)

    order_id = _safe_int((ctx or {}).get("order_id"), 0) or _safe_int(merged_entities.get("order_id"), 0) or None
    extracted = extract_quote_fields(text)
    order = await _find_order(
        db,
        ctx=ctx,
        order_id=order_id,
        plate_no=_to_str(extracted.get("plate_no") or merged_entities.get("plate_no")).strip() or None,
        owner_phone=_to_str(extracted.get("owner_phone") or merged_entities.get("owner_phone")).strip() or None,
        owner_name=_to_str(extracted.get("owner_name") or merged_entities.get("owner_name")).strip() or None,
    )
    case = await _get_or_create_case(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        order=order,
        platform_code=platform_code,
        platform_name=platform_name,
    )

    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="chat",
        role="user",
        content=text,
        payload={"image_message": True, "image_count": len(images)},
    )

    normalized_data = _merge_data(_json_obj(case.normalized_data), _order_data(order), extracted)
    attached_images = await _attach_uploaded_images(db, case=case, owner_user_id=owner_user_id, images=images)
    extracted_from_images = _merge_data(
        *[
            _json_obj(item.get("extracted_fields"))
            for item in attached_images
            if isinstance(item, dict) and item.get("extracted_fields")
        ]
    )
    if extracted_from_images:
        normalized_data = _merge_data(normalized_data, extracted_from_images)

    cancelled_waiting_tasks = 0
    if attached_images:
        cancelled_waiting_tasks = await _cancel_waiting_tasks_for_case(
            db,
            case=case,
            reason="cancelled_by_material_change",
        )

    images_by_slot = await _active_images_by_slot(db, case.id)
    missing = _missing_requirements(normalized_data, images_by_slot)
    case.normalized_data = normalized_data
    case.draft_order_data = normalized_data
    case.missing_requirements = missing
    case.status = "ready" if not missing else "collecting"
    case.updated_at = _now()

    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="status",
        role="assistant",
        payload={
            "status": case.status,
            "attached_images": attached_images,
            "missing": missing,
            "cancelled_waiting_tasks": cancelled_waiting_tasks,
        },
    )
    await db.commit()

    moved = [
        f"{slot_label(x.get('provided_slot_key') or '')}->{slot_label(x.get('confirmed_slot_key') or '')}"
        for x in attached_images
        if x.get("provided_slot_key") != x.get("confirmed_slot_key")
    ]
    by_slot_count: Dict[str, int] = {}
    for sk, rows in (images_by_slot or {}).items():
        count = len(rows or [])
        if count:
            by_slot_count[sk] = count

    lines = [
        f"已收到 {len(attached_images)} 张图片，已自动识别并放入报价材料。",
    ]
    if by_slot_count:
        lines.append("- 当前有效材料：" + "、".join(f"{slot_label(k)}{v}张" for k, v in by_slot_count.items()))
    if moved:
        lines.append("- 已自动归位：" + "、".join(moved[:5]))
    if cancelled_waiting_tasks:
        lines.append("- 材料已更新，上一条等待中的验证码已作废；请重新输入平台名+报价触发新流程。")
    if missing:
        lines.append("- 仍缺少：" + "、".join(item.get("label") or item.get("key") for item in missing[:8]))
    lines.append("下一步请直接输入平台名+报价，例如：太平洋报价。")

    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=missing,
        attached_images=attached_images,
        platform_account=None,
    )
    return "\n".join(lines), {
        "status": "success",
        "intent": "quote_image_collect",
        "trace_id": _new_trace_id(),
        "data": _mk_data(
            result_status=RESULT_SUCCESS if attached_images else RESULT_NEED_MORE,
            message="图片已进入报价材料",
            entities={**merged_entities, "quote_case_id": case.id, "order_id": case.order_id},
            payload=payload,
        ),
        "actions": [_mk_action("太平洋报价"), _mk_action("查看当前材料状态")],
    }


async def recall_quote_case_images(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: str,
    storage_keys: List[str],
) -> Dict[str, Any]:
    keys = {str(x or "").strip().lstrip("/") for x in (storage_keys or []) if str(x or "").strip()}
    if owner_user_id <= 0:
        raise ValueError("无法识别当前用户")
    if not session_id:
        raise ValueError("缺少会话")
    if not keys:
        raise ValueError("缺少要撤回的图片")

    rows = (
        await db.execute(
            select(QuoteCaseImage, QuoteCase)
            .join(QuoteCase, QuoteCase.id == QuoteCaseImage.quote_case_id)
            .where(
                QuoteCase.owner_user_id == owner_user_id,
                QuoteCase.session_id == session_id,
                QuoteCaseImage.storage_key.in_(keys),
                QuoteCaseImage.status == ACTIVE_IMAGE_STATUS,
            )
            .order_by(QuoteCaseImage.id.asc())
        )
    ).all()

    now = _now()
    changed_images: List[Dict[str, Any]] = []
    case_map: Dict[int, QuoteCase] = {}
    for image, case in rows:
        image.status = "deleted_by_user"
        image.updated_at = now
        case_map[int(case.id)] = case
        changed_images.append(
            {
                "id": image.id,
                "quote_case_id": image.quote_case_id,
                "storage_key": image.storage_key,
                "confirmed_slot_key": image.confirmed_slot_key,
            }
        )

    cancelled_tasks = 0
    for case in case_map.values():
        case_changed_images = [item for item in changed_images if item.get("quote_case_id") == case.id]
        case_cancelled_tasks = await _cancel_waiting_tasks_for_case(
            db,
            case=case,
            reason="cancelled_by_image_recall",
            now=now,
        )
        cancelled_tasks += case_cancelled_tasks

        images_by_slot = await _active_images_by_slot(db, int(case.id))
        normalized_data = _json_obj(case.normalized_data)
        missing = _missing_requirements(normalized_data, images_by_slot)
        case.missing_requirements = missing
        if case.status in {"ready", "waiting_sms", "failed"}:
            case.status = "collecting" if missing else "ready"
        case.updated_at = now
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="image",
            role="user",
            payload={
                "image_recall": True,
                "storage_keys": sorted(keys),
                "changed_images": case_changed_images,
                "cancelled_waiting_tasks": case_cancelled_tasks,
                "missing_requirements": missing,
            },
        )

    await db.flush()
    return {
        "recalled_count": len(changed_images),
        "changed_images": changed_images,
        "cancelled_waiting_tasks": cancelled_tasks,
        "storage_keys": sorted(keys),
    }


async def handle_quote_message(
    db: AsyncSession,
    *,
    ctx: Dict[str, Any],
    entities: Dict[str, Any],
    text: str,
) -> Tuple[str, Dict[str, Any]]:
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        raise ValueError("missing current user id")

    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    signal = detect_quote_signal(text)
    merged_entities = {**(entities or {}), **_json_obj(signal.get("entities"))}
    sms_code = _extract_sms_code(text)

    waiting_pair = await _find_waiting_task(db, owner_user_id=owner_user_id, session_id=session_id)
    if waiting_pair and sms_code:
        case, task = waiting_pair
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="chat",
            role="user",
            content="[短信验证码已输入]",
            payload={"sms_code_length": len(sms_code)},
        )
        return await _complete_waiting_task(db, case=case, task=task, owner_user_id=owner_user_id, sms_code=sms_code)

    if sms_code:
        expired_pair = await _find_waiting_task(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            include_expired=True,
        )
        if expired_pair and _is_sms_task_expired(expired_pair[1]):
            case, task = expired_pair
            task.status = "failed"
            task.login_state = "failed"
            task.error_detail = "sms_code_expired"
            task.finished_at = _now()
            task.updated_at = _now()
            case.status = "ready"
            case.current_task_id = None
            case.updated_at = _now()
            await _add_event(
                db,
                case=case,
                owner_user_id=owner_user_id,
                event_type="task",
                role="system",
                payload={"task_id": task.id, "status": "failed", "reason": "sms_code_expired"},
            )
            await db.commit()
            return (
                f"{case.platform_name or case.platform_code or '平台'}验证码已过期，我没有继续提交旧验证码。\n请重新发送“{case.platform_name or case.platform_code or '平台'}报价”触发新的短信验证码。",
                {
                    "status": "success",
                    "intent": "quote",
                    "trace_id": task.trace_id or _new_trace_id(),
                    "data": _mk_data(
                        result_status=RESULT_NOT_READY,
                        message="短信验证码已过期，请重新触发报价",
                        entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                        payload={"quote_case": {"id": case.id, "case_no": case.case_no, "status": case.status}},
                    ),
                    "actions": [_mk_action(f"{case.platform_name or '太平洋'}报价")],
                },
            )

    if waiting_pair:
        case, task = waiting_pair
        payload = {
            "quote_case": {
                "id": case.id,
                "case_no": case.case_no,
                "status": case.status,
                "platform_code": case.platform_code,
                "platform_name": case.platform_name,
            },
            "quote_task": {
                "id": task.id,
                "status": task.status,
                "login_state": task.login_state,
                "sms_phone_mask": task.sms_phone_mask,
                "trace_id": task.trace_id,
            },
        }
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="chat",
            role="user",
            content=text,
            payload={"waiting_sms": True},
        )
        await db.commit()
        return (
            f"{case.platform_name or case.platform_code or '平台'}登录正在等待短信验证码，请直接在聊天框输入 4-8 位验证码。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": task.trace_id or _new_trace_id(),
                "data": _mk_data(
                    result_status=RESULT_NOT_READY,
                    message="等待业务员输入短信验证码",
                    entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                    payload=payload,
                ),
                "actions": [_mk_action("输入短信验证码")],
            },
        )

    platform_code = _to_str(merged_entities.get("platform_code")).strip().upper()
    platform_name = _to_str(merged_entities.get("platform_name")).strip()
    if not platform_code and platform_name:
        platform_code = "STUB"
    if not platform_name and platform_code:
        platform_name = PLATFORM_ALIASES.get(platform_code, (platform_code, ()))[0]

    if not platform_name:
        return (
            "已识别到报价意图，但还缺少报价平台。请直接输入例如：太平洋报价、人保报价、平安报价。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": _new_trace_id(),
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="报价缺少平台信息",
                    entities=merged_entities,
                    payload={},
                ),
                "actions": [_mk_action("太平洋报价"), _mk_action("人保报价"), _mk_action("平安报价")],
            },
        )

    account_type_name = _extract_account_type_from_quote_text(text, platform_name)
    platform_account = await _select_quote_platform_account(
        db,
        owner_user_id=owner_user_id,
        platform_code=platform_code,
        account_type_name=account_type_name,
    )

    order_id = _safe_int((ctx or {}).get("order_id"), 0) or _safe_int(merged_entities.get("order_id"), 0) or None
    extracted = extract_quote_fields(text)
    plate_no = _to_str(extracted.get("plate_no") or merged_entities.get("plate_no")).strip() or None
    owner_phone = _to_str(extracted.get("owner_phone") or merged_entities.get("owner_phone")).strip() or None
    owner_name = _to_str(extracted.get("owner_name") or merged_entities.get("owner_name")).strip() or None
    order = await _find_order(
        db,
        ctx=ctx,
        order_id=order_id,
        plate_no=plate_no,
        owner_phone=owner_phone,
        owner_name=owner_name,
    )

    case = await _get_or_create_case(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        order=order,
        platform_code=platform_code,
        platform_name=platform_name,
    )
    await _add_event(db, case=case, owner_user_id=owner_user_id, event_type="chat", role="user", content=text, payload={})

    order_data = _order_data(order)
    old_draft = _json_obj(case.draft_order_data)
    normalized_data = _merge_data(old_draft, order_data, extracted)
    case.draft_order_data = normalized_data
    case.normalized_data = normalized_data

    await _sync_order_images_to_case(db, case=case, owner_user_id=owner_user_id, order=order)
    attached_images = await _attach_uploaded_images(
        db,
        case=case,
        owner_user_id=owner_user_id,
        images=_collect_context_images(ctx),
    )
    extracted_from_images = _merge_data(
        *[
            _json_obj(item.get("extracted_fields"))
            for item in attached_images
            if isinstance(item, dict) and item.get("extracted_fields")
        ]
    )
    if extracted_from_images:
        normalized_data = _merge_data(normalized_data, extracted_from_images)
        case.draft_order_data = normalized_data
        case.normalized_data = normalized_data

    images_by_slot = await _active_images_by_slot(db, case.id)
    missing = _missing_requirements(normalized_data, images_by_slot)
    case.missing_requirements = missing
    missing_account = platform_account is None

    if missing:
        case.status = "collecting"
        case.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="status",
            role="assistant",
            payload={"status": "collecting", "missing": missing},
        )
        await db.commit()

        missing_fields = [item["label"] for item in missing if item.get("type") == "field"]
        missing_images = [item["label"] for item in missing if item.get("type") == "image"]
        lines = [
            f"{platform_name}报价资料还不完整，暂不触发平台登录。",
        ]
        if missing_account:
            type_hint = f"（类型：{account_type_name}）" if account_type_name else ""
            lines.append(f"另外，{platform_name}{type_hint}还没有可用账号，请先在右上角“平台账号管理”新增或启用账号。")
        if missing_fields:
            lines.append("缺少字段：" + "、".join(missing_fields))
        if missing_images:
            lines.append("缺少图片：" + "、".join(missing_images))
        if attached_images:
            moved = [
                f"{slot_label(x.get('provided_slot_key') or '')}->{slot_label(x.get('confirmed_slot_key') or '')}"
                for x in attached_images
                if x.get("provided_slot_key") != x.get("confirmed_slot_key")
            ]
            if moved:
                lines.append("已自动识别并归位图片：" + "、".join(moved[:5]))

        payload = _case_payload(
            case=case,
            normalized_data=normalized_data,
            images_by_slot=images_by_slot,
            missing=missing,
            attached_images=attached_images,
            platform_account=platform_account,
        )
        return "\n".join(lines), {
            "status": "success",
            "intent": "quote",
            "trace_id": _new_trace_id(),
            "data": _mk_data(
                result_status=RESULT_NEED_MORE,
                message="报价资料未满足必填项",
                entities={**merged_entities, "quote_case_id": case.id, "order_id": case.order_id},
                payload=payload,
            ),
            "actions": [
                *(
                    [
                        _mk_action(
                            "平台账号管理",
                            "open_account_manager",
                            "quote_platform_accounts",
                            platform_code=platform_code,
                            platform_name=platform_name,
                        )
                    ]
                    if missing_account
                    else []
                ),
                _mk_action("查看当前材料状态"),
                _mk_action(f"{platform_name}报价"),
            ],
        }

    case.status = "ready"
    case.updated_at = _now()
    if missing_account:
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="status",
            role="assistant",
            payload={
                "status": "ready",
                "need_platform_account": True,
                "platform_code": platform_code,
                "account_type_name": account_type_name,
            },
        )
        await db.commit()
        payload = _case_payload(
            case=case,
            normalized_data=normalized_data,
            images_by_slot=images_by_slot,
            missing=[],
            attached_images=attached_images,
            platform_account=platform_account,
        )
        type_hint = f"（类型：{account_type_name}）" if account_type_name else ""
        lines = [
            f"{platform_name}{type_hint}报价资料已齐，但当前没有可用平台账号。",
            "请先点击右上角“平台账号管理”，新增/启用账号，或确认账号额度没有满。",
        ]
        return "\n".join(lines), {
            "status": "success",
            "intent": "quote",
            "trace_id": _new_trace_id(),
            "data": _mk_data(
                result_status=RESULT_NEED_MORE,
                message="报价资料已齐，等待配置可用平台账号",
                entities={**merged_entities, "quote_case_id": case.id, "order_id": case.order_id},
                payload=payload,
            ),
            "actions": [
                _mk_action(
                    "平台账号管理",
                    "open_account_manager",
                    "quote_platform_accounts",
                    platform_code=platform_code,
                    platform_name=platform_name,
                ),
                _mk_action(f"{platform_name}报价"),
            ],
        }

    trace_id = _new_trace_id()
    snapshot = _snapshot_payload(case=case, normalized_data=normalized_data, images_by_slot=images_by_slot)
    if platform_account.login_status == ACCOUNT_LOGIN_AUTHENTICATED:
        return await _complete_quote_without_sms(
            db,
            case=case,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            trace_id=trace_id,
            platform_account=platform_account,
            login_mode="reuse_authenticated",
        )

    if not bool(platform_account.auto_login):
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="status",
            role="assistant",
            payload={
                "status": "ready",
                "need_manual_account_login": True,
                "platform_account": _credential_public_payload(platform_account),
            },
        )
        await db.commit()
        payload = _case_payload(
            case=case,
            normalized_data=normalized_data,
            images_by_slot=images_by_slot,
            missing=[],
            attached_images=attached_images,
            platform_account=platform_account,
        )
        return (
            f"{platform_name}报价资料已齐，但所选账号未开启自动登录。请先在右上角“平台账号管理”里点击该账号的“登录”。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="账号未开启自动登录，等待手动登录",
                    entities={**merged_entities, "quote_case_id": case.id, "order_id": case.order_id},
                    payload=payload,
                ),
                "actions": [
                    _mk_action(
                        "平台账号管理",
                        "open_account_manager",
                        "quote_platform_accounts",
                        platform_code=platform_code,
                        platform_name=platform_name,
                    )
                ],
            },
        )

    login_runtime_result = await quote_platform_runtime.login(_platform_account_context(platform_account))
    login_status = _runtime_status(login_runtime_result)
    if not _is_runtime_login_success(login_status) and not _is_runtime_challenge(login_status):
        platform_account.login_status = ACCOUNT_LOGIN_FAILED
        platform_account.last_error = _runtime_detail(login_runtime_result, "平台登录失败")
        platform_account.last_check_at = _now()
        platform_account.updated_at = _now()
        await db.commit()
        return (
            f"{platform_name}登录失败：{platform_account.last_error}。请在平台账号管理中检查账号后重试。",
            {
                "status": "failed",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_FAILED,
                    message=platform_account.last_error,
                    entities={**merged_entities, "quote_case_id": case.id, "order_id": case.order_id},
                    payload={"platform_account": _credential_public_payload(platform_account)},
                ),
                "actions": [
                    _mk_action(
                        "平台账号管理",
                        "open_account_manager",
                        "quote_platform_accounts",
                        platform_code=platform_code,
                        platform_name=platform_name,
                    )
                ],
            },
        )

    if _is_runtime_login_success(login_status):
        return await _complete_quote_without_sms(
            db,
            case=case,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            trace_id=trace_id,
            platform_account=platform_account,
            login_mode="auto_login_without_code",
        )

    task = await _start_sms_task(
        db,
        case=case,
        owner_user_id=owner_user_id,
        snapshot=snapshot,
        trace_id=trace_id,
        platform_account=platform_account,
    )
    await db.commit()

    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=[],
        attached_images=attached_images,
        task=task,
        platform_account=platform_account,
    )
    account_payload = _credential_public_payload(platform_account) or {}
    reply = (
        f"{platform_name}报价必填项已满足，已触发登录短信验证（当前为本地假流程）。\n"
        f"- 已复用平台登录资料：{account_payload.get('login_phone_mask') or task.sms_phone_mask or '业务员手机号'}\n"
        "请在聊天框直接输入收到的 4-8 位短信验证码。"
    )
    return reply, {
        "status": "success",
        "intent": "quote",
        "trace_id": trace_id,
        "data": _mk_data(
            result_status=RESULT_NOT_READY,
            message="已触发平台登录短信验证，等待验证码",
            entities={**merged_entities, "quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
            payload=payload,
        ),
        "actions": [_mk_action("输入短信验证码")],
    }


async def handle_platform_credential_message(
    db: AsyncSession,
    *,
    ctx: Dict[str, Any],
    entities: Dict[str, Any],
    text: str,
) -> Tuple[str, Dict[str, Any]]:
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        raise ValueError("missing current user id")

    signal = detect_platform_credential_signal(text)
    merged_entities = {**(entities or {}), **_json_obj(signal.get("entities"))}
    platform_code = _to_str(merged_entities.get("platform_code")).strip().upper()
    platform_name = _to_str(merged_entities.get("platform_name")).strip()
    if not platform_name and platform_code:
        platform_name = _platform_display_name(platform_code)
    platform_text = f"{platform_name}平台" if platform_name else "对应平台"
    return (
        f"为了避免账号资料和报价会话混在一起，{platform_text}账号请统一在右上角“平台账号管理”中新增或编辑；聊天框不再保存账号、密码或手机号。",
        {
            "status": "success",
            "intent": "quote_credential",
            "trace_id": _new_trace_id(),
            "data": _mk_data(
                result_status=RESULT_NEED_MORE,
                message="平台账号资料请在账号管理中维护",
                entities=merged_entities,
                payload={},
            ),
            "actions": [
                _mk_action(
                    "平台账号管理",
                    "open_account_manager",
                    "quote_platform_accounts",
                    platform_code=platform_code,
                    platform_name=platform_name,
                )
            ],
        },
    )


async def handle_quote_material_status(
    db: AsyncSession,
    *,
    ctx: Dict[str, Any],
    entities: Dict[str, Any],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        return None
    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    case = await _latest_active_case(db, owner_user_id=owner_user_id, session_id=session_id)
    if not case:
        return None

    normalized_data = _json_obj(case.normalized_data)
    images_by_slot = await _active_images_by_slot(db, case.id)
    missing = _json_list(case.missing_requirements) or _missing_requirements(normalized_data, images_by_slot)
    platform_account = await _select_quote_platform_account(
        db,
        owner_user_id=owner_user_id,
        platform_code=case.platform_code or "",
    )
    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=missing,
        platform_account=platform_account,
    )

    lines = [
        "当前报价材料状态：",
        f"- 平台：{case.platform_name or case.platform_code or '-'}",
    ]
    ready_slots = payload.get("ready_slots") or {}
    if ready_slots:
        lines.append("- 已归位图片：" + "、".join(f"{slot_label(k)}{v}张" for k, v in ready_slots.items()))
    if missing:
        lines.append("- 仍缺少：" + "、".join(item.get("label") or item.get("key") for item in missing))
    else:
        lines.append("- 必填项已齐，可以继续报价流程。")
    if platform_account:
        account_payload = _credential_public_payload(platform_account) or {}
        lines.append(f"- 平台账号资料：已记住 {account_payload.get('login_phone_mask') or '登录资料'}")

    return "\n".join(lines), {
        "status": "success",
        "intent": "quote_material_status",
        "trace_id": _new_trace_id(),
        "data": _mk_data(
            result_status=RESULT_SUCCESS if not missing else RESULT_NOT_READY,
            message="已返回报价草稿材料状态",
            entities={**(entities or {}), "quote_case_id": case.id, "order_id": case.order_id},
            payload=payload,
        ),
        "actions": [_mk_action(f"{case.platform_name or '太平洋'}报价"), _mk_action("查看当前材料状态")],
    }
