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

from sqlalchemy import and_, desc, false as sql_false, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, selectinload

from app.core.access_control import normalize_team_names, user_team_match_expr
from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_MARKET, ROLE_SALES, ROLE_SUPER_ADMIN
from app.models.image_file import ImageFile
from app.models.order import Order, OrderImage
from app.models.order_info import OrderInfo
from app.models.quote_assistant import QuoteCase, QuoteCaseEvent, QuoteCaseImage, QuotePlatformAccount, QuoteTask
from app.models.user import User
from app.services.image_slot_classifier import (
    SLOT_KEYS,
    classify_image_slot,
    is_single_slot,
    slot_label,
)
from app.services.baidu_ocr import OcrCallError, OcrNotConfigured, call_ocr
from app.services.ocr_worker import _extract_by_type
from app.services.quote_secret_box import encrypt_json, encrypt_text
from app.services.storage import StorageService

TZ_BJ = timezone(timedelta(hours=8))
storage = StorageService()

RESULT_SUCCESS = "success"
RESULT_NEED_MORE = "need_more_info"
RESULT_NOT_READY = "not_ready"
RESULT_FAILED = "failed"

ACTIVE_CASE_STATUSES = ("collecting", "ready", "waiting_sms", "failed")
ACTIVE_IMAGE_STATUS = "active"
SINGLE_REQUIRED_SLOTS = ("vehicle_cert", "idcard_front", "driving_license_main")
QUOTE_IMAGE_OCR_CLASSIFY_ENABLED = os.getenv("QUOTE_IMAGE_OCR_CLASSIFY_ENABLED", "1") == "1"
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
    # 目前真实平台适配器尚未接入，太平洋先按“手机号短信验证优先，账号密码可补充”的口径走。
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
    label_expr = "|".join(re.escape(x) for x in labels if x)
    if not label_expr:
        return None
    pattern = rf"(?:{label_expr})\s*[:：=]?\s*([^\s,，;；。]+)"
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


def _has_login_phone_hint(text: str) -> bool:
    low = text.lower()
    return any(h.lower() in low for h in _LOGIN_PHONE_HINTS) or any(
        h in text for h in ("登录", "登陆", "验证码", "短信", "平台账号", "平台密码")
    )


def redact_quote_sensitive_text(text: Any) -> str:
    """Return chat-display/audit-safe text without leaking platform passwords."""

    raw = _to_str(text)
    if not raw:
        return ""

    out = raw
    out = re.sub(
        r"((?:登录密码|登陆密码|平台密码|密码|口令|password|pwd)\s*[:：=]?\s*)([^\s,，;；。]+)",
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
            rf"((?:{label_expr})\s*[:：=]?\s*)([^\s,，;；。]+)",
            lambda m: m.group(1) + "[VALUE]",
            out,
            flags=re.IGNORECASE,
        )
    return out


def _extract_platform_credentials(text: Any, *, allow_loose_phone: bool = False) -> Dict[str, str]:
    t = _norm_text(text)
    low = t.lower()
    has_login_hint = any(
        key in low
        for key in (
            "登录",
            "登陆",
            "验证码",
            "短信",
            "手机号",
            "手机",
            "账号",
            "账户",
            "用户名",
            "密码",
            "password",
            "phone",
            "mobile",
        )
    )
    out: Dict[str, str] = {}

    phone = _extract_labeled_value(t, _LOGIN_PHONE_HINTS, max_len=32)
    if not phone and (has_login_hint or allow_loose_phone):
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


def _credential_public_payload(row: Optional[QuotePlatformAccount]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    values = getattr(row, "__dict__", {}) or {}
    login_phone = values.get("login_phone")
    password_ciphertext = values.get("password_ciphertext")
    credential_payload = _json_obj(values.get("credential_payload"))
    return {
        "id": values.get("id"),
        "platform_code": values.get("platform_code"),
        "platform_name": values.get("platform_name"),
        "login_phone_mask": values.get("login_phone_mask") or _mask_phone(login_phone),
        "has_login_phone": bool(_to_str(login_phone).strip()),
        "account_username": values.get("account_username"),
        "has_password": bool(_to_str(password_ciphertext).strip()),
        "configured_fields": credential_payload.get("configured_fields") if isinstance(credential_payload.get("configured_fields"), list) else [],
        "saved_extra_fields": credential_payload.get("saved_extra_fields") if isinstance(credential_payload.get("saved_extra_fields"), list) else [],
        "last_login_state": values.get("last_login_state") or "none",
        "last_sms_at": _fmt_dt(values.get("last_sms_at")),
        "last_used_at": _fmt_dt(values.get("last_used_at")),
    }


def _platform_account_has_field(
    account: Optional[QuotePlatformAccount],
    key: str,
    field: Optional[Dict[str, Any]] = None,
) -> bool:
    if account is None:
        return False
    field = field or {}
    payload = _json_obj(getattr(account, "credential_payload", None))
    extra_public = _json_obj(payload.get("extra_public"))
    saved_extra_fields = set(_json_list(payload.get("saved_extra_fields")))
    if key == "login_phone":
        return bool(_to_str(getattr(account, "login_phone", None)).strip())
    if key == "account_username":
        return bool(_to_str(getattr(account, "account_username", None)).strip())
    if key == "account_password":
        return bool(_to_str(getattr(account, "password_ciphertext", None)).strip())
    if bool(field.get("secret")) or _to_str(field.get("type")).strip().lower() == "password":
        return key in saved_extra_fields
    return bool(_to_str(extra_public.get(key)).strip()) or key in saved_extra_fields


def _missing_platform_account_fields(
    account: Optional[QuotePlatformAccount],
    platform_code: str,
) -> List[Dict[str, Any]]:
    missing: List[Dict[str, Any]] = []

    for field in _platform_credential_fields(platform_code):
        if not bool(field.get("required")):
            continue
        key = _to_str(field.get("key")).strip()
        if not key:
            continue
        if not _platform_account_has_field(account, key, field):
            missing.append(
                {
                    "key": key,
                    "label": _to_str(field.get("label")).strip() or key,
                    "type": _to_str(field.get("type")).strip() or "text",
                    "required": True,
                    "secret": bool(field.get("secret")),
                }
            )
    return missing


def _missing_platform_account_labels(missing: List[Dict[str, Any]]) -> str:
    labels = [_to_str(x.get("label")).strip() or _to_str(x.get("key")).strip() for x in missing]
    labels = [x for x in labels if x]
    return "、".join(labels) if labels else "平台登录资料"


def list_platform_account_schemas() -> List[Dict[str, Any]]:
    return [_platform_schema(code, name) for code, (name, _) in PLATFORM_ALIASES.items()]


async def list_platform_accounts_public(
    db: AsyncSession,
    *,
    owner_user_id: int,
) -> Dict[str, Dict[str, Any]]:
    if owner_user_id <= 0:
        return {}
    rows = (
        await db.execute(
            select(QuotePlatformAccount)
            .where(QuotePlatformAccount.owner_user_id == int(owner_user_id))
            .order_by(QuotePlatformAccount.platform_code.asc(), QuotePlatformAccount.id.desc())
        )
    ).scalars().all()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code = _to_str(row.platform_code).strip().upper()
        if code and code not in out:
            out[code] = _credential_public_payload(row) or {}
    return out


def get_platform_account_schema(platform_code: str, platform_name: Optional[str] = None) -> Dict[str, Any]:
    code = _to_str(platform_code).strip().upper()
    if not code:
        raise ValueError("请选择平台")
    return _platform_schema(code, platform_name)


def _normalize_form_credentials(
    *,
    platform_code: str,
    platform_name: Optional[str],
    values: Dict[str, Any],
    existing_account: Optional[QuotePlatformAccount] = None,
) -> Tuple[str, str, Dict[str, str], Dict[str, Any], Dict[str, str], List[str]]:
    code = _to_str(platform_code).strip().upper()
    if not code:
        raise ValueError("请选择平台")
    name = _platform_display_name(code, platform_name)
    raw_values = values if isinstance(values, dict) else {}

    credentials: Dict[str, str] = {}
    extra_public: Dict[str, Any] = {}
    extra_secret: Dict[str, str] = {}
    configured_fields: List[str] = []

    for field in _platform_credential_fields(code):
        key = _to_str(field.get("key")).strip()
        if not key:
            continue
        label = _to_str(field.get("label")).strip() or key
        raw = raw_values.get(key)
        value = _to_str(raw).strip()
        existing_ok = _platform_account_has_field(existing_account, key, field)
        if bool(field.get("required")) and not value and not existing_ok:
            raise ValueError(f"{label}不能为空")
        if not value:
            if existing_ok:
                configured_fields.append(key)
            continue

        ftype = _to_str(field.get("type")).strip().lower()
        if ftype == "phone":
            digits = re.sub(r"\D+", "", value)
            if len(digits) != 11:
                raise ValueError(f"{label}格式不正确，请填写 11 位手机号")
            value = digits

        configured_fields.append(key)
        if key in {"login_phone", "account_username", "account_password"}:
            credentials[key] = value
        elif bool(field.get("secret")) or ftype == "password":
            extra_secret[key] = value
        else:
            extra_public[key] = value

    if not credentials and not extra_public and not extra_secret and not configured_fields:
        raise ValueError("请至少填写一项平台登录信息")

    return code, name, credentials, extra_public, extra_secret, configured_fields


async def save_platform_account_form(
    db: AsyncSession,
    *,
    owner_user_id: int,
    platform_code: str,
    platform_name: Optional[str] = None,
    values: Optional[Dict[str, Any]] = None,
) -> Optional[QuotePlatformAccount]:
    if owner_user_id <= 0:
        raise ValueError("无法识别当前用户")
    existing_account = None
    if hasattr(db, "execute"):
        existing_account = await _get_platform_account(
            db,
            owner_user_id=owner_user_id,
            platform_code=platform_code,
        )
    code, name, credentials, extra_public, extra_secret, configured_fields = _normalize_form_credentials(
        platform_code=platform_code,
        platform_name=platform_name,
        values=values or {},
        existing_account=existing_account,
    )

    account = await _save_platform_credentials(
        db,
        owner_user_id=owner_user_id,
        platform_code=code,
        platform_name=name,
        credentials=credentials,
    )
    if account is None:
        account = await _get_platform_account(db, owner_user_id=owner_user_id, platform_code=code)
    if account is None:
        account = QuotePlatformAccount(
            owner_user_id=owner_user_id,
            platform_code=code,
            platform_name=name,
            credential_payload={},
            last_login_state="none",
        )
        db.add(account)
        await db.flush()

    payload = _json_obj(account.credential_payload).copy()
    payload["configured_fields"] = configured_fields
    if extra_public:
        old_extra = _json_obj(payload.get("extra_public")).copy()
        old_extra.update(extra_public)
        payload["extra_public"] = old_extra
    saved_extra_fields = sorted(set(_json_list(payload.get("saved_extra_fields")) + list(extra_public.keys()) + list(extra_secret.keys())))
    payload["saved_extra_fields"] = saved_extra_fields
    payload["schema_version"] = 1
    payload["secret_storage"] = "encrypted" if (account.password_ciphertext or extra_secret) else payload.get("secret_storage", "none")
    account.credential_payload = payload
    if extra_secret:
        account.secret_payload_ciphertext = encrypt_json(
            extra_secret,
            aad=_credential_aad(owner_user_id, code) + ":extra",
        )
    account.updated_at = _now()
    await db.flush()
    return account


async def _get_platform_account(
    db: AsyncSession,
    *,
    owner_user_id: int,
    platform_code: str,
) -> Optional[QuotePlatformAccount]:
    code = _to_str(platform_code).strip().upper()
    if owner_user_id <= 0 or not code:
        return None
    return (
        await db.execute(
            select(QuotePlatformAccount)
            .where(
                QuotePlatformAccount.owner_user_id == owner_user_id,
                QuotePlatformAccount.platform_code == code,
            )
            .limit(1)
        )
    ).scalars().first()


async def _save_platform_credentials(
    db: AsyncSession,
    *,
    owner_user_id: int,
    platform_code: str,
    platform_name: str,
    credentials: Dict[str, str],
) -> Optional[QuotePlatformAccount]:
    code = _to_str(platform_code).strip().upper()
    if owner_user_id <= 0 or not code or not credentials:
        return None

    row = await _get_platform_account(db, owner_user_id=owner_user_id, platform_code=code)
    if row is None:
        row = QuotePlatformAccount(
            owner_user_id=owner_user_id,
            platform_code=code,
            platform_name=platform_name or code,
            credential_payload={},
            last_login_state="none",
        )
        db.add(row)

    row.platform_name = platform_name or row.platform_name or code
    payload = _json_obj(row.credential_payload).copy()
    changed_fields: List[str] = []

    phone = _to_str(credentials.get("login_phone")).strip()
    if phone:
        row.login_phone = phone
        row.login_phone_mask = _mask_phone(phone)
        payload["login_phone_mask"] = row.login_phone_mask
        changed_fields.append("login_phone")

    username = _to_str(credentials.get("account_username")).strip()
    if username:
        row.account_username = username
        changed_fields.append("account_username")

    password = _to_str(credentials.get("account_password"))
    if password:
        row.password_ciphertext = encrypt_text(password, aad=_credential_aad(owner_user_id, code))
        changed_fields.append("account_password")

    payload["updated_fields"] = sorted(set(changed_fields))
    payload["secret_storage"] = "encrypted" if row.password_ciphertext else "none"
    row.credential_payload = payload
    row.updated_at = _now()
    await db.flush()
    return row


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
    row = (
        await db.execute(
            select(QuotePlatformAccount)
            .where(
                QuotePlatformAccount.id == int(account_id),
                QuotePlatformAccount.owner_user_id == int(owner_user_id),
            )
            .limit(1)
        )
    ).scalars().first()
    if not row:
        return
    row.last_login_state = login_state or row.last_login_state
    row.last_used_at = _now()
    if sms_at:
        row.last_sms_at = _now()
    row.updated_at = _now()
    await db.flush()


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

    if not entities.get("platform_name"):
        m = re.search(
            r"(?:^|[\s，,。；;])([\u4e00-\u9fa5A-Za-z0-9]{1,16})(?:保险)?\s*报价(?:$|[\s，,。；;])",
            t,
        )
        if m:
            platform_name = m.group(1).strip()
            entities["platform_name"] = platform_name
            entities["platform_code"] = "STUB"

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
        r"(?:订单号|订单|order)\s*[:：#]?\s*(\d{1,12})",
        r"\border\s*[:：#]?\s*(\d{1,12})\b",
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

    name = re.search(r"(?:车主|姓名|被保人|投保人|联系人)\s*[:：]?\s*([\u4e00-\u9fa5]{2,12})", t)
    if name:
        out["owner_name"] = name.group(1).strip()

    model = re.search(r"(?:车型|品牌型号|车辆型号)\s*[:：]?\s*([^\s,，;；]{2,60})", t)
    if model:
        out["vehicle_model"] = model.group(1).strip()

    exp_date = re.search(r"(?:保险到期|到期日|保险止期)\s*[:：]?\s*(\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2})", t)
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

    stmt = select(QuoteCase).where(
        QuoteCase.owner_user_id == owner_user_id,
        QuoteCase.status.in_(ACTIVE_CASE_STATUSES),
    )
    if order_id:
        stmt = stmt.where(QuoteCase.order_id == order_id)
    elif session_id:
        stmt = stmt.where(QuoteCase.session_id == session_id, QuoteCase.order_id.is_(None))
    else:
        stmt = stmt.where(QuoteCase.order_id.is_(None))

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
    classification = classify_image_slot(
        provided_slot_key=provided_slot,
        original_name=image.get("original_name"),
        storage_key=storage_key,
        ocr_text=image.get("ocr_text") or image.get("ocr_text_sample"),
        raw_payload=image.get("raw") or image.get("ocr_raw"),
    )
    if (
        not QUOTE_IMAGE_OCR_CLASSIFY_ENABLED
        or classification.confidence >= 0.78
        or image.get("ocr_text")
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

    for slot_key, api_type, side in _ocr_candidates_for_image(provided_slot, storage_key):
        try:
            raw = await asyncio.to_thread(call_ocr, api_type, image_url, side, True)
        except (OcrNotConfigured, OcrCallError, ValueError, RuntimeError):
            continue
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
    platform_account: Optional[QuotePlatformAccount] = None,
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
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        return False
    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    case = await _latest_active_case(db, owner_user_id=owner_user_id, session_id=session_id)
    if not case or not case.platform_code:
        return False
    account = await _get_platform_account(db, owner_user_id=owner_user_id, platform_code=case.platform_code)
    if account and _to_str(account.login_phone).strip():
        return False
    return case.status in {"collecting", "ready", "waiting_sms"}


async def _start_sms_task(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    snapshot: Dict[str, Any],
    trace_id: str,
    platform_account: QuotePlatformAccount,
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
        login_state="sms_required",
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
    result = _fake_quote_result(snapshot, platform_code=platform_code, platform_name=platform_name, trace_id=trace_id)

    task.status = "success"
    task.login_state = "authenticated"
    task.response_payload = {
        "stub": True,
        "sms_code_length": len(sms_code),
        "login": "ok",
        "quote": "ok",
    }
    task.result_payload = result
    task.finished_at = _now()
    task.updated_at = _now()

    case.status = "quoted"
    case.quote_count = _safe_int(case.quote_count, 0) + 1
    case.current_task_id = task.id
    case.updated_at = _now()
    account_payload = _json_obj(_json_obj(task.request_payload).get("platform_account"))
    await _mark_platform_account_used(
        db,
        account_id=_safe_int(account_payload.get("id"), 0) or None,
        owner_user_id=owner_user_id,
        login_state="authenticated",
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
        payload={"status": case.status, "attached_images": attached_images, "missing": missing},
    )
    await db.commit()

    moved = [
        f"{slot_label(x.get('provided_slot_key') or '')}->{slot_label(x.get('confirmed_slot_key') or '')}"
        for x in attached_images
        if x.get("provided_slot_key") != x.get("confirmed_slot_key")
    ]
    by_slot_count: Dict[str, int] = {}
    for item in attached_images:
        sk = _to_str(item.get("confirmed_slot_key")).strip()
        if sk:
            by_slot_count[sk] = by_slot_count.get(sk, 0) + 1

    lines = [
        f"已收到 {len(attached_images)} 张图片，后台已自动识别并放入报价材料池。",
        f"- 报价草稿：{case.case_no}",
    ]
    if by_slot_count:
        lines.append("- 识别结果：" + "、".join(f"{slot_label(k)}{v}张" for k, v in by_slot_count.items()))
    if moved:
        lines.append("- 已按识别结果静默归位：" + "、".join(moved[:5]))
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
            message="图片已进入报价材料池",
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
        active_waiting_tasks = (
            await db.execute(
                select(QuoteTask).where(
                    QuoteTask.quote_case_id == case.id,
                    QuoteTask.status == "waiting_sms",
                    QuoteTask.login_state == "sms_required",
                )
            )
        ).scalars().all()
        for task in active_waiting_tasks:
            task.status = "cancelled"
            task.login_state = "failed"
            task.error_detail = "cancelled_by_image_recall"
            task.finished_at = now
            task.updated_at = now
            cancelled_tasks += 1

        images_by_slot = await _active_images_by_slot(db, int(case.id))
        normalized_data = _json_obj(case.normalized_data)
        missing = _missing_requirements(normalized_data, images_by_slot)
        case.missing_requirements = missing
        if case.status in {"ready", "waiting_sms", "failed"}:
            case.status = "collecting" if missing else "ready"
        case.current_task_id = None if cancelled_tasks else case.current_task_id
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
                "changed_images": changed_images,
                "cancelled_waiting_tasks": cancelled_tasks,
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
            case.current_task_id = task.id
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

    if waiting_pair and not signal.get("is_quote"):
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
                "actions": [_mk_action("我已收到验证码，输入 123456")],
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

    credential_update = _extract_platform_credentials(text)
    platform_account = None
    if credential_update:
        platform_account = await _save_platform_credentials(
            db,
            owner_user_id=owner_user_id,
            platform_code=platform_code,
            platform_name=platform_name,
            credentials=credential_update,
        )

    order_id = _safe_int((ctx or {}).get("order_id"), 0) or _safe_int(merged_entities.get("order_id"), 0) or None
    extracted = extract_quote_fields(text)
    plate_no = _to_str(extracted.get("plate_no") or merged_entities.get("plate_no")).strip() or None
    owner_phone = _to_str(extracted.get("owner_phone") or merged_entities.get("owner_phone")).strip() or None
    if credential_update.get("login_phone") and owner_phone == credential_update.get("login_phone") and not extracted.get("owner_phone"):
        owner_phone = None
        merged_entities.pop("owner_phone", None)
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
    if platform_account is None:
        platform_account = await _get_platform_account(db, owner_user_id=owner_user_id, platform_code=platform_code)
    missing_account_fields = _missing_platform_account_fields(platform_account, platform_code)

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
            f"已创建/更新报价草稿：{case.case_no}。",
            f"目标平台：{platform_name}。",
            "当前信息还不满足报价必填项，暂不触发平台登录。",
        ]
        if missing_account_fields:
            lines.append(f"另外，{platform_name}平台账号还未绑定或资料不完整：缺少{_missing_platform_account_labels(missing_account_fields)}。")
            lines.append("你可以先点击右上角“绑定账号”补齐，资料齐全后再输入报价命令。")
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
                lines.append("已静默识别并归位图片：" + "、".join(moved[:5]))

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
                            "绑定账号",
                            "open_account_bind",
                            "quote_platform_account",
                            platform_code=platform_code,
                            platform_name=platform_name,
                        )
                    ]
                    if missing_account_fields
                    else []
                ),
                _mk_action("查看当前材料状态"),
                _mk_action(f"{platform_name}报价"),
            ],
        }

    case.status = "ready"
    case.updated_at = _now()
    if missing_account_fields:
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
                "missing_account_fields": missing_account_fields,
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
        saved_bits = []
        if platform_account and platform_account.account_username:
            saved_bits.append(f"账号 {platform_account.account_username}")
        if platform_account and platform_account.password_ciphertext:
            saved_bits.append("密码已加密保存")
        saved_text = "；".join(saved_bits)
        missing_text = _missing_platform_account_labels(missing_account_fields)
        lines = [
            f"{platform_name}报价资料已齐，但平台账号还不能用于报价。",
            f"- 报价草稿：{case.case_no}",
            f"- 缺少登录资料：{missing_text}",
        ]
        if saved_text:
            lines.append(f"- 已保存：{saved_text}")
        if platform_account is None:
            lines.append("请点击右上角“绑定账号”，选择平台后按表单填写并保存；保存后再输入报价命令。")
        else:
            lines.append("请点击右上角“绑定账号”补全该平台资料；保存后再输入报价命令。")
        return "\n".join(lines), {
            "status": "success",
            "intent": "quote",
            "trace_id": _new_trace_id(),
            "data": _mk_data(
                result_status=RESULT_NEED_MORE,
                message="报价资料已齐，等待绑定或补全平台账号",
                entities={**merged_entities, "quote_case_id": case.id, "order_id": case.order_id},
                payload=payload,
            ),
            "actions": [
                _mk_action(
                    "绑定账号",
                    "open_account_bind",
                    "quote_platform_account",
                    platform_code=platform_code,
                    platform_name=platform_name,
                ),
                _mk_action(f"{platform_name}报价"),
            ],
        }

    trace_id = _new_trace_id()
    snapshot = _snapshot_payload(case=case, normalized_data=normalized_data, images_by_slot=images_by_slot)
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
        f"- 报价草稿：{case.case_no}\n"
        f"- 来源：{'已有订单' if case.order_id else '新订单草稿'}\n"
        f"- 已复用平台登录资料：{account_payload.get('login_phone_mask') or task.sms_phone_mask or '业务员手机号'}\n"
        "请在聊天框直接输入验证码，例如：123456。"
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
        "actions": [_mk_action("输入验证码 123456")],
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

    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    signal = detect_platform_credential_signal(text)
    merged_entities = {**(entities or {}), **_json_obj(signal.get("entities"))}
    credentials = _json_obj(signal.get("credentials")) or _extract_platform_credentials(text, allow_loose_phone=True)

    case = await _latest_active_case(db, owner_user_id=owner_user_id, session_id=session_id)
    platform_code = _to_str(merged_entities.get("platform_code")).strip().upper()
    platform_name = _to_str(merged_entities.get("platform_name")).strip()
    if not platform_code and case and case.platform_code:
        platform_code = _to_str(case.platform_code).strip().upper()
    if not platform_name and case and case.platform_name:
        platform_name = _to_str(case.platform_name).strip()
    if not platform_code and platform_name:
        platform_code = "STUB"
    if not platform_name and platform_code:
        platform_name = PLATFORM_ALIASES.get(platform_code, (platform_code, ()))[0]

    if not platform_code or not platform_name:
        return (
            "我可以帮你记住平台登录资料，但还不知道是哪家平台。请这样发：太平洋登录手机号 你的手机号，账号 xxx，密码 xxx。",
            {
                "status": "success",
                "intent": "quote_credential",
                "trace_id": _new_trace_id(),
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="平台登录资料缺少平台名称",
                    entities=merged_entities,
                    payload={},
                ),
                "actions": [_mk_action("太平洋登录手机号 你的手机号")],
            },
        )

    if not credentials:
        return (
            f"已定位到{platform_name}，但还没有识别到可保存的登录资料。你可以输入：{platform_name}登录手机号 你的手机号，账号 xxx，密码 xxx。",
            {
                "status": "success",
                "intent": "quote_credential",
                "trace_id": _new_trace_id(),
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="没有识别到登录手机号/账号/密码",
                    entities=merged_entities,
                    payload={},
                ),
                "actions": [_mk_action(f"{platform_name}登录手机号 你的手机号")],
            },
        )

    account = await _save_platform_credentials(
        db,
        owner_user_id=owner_user_id,
        platform_code=platform_code,
        platform_name=platform_name,
        credentials=credentials,
    )
    if case:
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="status",
            role="assistant",
            payload={
                "platform_credential_saved": True,
                "platform_code": platform_code,
                "account": _credential_public_payload(account),
            },
        )
    await db.commit()

    public_payload = _credential_public_payload(account) or {}
    saved_parts = []
    if credentials.get("login_phone"):
        saved_parts.append(f"登录手机号 {public_payload.get('login_phone_mask') or _mask_phone(credentials.get('login_phone'))}")
    if credentials.get("account_username"):
        saved_parts.append(f"账号 {credentials.get('account_username')}")
    if credentials.get("account_password"):
        saved_parts.append("密码已加密保存")
    saved_text = "、".join(saved_parts) or "登录资料"
    reply_lines = [
        f"已为你记住{platform_name}的{saved_text}。",
        "后续同一账号再走这个平台报价时，我会优先复用这些资料，不会反复向你要。",
    ]
    if case and case.status == "ready" and public_payload.get("has_login_phone"):
        reply_lines.append(f"当前草稿 {case.case_no} 已经可以继续触发报价，你可以直接输入：{platform_name}报价。")

    return "\n".join(reply_lines), {
        "status": "success",
        "intent": "quote_credential",
        "trace_id": _new_trace_id(),
        "data": _mk_data(
            result_status=RESULT_SUCCESS,
            message="平台登录资料已保存",
            entities={**merged_entities, "quote_case_id": case.id if case else None},
            payload={"platform_account": public_payload},
        ),
        "actions": [_mk_action(f"{platform_name}报价"), _mk_action("查看当前材料状态")],
    }


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
    platform_account = await _get_platform_account(
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
        f"报价草稿材料状态：{case.case_no}",
        f"- 平台：{case.platform_name or case.platform_code or '-'}",
        f"- 来源：{'已有订单' if case.order_id else '新订单草稿'}",
        f"- 状态：{case.status}",
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
        lines.append(f"- 平台登录资料：已记住 {account_payload.get('login_phone_mask') or '登录资料'}")

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
