# encoding: utf-8
from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import os
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import and_, desc, false as sql_false, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, selectinload

from app.core.access_control import (
    normalize_team_names,
    require_quote_assistant_quote_use_access,
    user_team_match_expr,
)
from app.core.constants import ROLE_FINANCE, ROLE_MANAGER, ROLE_MARKET, ROLE_SALES, ROLE_SUPER_ADMIN
from app.models.image_file import ImageFile
from app.models.order import Order, OrderImage
from app.models.order_info import OrderInfo
from app.models.quote_assistant import (
    QuoteCase,
    QuoteCaseEvent,
    QuoteCaseImage,
    QuoteAssistantMessage,
    QuotePlatformAccountEvent,
    QuotePlatformAccountLoginTask,
    QuotePlatformAccountProfile,
    QuotePlatformAccountQuota,
    QuotePlatformAccountSessionState,
    QuotePlatformAccountType,
    QuotePlatformDefaultConfig,
    QuoteTask,
)
from app.models.user import User
from app.models.ocr_image_cache import OcrImageCache
from app.services.image_slot_classifier import (
    SLOT_KEYS,
    SlotClassification,
    classify_image_slot,
    is_single_slot,
    slot_label,
)
from app.services.baidu_ocr import OcrCallError, OcrNotConfigured, call_ocr
from app.services.ocr_cleaner import clean_dynamic_data_for_ocr, correct_vehicle_cert_field
from app.services.ocr_worker import _cache_put, _extract_by_type
from app.services.quote_platforms import runtime as quote_platform_runtime
from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.chat_session_lock import release_chat_session_lock_for_platform_io
from app.services.quote_platforms.browser_manager import account_profile_dir
from app.services.quote_platforms.platforms.picc.business import (
    picc_motor_builtin_default_values,
    _duplicate_quote_next_day_adjustments,
)
from app.services.quote_platforms.platforms.picc.presentation import (
    picc_is_new_energy_vehicle,
    picc_result_amount_text,
    picc_result_kind_name,
)
from app.services.quote_platforms.session_manager import (
    SESSION_STATUS_AUTHENTICATED,
    SESSION_STATUS_DEGRADED,
    SESSION_STATUS_DISABLED,
    SESSION_STATUS_EXPIRED,
    SESSION_STATUS_OFFLINE,
    session_manager as quote_platform_session_manager,
)
from app.services.quote_result_image import save_quote_result_card_image
from app.services.quote_result_validation import quote_result_real_data_error
from app.services.quote_secret_box import decrypt_text, encrypt_json, encrypt_text
from app.services.storage import StorageService
from app.core.config import settings

TZ_BJ = timezone(timedelta(hours=8))
storage = StorageService()
logger = logging.getLogger(__name__)


def _elapsed_ms(start: float) -> int:
    try:
        return max(0, int(round((time.perf_counter() - start) * 1000)))
    except Exception:
        return 0


def _log_quote_perf(
    *,
    stage: str,
    trace_id: str,
    case_id: Any = None,
    task_id: Any = None,
    platform_code: Any = "",
    account_id: Any = None,
    perf: Optional[Mapping[str, Any]] = None,
) -> None:
    try:
        logger.info(
            "[quote_perf] stage=%s trace_id=%s case_id=%s task_id=%s platform=%s account_id=%s perf=%s",
            _to_str(stage).strip() or "-",
            _to_str(trace_id).strip() or "-",
            _to_str(case_id).strip() or "-",
            _to_str(task_id).strip() or "-",
            _to_str(platform_code).strip().upper() or "-",
            _to_str(account_id).strip() or "-",
            json.dumps(dict(perf or {}), ensure_ascii=False, sort_keys=True),
        )
    except Exception:
        logger.debug("quote perf log failed", exc_info=True)


def _quote_result_image_async_enabled() -> bool:
    # 默认异步生成结果图，先把报价文本返回给前端，再由历史页补齐结果图。
    value = _to_str(os.getenv("QUOTE_RESULT_IMAGE_ASYNC_ENABLED", "1")).strip().lower()
    return value not in {"0", "false", "no", "off", "否", "关闭"}


RESULT_SUCCESS = "success"
RESULT_NEED_MORE = "need_more_info"
RESULT_NOT_READY = "not_ready"
RESULT_FAILED = "failed"

# Stable failure taxonomy for chat/API consumers. Do not rename without frontend sync.
FAILURE_CODE_MATERIAL_MISSING = "material_missing"
FAILURE_CODE_MATERIAL_CHANGED = "material_changed"
FAILURE_CODE_DEFAULT_CONFIG_CHANGED = "default_config_changed"
FAILURE_CODE_DEFAULT_CONFIG_MISSING = "default_config_missing"
FAILURE_CODE_ACCOUNT_LOGIN = "account_login"
FAILURE_CODE_SESSION_EXPIRED = "session_expired"
FAILURE_CODE_QUOTA_FULL = "quota_full"
FAILURE_CODE_PLATFORM = "platform"
FAILURE_CODE_RESULT_MATERIALIZATION = "result_materialization"
FAILURE_CODE_SMS_EXPIRED = "sms_expired"
FAILURE_CODE_STALE_TIMEOUT = "stale_timeout"
FAILURE_CODE_ACCOUNT_MISSING = "account_missing"
FAILURE_CODE_CANCELLED_SUPERSEDED = "cancelled_superseded"
FAILURE_CODE_DUPLICATE_QUOTE = "duplicate_quote"
FAILURE_CODE_PREFLIGHT = "preflight_blocked"

QUOTE_FAILURE_NEXT_ACTIONS: Dict[str, str] = {
    FAILURE_CODE_MATERIAL_MISSING: "请补齐材料后重新发起报价",
    FAILURE_CODE_MATERIAL_CHANGED: "请确认材料后重新发起报价",
    FAILURE_CODE_DEFAULT_CONFIG_CHANGED: "请确认默认参数后重新发起报价",
    FAILURE_CODE_DEFAULT_CONFIG_MISSING: "请在默认参数配置中新增并启用该账号类型后再发起报价",
    FAILURE_CODE_ACCOUNT_LOGIN: "请完成平台账号登录后重新发起报价",
    FAILURE_CODE_SESSION_EXPIRED: "请重新登录平台账号后再发起报价",
    FAILURE_CODE_QUOTA_FULL: "请切换可用账号或联系管理员后再发起报价",
    FAILURE_CODE_PLATFORM: "请根据平台提示核实后重试",
    FAILURE_CODE_RESULT_MATERIALIZATION: "请重新发起报价以重新生成结果图",
    FAILURE_CODE_SMS_EXPIRED: "请重新发送平台报价以获取新的验证码",
    FAILURE_CODE_STALE_TIMEOUT: "请重新发起报价；若反复超时请检查账号会话",
    FAILURE_CODE_ACCOUNT_MISSING: "请先配置并登录可用的平台账号",
    FAILURE_CODE_CANCELLED_SUPERSEDED: "请继续当前最新报价请求",
    FAILURE_CODE_DUPLICATE_QUOTE: "请确认是否继续重复投保后再报价",
    FAILURE_CODE_PREFLIGHT: "请按清单补齐后重新发起报价",
}

ACTIVE_CASE_STATUSES = ("collecting", "ready", "waiting_sms", "waiting_duplicate_confirm", "failed", "quoted")
ACTIVE_IMAGE_STATUS = "active"
CASE_STATUS_QUOTED = "quoted"
CASE_STATUS_READY = "ready"
CASE_STATUS_COLLECTING = "collecting"
CASE_STATUS_WAITING_SMS = "waiting_sms"
CASE_STATUS_WAITING_DUPLICATE_CONFIRM = "waiting_duplicate_confirm"
CASE_STATUS_FAILED = "failed"  # legacy read-only; writers keep case ready/collecting after failure
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_WAITING_SMS = "waiting_sms"
TASK_STATUS_WAITING_DUPLICATE_CONFIRM = "waiting_duplicate_confirm"
TASK_STATUS_CANCELLED = "cancelled"
QUOTE_MATERIAL_CHANGED_MESSAGE = "材料已更新，请重新发起报价"
QUOTE_DUPLICATE_CONFIRM_REPLACED_MESSAGE = "报价调整已更新，请重新确认重复投保"
QUOTE_DEFAULT_CONFIG_CHANGED_MESSAGE = "默认参数已更新，请重新发起报价"
QUOTE_SUPERSEDED_MESSAGE = "报价参数已更新，旧报价已停止"
QUOTE_STALE_TIMEOUT_MESSAGE = "报价任务超时，已自动中止"
QUOTE_SMS_EXPIRED_MESSAGE = "短信验证码已过期"
SINGLE_REQUIRED_SLOTS = ("vehicle_cert", "idcard_front", "driving_license_main")
QUOTE_IMAGE_AUTO_CONFIRM_MIN_CONFIDENCE = 0.72
QUOTE_IMAGE_OCR_CLASSIFY_ENABLED = os.getenv("QUOTE_IMAGE_OCR_CLASSIFY_ENABLED", "1") == "1"
try:
    QUOTE_IMAGE_OCR_CALL_TIMEOUT_SECONDS = max(1.0, float(os.getenv("QUOTE_IMAGE_OCR_CALL_TIMEOUT_SECONDS", "4") or "4"))
except Exception:
    QUOTE_IMAGE_OCR_CALL_TIMEOUT_SECONDS = 4.0
try:
    QUOTE_IMAGE_OCR_TOTAL_TIMEOUT_SECONDS = max(1.0, float(os.getenv("QUOTE_IMAGE_OCR_TOTAL_TIMEOUT_SECONDS", "8") or "8"))
except Exception:
    QUOTE_IMAGE_OCR_TOTAL_TIMEOUT_SECONDS = 8.0
try:
    QUOTE_IMAGE_OCR_CONCURRENCY = max(1, min(6, int(os.getenv("QUOTE_IMAGE_OCR_CONCURRENCY", "3") or "3")))
except Exception:
    QUOTE_IMAGE_OCR_CONCURRENCY = 3
try:
    QUOTE_ACCURATE_BASIC_ERROR_COOLDOWN_SECONDS = max(
        0,
        int(os.getenv("QUOTE_ACCURATE_BASIC_ERROR_COOLDOWN_SECONDS", "600") or "600"),
    )
except Exception:
    QUOTE_ACCURATE_BASIC_ERROR_COOLDOWN_SECONDS = 600
try:
    QUOTE_SMS_CODE_TTL_SECONDS = max(60, int(os.getenv("QUOTE_SMS_CODE_TTL_SECONDS", "600") or "600"))
except Exception:
    QUOTE_SMS_CODE_TTL_SECONDS = 600
try:
    QUOTE_RUNNING_TASK_STALE_SECONDS = max(
        300,
        int(os.getenv("QUOTE_RUNNING_TASK_STALE_SECONDS", "900") or "900"),
    )
except Exception:
    QUOTE_RUNNING_TASK_STALE_SECONDS = 900
try:
    PICC_SECURITY_CODE_TTL_SECONDS = max(30, int(os.getenv("PICC_SECURITY_CODE_TTL_SECONDS", "55") or "55"))
except Exception:
    PICC_SECURITY_CODE_TTL_SECONDS = 55

_QUOTE_ACCURATE_BASIC_DISABLED_UNTIL = 0.0
_QUOTE_ACCURATE_BASIC_DISABLED_REASON = ""

OCR_SLOT_CANDIDATES: Tuple[Tuple[str, str, Optional[str]], ...] = (
    ("vehicle_cert", "vehicle_certificate", None),
    ("idcard_front", "idcard", "front"),
    ("idcard_back", "idcard", "back"),
    ("driving_license_main", "vehicle_license", "front"),
    ("driving_license_sub", "vehicle_license", "back"),
    ("related", "accurate_basic", None),
)

UNKNOWN_IMAGE_OCR_CANDIDATES: Tuple[Tuple[str, str, Optional[str]], ...] = (
    ("idcard_front", "idcard", "front"),
    ("driving_license_main", "vehicle_license", "front"),
    ("vehicle_cert", "vehicle_certificate", None),
    ("idcard_back", "idcard", "back"),
    ("driving_license_sub", "vehicle_license", "back"),
    ("related", "accurate_basic", None),
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

# Only platforms with a real quote business flow should pass into account/material checks.
DEVELOPED_QUOTE_PLATFORM_CODES: Set[str] = {"PICC"}

DEFAULT_PLATFORM_CREDENTIAL_FIELDS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "login_phone",
        "label": "接收验证码手机号",
        "type": "phone",
        "required": False,
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

QUOTE_ACCOUNT_TYPE_OPTIONS: Tuple[str, ...] = ("油车-新", "油车-旧", "新能源车-新", "新能源车-旧")
QUOTE_ACCOUNT_TYPE_ALIASES: Dict[str, str] = {
    "新油车": "油车-新",
    "旧油车": "油车-旧",
    "油车新": "油车-新",
    "油车旧": "油车-旧",
    "燃油车新": "油车-新",
    "燃油车旧": "油车-旧",
    "新燃油": "油车-新",
    "旧燃油": "油车-旧",
    "新车": "油车-新",
    "旧车": "油车-旧",
    "二手车": "油车-旧",
    "过户车": "油车-旧",
    "燃油新车": "油车-新",
    "新燃油车": "油车-新",
    "油车新车": "油车-新",
    "燃油旧车": "油车-旧",
    "旧燃油车": "油车-旧",
    "油车旧车": "油车-旧",
    "新能源车": "新能源车-新",
    "旧能源车": "新能源车-旧",
    "新能源新车": "新能源车-新",
    "新新能源车": "新能源车-新",
    "新能源车新": "新能源车-新",
    "旧新能源车": "新能源车-旧",
    "新能源旧车": "新能源车-旧",
    "新能源车旧": "新能源车-旧",
    "新能源新": "新能源车-新",
    "新能源旧": "新能源车-旧",
    "纯电新车": "新能源车-新",
    "纯电旧车": "新能源车-旧",
    "电车新车": "新能源车-新",
    "电车旧车": "新能源车-旧",
    "二手新能源车": "新能源车-旧",
}
QUOTE_ACCOUNT_TYPE_SET = set(QUOTE_ACCOUNT_TYPE_OPTIONS)
QUOTE_FLOW_NORMAL = "normal_motor_quote"
QUOTE_FLOW_RENEWAL = "renewal_motor_quote"
QUOTE_FLOW_TYPE_KEY = "quote_flow_type"
RENEWAL_LOOKUP_OPERATION = "renewal_lookup"
LICENSE_TYPE_DECISION_KEY = "license_type_decision"
LICENSE_TYPE_FUEL = "02"
LICENSE_TYPE_NEW_ENERGY = "52"
LICENSE_COLOR_BY_TYPE: Dict[str, str] = {
    LICENSE_TYPE_FUEL: "01",
    LICENSE_TYPE_NEW_ENERGY: "52",
}

ACCOUNT_LOGIN_NOT_LOGGED_IN = "not_logged_in"
ACCOUNT_LOGIN_LOGGING_IN = "logging_in"
ACCOUNT_LOGIN_NEEDS_CODE = "needs_code"
ACCOUNT_LOGIN_AUTHENTICATED = "authenticated"
ACCOUNT_LOGIN_EXPIRED = "expired"
ACCOUNT_LOGIN_FAILED = "failed"
ACCOUNT_LOGIN_DISABLED = "disabled"
ACCOUNT_LOGIN_DEGRADED = "degraded"

ACCOUNT_QUOTA_UNKNOWN = "unknown"
ACCOUNT_QUOTA_AVAILABLE = "available"
ACCOUNT_QUOTA_WARNING = "warning"
ACCOUNT_QUOTA_FULL = "full"
ACCOUNT_QUOTA_RESET = "reset"
ACCOUNT_QUOTA_PERIOD_DAY = "day"
ACCOUNT_QUOTA_PERIOD_WEEK = "week"
ACCOUNT_QUOTA_PERIOD_MONTH = "month"
ACCOUNT_QUOTA_PERIOD_TYPES = {
    ACCOUNT_QUOTA_PERIOD_DAY,
    ACCOUNT_QUOTA_PERIOD_WEEK,
    ACCOUNT_QUOTA_PERIOD_MONTH,
}
ACCOUNT_QUOTA_PERIOD_LABELS = {
    ACCOUNT_QUOTA_PERIOD_DAY: "\u65e5",
    ACCOUNT_QUOTA_PERIOD_WEEK: "\u5468",
    ACCOUNT_QUOTA_PERIOD_MONTH: "\u6708",
}

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
RUNTIME_QUOTA_FULL_STATUSES = {
    "quota_full",
    "quota_exceeded",
    "limit_exceeded",
    "quota_exhausted",
    "daily_limit_exceeded",
    "weekly_limit_exceeded",
    "monthly_limit_exceeded",
    "usage_limit_exceeded",
    "no_quota",
}
RUNTIME_SESSION_EXPIRED_STATUSES = {"expired", "session_expired", "not_authenticated", "unauthorized", "status_16"}
RUNTIME_SESSION_DEGRADED_STATUSES = {"degraded", "timeout", "network_error", "conflict"}
RUNTIME_STATUS_USER_LABELS = {
    "success": "处理成功",
    "ok": "处理成功",
    "quoted": "报价成功",
    "failed": "平台处理失败",
    "disabled": "账号已停用",
    "expired": "登录已过期",
    "session_expired": "登录已过期",
    "not_authenticated": "账号未登录",
    "unauthorized": "账号授权已失效",
    "timeout": "平台响应超时",
    "network_error": "网络连接异常",
    "conflict": "账号正在被其他请求处理，请稍后重试",
    "duplicate_quote": "平台提示该车辆已报价过",
    "duplicate_quote_confirm_required": "平台提示该车辆可能重复投保，等待确认",
    "quota_full": "查询额度已用完",
    "quota_exceeded": "查询额度已用完",
    "limit_exceeded": "查询额度已用完",
    "quota_exhausted": "查询额度已用完",
    "daily_limit_exceeded": "今日查询额度已用完",
    "weekly_limit_exceeded": "本周查询额度已用完",
    "monthly_limit_exceeded": "本月查询额度已用完",
    "usage_limit_exceeded": "查询额度已用完",
    "no_quota": "查询额度已用完",
}

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


def _platform_code_from_display_name(platform_name: Any) -> str:
    name = _to_str(platform_name).strip()
    if not name:
        return ""
    low = name.lower()
    for code, (display_name, aliases) in PLATFORM_ALIASES.items():
        candidates = {display_name, *aliases, code}
        if any(low == _to_str(candidate).strip().lower() for candidate in candidates if _to_str(candidate).strip()):
            return code
    return ""


def _quote_platform_name_from_command(text: Any) -> str:
    raw = _norm_text(text)
    if not raw:
        return ""
    professional = _detect_professional_quote_command(raw)
    professional_entities = _json_obj(professional.get("entities"))
    if professional.get("is_quote") and professional_entities.get("platform_name"):
        return _to_str(professional_entities.get("platform_name")).strip()
    return ""


def _is_quote_platform_developed(platform_code: Any) -> bool:
    code = _to_str(platform_code).strip().upper()
    return bool(code and code in DEVELOPED_QUOTE_PLATFORM_CODES)


def _unsupported_quote_platform_response(
    *,
    platform_code: Any = "",
    platform_name: Any = "",
    entities: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    code = _to_str(platform_code).strip().upper()
    display_name = _to_str(platform_name).strip()
    if not display_name and code and code != "STUB":
        display_name = _platform_display_name(code)
    if not display_name or display_name.upper() == "STUB":
        display_name = "该平台"
    message = f"暂未增加{display_name}平台报价流程，请耐心等待。"
    return (
        message,
        {
            "status": "success",
            "intent": "quote",
            "trace_id": _new_trace_id(),
            "data": _mk_data(
                result_status=RESULT_NOT_READY,
                message="报价平台暂未接入",
                entities={
                    **(entities or {}),
                    "platform_code": code,
                    "platform_name": display_name,
                },
                payload={"unsupported_platform": True},
            ),
            "actions": [],
        },
    )


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


def sanitize_quote_user_message(
    value: Any,
    default_message: str = "",
    *,
    platform_code: str = "",
    platform_name: str = "",
) -> str:
    """Convert adapter/runtime diagnostics into business-facing Chinese text."""
    raw = _to_str(value).strip()
    if not raw:
        return default_message

    code = _to_str(platform_code).strip().upper()
    display_name = _platform_display_name(code, platform_name) if (code or platform_name) else ""
    text = raw
    low = text.lower()
    protected_values: List[str] = []

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[^>]+>", "", text)
    low = text.lower()

    if (
        re.search(r"\b(?:502|503|504)\b", raw)
        or re.search(r"\b(?:bad gateway|gateway timeout|service unavailable)\b", low, flags=re.IGNORECASE)
        or re.search(r"</?(?:html|head|body|center|h1)\b", low, flags=re.IGNORECASE)
    ):
        prefix = f"{display_name}平台" if display_name else "平台"
        return f"{prefix}网关临时异常，请稍后重试"

    def protect_material_value(match: re.Match[str]) -> str:
        protected_values.append(match.group(2))
        return f"{match.group(1)}【材料值{len(protected_values) - 1}】"

    text = re.sub(
        r"((?:车辆合格证|行驶证|身份证|车主|平台)?"
        r"(?:发动机号|车架号|VIN|车型名称|车型|品牌型号|号牌号码|车牌号|身份证号|证件号)"
        r"[^：:，。；\n]{0,12}[：:])([^，。；\n（）)]{2,64})",
        protect_material_value,
        text,
        flags=re.IGNORECASE,
    )

    session_key_error = (
        re.fullmatch(
            r"\s*(?:keyerror\s*)?\(?\s*['\"]?(?:session|session_snapshot|jsessionid|jsession_id)['\"]?\s*\)?\s*",
            raw,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\bkeyerror\b.*(?:session|session_snapshot|jsessionid|jsession_id)",
            low,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\ban\s+['\"](?:session|session_snapshot|jsessionid|jsession_id)['\"]",
            low,
            flags=re.IGNORECASE,
        )
        or (
            re.search(r"['\"](?:session|session_snapshot|jsessionid|jsession_id)['\"]", low, flags=re.IGNORECASE)
            and (
                "异常" in raw
                or "验证码" in raw
                or "登录" in raw
                or "challenge" in low
                or "platform" in low
            )
        )
    )
    if session_key_error:
        text = "登录会话已失效，请重新点击登录"
        low = text.lower()

    config_name_replacements = (
        (r"\bPICC_CAPTCHA_USERNAME\b", "打码平台账号"),
        (r"\bPICC_CAPTCHA_PASSWORD\b", "打码平台密码"),
        (r"\bPICC_CAPTCHA_API_URL\b", "打码平台接口地址"),
        (r"\bPICC_CAPTCHA_TYPE_ID\b", "打码平台验证码类型"),
        (r"\bPICC_CAPTCHA_TIMEOUT\b", "打码平台超时时间"),
        (r"\bPICC_CAPTCHA_MAX_ROUNDS\b", "滑块最大重试次数"),
    )
    for pattern, repl in config_name_replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    text = re.sub(r"请设置\s*打码平台账号\s*/\s*打码平台密码", "请设置打码平台账号和打码平台密码", text)
    if "滑块验证码识别未配置" in text and re.search(r"请设置\s*/\s*$", text):
        text = re.sub(r"请设置\s*/\s*$", "请设置打码平台账号和打码平台密码", text)

    if re.fullmatch(r"\s*(?:No permission(?:\s+to\s+access\s+data)?|error_code\s*=\s*6|无权限)\s*", text, flags=re.IGNORECASE):
        text = "接口暂无访问权限，请检查账号权限或稍后重试"
    elif re.fullmatch(r"\s*(?:timed out|timeout|TimeoutError|超时)\s*", text, flags=re.IGNORECASE):
        text = "平台响应超时，请稍后重试"
    elif re.fullmatch(r"\s*(?:ConnectionError|network_error|连接异常|网络异常)\s*", text, flags=re.IGNORECASE):
        text = "平台网络连接异常，请稍后重试"

    replacements = (
        (r"(?<![A-Za-z0-9_])PICC(?![A-Za-z0-9_])", "人保"),
        (r"(?<![A-Za-z0-9_])CPIC(?![A-Za-z0-9_])", "太平洋"),
        (r"(?<![A-Za-z0-9_])OCR(?![A-Za-z0-9_])", "文字识别"),
        (r"(?<![A-Za-z0-9_])VIN(?![A-Za-z0-9_])", "车架号"),
        (r"(?<![A-Za-z0-9_])JSON(?![A-Za-z0-9_])", "返回格式"),
        (r"(?<![A-Za-z0-9_])HTTP\s*=?\s*\d*", "接口响应异常"),
        (r"(?<![A-Za-z0-9_])accurate_basic(?![A-Za-z0-9_])", "通用文字识别"),
        (r"\bOcrCallError\b", "文字识别调用失败"),
        (r"\bOcrNotConfigured\b", "文字识别服务未配置"),
        (r"\bTimeoutError\b", "处理超时"),
        (r"\bConnectionError\b", "网络连接异常"),
        (r"\bOperationalError\b", "数据库连接异常"),
        (r"\bInternal Server Error\b", "服务器处理异常"),
        (r"\bNo permission(?:\s+to\s+access\s+data)?\b", "接口暂无访问权限"),
        (r"\bstatusText\b", "平台返回提示"),
        (r"\bresponse\b", "平台返回"),
        (r"\bdistance\s*=\s*\d+\b", "滑块校验信息"),
        (r"\bencodeKey\b", "登录上下文"),
        (r"\bJSESSIONID\b", "登录会话"),
        (r"\bUSER_TOKEN\b", "登录令牌"),
        (r"\bRSA\b", "密码加密"),
    )
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    text = re.sub(r"请输入\s+([\u4e00-\u9fffA-Za-z0-9]+)\s+平台", r"请输入\1平台", text)

    text = re.sub(
        r"error_msg\s*[:=]\s*No permission(?:\s+to\s+access\s+data)?",
        "接口暂无访问权限",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"error_code\s*[:=]\s*[-\w.]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"error_msg\s*[:=]\s*[^，。；\n]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"status\s*[:=：]\s*[-\w.]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"body\s*[:=：]\s*.+", "返回内容异常", text, flags=re.IGNORECASE)
    text = re.sub(r"token|authorization|cookie|session|trace|stack|payload|debug", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:KeyError|ValueError|TypeError|RuntimeError|Traceback|AxiosError|TimeoutError|ConnectionError|Exception)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bERR_[A-Z_]+\b", "", text)
    text = re.sub(r"Network Error", "网络连接异常", text, flags=re.IGNORECASE)
    text = re.sub(r"Request failed", "请求处理失败", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[A-Za-z][A-Za-z0-9_./:-]{2,}\b", "", text)
    for index, protected in enumerate(protected_values):
        text = text.replace(f"【材料值{index}】", protected)
    text = re.sub(r"[ \t\r\f\v]+([，。；、）])", r"\1", text)
    text = re.sub(r"([（：])[ \t\r\f\v]+", r"\1", text)
    text = re.sub(r"[，；： \t\r\f\v]+$", "", text)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if display_name:
        text = text.replace(code, display_name)
    return text or default_message or "处理失败，请稍后重试"


def _sanitize_duplicate_quote_warning(value: Any, default_message: str = "") -> str:
    raw = _to_str(value).strip()
    if not raw:
        return default_message
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"</?[^>]+>", "", text)
    text = re.sub(r"error_code\s*[:=]\s*[-\w.]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"error_msg\s*[:=]\s*[^，。；\n]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"status\s*[:=：]\s*[-\w.]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"token|authorization|cookie|session|trace|stack|payload|debug", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t\r\f\v]+([，。；、）])", r"\1", text)
    text = re.sub(r"([（：:])[ \t\r\f\v]+", r"\1", text)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or default_message


PLATFORM_DIALOG_MESSAGE_KEYS = (
    "platform_dialog",
    "businessControlMsg",
    "businessMsg",
    "errorMessage",
    "errorMsg",
    "resultMessage",
    "resultMsg",
    "normalizeErrorMsg",
    "statusText",
    "message",
    "msg",
    "detail",
    "reason",
)


def _sanitize_platform_dialog_message(
    value: Any,
    default_message: str = "",
    *,
    platform_code: str = "",
    platform_name: str = "",
) -> str:
    text = _sanitize_duplicate_quote_warning(value, default_message)
    if not text:
        return default_message
    display_name = _platform_display_name(platform_code, platform_name) if (platform_code or platform_name) else ""
    low = text.lower()
    if (
        re.search(r"\b(?:502|503|504)\b", text)
        or re.search(r"\b(?:bad gateway|gateway timeout|service unavailable)\b", low, flags=re.IGNORECASE)
        or re.search(r"</?(?:html|head|body|center|h1)\b", low, flags=re.IGNORECASE)
    ):
        prefix = f"{display_name}平台" if display_name else "平台"
        return f"{prefix}网关临时异常，请稍后重试"
    text = re.sub(r"token|authorization|cookie|session|trace|stack|payload|debug", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:KeyError|ValueError|TypeError|RuntimeError|Traceback|AxiosError|TimeoutError|ConnectionError|Exception)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bERR_[A-Z_]+\b", "", text)
    text = re.sub(r"Network Error", "网络连接异常", text, flags=re.IGNORECASE)
    text = re.sub(r"Request failed", "请求处理失败", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\d+\s*(?=当前|该|此|请|业务|错误|提示)", "", text).strip()
    text = re.sub(r"(?:。)?请检查平台账号状态或报价资料后重试[。.]?$", "", text).strip()
    text = "\n".join(
        line
        for line in text.splitlines()
        if not re.fullmatch(r"\s*(?:错误标识码|错误码|标识码)\s*[:：]?\s*", line)
    )
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or default_message


def _platform_dialog_candidate_texts(value: Any, *, depth: int = 0) -> List[str]:
    if depth > 5:
        return []
    out: List[str] = []
    if isinstance(value, Mapping):
        for key in PLATFORM_DIALOG_MESSAGE_KEYS:
            item = value.get(key)
            if item in (None, "", {}, []):
                continue
            if isinstance(item, (Mapping, list, tuple)):
                out.extend(_platform_dialog_candidate_texts(item, depth=depth + 1))
            else:
                text = _to_str(item).strip()
                if text:
                    out.append(text)
        for item in value.values():
            if isinstance(item, (Mapping, list, tuple)):
                out.extend(_platform_dialog_candidate_texts(item, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_platform_dialog_candidate_texts(item, depth=depth + 1))
    elif value not in (None, ""):
        text = _to_str(value).strip()
        if text:
            out.append(text)
    return out


def _runtime_platform_dialog_message(
    result: Optional[PlatformRuntimeResult],
    default_message: str,
    *,
    platform_code: str = "",
    platform_name: str = "",
) -> str:
    data = _json_obj(getattr(result, "data", None) if result is not None else None)
    candidates: List[Any] = []
    platform_response = _json_obj(data.get("platform_response"))
    platform_dialog = _json_obj(data.get("platform_dialog"))
    platform_response_body = _json_obj(platform_response.get("response"))
    candidates.extend(
        [
            platform_dialog.get("message"),
            platform_dialog.get("content"),
            platform_response.get("raw_message"),
            platform_response_body.get("normalizeErrorMsg"),
            platform_response_body.get("errorMsg"),
            platform_response_body.get("errorMessage"),
            platform_response_body.get("businessControlMsg"),
            platform_response_body.get("businessMsg"),
            platform_response.get("message"),
            platform_response.get("statusText"),
            platform_response.get("errorMessage"),
            platform_response_body.get("statusText"),
            data.get("platform_status_text"),
            data.get("error_message"),
            data.get("message"),
        ]
    )
    candidates.extend(_platform_dialog_candidate_texts(platform_response))
    candidates.extend(_platform_dialog_candidate_texts(data.get("response")))
    if result is not None:
        candidates.append(getattr(result, "message", ""))

    seen: Set[str] = set()
    for raw in candidates:
        text = _sanitize_platform_dialog_message(
            raw,
            "",
            platform_code=platform_code,
            platform_name=platform_name,
        )
        compact = re.sub(r"\s+", "", text)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        if compact.lower() in {"success", "ok", "fail", "failed", "error", "错误", "错误信息"}:
            continue
        return text
    return _sanitize_platform_dialog_message(
        default_message,
        default_message,
        platform_code=platform_code,
        platform_name=platform_name,
    )


def _renewal_lookup_failure_text(value: Any) -> str:
    text = _to_str(value).strip()
    if not text:
        return ""
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return ""
    if re.search(
        r"(没有此车辆信息或不是可续保车辆|没有此车辆信息|此车辆信息不存在|车辆信息不存在|"
        r"不是可续保车辆|不是续保车辆|不是续保车|不可续保车辆|非可续保车辆|"
        r"续保车辆不存在|无续保车辆|未查询到续保|没有续保信息|无续保信息|"
        r"不满足续保|无法续保)",
        compact,
    ):
        return text
    return ""


def _extract_renewal_lookup_failure_text(result: Optional[PlatformRuntimeResult]) -> str:
    data = _json_obj(getattr(result, "data", None) if result is not None else None)
    platform_response = _json_obj(data.get("platform_response"))
    platform_dialog = _json_obj(data.get("platform_dialog"))
    platform_response_body = _json_obj(platform_response.get("response"))
    candidates: List[Any] = [
        platform_dialog.get("message"),
        platform_dialog.get("content"),
        platform_response.get("raw_message"),
        platform_response_body.get("normalizeErrorMsg"),
        platform_response_body.get("errorMsg"),
        platform_response_body.get("errorMessage"),
        platform_response_body.get("businessControlMsg"),
        platform_response_body.get("businessMsg"),
        platform_response.get("message"),
        platform_response.get("statusText"),
        platform_response.get("errorMessage"),
        platform_response_body.get("statusText"),
        data.get("platform_status_text"),
        data.get("error_message"),
        data.get("message"),
        getattr(result, "message", "") if result is not None else "",
    ]
    candidates.extend(_platform_dialog_candidate_texts(data))
    candidates.extend(_platform_dialog_candidate_texts(platform_response))
    seen: Set[str] = set()
    for raw in candidates:
        text = _renewal_lookup_failure_text(raw)
        compact = re.sub(r"\s+", "", text)
        if text and compact not in seen:
            seen.add(compact)
            return text
    blob = json.dumps(data, ensure_ascii=False, default=str)
    match = re.search(
        r"(没有此车辆信息或不是可续保车辆|没有此车辆信息|此车辆信息不存在|车辆信息不存在|"
        r"不是可续保车辆|不是续保车辆|不是续保车|不可续保车辆|非可续保车辆|"
        r"续保车辆不存在|无续保车辆|未查询到续保|没有续保信息|无续保信息|"
        r"不满足续保|无法续保)",
        blob,
    )
    return match.group(1) if match else ""


def _platform_dialog_id(
    *,
    subtype: str,
    message: str,
    trace_id: str = "",
    task_id: Any = None,
    case_id: Any = None,
) -> str:
    base = "|".join(
        [
            _to_str(subtype).strip() or "platform_notice",
            _to_str(task_id).strip(),
            _to_str(trace_id).strip(),
            _to_str(case_id).strip(),
            _to_str(message).strip()[:240],
        ]
    )
    digest = hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:12]
    anchor = _to_str(task_id).strip() or _to_str(trace_id).strip() or _to_str(case_id).strip() or digest
    return f"platform_dialog:{_to_str(subtype).strip() or 'platform_notice'}:{anchor}:{digest}"


def _make_platform_dialog(
    *,
    message: Any,
    title: str = "报价提示",
    subtype: str = "platform_notice",
    severity: str = "warning",
    confirm_required: bool = False,
    trace_id: str = "",
    task_id: Any = None,
    case_id: Any = None,
    confirm_text: str = "",
    cancel_text: str = "",
    close_text: str = "",
    confirm_command: str = "",
    cancel_command: str = "",
    platform_code: str = "",
    platform_name: str = "",
) -> Dict[str, Any]:
    if subtype == "duplicate_quote":
        safe_message = _sanitize_duplicate_quote_warning(
            message,
            "平台提示该车辆可能重复投保，请核实后再继续报价。",
        )
    else:
        safe_message = _sanitize_platform_dialog_message(
            message,
            "平台返回报价提示，请核实后再继续。",
            platform_code=platform_code,
            platform_name=platform_name,
        )
    dialog = {
        "id": _platform_dialog_id(
            subtype=subtype,
            message=safe_message,
            trace_id=trace_id,
            task_id=task_id,
            case_id=case_id,
        ),
        "type": "confirm" if confirm_required else "notice",
        "subtype": subtype,
        "title": title or "报价提示",
        "message": safe_message,
        "severity": severity or "warning",
        "confirm_required": bool(confirm_required),
        "confirm_text": confirm_text or ("继续报价" if confirm_required else "确定"),
        "cancel_text": cancel_text or ("中止" if confirm_required else ""),
        "close_text": close_text or ("关闭" if not confirm_required else ""),
        "platform_code": _to_str(platform_code).strip().upper(),
        "platform_name": _platform_display_name(platform_code, platform_name) if (platform_code or platform_name) else "",
        "ui_visible": True,
    }
    if confirm_command:
        dialog["confirm_action"] = {"command": confirm_command}
    if cancel_command:
        dialog["cancel_action"] = {"command": cancel_command}
    return dialog


def _platform_dialog_from_source(
    source: Any,
    *,
    trace_id: str = "",
    task_id: Any = None,
    case_id: Any = None,
    platform_code: str = "",
    platform_name: str = "",
) -> Dict[str, Any]:
    source_dialog = _json_obj(source)
    if not source_dialog:
        return {}
    source_confirm_action = _json_obj(source_dialog.get("confirm_action"))
    source_cancel_action = _json_obj(source_dialog.get("cancel_action"))
    source_confirm_required = (
        source_dialog.get("confirm_required") is True
        or _to_str(source_dialog.get("type")).strip().lower() == "confirm"
    )
    message = _to_str(source_dialog.get("message")).strip() or _to_str(source_dialog.get("content")).strip()
    if not message:
        return {}
    return _make_platform_dialog(
        message=message,
        title=_to_str(source_dialog.get("title")).strip() or "报价提示",
        subtype=_to_str(source_dialog.get("subtype")).strip() or "platform_notice",
        severity=_to_str(source_dialog.get("severity")).strip() or "warning",
        confirm_required=source_confirm_required,
        trace_id=trace_id,
        task_id=task_id,
        case_id=case_id,
        confirm_text=_to_str(source_dialog.get("confirm_text")).strip(),
        cancel_text=_to_str(source_dialog.get("cancel_text")).strip(),
        close_text=_to_str(source_dialog.get("close_text")).strip(),
        confirm_command=_to_str(source_confirm_action.get("command") or source_dialog.get("confirm_command")).strip(),
        cancel_command=_to_str(source_cancel_action.get("command") or source_dialog.get("cancel_command")).strip(),
        platform_code=platform_code,
        platform_name=platform_name,
    )


def _duplicate_quote_platform_dialog(
    *,
    warning: str,
    platform_code: str = "",
    platform_name: str = "",
    trace_id: str = "",
    task_id: Any = None,
    case_id: Any = None,
) -> Dict[str, Any]:
    return _make_platform_dialog(
        message=warning,
        title="重复投保提示",
        subtype="duplicate_quote",
        severity="warning",
        confirm_required=True,
        trace_id=trace_id,
        task_id=task_id,
        case_id=case_id,
        confirm_text="继续报价",
        cancel_text="中止",
        confirm_command="继续报价",
        cancel_command="中止重复报价",
        platform_code=platform_code,
        platform_name=platform_name,
    )


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
    ("vin", "车架号"),
    ("engine_no", "发动机号"),
    ("vehicle_model", "品牌型号/车型"),
)

CORE_REQUIRED_FIELDS_BY_ACCOUNT_TYPE: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "油车-新": (
        ("engine_no", "发动机号"),
        ("vin", "车架号"),
        ("vehicle_model", "车型名称"),
        ("owner_name", "车主姓名"),
    ),
    "油车-旧": (
        ("plate_no", "号牌号码"),
        ("engine_no", "发动机号"),
        ("vin", "车架号"),
        ("first_register_date", "初登日期"),
        ("vehicle_model", "车型名称"),
        ("owner_name", "车主姓名"),
    ),
    "新能源车-新": (
        ("engine_no", "发动机号"),
        ("vin", "车架号"),
        ("vehicle_model", "车型名称"),
        ("owner_name", "车主姓名"),
    ),
    "新能源车-旧": (
        ("plate_no", "号牌号码"),
        ("engine_no", "发动机号"),
        ("vin", "车架号"),
        ("first_register_date", "初登日期"),
        ("vehicle_model", "车型名称"),
        ("owner_name", "车主姓名"),
    ),
}

QUOTE_MANUAL_MATERIAL_FIELD_ORDER: Tuple[Tuple[str, str, str], ...] = (
    ("account_type_name", "报价类型", "select"),
    ("license_type", "号牌种类", "select"),
    ("owner_name", "车主姓名", "text"),
    ("owner_phone", "车主手机号", "text"),
    ("id_number", "身份证号", "text"),
    ("plate_no", "号牌号码", "text"),
    ("engine_no", "发动机号", "text"),
    ("vin", "VIN/车架号", "text"),
    ("first_register_date", "初登日期", "date"),
    ("issue_date", "行驶证发证日期", "date"),
    ("commercial_start_date", "商业起保日期", "date"),
    ("compulsory_start_date", "交强起保日期", "date"),
    ("vehicle_model", "车型名称", "text"),
    ("car_name", "销售车型", "text"),
)

QUOTE_MANUAL_EXTRA_CONFIG_FIELD_ORDER: Tuple[Tuple[str, str, str], ...] = (
    ("机动车增值服务特约条款（道路救援服务）", "道路救援次数", "text"),
    ("附加外部电网故障损失险", "外部电网故障损失险", "text"),
)

QUOTE_MANUAL_EXTRA_CONFIG_FIELD_KEYS = {key for key, _, _ in QUOTE_MANUAL_EXTRA_CONFIG_FIELD_ORDER}

QUOTE_MATERIAL_FORM_REQUIRED_BY_TYPE: Dict[str, Tuple[str, ...]] = {
    type_name: tuple(key for key, _ in fields)
    for type_name, fields in CORE_REQUIRED_FIELDS_BY_ACCOUNT_TYPE.items()
}

CORE_REQUIRED_SLOTS_BY_ACCOUNT_TYPE: Dict[str, Tuple[str, ...]] = {
    "油车-新": ("vehicle_cert",),
    "油车-旧": ("driving_license_main",),
    "新能源车-新": ("vehicle_cert",),
    "新能源车-旧": ("driving_license_main",),
}

QUOTE_CONFIG_OVERRIDE_ALIASES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("途家安顺保费", ("途家安顺保费", "途家安顺", "途顺家安", "途家安顺非车保费", "非车", "意外", "意外险", "驾乘意外")),
    ("机动车损失保险", ("机动车损失保险", "车辆损失险", "车损险", "车损")),
    ("医保外医疗费用责任险（第三者责任险）", ("医保外医疗费用责任险（第三者责任险）", "医保外医疗费用责任险(第三者责任险)", "医保外三者", "医保外")),
    ("第三者责任险", ("机动车第三者责任保险", "第三者责任险", "第三责任险", "第三者", "三者险", "三者", "三责")),
    ("车上人员责任险（司机）", ("车上人员责任险（司机）", "车上人员责任险(司机)", "司机责任险", "司机险", "司机")),
    ("车上人员责任险（乘客）", ("车上人员责任险（乘客）", "车上人员责任险(乘客)", "乘客责任险", "乘客险", "乘客")),
    ("机动车增值服务特约条款（道路救援服务）", ("机动车增值服务特约条款（道路救援服务）", "附加机动车增值服务特约条款（道路救援服务）", "道路救援服务", "道路救援", "救援")),
    ("附加外部电网故障损失险", ("附加外部电网故障损失险", "外部电网故障损失险", "外部电网", "电网故障损失险", "电网故障")),
    ("交强", ("交强险", "交强", "交强主险")),
    ("共享主险限额", ("共享主险限额", "主险限额共享")),
)

QUOTE_THIRD_PARTY_LABEL = "第三者责任险"
QUOTE_MEDICAL_THIRD_LABEL = "医保外医疗费用责任险（第三者责任险）"
QUOTE_SHARED_LIMIT_LABEL = "共享主险限额"
QUOTE_LOSS_LABEL = "机动车损失保险"
QUOTE_DRIVER_LABEL = "车上人员责任险（司机）"
QUOTE_PASSENGER_LABEL = "车上人员责任险（乘客）"
QUOTE_ROAD_RESCUE_LABEL = "机动车增值服务特约条款（道路救援服务）"
QUOTE_EXTERNAL_GRID_LABEL = "附加外部电网故障损失险"
QUOTE_COMPULSORY_LABEL = "交强"
QUOTE_PRODUCT_EXCLUSIONS_KEY = "quote_product_exclusions"

PICC_FULL_COVER_COMMANDS = {
    "全保",
    "人保全保",
    "中国人保全保",
    "PICC全保",
    "人保报价",
    "中国人保报价",
    "PICC报价",
    "全保报价",
    "人保全保报价",
    "中国人保全保报价",
    "PICC全保报价",
    "人保重报",
    "中国人保重报",
    "PICC重报",
    "全保重报",
    "人保全保重报",
    "中国人保全保重报",
    "PICC全保重报",
}
PICC_JIAOSAN_COMMANDS = {
    "交三",
    "人保交三",
    "中国人保交三",
    "PICC交三",
    "交三报价",
    "人保交三报价",
    "中国人保交三报价",
    "PICC交三报价",
    "交三重报",
    "人保交三重报",
    "中国人保交三重报",
    "PICC交三重报",
}
PICC_DANSHANG_COMMANDS = {
    "单商",
    "人保单商",
    "中国人保单商",
    "PICC单商",
    "单商报价",
    "人保单商报价",
    "中国人保单商报价",
    "PICC单商报价",
    "单商重报",
    "人保单商重报",
    "中国人保单商重报",
    "PICC单商重报",
}
PICC_RENEWAL_FULL_COVER_COMMANDS = {
    "续保",
    "人保续保",
    "中国人保续保",
    "PICC续保",
    "续保报价",
    "人保续保报价",
    "中国人保续保报价",
    "PICC续保报价",
    "续保全保",
    "人保续保全保",
    "中国人保续保全保",
    "PICC续保全保",
    "续保全保报价",
    "人保续保全保报价",
    "中国人保续保全保报价",
    "PICC续保全保报价",
}
PICC_RENEWAL_JIAOSAN_COMMANDS = {
    "续保交三",
    "人保续保交三",
    "中国人保续保交三",
    "PICC续保交三",
    "续保交三报价",
    "人保续保交三报价",
    "中国人保续保交三报价",
    "PICC续保交三报价",
}
PICC_RENEWAL_DANSHANG_COMMANDS = {
    "续保单商",
    "人保续保单商",
    "中国人保续保单商",
    "PICC续保单商",
    "续保单商报价",
    "人保续保单商报价",
    "中国人保续保单商报价",
    "PICC续保单商报价",
}


def _picc_quote_command_mode_from_compact(compact: str) -> str:
    text = re.sub(r"\s+", "", _to_str(compact))
    if not text:
        return ""
    platform_prefixed = (
        text.startswith("人保")
        or text.startswith("中国人保")
        or text.upper().startswith("PICC")
    )
    if text in PICC_DANSHANG_COMMANDS:
        return "单商"
    if text in PICC_JIAOSAN_COMMANDS:
        return "交三"
    if text in PICC_FULL_COVER_COMMANDS:
        return "全保"
    if (
        "单商" in text
        and (
            platform_prefixed
            or text.startswith("单商")
            or "人保单商" in text
            or "中国人保单商" in text
            or "PICC单商" in text.upper()
        )
    ):
        return "单商"
    if (
        "交三" in text
        and (
            platform_prefixed
            or text.startswith("交三")
            or "人保交三" in text
            or "中国人保交三" in text
            or "PICC交三" in text.upper()
        )
    ):
        return "交三"
    if (
        "全保" in text
        and (
            platform_prefixed
            or text.startswith("全保")
            or "人保全保" in text
            or "中国人保全保" in text
            or "PICC全保" in text.upper()
        )
    ):
        return "全保"
    if any(keyword in text.upper() for keyword in ("人保报价", "中国人保报价", "PICC报价", "人保重报", "中国人保重报", "PICC重报")):
        return "全保"
    return ""


def _picc_quote_flow_command_from_compact(compact: str) -> Tuple[str, str]:
    text = re.sub(r"\s+", "", _to_str(compact))
    if not text:
        return "", ""
    if text in PICC_RENEWAL_DANSHANG_COMMANDS:
        return QUOTE_FLOW_RENEWAL, "单商"
    if text in PICC_RENEWAL_JIAOSAN_COMMANDS:
        return QUOTE_FLOW_RENEWAL, "交三"
    if text in PICC_RENEWAL_FULL_COVER_COMMANDS:
        return QUOTE_FLOW_RENEWAL, "全保"
    if "续保" in text and (
        text.startswith("续保")
        or text.startswith("人保续保")
        or text.startswith("中国人保续保")
        or text.upper().startswith("PICC续保")
    ):
        if "单商" in text:
            return QUOTE_FLOW_RENEWAL, "单商"
        if "交三" in text:
            return QUOTE_FLOW_RENEWAL, "交三"
        return QUOTE_FLOW_RENEWAL, "全保"
    mode = _picc_quote_command_mode_from_compact(text)
    return (QUOTE_FLOW_NORMAL, mode) if mode else ("", "")

QUOTE_CONFIG_GENERIC_FIELD_BLOCKLIST = {
    "车牌号",
    "号牌号码",
    "车架号",
    "VIN",
    "VIN车架号",
    "发动机号",
    "车主",
    "车主姓名",
    "姓名",
    "手机号",
    "车主手机号",
    "身份证号",
    "车型",
    "车型名称",
    "品牌型号",
    "送修码",
    "送修码代码",
    "送修码名称",
    "专管代码",
    "专管名称",
    "monopolyCode",
    "monopolyName",
}

QUOTE_DATA_OVERRIDES_KEY = "quote_data_overrides"

QUOTE_DATA_OVERRIDE_ALIASES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("owner_name", "车主姓名", ("车主姓名", "车主名称", "客户姓名", "客户名称", "行驶证所有人", "所有人", "被保险人姓名", "被保险人", "投保人姓名", "投保人", "联系人姓名", "车主", "姓名")),
    ("id_name", "身份证姓名", ("身份证姓名", "证件姓名", "身份证名字", "证件名字")),
    ("owner_phone", "车主手机号", ("车主手机号", "车主手机", "车主电话", "客户手机号", "客户手机", "被保险人手机号", "被保人手机号", "投保人手机号", "联系电话", "手机号", "手机号码", "手机", "电话")),
    ("id_number", "身份证号", ("身份证号", "身份证号码", "证件号", "证件号码", "身份证")),
    ("plate_no", "车牌号", ("号牌号码", "车牌号码", "车牌号", "号牌", "车牌")),
    ("vin", "车架号", ("车辆识别代号", "VIN码", "VIN", "车架号", "车架")),
    ("engine_no", "发动机号", ("发动机号码", "发动机号", "发动机")),
    ("vehicle_model", "车型名称", ("车辆品牌/车辆名称", "车辆品牌/车辆型号", "品牌型号", "车型名称", "车辆型号", "车型")),
    ("car_name", "销售车型", ("销售车型", "英文车型", "车型简称", "CarName", "VehicleName")),
    ("first_register_date", "初登日期", ("初登日期", "初登", "初次登记日期", "注册日期", "登记日期")),
    ("issue_date", "行驶证发证日期", ("行驶证发证日期", "发证日期", "发证时间")),
    ("commercial_start_date", "商业起保日期", ("商业起保日期", "商业险起保日期", "商业起保", "商业险起期")),
    ("compulsory_start_date", "交强起保日期", ("交强起保日期", "交强险起保日期", "交强起保", "交强险起期")),
    ("license_type", "号牌种类", ("号牌种类", "号牌类型", "牌照类型", "车牌类型", "牌照种类", "licenseType")),
)

QUOTE_DATA_OVERRIDE_LABELS: Dict[str, str] = {
    key: label for key, label, _ in QUOTE_DATA_OVERRIDE_ALIASES
}

QUOTE_IMAGE_FIELDS_BY_SLOT: Dict[str, Tuple[str, ...]] = {
    "vehicle_cert": (
        "vin",
        "engine_no",
        "vehicle_model",
        "car_name",
        "vehicle_type",
        "vehicle_brand_name",
        "manufacturer_name",
        "approved_passenger_count",
        "energy_type",
        "vehicle_energy_type",
        "fuel_type",
        "fuel_kind",
    ),
    "driving_license_main": (
        "plate_no",
        "owner_name",
        "use_nature",
        "first_register_date",
        "issue_date",
        "issuer_org",
        "vin",
        "engine_no",
        "vehicle_model",
        "car_name",
        "vehicle_brand_name",
        "vehicle_type",
    ),
    "driving_license_sub": (
        "use_nature",
        "issuer_org",
        "issue_date",
    ),
    "idcard_front": (
        "id_name",
        "id_number",
        "id_address",
        "id_gender",
        "id_ethnicity",
        "id_birth_date",
    ),
    "idcard_back": (
        "id_issuer",
        "id_validity",
        "id_valid_from",
        "id_valid_to",
    ),
    "related": (
        "commercial_start_date",
        "compulsory_start_date",
        "approved_passenger_count",
        "quote_field_overrides",
    ),
}
QUOTE_IMAGE_MANAGED_FIELDS = frozenset(
    field for fields in QUOTE_IMAGE_FIELDS_BY_SLOT.values() for field in fields
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


def _json_for_hash(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _fmt_dt(value) or value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_for_hash(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_json_for_hash(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _to_str(value)


def _stable_json_dumps(value: Any) -> str:
    return json.dumps(
        _json_for_hash(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_stable_json_dumps(value).encode("utf-8")).hexdigest()


def _compact_quote_data(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        out[key] = value
    return out


def _normalize_owner_phone(value: Any) -> Optional[str]:
    digits = re.sub(r"\D+", "", _to_str(value))
    if re.fullmatch(r"1\d{10}", digits):
        return digits
    return None


def _quote_plate_no_valid(value: Any) -> bool:
    text = re.sub(r"\s+", "", _to_str(value).upper())
    return bool(re.fullmatch(r"[\u4e00-\u9fff][A-Z][A-Z0-9]{4,6}", text))


def _quote_plate_no_present(value: Any) -> bool:
    text = re.sub(r"\s+", "", _to_str(value).upper())
    return bool(_quote_plate_no_valid(text) or re.search(r"[\u4e00-\u9fff][A-Z][A-Z0-9]{4,7}", text))


def _quote_plate_no_candidates(value: Any) -> Tuple[str, ...]:
    text = re.sub(r"\s+", "", _to_str(value).upper())
    if not text:
        return ()
    return tuple(re.findall(r"[\u4e00-\u9fff][A-Z][A-Z0-9]{5,6}", text))


def _quote_new_energy_plate_no_present(value: Any) -> bool:
    for plate in _quote_plate_no_candidates(value):
        if len(plate) >= 8 and (plate[2] in {"D", "F"} or plate[-1] in {"D", "F"}):
            return True
    return False


def _quote_labeled_plate_no_present(value: Any) -> bool:
    text = re.sub(r"\s+", "", _to_str(value).upper())
    if not text:
        return False
    return bool(re.search(r"(?:号牌号码|车牌号码|车牌号|号牌|车牌)[:：=]?[\u4e00-\u9fff][A-Z][A-Z0-9]{4,6}", text))


def _quote_new_energy_text_present(*values: Any) -> bool:
    text = re.sub(r"\s+", "", " ".join(_to_str(value) for value in values if _to_str(value).strip()))
    if not text:
        return False
    lowered = text.lower()
    if re.search(r"非新能源|不是新能源", lowered, flags=re.IGNORECASE):
        return False
    if re.search(
        r"新能源|绿牌|渐变绿|纯电动轿车|纯电|插电|插混|混动|油电|电动|燃料电池|氢能源|新能源车辆|新能源类型为纯电动|new_energy|electric|bev|phev|reev|增程",
        lowered,
        flags=re.IGNORECASE,
    ):
        return True
    model_values = [
        re.sub(r"[^A-Z0-9]+", "", _to_str(value).upper())
        for value in values
        if re.search(r"[A-Za-z0-9]", _to_str(value))
    ]
    return any(re.search(r"(?:BEV|PHEV|REEV)(?:\d|[A-Z]|$)", item) for item in model_values)


def _quote_fuel_text_present(*values: Any) -> bool:
    text = re.sub(r"\s+", "", " ".join(_to_str(value) for value in values if _to_str(value).strip())).lower()
    if not text:
        return False
    if re.search(r"非新能源|不是新能源|油车|蓝牌|蓝色号牌|汽油|柴油|燃油|fuel|gasoline|diesel", text, flags=re.IGNORECASE):
        return True
    return False


def _normalize_license_type_value(value: Any) -> str:
    text = re.sub(r"\s+", "", _to_str(value)).upper()
    if not text:
        return ""
    if text in {LICENSE_TYPE_NEW_ENERGY, "新能源", "新能源车", "小型新能源汽车", "小型新能源汽车号牌", "绿色", "绿牌"}:
        return LICENSE_TYPE_NEW_ENERGY
    if text in {LICENSE_TYPE_FUEL, "油车", "燃油", "燃油车", "小型汽车", "小型汽车号牌", "蓝色", "蓝牌"}:
        return LICENSE_TYPE_FUEL
    if re.search(r"(?:新能源|绿牌|绿色|小型新能源)", text):
        return LICENSE_TYPE_NEW_ENERGY
    if re.search(r"(?:燃油|油车|蓝牌|蓝色|小型汽车号牌|小型汽车)", text):
        return LICENSE_TYPE_FUEL
    if text in LICENSE_COLOR_BY_TYPE:
        return text
    return ""


def _license_color_for_type(license_type: Any) -> str:
    return LICENSE_COLOR_BY_TYPE.get(_normalize_license_type_value(license_type), "")


def _license_type_label(license_type: Any) -> str:
    value = _normalize_license_type_value(license_type)
    if value == LICENSE_TYPE_NEW_ENERGY:
        return "52-小型新能源汽车"
    if value == LICENSE_TYPE_FUEL:
        return "02-小型汽车号牌"
    return ""


def _license_type_decision_payload(license_type: str, *, source: str, reason: str = "") -> Dict[str, Any]:
    value = _normalize_license_type_value(license_type)
    if not value:
        return {}
    return {
        "license_type": value,
        "license_color_code": _license_color_for_type(value),
        "label": _license_type_label(value),
        "source": _to_str(source).strip() or "unknown",
        "reason": _to_str(reason).strip(),
    }


def _resolve_license_type_decision(
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
    *,
    vehicle_type_detect: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = _json_obj(normalized_data)
    previous = _json_obj(data.get(LICENSE_TYPE_DECISION_KEY))
    previous_source = _to_str(previous.get("source")).strip()

    data_overrides = _json_obj(data.get(QUOTE_DATA_OVERRIDES_KEY))
    manual_source_value = data.get("license_type_override")
    if not manual_source_value and "license_type" in data_overrides:
        manual_source_value = data_overrides.get("license_type")
    if not manual_source_value and previous_source == "user_override":
        manual_source_value = data.get("license_type") or data.get("licenseType") or data.get("licensePlateType")
    manual_value = _normalize_license_type_value(manual_source_value)
    if manual_value:
        return _license_type_decision_payload(manual_value, source="user_override", reason="用户指定号牌种类")

    if previous_source in {"user_override", "renewal_lookup"}:
        previous_value = _normalize_license_type_value(previous.get("license_type"))
        if previous_value:
            return _license_type_decision_payload(
                previous_value,
                source=previous_source,
                reason=_to_str(previous.get("reason")).strip() or "沿用已确认号牌种类",
            )

    field_license_type = _normalize_license_type_value(
        data.get("license_type") or data.get("licenseType") or data.get("licensePlateType") or data.get("license_color_code")
    )
    if field_license_type == LICENSE_TYPE_NEW_ENERGY:
        return _license_type_decision_payload(LICENSE_TYPE_NEW_ENERGY, source="license_type_field", reason="资料号牌种类为52")

    if _quote_new_energy_plate_no_present(data.get("plate_no")):
        return _license_type_decision_payload(LICENSE_TYPE_NEW_ENERGY, source="plate_no", reason="新能源号牌")

    haystack = _quote_detect_haystack(data, images_by_slot or {})
    compact_haystack = re.sub(r"\s+", "", haystack).lower()
    if _quote_labeled_plate_no_present(haystack) and _quote_new_energy_plate_no_present(haystack):
        return _license_type_decision_payload(LICENSE_TYPE_NEW_ENERGY, source="ocr_plate_no", reason="OCR文本包含新能源号牌")
    if _quote_new_energy_text_present(compact_haystack):
        return _license_type_decision_payload(LICENSE_TYPE_NEW_ENERGY, source="ocr_energy_text", reason="材料文本包含新能源特征")
    if _quote_fuel_text_present(compact_haystack):
        return _license_type_decision_payload(LICENSE_TYPE_FUEL, source="ocr_energy_text", reason="材料文本包含燃油特征")
    if field_license_type == LICENSE_TYPE_FUEL:
        return _license_type_decision_payload(LICENSE_TYPE_FUEL, source="license_type_field", reason="资料号牌种类为02")

    detect = _json_obj(vehicle_type_detect)
    energy_type = _to_str(detect.get("vehicle_energy_type")).strip()
    if energy_type == "new_energy":
        return _license_type_decision_payload(LICENSE_TYPE_NEW_ENERGY, source="vehicle_energy_type", reason="车辆识别为新能源")
    if energy_type == "fuel":
        return _license_type_decision_payload(LICENSE_TYPE_FUEL, source="vehicle_energy_type", reason="车辆识别为燃油")

    account_type_name = _normalize_account_type_name(data.get("account_type_name") or detect.get("config_type_name"))
    if "新能源" in account_type_name:
        return _license_type_decision_payload(LICENSE_TYPE_NEW_ENERGY, source="account_type", reason=f"报价类型为{account_type_name}")
    if account_type_name:
        return _license_type_decision_payload(LICENSE_TYPE_FUEL, source="account_type", reason=f"报价类型为{account_type_name}")

    previous_value = _normalize_license_type_value(previous.get("license_type"))
    if previous_value and previous_source not in {"fallback"}:
        return _license_type_decision_payload(
            previous_value,
            source=previous_source or "previous",
            reason=_to_str(previous.get("reason")).strip() or "沿用上一轮号牌种类",
        )

    return _license_type_decision_payload(LICENSE_TYPE_FUEL, source="fallback", reason="未识别到新能源特征，默认小型汽车号牌")


def _apply_license_type_decision(
    data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
    *,
    vehicle_type_detect: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out = dict(_json_obj(data))
    decision = _resolve_license_type_decision(out, images_by_slot, vehicle_type_detect=vehicle_type_detect)
    if decision:
        out[LICENSE_TYPE_DECISION_KEY] = decision
        if _to_str(decision.get("source")).strip() != "fallback":
            out["license_type"] = decision.get("license_type")
            out["license_color_code"] = decision.get("license_color_code")
        else:
            out.pop("license_type", None)
            out.pop("license_color_code", None)
    return _compact_quote_data(out)


def _quote_date_obj(value: Any) -> Optional[datetime]:
    text = _normalize_quote_date_text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def _one_calendar_year_ago(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        # 2/29 falls back to 2/28 for the "one calendar year" boundary.
        return value.replace(year=value.year - 1, day=28)


def _recent_driving_license_issue_date(value: Any, *, now: Optional[datetime] = None) -> Tuple[str, bool, Optional[int]]:
    issue_dt = _quote_date_obj(value)
    if issue_dt is None:
        return "", False, None
    today = (now or _now()).replace(hour=0, minute=0, second=0, microsecond=0)
    issue_day = issue_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if issue_day > today:
        return issue_day.strftime("%Y-%m-%d"), False, None
    age_days = (today - issue_day).days
    return issue_day.strftime("%Y-%m-%d"), issue_day > _one_calendar_year_ago(today), age_days


def _extract_transfer_vehicle_command(text: Any) -> Dict[str, Any]:
    raw = _norm_text(text)
    compact = re.sub(r"\s+", "", raw)
    if not compact or "过户车" not in compact:
        return {}
    if re.search(r"(?:非|不是|并非|不算|不要|不用|取消|撤销|去掉|关闭)过户车|过户车(?:取消|撤销|不要|不用|否|不是)", compact):
        return {
            "is_command": True,
            "is_transfer_vehicle": False,
            "transfer_vehicle_override": "not_transfer",
            "raw_text": raw,
        }
    if re.search(r"(?:按|是|属于|改成|改为|设置为|设为|确认)?过户车", compact):
        transfer_date = _normalize_quote_date_text(raw)
        return {
            "is_command": True,
            "is_transfer_vehicle": True,
            "transfer_vehicle_override": "transfer",
            "transfer_date": transfer_date,
            "raw_text": raw,
        }
    return {}


def _apply_transfer_vehicle_state(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(_json_obj(data))
    override = _to_str(out.get("transfer_vehicle_override")).strip()
    issue_date, recent_issue, age_days = _recent_driving_license_issue_date(out.get("issue_date"))

    if override == "not_transfer":
        out["is_transfer_vehicle"] = False
        out["transfer_date"] = ""
        out["transfer_vehicle_source"] = "user_override"
        out["transfer_vehicle_reason"] = "用户指定非过户车"
        return _compact_quote_data(out)

    if override == "transfer":
        transfer_date = _normalize_quote_date_text(out.get("transfer_date")) or issue_date
        out["is_transfer_vehicle"] = True
        out["transfer_date"] = transfer_date
        out["transfer_vehicle_source"] = "user_override"
        out["transfer_vehicle_reason"] = "用户指定过户车"
        return _compact_quote_data(out)

    if recent_issue and issue_date:
        out["is_transfer_vehicle"] = True
        out["transfer_date"] = issue_date
        out["transfer_vehicle_source"] = "driving_license_issue_date"
        out["transfer_vehicle_reason"] = f"行驶证发证日期距今{age_days}天，不满一年，按过户车处理"
    else:
        # Clear stale auto-derived transfer state when materials are replaced or recalled.
        if _to_str(out.get("transfer_vehicle_source")).strip() in {"", "driving_license_issue_date"}:
            out["is_transfer_vehicle"] = False
            out.pop("transfer_date", None)
            out.pop("transfer_vehicle_source", None)
            out.pop("transfer_vehicle_reason", None)
    return _compact_quote_data(out)


def _clean_quote_dynamic_data(data: Dict[str, Any], *, derive_owner_name: bool = True) -> Dict[str, Any]:
    cleaned = clean_dynamic_data_for_ocr(_json_obj(data))
    if "owner_phone" in (data or {}) or "owner_phone" in cleaned:
        cleaned["owner_phone"] = _normalize_owner_phone(cleaned.get("owner_phone") or (data or {}).get("owner_phone"))
    if "plate_no" in (data or {}) or "plate_no" in cleaned:
        cleaned["plate_no"] = cleaned.get("plate_no") if _quote_plate_no_valid(cleaned.get("plate_no")) else None
    if derive_owner_name and _to_str(cleaned.get("id_name")).strip() and not _to_str(cleaned.get("owner_name")).strip():
        cleaned["owner_name"] = cleaned.get("id_name")
    return _compact_quote_data(cleaned)


_QUOTE_VEHICLE_CERT_OCR_LABELS = (
    "CarModel",
    "CarBrand",
    "CarName",
    "VehicleName",
    "VehicleType",
    "CertificateDate",
    "CarColor",
    "Displacement",
    "Power",
    "Manufacturer",
    "ManufactureDate",
    "EngineNo",
    "FuelType",
    "EmissionStandard",
    "SteeringType",
    "LimitPassenger",
    "EngineType",
    "TyreNum",
    "SpeedLimit",
    "TotalWeight",
    "SaddleMass",
    "VinNo",
    "Wheelbase",
    "AxleNum",
    "CertificationNo",
    "ChassisModel",
    "ChassisID",
    "SeatingCapacity",
    "QualifySeal",
    "CGSSeal",
    "log_id",
    "words_result_num",
)


def _quote_feature_text(features: Any, fallback_text: Any = "") -> str:
    data = _json_obj(features)
    generic = _json_obj(data.get("generic_ocr"))
    return _to_str(
        data.get("generic_ocr_text")
        or generic.get("text")
        or generic.get("words_result_text")
        or fallback_text
        or data.get("ocr_text_sample")
    ).strip()


def _quote_labeled_ocr_value(text: Any, label: str) -> str:
    source = re.sub(r"\s+", " ", _to_str(text)).strip()
    wanted = _to_str(label).strip()
    if not source or not wanted:
        return ""
    terminators = [item for item in _QUOTE_VEHICLE_CERT_OCR_LABELS if item != wanted]
    term_pattern = "|".join(re.escape(item) for item in terminators)
    pattern = rf"(?:^|\s){re.escape(wanted)}\s*[:：]?\s*(.+?)(?=\s+(?:{term_pattern})\s*[:：]?|$)"
    match = re.search(pattern, source, flags=re.IGNORECASE)
    if not match:
        return ""
    value = re.sub(r"\s+", "", match.group(1)).strip()
    value = re.sub(r"(?:words|location|top|left|width|height)$", "", value, flags=re.IGNORECASE).strip()
    if len(value) > 40:
        return ""
    return value


def _quote_car_name_from_features(features: Any, fallback_text: Any = "") -> str:
    text = _quote_feature_text(features, fallback_text)
    for label in ("CarName", "VehicleName"):
        value = _quote_labeled_ocr_value(text, label)
        compact = re.sub(r"[\s\-_/]", "", value).upper()
        # CarName is frequently an English/alphanumeric sales model (for
        # example "CT200h"). Do not require Chinese characters here; the PICC
        # catalogue accepts those model names directly. A standalone 17-char
        # VIN remains invalid as a model hint and is handled by the platform
        # resolver as a VIN prefix instead.
        if (
            value
            and len(compact) >= 2
            and (
                re.search(r"[\u4e00-\u9fff]", value)
                or re.fullmatch(r"[A-Z][A-Z0-9 ._-]{1,39}", value, flags=re.IGNORECASE)
            )
            and not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", compact)
        ):
            return value
    return ""


_QUOTE_SALES_MODEL_CODE_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z]{1,8}\d[A-Z0-9]{0,12}|\d{2,5}[A-Z][A-Z0-9]{1,12})(?![A-Z0-9])",
    flags=re.IGNORECASE,
)
_QUOTE_ENGLISH_BRAND_PREFIXES = (
    "LEXUS",
    "TOYOTA",
    "HONDA",
    "NISSAN",
    "HYUNDAI",
    "BUICK",
    "MAZDA",
    "TESLA",
    "VOLVO",
    "AUDI",
    "BENZ",
    "FORD",
    "JEEP",
    "MINI",
    "BMW",
    "KIA",
)


def _quote_normalize_sales_model_candidate(code: str) -> str:
    text = _to_str(code).strip()
    if not text:
        return ""
    upper = text.upper()
    for brand in _QUOTE_ENGLISH_BRAND_PREFIXES:
        if upper.startswith(brand) and len(upper) > len(brand):
            rest = text[len(brand) :].lstrip(" -_")
            if rest and re.search(r"\d", rest):
                return rest
    return text


def _quote_sales_model_hint_from_model_text(value: Any, *, vin: Any = "") -> str:
    """Extract a usable sales-model token such as CT200h from brand+model OCR text."""
    source = re.sub(r"\s+", " ", _to_str(value).strip())
    if not source:
        return ""
    compact = re.sub(r"[\s\-_/]", "", source)
    vin_compact = re.sub(r"[^A-Z0-9]", "", _to_str(vin).strip().upper())
    vin_prefix = vin_compact[:8] if len(vin_compact) == 17 else ""
    candidates: List[str] = []
    for match in _QUOTE_SALES_MODEL_CODE_RE.finditer(compact):
        code = _quote_normalize_sales_model_candidate(match.group(1).strip())
        if not code:
            continue
        upper = code.upper()
        if vin_prefix and upper == vin_prefix:
            continue
        if len(upper) == 17 and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", upper):
            continue
        # "雷克萨斯JTHKR5BH" style merges: reject 8-char VIN family prefixes when the
        # text has no other Latin sales token beyond that prefix.
        if len(upper) == 8 and re.fullmatch(r"[A-HJ-NPR-Z0-9]{8}", upper):
            remainder = re.sub(r"[\u4e00-\u9fff牌]", "", compact.upper().replace(upper, ""))
            if not remainder:
                continue
        candidates.append(code)

    def rank(code: str) -> tuple[int, int]:
        # Prefer compact sales shapes like CT200h over long catalogue blobs.
        shaped = 1 if re.fullmatch(r"[A-Za-z]{1,5}\d{2,4}[A-Za-z0-9]{0,6}", code) else 0
        return (shaped, len(code))

    ranked = sorted(
        (code for code in candidates if re.search(r"[A-Za-z]", code) and re.search(r"\d", code)),
        key=rank,
        reverse=True,
    )
    if ranked:
        return ranked[0]
    return candidates[-1] if candidates else ""


def _quote_leading_brand_from_model_text(value: Any) -> str:
    compact = re.sub(r"\s+", "", _to_str(value).strip())
    if not compact:
        return ""
    match = re.match(r"^(?P<brand>[\u4e00-\u9fff]{2,24})牌?(?=[A-Za-z0-9])", compact)
    return match.group("brand") if match else ""


def _quote_field_quality_score(field_key: str, value: Any, *, vin: Any = "") -> int:
    text = _to_str(value).strip()
    if not text:
        return -1
    key = _to_str(field_key).strip()
    if key == "vin":
        compact = re.sub(r"[^A-Z0-9]", "", text.upper())
        if len(compact) == 17 and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", compact):
            return 100
        if 11 <= len(compact) <= 20:
            return 40
        return 10
    if key == "engine_no":
        compact = re.sub(r"[^A-Z0-9]", "", text.upper())
        return min(80, max(5, len(compact) * 4))
    if key in {"vehicle_model", "car_name"}:
        sales = _quote_sales_model_hint_from_model_text(text, vin=vin)
        if sales and sales.upper() == re.sub(r"[\s\-_/]", "", text).upper():
            return 90
        if sales:
            return 75
        if re.search(r"[A-Za-z0-9]", text) and re.search(r"[\u4e00-\u9fff]", text):
            return 55
        if re.search(r"[\u4e00-\u9fff]", text):
            return 25
        return 35
    return 1


def _merge_quote_extracted_prefer(*items: Dict[str, Any]) -> Dict[str, Any]:
    """Merge OCR slot fields, preferring higher-quality VIN / model / engine values."""
    out: Dict[str, Any] = {}
    for item in items:
        vin_context = out.get("vin")
        for key, value in (item or {}).items():
            if value in (None, ""):
                continue
            current = out.get(key)
            if current in (None, ""):
                out[key] = value
                continue
            vin_for_score = vin_context or (value if key == "vin" else "") or out.get("vin")
            if _quote_field_quality_score(key, value, vin=vin_for_score) > _quote_field_quality_score(
                key, current, vin=vin_for_score
            ):
                out[key] = value
    return out


def _backfill_quote_sales_model_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data or {})
    vin = out.get("vin")
    model = _to_str(out.get("vehicle_model")).strip()
    car_name = _to_str(out.get("car_name")).strip()
    if not car_name and model:
        hint = _quote_sales_model_hint_from_model_text(model, vin=vin)
        if hint:
            out["car_name"] = hint
    if not _to_str(out.get("vehicle_brand_name")).strip() and model:
        brand = _quote_leading_brand_from_model_text(model)
        if brand:
            out["vehicle_brand_name"] = brand
    return out


def _quote_image_extracted_fields_from_features(features: Any, fallback_text: Any = "") -> Dict[str, Any]:
    data = _json_obj(_json_obj(features).get("ocr_extracted_fields"))
    if data and not _to_str(data.get("car_name")).strip():
        car_name = _quote_car_name_from_features(features, fallback_text)
        if car_name:
            data["car_name"] = car_name
    if data:
        data = _backfill_quote_sales_model_fields(data)
    return _clean_quote_dynamic_data(data, derive_owner_name=False) if data else {}


def _quote_image_features(features: Any, extracted_fields: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    out = dict(_json_obj(features))
    out["quote_image_upload"] = True
    raw_fields = _json_obj(extracted_fields)
    cleaned_fields = _clean_quote_dynamic_data(_json_obj(extracted_fields), derive_owner_name=False)
    raw_field_keys = sorted(k for k, v in raw_fields.items() if _to_str(v).strip())
    if raw_field_keys:
        out["ocr_raw_extracted_field_keys"] = raw_field_keys
        out["ocr_cleaner_dropped_fields"] = sorted(k for k in raw_field_keys if k not in cleaned_fields)
    if cleaned_fields:
        cleaned_fields = _backfill_quote_sales_model_fields(cleaned_fields)
        out["ocr_extracted_fields"] = cleaned_fields
        out["ocr_extracted_field_keys"] = sorted(cleaned_fields.keys())
    else:
        out.pop("ocr_extracted_fields", None)
        out.pop("ocr_extracted_field_keys", None)
    return out, cleaned_fields


def _slot_filtered_extracted_fields(slot_key: str, image: Dict[str, Any]) -> Dict[str, Any]:
    allowed = set(QUOTE_IMAGE_FIELDS_BY_SLOT.get(slot_key, ()))
    if not allowed:
        return {}
    fields = _json_obj(image.get("extracted_fields")) or _quote_image_extracted_fields_from_features(
        image.get("text_features"),
        image.get("ocr_text_sample"),
    )
    if not fields:
        return {}
    cleaned = _clean_quote_dynamic_data(fields, derive_owner_name=False)
    cleaned = _backfill_quote_sales_model_fields(cleaned)
    return {key: value for key, value in cleaned.items() if key in allowed}


def _slot_extracted_data_map(images_by_slot: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for slot_key, rows in (images_by_slot or {}).items():
        merged: Dict[str, Any] = {}
        for image in rows or []:
            merged = _merge_quote_extracted_prefer(merged, _slot_filtered_extracted_fields(slot_key, image))
        if merged:
            out[slot_key] = merged
    return out


def _active_image_extracted_data(images_by_slot: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    slot_data = _slot_extracted_data_map(images_by_slot)
    merged: Dict[str, Any] = {}
    for slot_key in ("driving_license_main", "driving_license_sub", "vehicle_cert", "idcard_back", "idcard_front", "related"):
        merged = _merge_quote_extracted_prefer(merged, slot_data.get(slot_key, {}))
    return _clean_quote_dynamic_data(_backfill_quote_sales_model_fields(merged), derive_owner_name=True)


def _slot_has_uploaded_quote_image(rows: List[Dict[str, Any]]) -> bool:
    for image in rows or []:
        features = _json_obj(image.get("text_features"))
        if features.get("quote_image_upload") or image.get("method") != "order_slot":
            return True
    return False


def _drop_uploaded_image_managed_fields(
    data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    out = dict(data or {})
    for slot_key, fields in QUOTE_IMAGE_FIELDS_BY_SLOT.items():
        rows = (images_by_slot or {}).get(slot_key) or []
        if not rows or not _slot_has_uploaded_quote_image(rows):
            continue
        has_replacement_data = any(_slot_filtered_extracted_fields(slot_key, image) for image in rows)
        if not has_replacement_data:
            continue
        for field in fields:
            out.pop(field, None)
    return out


def _normalize_quote_case_data(
    *,
    base_data: Dict[str, Any],
    order_data: Dict[str, Any],
    text_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    base = _clean_quote_dynamic_data(_merge_data(_json_obj(base_data), _json_obj(order_data)))
    base_overrides = _json_obj(base.get(QUOTE_DATA_OVERRIDES_KEY))
    base = _drop_uploaded_image_managed_fields(base, images_by_slot)
    image_data = _active_image_extracted_data(images_by_slot)
    text_clean = _clean_quote_dynamic_data(_json_obj(text_data))
    text_overrides = _json_obj(text_clean.get(QUOTE_DATA_OVERRIDES_KEY))
    merged_overrides = _merge_quote_data_overrides(base_overrides, text_overrides)
    merged = _clean_quote_dynamic_data(_merge_data(base, image_data, text_clean, merged_overrides))
    merged = _backfill_quote_sales_model_fields(merged)
    if merged_overrides:
        merged[QUOTE_DATA_OVERRIDES_KEY] = merged_overrides
    merged = _apply_transfer_vehicle_state(merged)
    return _apply_license_type_decision(merged, images_by_slot)


def _same_material_text(left: Any, right: Any) -> bool:
    lval = re.sub(r"[\s,，;；:：()（）\-_/]+", "", _to_str(left)).upper()
    rval = re.sub(r"[\s,，;；:：()（）\-_/]+", "", _to_str(right)).upper()
    return bool(lval and rval and lval == rval)


def _quote_material_issues(
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    data = _clean_quote_dynamic_data(_json_obj(normalized_data))
    manual_overrides = _json_obj(data.get(QUOTE_DATA_OVERRIDES_KEY))
    issues: List[Dict[str, Any]] = []

    owner_name = _to_str(data.get("owner_name")).strip()
    id_name = _to_str(data.get("id_name")).strip()
    if (
        owner_name
        and id_name
        and not _same_material_text(owner_name, id_name)
        and not ({"owner_name", "id_name"} & set(manual_overrides))
    ):
        issues.append(
            {
                "type": "data_conflict",
                "key": "owner_name_id_name_conflict",
                "label": "行驶证所有人与身份证姓名不一致",
                "detail": {"owner_name": owner_name, "id_name": id_name},
            }
        )

    slot_data = _slot_extracted_data_map(images_by_slot)
    cert_vin = _to_str(slot_data.get("vehicle_cert", {}).get("vin")).strip()
    license_vin = _to_str(slot_data.get("driving_license_main", {}).get("vin")).strip()
    if cert_vin and license_vin and cert_vin != license_vin and "vin" not in manual_overrides:
        issues.append(
            {
                "type": "data_conflict",
                "key": "vehicle_cert_license_vin_conflict",
                "label": "车辆合格证与行驶证车架号不一致",
                "detail": {"vehicle_cert_vin": cert_vin, "driving_license_vin": license_vin},
            }
        )

    cert_engine = _to_str(slot_data.get("vehicle_cert", {}).get("engine_no")).strip()
    license_engine = _to_str(slot_data.get("driving_license_main", {}).get("engine_no")).strip()
    if cert_engine and license_engine and cert_engine != license_engine and "engine_no" not in manual_overrides:
        issues.append(
            {
                "type": "data_conflict",
                "key": "vehicle_cert_license_engine_conflict",
                "label": "车辆合格证与行驶证发动机号不一致",
                "detail": {"vehicle_cert_engine_no": cert_engine, "driving_license_engine_no": license_engine},
            }
        )

    return issues


def _vehicle_cert_vin_failure_detail(images_by_slot: Dict[str, List[Dict[str, Any]]]) -> str:
    details: List[str] = []
    for image in (images_by_slot or {}).get("vehicle_cert") or []:
        features = _json_obj(image.get("text_features"))
        dropped = set(_json_list(features.get("ocr_cleaner_dropped_fields")))
        if "vin" in dropped:
            details.append("车辆合格证识别到的车架号格式不完整，请补充正确车架号或重新上传清晰合格证")
        generic = _json_obj(features.get("generic_ocr"))
        error = _json_obj(generic.get("error"))
        if error:
            detail = _quote_ocr_error_user_message(error)
            if detail:
                details.append(detail)
    deduped: List[str] = []
    for detail in details:
        if detail and detail not in deduped:
            deduped.append(detail)
    return "；".join(deduped[:2])


def _quote_ocr_error_user_message(error: Dict[str, Any]) -> str:
    message = _to_str(error.get("message") or error.get("type")).strip()
    low = message.lower()
    if "no permission" in low or "error_code=6" in low or "无权限" in message or "权限" in message:
        return "通用文字识别接口暂无访问权限，请补充缺少字段或稍后重试"
    if "timeout" in low or "超时" in message:
        return "通用文字识别兜底调用超时，请稍后重试或重新上传清晰图片"
    if "not configured" in low or "未配置" in message:
        return "通用文字识别兜底尚未配置，当前只能使用已启用的证件识别结果"
    if "临时跳过" in message:
        return "通用文字识别兜底暂不可用，请稍后重试或补充缺少字段"
    if message:
        return "通用文字识别兜底失败，请检查图片清晰度或稍后重试"
    return ""


def _quote_vehicle_type_detect_safe(
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    try:
        return detect_quote_vehicle_type(normalized_data, images_by_slot)
    except Exception:
        return {}


def _required_fields_for_quote(
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
    *,
    platform_code: str = "",
    account_type_name: Optional[str] = None,
) -> Tuple[Tuple[str, str], ...]:
    vehicle_detect = _quote_vehicle_type_detect_safe(normalized_data, images_by_slot)
    skip: set[str] = set()
    config_type = _normalize_account_type_name(account_type_name) or _normalize_account_type_name(
        vehicle_detect.get("config_type_name")
    )
    if _to_str(platform_code).strip().upper() == "PICC" and not config_type:
        return (("account_type_name", "车辆类型（油车-新/油车-旧/新能源车-新/新能源车-旧）"),)
    if config_type in CORE_REQUIRED_FIELDS_BY_ACCOUNT_TYPE:
        return CORE_REQUIRED_FIELDS_BY_ACCOUNT_TYPE[config_type]
    if config_type == "油车-旧":
        skip.update({"owner_phone", "id_number"})
    if vehicle_detect.get("vehicle_usage_type") == "new_car":
        # New cars are commonly quoted before registration, so they may not
        # have a plate number yet. Used/unknown vehicles still require it.
        skip.add("plate_no")
    fields = [(key, label) for key, label in REQUIRED_FIELDS if key not in skip]
    if config_type == "油车-旧":
        fields.append(("first_register_date", "初登日期"))
    return tuple(fields)


def _required_slots_for_quote(
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
    *,
    platform_code: str = "",
    account_type_name: Optional[str] = None,
) -> Tuple[str, ...]:
    vehicle_detect = _quote_vehicle_type_detect_safe(normalized_data, images_by_slot)
    config_type = _normalize_account_type_name(account_type_name) or _normalize_account_type_name(
        vehicle_detect.get("config_type_name")
    )
    if _to_str(platform_code).strip().upper() == "PICC" and not config_type:
        return ()
    if config_type in CORE_REQUIRED_SLOTS_BY_ACCOUNT_TYPE:
        if _quote_text_only_material_mode(normalized_data, images_by_slot):
            return ()
        required_fields = CORE_REQUIRED_FIELDS_BY_ACCOUNT_TYPE.get(config_type, ())
        if required_fields and all(_to_str(_json_obj(normalized_data).get(key)).strip() for key, _ in required_fields):
            return ()
        return CORE_REQUIRED_SLOTS_BY_ACCOUNT_TYPE[config_type]
    slots = list(SINGLE_REQUIRED_SLOTS)
    if vehicle_detect.get("vehicle_usage_type") == "new_car":
        # New-car quote can be driven by vehicle certificate instead of
        # driving license, which may not exist before the vehicle is plated.
        slots = [slot for slot in slots if slot != "driving_license_main"]
    return tuple(slots)


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


def _quote_failure_next_action(failure_code: str) -> str:
    return _to_str(QUOTE_FAILURE_NEXT_ACTIONS.get(_to_str(failure_code).strip(), "")).strip() or "请核实后重试"


def _quote_failure_fields(
    *,
    code: str,
    reason: str,
    next_action: str = "",
) -> Dict[str, str]:
    failure_code = _to_str(code).strip() or FAILURE_CODE_PLATFORM
    failure_reason = sanitize_quote_user_message(reason, "报价失败")
    action = _to_str(next_action).strip() or _quote_failure_next_action(failure_code)
    return {
        "failure_code": failure_code,
        "failure_reason": failure_reason,
        "next_action": action,
    }


def _attach_quote_failure(
    data: Dict[str, Any],
    *,
    code: str,
    reason: str,
    next_action: str = "",
) -> Dict[str, Any]:
    """Attach stable failure fields on data + payload. Real failures must stay user-visible."""
    fields = _quote_failure_fields(code=code, reason=reason, next_action=next_action)
    data["failure_code"] = fields["failure_code"]
    data["failure_reason"] = fields["failure_reason"]
    data["next_action"] = fields["next_action"]
    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        data["payload"] = payload
    payload.update(fields)
    # Never hide a classified user-facing failure behind silent flags.
    data["silent"] = False
    data["ui_visible"] = True
    return data


def _failure_code_for_platform_dialog_subtype(subtype: str) -> str:
    key = _to_str(subtype).strip().lower()
    if key == "session_expired":
        return FAILURE_CODE_SESSION_EXPIRED
    if key == "quota_full":
        return FAILURE_CODE_QUOTA_FULL
    if key in {"duplicate_quote", "duplicate_quote_notice"}:
        return FAILURE_CODE_DUPLICATE_QUOTE
    return FAILURE_CODE_PLATFORM


def _build_quote_user_failure_response(
    *,
    reply: str,
    case: QuoteCase,
    task: Optional[QuoteTask],
    trace_id: str,
    failure_code: str,
    failure_reason: str = "",
    next_action: str = "",
    result_status: str = RESULT_FAILED,
    response_status: str = "failed",
    actions: Optional[List[Dict[str, Any]]] = None,
    payload: Optional[Dict[str, Any]] = None,
    entities: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Canonical user-visible quote failure / recoverable stop response."""
    reason = sanitize_quote_user_message(failure_reason or reply, "报价失败")
    safe_reply = sanitize_quote_user_message(reply or reason, reason)
    data_payload = dict(payload or {})
    data = _mk_data(
        result_status=result_status,
        message=safe_reply,
        entities=entities
        or {
            "quote_case_id": case.id,
            "quote_task_id": task.id if task is not None else None,
            "order_id": case.order_id,
        },
        payload=data_payload,
    )
    _attach_quote_failure(
        data,
        code=failure_code,
        reason=reason,
        next_action=next_action,
    )
    return safe_reply, {
        "status": response_status,
        "intent": "quote",
        "trace_id": trace_id,
        "silent": False,
        "ui_visible": True,
        "data": data,
        "actions": actions or [],
    }


def _case_no() -> str:
    return "QA" + datetime.now(TZ_BJ).strftime("%Y%m%d") + uuid.uuid4().hex[:8].upper()


def _mask_phone(phone: Any) -> str:
    s = re.sub(r"\D+", "", _to_str(phone))
    if len(s) == 11:
        return f"{s[:3]}****{s[-4:]}"
    if len(s) >= 4:
        return "*" * max(0, len(s) - 4) + s[-4:]
    return ""


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
    pattern = rf"(?:{label_expr})\s*(?:[:：=]|是|为)?\s*([^\s,，,;；。]+)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    value = _clean_secret_value(match.group(1), max_len=max_len)
    return value or None


_QUOTE_TEXT_VALUE_BOUNDARY_LABELS = (
    "车主手机号",
    "车主手机",
    "车主电话",
    "客户手机号",
    "客户手机",
    "手机号",
    "手机号码",
    "手机",
    "电话",
    "被保险人手机号",
    "被保人手机号",
    "投保人手机号",
    "联系电话",
    "身份证号",
    "身份证号码",
    "身份证",
    "证件号",
    "证件号码",
    "车牌号码",
    "车牌号",
    "车牌",
    "号牌号码",
    "号牌",
    "VIN码",
    "VIN",
    "车架号",
    "车辆识别代号",
    "发动机号",
    "发动机号码",
    "发动机",
    "车辆品牌/车辆名称",
    "车辆品牌/车辆型号",
    "车型名称",
    "车型",
    "品牌型号",
    "车辆型号",
    "初登",
    "初登日期",
    "初次登记日期",
    "注册日期",
    "登记日期",
    "发证日期",
    "行驶证发证日期",
    "商业起保日期",
    "商业险起保日期",
    "交强起保日期",
    "交强险起保日期",
)


_QUOTE_OWNER_NAME_FORBIDDEN_PREFIXES = (
    "车主手机号",
    "车主手机",
    "车主电话",
    "客户手机号",
    "客户手机",
    "手机号",
    "手机号码",
    "手机",
    "电话",
    "身份证号",
    "身份证号码",
    "身份证",
    "证件号",
    "证件号码",
    "车牌号码",
    "车牌号",
    "车牌",
    "号牌号码",
    "号牌",
    "VIN码",
    "VIN",
    "车架号",
    "车辆识别代号",
    "发动机号",
    "发动机号码",
    "发动机",
    "车型名称",
    "车型",
    "品牌型号",
)


def _trim_quote_text_value_at_next_label(value: Any) -> str:
    text = _to_str(value).strip()
    if not text:
        return ""
    cut_at: Optional[int] = None
    for label in _QUOTE_TEXT_VALUE_BOUNDARY_LABELS:
        label_text = _to_str(label).strip()
        if not label_text:
            continue
        pos = text.find(label_text)
        if pos > 0 and (cut_at is None or pos < cut_at):
            cut_at = pos
    return (text[:cut_at] if cut_at is not None else text).strip()


def _quote_owner_name_value_blocked(value: Any) -> bool:
    text = _to_str(value).strip()
    if not text:
        return True
    compact = re.sub(r"\s+", "", text)
    return any(compact.startswith(prefix) for prefix in _QUOTE_OWNER_NAME_FORBIDDEN_PREFIXES)


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
    "手机号",
    "手机号码",
    "手机",
    "电话",
    "被保险人手机号",
    "被保人手机号",
    "投保人手机号",
    "联系电话",
)

_OWNER_NAME_HINTS = (
    "被保险人姓名",
    "被保人姓名",
    "投保人姓名",
    "联系人姓名",
    "车主姓名",
    "车主名称",
    "车主",
    "所有人",
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
    if not text:
        return ""
    return QUOTE_ACCOUNT_TYPE_ALIASES.get(text, text)[:64]


def _account_type_db_names(value: Any, *, allow_empty: bool = False) -> Tuple[str, ...]:
    normalized = _normalize_account_type_name(value)
    if not normalized:
        return ("",) if allow_empty else ()
    names = [normalized]
    for alias, target in QUOTE_ACCOUNT_TYPE_ALIASES.items():
        if target == normalized and alias not in names:
            names.append(alias)
    return tuple(names)


def _ensure_fixed_quote_account_type(value: Any, *, allow_empty: bool = False) -> str:
    type_name = _normalize_account_type_name(value)
    if not type_name:
        if allow_empty:
            return ""
        raise ValueError("请选择账号类型：油车-新、油车-旧、新能源车-新、新能源车-旧")
    if type_name not in QUOTE_ACCOUNT_TYPE_SET:
        raise ValueError("账号类型只能选择：油车-新、油车-旧、新能源车-新、新能源车-旧")
    return type_name


def _normalize_quote_config_override_value(value: Any, unit: Any = "") -> str:
    text = _to_str(value).strip()
    text = re.sub(r"[，,。；;]+$", "", text).strip()
    text = text.replace(",", "").replace("，", "")
    if not text:
        return ""
    unit_text = _to_str(unit).strip()
    if unit_text and not text.endswith(unit_text):
        text = f"{text}{unit_text}"
    text = re.sub(r"\s+", "", text)
    is_wan = text.endswith("万") or text.endswith("万元")
    text = re.sub(r"(?:万元|万|元)$", "", text)
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            amount = Decimal(text)
            if is_wan:
                amount *= Decimal("10000")
            if amount == amount.to_integral():
                return str(int(amount))[:128]
            return format(amount.normalize(), "f").rstrip("0").rstrip(".")[:128]
        except Exception:
            pass
    return text[:128]


def _config_numeric_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except Exception:
            return None
    text = _to_str(value).strip()
    if not text:
        return None
    text = re.sub(r"\s+", "", text).replace(",", "").replace("，", "")
    text = re.sub(r"(?:元|万)$", "", text)
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    try:
        return Decimal(text)
    except Exception:
        return None


def _quote_config_field_allows_zero(field_name: Any) -> bool:
    label = re.sub(r"\s+", "", _to_str(field_name).strip())
    return label in {
        "途家安顺保费",
        "途家安顺",
        "途顺家安",
        "途家安顺非车保费",
        "非车",
        "送修码启用",
        "机动车增值服务特约条款（道路救援服务）",
        "附加机动车增值服务特约条款（道路救援服务）",
        "道路救援服务",
        "道路救援",
        "救援",
    }


def _quote_false_text() -> str:
    return "false"


def _quote_true_text() -> str:
    return "true"


def _quote_config_alias_pattern(alias: str) -> str:
    if alias == "三者":
        return r"(?<!第)三者"
    return re.escape(alias)


def _expand_third_party_medical_overrides(overrides: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(_json_obj(overrides))
    has_third = QUOTE_THIRD_PARTY_LABEL in merged
    has_medical_third = QUOTE_MEDICAL_THIRD_LABEL in merged
    if has_third and not has_medical_third:
        merged[QUOTE_MEDICAL_THIRD_LABEL] = merged[QUOTE_THIRD_PARTY_LABEL]
    elif has_medical_third and not has_third:
        merged[QUOTE_THIRD_PARTY_LABEL] = merged[QUOTE_MEDICAL_THIRD_LABEL]
    if has_third or has_medical_third:
        # PICC keeps this checked; the page copies the third-party main amount to the attached medical coverage.
        merged[QUOTE_SHARED_LIMIT_LABEL] = _quote_true_text()
    return merged


def _ensure_positive_numeric_config_value(field_name: Any, value: Any, *, context: str = "默认参数") -> None:
    amount = _config_numeric_decimal(value)
    if amount is None or amount > 0 or (amount == 0 and _quote_config_field_allows_zero(field_name)):
        return
    label = _to_str(field_name).strip() or "未命名字段"
    if _quote_config_field_allows_zero(field_name):
        raise ValueError(f"{context}“{label}”不能小于 0")
    raise ValueError(f"{context}“{label}”必须填写正数，不能小于或等于 0")


def _canonical_quote_config_override_label(value: Any) -> str:
    label = re.sub(r"\s+", "", _to_str(value).strip())
    label = label.replace("(", "（").replace(")", "）")
    if not label:
        return ""
    low = label.lower()
    for canonical, aliases in QUOTE_CONFIG_OVERRIDE_ALIASES:
        candidates = {canonical, *aliases}
        for alias in candidates:
            alias_norm = re.sub(r"\s+", "", _to_str(alias).strip()).replace("(", "（").replace(")", "）")
            if alias_norm and (label == alias_norm or low == alias_norm.lower()):
                return canonical
    return label[:128]


def _normalize_quote_product_exclusions(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        parsed: Any = None
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
        items = parsed if isinstance(parsed, list) else re.split(r"[,，、;；\s]+", raw)
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]

    exclusions: List[str] = []
    removable = {
        QUOTE_COMPULSORY_LABEL,
        QUOTE_LOSS_LABEL,
        QUOTE_THIRD_PARTY_LABEL,
        QUOTE_DRIVER_LABEL,
        QUOTE_PASSENGER_LABEL,
        QUOTE_MEDICAL_THIRD_LABEL,
        QUOTE_ROAD_RESCUE_LABEL,
        QUOTE_EXTERNAL_GRID_LABEL,
    }
    for item in items:
        label = _canonical_quote_config_override_label(item)
        if label in removable and label not in exclusions:
            exclusions.append(label)
    if QUOTE_THIRD_PARTY_LABEL in exclusions and QUOTE_MEDICAL_THIRD_LABEL not in exclusions:
        exclusions.append(QUOTE_MEDICAL_THIRD_LABEL)
    return exclusions


def _extract_quote_product_exclusions(text: Any) -> List[str]:
    compact = re.sub(r"\s+", "", _norm_text(text))
    if not compact:
        return []
    remove_words = QUOTE_CHAT_NEGATE_OBJECT_WORDS
    remove_group = "|".join(re.escape(word) for word in remove_words)
    removable = {
        QUOTE_COMPULSORY_LABEL,
        QUOTE_LOSS_LABEL,
        QUOTE_THIRD_PARTY_LABEL,
        QUOTE_DRIVER_LABEL,
        QUOTE_PASSENGER_LABEL,
        QUOTE_MEDICAL_THIRD_LABEL,
        QUOTE_ROAD_RESCUE_LABEL,
        QUOTE_EXTERNAL_GRID_LABEL,
    }
    exclusions: List[str] = []
    for canonical, aliases in QUOTE_CONFIG_OVERRIDE_ALIASES:
        label = _canonical_quote_config_override_label(canonical)
        if label not in removable:
            continue
        for alias in sorted(aliases, key=len, reverse=True):
            alias_norm = re.sub(r"\s+", "", _to_str(alias).strip())
            if not alias_norm:
                continue
            pattern = rf"(?:{remove_group}){re.escape(alias_norm)}|{re.escape(alias_norm)}(?:{remove_group})"
            if re.search(pattern, compact):
                if label not in exclusions:
                    exclusions.append(label)
                break
    seat_aliases = ("司乘", "司乘险", "司机乘客", "司机和乘客", "车上人员")
    for alias in seat_aliases:
        alias_norm = re.sub(r"\s+", "", alias)
        pattern = rf"(?:{remove_group}){re.escape(alias_norm)}|{re.escape(alias_norm)}(?:{remove_group})"
        if re.search(pattern, compact):
            for label in (QUOTE_DRIVER_LABEL, QUOTE_PASSENGER_LABEL):
                if label not in exclusions:
                    exclusions.append(label)
            break
    return _normalize_quote_product_exclusions(exclusions)


def _detect_professional_quote_command(text: Any) -> Dict[str, Any]:
    compact = re.sub(r"\s+", "", _norm_text(text))
    if not compact:
        return {}
    flow_type, mode = _picc_quote_flow_command_from_compact(compact)
    if flow_type == QUOTE_FLOW_RENEWAL:
        entities = {
            "platform_code": "PICC",
            "platform_name": "人保",
            QUOTE_FLOW_TYPE_KEY: QUOTE_FLOW_RENEWAL,
            "quote_command_mode": mode or "全保",
        }
        if mode == "交三":
            entities[QUOTE_PRODUCT_EXCLUSIONS_KEY] = [QUOTE_LOSS_LABEL]
        elif mode == "单商":
            entities[QUOTE_PRODUCT_EXCLUSIONS_KEY] = [QUOTE_COMPULSORY_LABEL]
        else:
            entities[QUOTE_PRODUCT_EXCLUSIONS_KEY] = []
        return {"is_quote": True, "entities": entities}
    mode = mode or _picc_quote_command_mode_from_compact(compact)
    if mode == "交三":
        return {
            "is_quote": True,
            "entities": {
                "platform_code": "PICC",
                "platform_name": "人保",
                QUOTE_PRODUCT_EXCLUSIONS_KEY: [QUOTE_LOSS_LABEL],
                "quote_command_mode": "交三",
            },
        }
    if mode == "单商":
        return {
            "is_quote": True,
            "entities": {
                "platform_code": "PICC",
                "platform_name": "人保",
                QUOTE_PRODUCT_EXCLUSIONS_KEY: [QUOTE_COMPULSORY_LABEL],
                "quote_command_mode": "单商",
            },
        }
    if mode == "全保":
        return {
            "is_quote": True,
            "entities": {
                "platform_code": "PICC",
                "platform_name": "人保",
                QUOTE_PRODUCT_EXCLUSIONS_KEY: [],
                "quote_command_mode": "全保",
            },
        }
    return {}


def extract_quote_config_overrides(text: Any) -> Dict[str, Any]:
    t = _norm_text(text)
    compact = re.sub(r"\s+", "", t)
    overrides: Dict[str, Any] = {}
    consumed_spans: List[Tuple[int, int]] = []
    connector = r"(?:保额|金额|额度|限额|改成|改为|改到|调整成|调整为|调整到|调成|调到|调至|设置为|设为|变成|变为|变到|调整|改|变|到|为)?"
    separator = r"(?:[:：=+＋])?"
    number = r"(\d+(?:[,，]\d{3})*(?:\.\d+)?)"

    seat_pattern = rf"(?:司乘|司乘险|司机乘客|司机和乘客|车上人员)\s*{separator}\s*{connector}\s*{separator}\s*{number}\s*(万|元)?"
    seat_match = re.search(seat_pattern, compact, flags=re.IGNORECASE)
    if seat_match:
        value = _normalize_quote_config_override_value(seat_match.group(1), seat_match.group(2))
        if value:
            overrides[QUOTE_DRIVER_LABEL] = value
            overrides[QUOTE_PASSENGER_LABEL] = value
            consumed_spans.append(seat_match.span())

    for canonical, aliases in QUOTE_CONFIG_OVERRIDE_ALIASES:
        for alias in sorted(aliases, key=len, reverse=True):
            pattern = rf"{_quote_config_alias_pattern(alias)}\s*{separator}\s*{connector}\s*{separator}\s*{number}\s*(万|元)?"
            match = next(
                (
                    item
                    for item in re.finditer(pattern, compact, flags=re.IGNORECASE)
                    if not any(not (item.end() <= start or item.start() >= end) for start, end in consumed_spans)
                ),
                None,
            )
            if not match:
                continue
            value = _normalize_quote_config_override_value(match.group(1), match.group(2))
            if value:
                overrides[canonical] = value
                consumed_spans.append(match.span())
            break

    if not overrides:
        generic = re.fullmatch(r"\s*([\u4e00-\u9fffA-Za-z0-9（）()·_\-/]{2,40})\s*[:：=]\s*(.{1,128})\s*", t)
        if generic:
            label = _canonical_quote_config_override_label(generic.group(1))
            value = _normalize_quote_config_override_value(generic.group(2))
            if label and value and label not in QUOTE_CONFIG_GENERIC_FIELD_BLOCKLIST:
                overrides[label] = value
    return overrides


def detect_quote_config_override_signal(text: Any) -> Dict[str, Any]:
    overrides = extract_quote_config_overrides(text)
    return {
        "is_override": bool(overrides),
        "entities": {"quote_field_overrides": overrides, "force_requote": True} if overrides else {},
        "overrides": overrides,
    }


def _merge_quote_config_overrides(*items: Any, validate_positive: bool = True) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for item in items:
        for key, value in _json_obj(item).items():
            label = _canonical_quote_config_override_label(key)
            if not label:
                continue
            if value is None or _to_str(value).strip() == "":
                continue
            if validate_positive:
                _ensure_positive_numeric_config_value(label, value, context="报价调整字段")
            merged[label] = value
    return _expand_third_party_medical_overrides(merged)


def _quote_override_summary(overrides: Any) -> str:
    pairs = []
    for key, value in _json_obj(overrides).items():
        label = _to_str(key).strip()
        text = _to_str(value).strip()
        if label and text:
            pairs.append(f"{label}={text}")
    return "、".join(pairs[:8])


def _quote_data_override_alias_items() -> Tuple[Tuple[str, str, str], ...]:
    items: List[Tuple[str, str, str]] = []
    for key, label, aliases in QUOTE_DATA_OVERRIDE_ALIASES:
        for alias in aliases:
            alias_text = _to_str(alias).strip()
            if alias_text:
                items.append((key, label, alias_text))
    return tuple(sorted(items, key=lambda item: len(item[2]), reverse=True))


def _quote_data_override_value_pattern(field_key: str) -> str:
    if field_key == "owner_phone":
        return r"(1\d{10})"
    if field_key == "id_number":
        return r"([0-9A-Za-z\u00d7Xx]{18})"
    if field_key == "plate_no":
        return r"([\u4e00-\u9fff][A-Za-z][A-Za-z0-9]{4,7})"
    if field_key == "vin":
        return r"([A-Za-z0-9]{11,20})"
    if field_key == "engine_no":
        return r"([A-Za-z0-9\-]{4,32})"
    if field_key in {"first_register_date", "issue_date", "commercial_start_date", "compulsory_start_date"}:
        return r"(\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2})"
    if field_key == "license_type":
        return r"(02|52|小型新能源汽车号牌|小型新能源汽车|小型汽车号牌|小型汽车|新能源车|新能源|燃油车|燃油|油车|绿牌|蓝牌|绿色|蓝色)"
    if field_key in {"owner_name", "id_name"}:
        return r"([\u4e00-\u9fffA-Za-z0-9（）()·\-]{2,40})"
    if field_key == "vehicle_model":
        return r"([^,，;；。\n\r]{2,90})"
    if field_key == "car_name":
        return r"([A-Za-z0-9][A-Za-z0-9 ._-]{1,39}|[\u4e00-\u9fffA-Za-z0-9]{2,40})"
    return r"([^\s,，,;；。]{1,128})"


def _trim_quote_data_override_value(field_key: str, value: str) -> str:
    text = _to_str(value).strip()
    if field_key not in {"owner_name", "id_name", "vehicle_model", "car_name"} or not text:
        return text
    current_aliases = {
        alias
        for key, _label, aliases in QUOTE_DATA_OVERRIDE_ALIASES
        if key == field_key
        for alias in aliases
    }
    cut_at: Optional[int] = None
    for key, _label, aliases in QUOTE_DATA_OVERRIDE_ALIASES:
        if key == field_key:
            continue
        for alias in aliases:
            alias_text = _to_str(alias).strip()
            if not alias_text or alias_text in current_aliases:
                continue
            pos = text.find(alias_text)
            if pos == 0:
                return ""
            if pos > 0 and (cut_at is None or pos < cut_at):
                cut_at = pos
    return text[:cut_at].strip() if cut_at is not None else text


def _clean_quote_data_override_value(field_key: str, value: Any) -> Any:
    raw = _trim_quote_data_override_value(field_key, _to_str(value)).strip().strip("，,。.;；")
    if not raw:
        return None
    if field_key in {"first_register_date", "issue_date", "commercial_start_date", "compulsory_start_date"}:
        return _normalize_quote_date_text(raw) or None
    if field_key == "license_type":
        return _normalize_license_type_value(raw) or None
    if field_key in {"vin", "engine_no", "plate_no", "id_number"}:
        raw = re.sub(r"\s+", "", raw).upper()
    if field_key == "owner_phone":
        return _normalize_owner_phone(raw)
    cleaned = _clean_quote_dynamic_data({field_key: raw}, derive_owner_name=False)
    value = cleaned.get(field_key)
    if value in (None, ""):
        return None
    return value


def _merge_quote_data_overrides(*items: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    allowed = set(QUOTE_DATA_OVERRIDE_LABELS)
    for item in items:
        for key, value in _json_obj(item).items():
            field_key = _to_str(key).strip()
            if field_key not in allowed:
                continue
            cleaned = _clean_quote_data_override_value(field_key, value)
            if cleaned in (None, ""):
                continue
            merged[field_key] = cleaned
    # 身份姓名在报价请求里最终按车主姓名使用。人工只修正其中一个时，
    # 两侧同步，避免旧 OCR 姓名继续参与报价或材料冲突判断。
    if _to_str(merged.get("owner_name")).strip() and not _to_str(merged.get("id_name")).strip():
        merged["id_name"] = merged["owner_name"]
    if _to_str(merged.get("id_name")).strip() and not _to_str(merged.get("owner_name")).strip():
        merged["owner_name"] = merged["id_name"]
    license_type = _normalize_license_type_value(merged.get("license_type"))
    if license_type:
        merged["license_type"] = license_type
        merged["license_type_override"] = license_type
    return merged


def extract_quote_data_overrides(text: Any) -> Dict[str, Any]:
    raw_text = _norm_text(text)
    if not raw_text:
        return {}
    compact_query_guard = re.sub(r"\s+", "", raw_text)
    if re.match(r"^(查找|查询|查一下|查|搜索|搜|找)", compact_query_guard) and not re.search(
        r"(改成|改为|改到|调整成|调整为|调整到|调成|调到|调至|设置成|设置为|设成|设为|变成|变为|变到|修改成|修改为|修改到|更正成|更正为|更正到|修正成|修正为|修正到|纠正成|纠正为|纠正到|改|变)",
        compact_query_guard,
    ):
        return {}
    connector = r"(?:改成|改为|改到|调整成|调整为|调整到|调成|调到|调至|设置成|设置为|设成|设为|变成|变为|变到|修改成|修改为|修改到|更正成|更正为|更正到|修正成|修正为|修正到|纠正成|纠正为|纠正到|改|变)"
    separator = r"(?:[:：=+＋])?"
    scan_texts = [raw_text]
    compact = re.sub(r"\s+", "", raw_text)
    if compact and compact != raw_text:
        scan_texts.append(compact)

    overrides: Dict[str, Any] = {}
    consumed_by_text: Dict[str, List[Tuple[int, int]]] = {}
    for scan_text in scan_texts:
        consumed = consumed_by_text.setdefault(scan_text, [])
        for field_key, _label, alias in _quote_data_override_alias_items():
            value_pattern = _quote_data_override_value_pattern(field_key)
            pattern = rf"{re.escape(alias)}\s*{separator}\s*{connector}\s*{separator}\s*{value_pattern}"
            for match in re.finditer(pattern, scan_text, flags=re.IGNORECASE):
                overlaps = any(not (match.end() <= start or match.start() >= end) for start, end in consumed)
                if overlaps and field_key in overrides:
                    continue
                if overlaps and field_key not in {"owner_name", "id_name", "vehicle_model", "car_name"}:
                    continue
                cleaned = _clean_quote_data_override_value(field_key, match.group(1))
                if cleaned in (None, ""):
                    continue
                overrides[field_key] = cleaned
                consumed.append(match.span())
                break
    fallback_connectors = (
        "\u4fee\u6539",
        "\u66f4\u6b63",
        "\u4fee\u6b63",
        "\u7ea0\u6b63",
        "\u8c03\u6574",
        "\u8bbe\u7f6e",
        "\u53d8",
        "\u6539",
        "\u8c03",
        "\u8bbe",
    )
    fallback_connector = "|".join(re.escape(item) for item in fallback_connectors)
    fallback_separator = r"(?::|\uff1a|=|\+|\uff0c|,)?"
    fallback_suffix = r"(?:\u4e3a|\u6210|\u5230|\u81f3)?"
    for scan_text in scan_texts:
        consumed = consumed_by_text.setdefault(scan_text, [])
        for field_key, _label, alias in _quote_data_override_alias_items():
            if field_key in overrides:
                continue
            value_pattern = _quote_data_override_value_pattern(field_key)
            pattern = rf"{re.escape(alias)}\s*{fallback_separator}\s*(?:{fallback_connector}){fallback_suffix}\s*{fallback_separator}\s*{value_pattern}"
            for match in re.finditer(pattern, scan_text, flags=re.IGNORECASE):
                if any(not (match.end() <= start or match.start() >= end) for start, end in consumed):
                    continue
                cleaned = _clean_quote_data_override_value(field_key, match.group(1))
                if cleaned in (None, ""):
                    continue
                overrides[field_key] = cleaned
                consumed.append(match.span())
                break
    direct_value_fields = {
        "first_register_date",
        "issue_date",
        "commercial_start_date",
        "compulsory_start_date",
        "license_type",
        "vin",
        "engine_no",
        "plate_no",
        "id_number",
    }
    for scan_text in scan_texts:
        consumed = consumed_by_text.setdefault(scan_text, [])
        for field_key, _label, alias in _quote_data_override_alias_items():
            if field_key in overrides or field_key not in direct_value_fields:
                continue
            value_pattern = _quote_data_override_value_pattern(field_key)
            pattern = rf"{re.escape(alias)}\s*{separator}\s*{value_pattern}"
            for match in re.finditer(pattern, scan_text, flags=re.IGNORECASE):
                if any(not (match.end() <= start or match.start() >= end) for start, end in consumed):
                    continue
                cleaned = _clean_quote_data_override_value(field_key, match.group(1))
                if cleaned in (None, ""):
                    continue
                overrides[field_key] = cleaned
                consumed.append(match.span())
                break
    compact_only = re.sub(r"\s+", "", raw_text).upper()
    if "vin" not in overrides and re.fullmatch(r"[A-Z0-9]{17}", compact_only):
        cleaned = _clean_quote_data_override_value("vin", compact_only)
        if cleaned:
            overrides["vin"] = cleaned
    if "id_number" not in overrides and re.fullmatch(r"[0-9A-Z\u00d7X]{18}", compact_only):
        cleaned = _clean_quote_data_override_value("id_number", compact_only)
        if cleaned:
            overrides["id_number"] = cleaned
    if "plate_no" not in overrides and re.fullmatch(r"[\u4e00-\u9fff][A-Z][A-Z0-9]{4,7}", compact_only):
        cleaned = _clean_quote_data_override_value("plate_no", compact_only)
        if cleaned:
            overrides["plate_no"] = cleaned
    return _merge_quote_data_overrides(overrides)


def _quote_text_has_explicit_data_override_operator(text: Any) -> bool:
    compact = re.sub(r"\s+", "", _norm_text(text))
    if not compact:
        return False
    return bool(
        re.search(
            r"改成|改为|改到|调整成|调整为|调整到|调成|调到|调至|设置为|设为|变成|变为|变到|更正为|修正为|纠正为|调整|修改|更正|修正|纠正|改|变",
            compact,
        )
    )


def detect_quote_data_override_signal(text: Any) -> Dict[str, Any]:
    overrides = extract_quote_data_overrides(text)
    if not overrides and not _quote_text_has_explicit_data_override_operator(text):
        return {"is_override": False, "entities": {}, "overrides": {}}
    return {
        "is_override": bool(overrides),
        "entities": {QUOTE_DATA_OVERRIDES_KEY: overrides, "force_requote": True} if overrides else {},
        "overrides": overrides,
    }


def _quote_data_override_summary(overrides: Any) -> str:
    pairs: List[str] = []
    for key, value in _json_obj(overrides).items():
        label = QUOTE_DATA_OVERRIDE_LABELS.get(_to_str(key).strip(), _to_str(key).strip())
        text = _to_str(value).strip()
        if label and text:
            pairs.append(f"{label}：{text}")
    return "、".join(pairs[:8])


def _quote_lookup_value(overrides: Any, extracted: Any, entities: Any, key: str) -> str:
    """Use manually confirmed quote data first when finding/reusing an order."""

    field_key = _to_str(key).strip()
    if not field_key:
        return ""
    for source in (_json_obj(overrides), _json_obj(extracted), _json_obj(entities)):
        value = _to_str(source.get(field_key)).strip()
        if value:
            return value
    return ""


def _quote_product_exclusion_summary(exclusions: Any) -> str:
    labels = _normalize_quote_product_exclusions(exclusions)
    if not labels:
        return ""
    return "去掉" + "、".join(labels[:6])


def _snapshot_quote_product_exclusions(snapshot: Any) -> List[str]:
    snap = _json_obj(snapshot)
    normalized = _json_obj(snap.get("normalized_data"))
    platform_default = _json_obj(snap.get("platform_default_config"))
    default_json = _json_obj(snap.get("default_config_json"))
    for source in (normalized, default_json, platform_default):
        labels = _normalize_quote_product_exclusions(source.get(QUOTE_PRODUCT_EXCLUSIONS_KEY))
        if labels:
            return labels
        if QUOTE_PRODUCT_EXCLUSIONS_KEY in source:
            return []
    return []


def _quote_product_state_changes_current(
    *,
    quote_command_mode: str,
    quote_product_exclusions: Any,
    current_exclusions: Any,
) -> bool:
    current = _normalize_quote_product_exclusions(current_exclusions)
    incoming = _normalize_quote_product_exclusions(quote_product_exclusions)
    mode = _to_str(quote_command_mode).strip()
    if mode == "全保":
        desired = incoming
    elif mode == "单商":
        desired = _normalize_quote_product_exclusions([QUOTE_COMPULSORY_LABEL, *incoming])
    elif mode == "交三":
        desired = _normalize_quote_product_exclusions([QUOTE_LOSS_LABEL, *incoming])
    elif incoming:
        desired = _normalize_quote_product_exclusions([*current, *incoming])
    else:
        return False
    return desired != current


def _quote_flow_type_from_case_data(value: Any) -> str:
    data = _json_obj(value)
    flow_type = _to_str(data.get(QUOTE_FLOW_TYPE_KEY)).strip()
    if flow_type in {QUOTE_FLOW_NORMAL, QUOTE_FLOW_RENEWAL}:
        return flow_type
    if _has_reusable_renewal_quote_context(data):
        return QUOTE_FLOW_RENEWAL
    return ""


def _resolve_followup_quote_flow_type(
    *,
    current_flow_type: Any,
    merged_entities: Any,
    case_data: Any,
    quote_state_changed: bool,
) -> str:
    flow_type = _to_str(current_flow_type).strip() or QUOTE_FLOW_NORMAL
    entities = _json_obj(merged_entities)
    if not quote_state_changed or QUOTE_FLOW_TYPE_KEY in entities:
        return flow_type
    inherited_flow_type = _quote_flow_type_from_case_data(case_data)
    if inherited_flow_type and inherited_flow_type != QUOTE_FLOW_NORMAL:
        return inherited_flow_type
    return flow_type


def _merge_quote_product_exclusions_for_command(
    *,
    current_exclusions: Any,
    merged_entities: Any,
    quote_command_mode: str,
    quote_product_exclusions: Any,
) -> Tuple[List[str], bool]:
    current = _normalize_quote_product_exclusions(current_exclusions)
    entities = _json_obj(merged_entities)
    incoming = _normalize_quote_product_exclusions(entities.get(QUOTE_PRODUCT_EXCLUSIONS_KEY))
    explicit_exclusions = _normalize_quote_product_exclusions(quote_product_exclusions)
    mode = _to_str(quote_command_mode).strip()
    if mode == "全保":
        return _normalize_quote_product_exclusions(explicit_exclusions), True
    if mode == "单商":
        return _normalize_quote_product_exclusions([QUOTE_COMPULSORY_LABEL, *explicit_exclusions]), True
    if mode == "交三":
        return _normalize_quote_product_exclusions([QUOTE_LOSS_LABEL, *explicit_exclusions]), True
    if explicit_exclusions:
        return _normalize_quote_product_exclusions([*current, *incoming]), True
    if QUOTE_PRODUCT_EXCLUSIONS_KEY in entities:
        return incoming, True
    return current, False


def _extract_quote_repair_code_command(text: Any) -> Dict[str, Any]:
    raw = _norm_text(text)
    compact = re.sub(r"\s+", "", raw)
    if not compact:
        return {}
    if re.search(r"(?:取消|非|不要|不用|不使用|关闭|去掉)送修码", compact):
        return {"is_command": True, "enabled": False, "raw_text": raw}
    match = re.search(r"送\s*修\s*码", raw)
    if not match:
        return {}
    body = raw[match.end() :].strip()
    body = re.sub(r"^[：:=\-—－,，;；\s]+", "", body).strip()
    body = re.sub(r"\s*(?:人保|PICC|picc|太平洋|平安)?\s*报价\s*$", "", body).strip()
    if not body:
        return {"is_command": True, "enabled": True, "query": "", "raw_text": raw}
    code_match = re.search(r"\d{6,20}", body)
    code = code_match.group(0) if code_match else ""
    name = body
    if code:
        name = name.replace(code, " ")
    name = re.sub(r"^[：:=\-—－,，;；\s]+|[：:=\-—－,，;；\s]+$", "", name).strip()
    return {
        "is_command": True,
        "enabled": True,
        "query": body,
        "code": code,
        "name": name,
        "raw_text": raw,
    }


def _repair_code_match_key(value: Any) -> str:
    return re.sub(r"[\s:：=+\-—－_,，,;；。·（）()【】\\[\\]{}]+", "", _to_str(value).strip()).lower()


def _is_repair_code_subsequence(needle: str, haystack: str) -> bool:
    if not needle or not haystack:
        return False
    pos = 0
    for char in haystack:
        if pos < len(needle) and char == needle[pos]:
            pos += 1
    return pos == len(needle)


def _repair_code_fuzzy_score(needle: Any, haystack: Any) -> int:
    needle_key = _repair_code_match_key(needle)
    haystack_key = _repair_code_match_key(haystack)
    if not needle_key or not haystack_key:
        return 0
    if needle_key == haystack_key:
        return 120
    if needle_key in haystack_key:
        return 100
    if haystack_key in needle_key and len(haystack_key) >= 2:
        return 90
    if len(needle_key) >= 2 and _is_repair_code_subsequence(needle_key, haystack_key):
        return 80
    needle_chars = [char for char in needle_key if not char.isdigit()]
    if len(needle_chars) >= 2:
        hit = sum(1 for char in needle_chars if char in haystack_key)
        coverage = hit / max(len(needle_chars), 1)
        if coverage >= 0.8:
            return int(60 + coverage * 10)
    return 0


def _repair_code_overrides(*, enabled: bool, code: str = "", name: str = "") -> Dict[str, Any]:
    if not enabled:
        return {"送修码启用": "0"}
    return {
        "送修码启用": "1",
        "送修码": code,
        "送修码名称": name,
        "专管代码": code,
        "专管名称": name,
        "monopolyCode": code,
        "monopolyName": name,
    }


def _pick_repair_code_row(rows: Iterable[Any], command: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    safe_rows = [
        {
            "flag": _to_str(_json_obj(row).get("flag")).strip(),
            "monopolyCode": _to_str(_json_obj(row).get("monopolyCode")).strip(),
            "monopolyName": _to_str(_json_obj(row).get("monopolyName")).strip(),
        }
        for row in rows
        if _to_str(_json_obj(row).get("monopolyCode")).strip() or _to_str(_json_obj(row).get("monopolyName")).strip()
    ]
    code = _to_str(command.get("code")).strip()
    name_key = _repair_code_match_key(command.get("name"))
    query_key = _repair_code_match_key(command.get("query"))

    scored_matches: List[Tuple[int, Dict[str, Any]]] = []
    if code:
        for row in safe_rows:
            if _to_str(row.get("monopolyCode")).strip() != code:
                continue
            score = 200
            if name_key:
                score += _repair_code_fuzzy_score(name_key, row.get("monopolyName"))
            if name_key and score <= 200:
                continue
            scored_matches.append((score, row))
        if not scored_matches:
            return {}, safe_rows[:8]
    else:
        needle = name_key or query_key
        if not needle:
            return {}, safe_rows[:8]
        for row in safe_rows:
            haystack = _repair_code_match_key(f"{row.get('monopolyCode')}{row.get('monopolyName')}")
            score = max(
                _repair_code_fuzzy_score(needle, haystack),
                _repair_code_fuzzy_score(needle, row.get("monopolyName")),
                _repair_code_fuzzy_score(needle, row.get("monopolyCode")),
            )
            if score > 0:
                scored_matches.append((score, row))
    scored_matches.sort(
        key=lambda item: (
            0 if _to_str(item[1].get("flag")).strip() == "1" else 1,
            -item[0],
            len(_to_str(item[1].get("monopolyName")).strip()),
            _to_str(item[1].get("monopolyCode")).strip(),
        )
    )
    matches = [row for _, row in scored_matches]
    return (matches[0] if matches else {}), matches[:8] if matches else safe_rows[:8]


async def _resolve_quote_repair_code_command(
    db: AsyncSession,
    *,
    ctx: Dict[str, Any],
    owner_user_id: int,
    command: Mapping[str, Any],
) -> Dict[str, Any]:
    if not command:
        return {}
    if not bool(command.get("enabled")):
        return {
            "overrides": _repair_code_overrides(enabled=False),
            "summary": "已取消送修码",
            "query": "",
            "matched": None,
        }

    account = await _select_logged_quote_platform_account(
        db,
        owner_user_id=owner_user_id,
        platform_code="PICC",
        account_type_name=None,
    )
    if account is None:
        raise ValueError("当前没有可用的人保平台账号，无法查询送修码列表")
    runtime_result = await quote_platform_runtime.query_repair_codes(
        _platform_account_context(account),
        {"query": command.get("query") or command.get("raw_text") or "", "rows": 1000},
        db=db,
    )
    if _is_runtime_session_expired_result(runtime_result):
        _apply_platform_account_runtime_status(account, runtime_result, default_error="送修码查询失败")
        raise ValueError(_runtime_detail(runtime_result, "送修码查询失败"))
    if _runtime_status(runtime_result) not in {"success", "ok"}:
        raise ValueError(_runtime_detail(runtime_result, "送修码查询失败"))
    runtime_payload = _runtime_result_payload(runtime_result)
    rows = _json_list(_json_obj(runtime_payload.get("data")).get("rows"))
    matched, candidates = _pick_repair_code_row(rows, command)
    if not matched:
        query_text = _to_str(command.get("query") or command.get("raw_text")).strip()
        tips = [
            f"{row.get('monopolyCode')}-{row.get('monopolyName')}"
            for row in candidates[:5]
            if _to_str(row.get("monopolyCode")).strip() or _to_str(row.get("monopolyName")).strip()
        ]
        suffix = f"。可选示例：{'；'.join(tips)}" if tips else ""
        raise ValueError(f"未在平台送修码列表中匹配到“{query_text or '空'}”{suffix}")
    code = _to_str(matched.get("monopolyCode")).strip()
    name = _to_str(matched.get("monopolyName")).strip()
    return {
        "overrides": _repair_code_overrides(enabled=True, code=code, name=name),
        "summary": f"送修码={code}-{name}",
        "query": _to_str(command.get("query")).strip(),
        "matched": matched,
        "candidates": candidates,
        "platform_account": _credential_public_payload(account),
    }


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


def _account_profile_aad_values(owner_user_id: int, platform_code: Any, account_username: Any) -> str:
    return (
        f"quote_platform_account_profile:{int(owner_user_id or 0)}:"
        f"{_to_str(platform_code).strip().upper()}:{_to_str(account_username).strip()}"
    )


def _account_profile_aad(row: QuotePlatformAccountProfile) -> str:
    return _account_profile_aad_values(row.owner_user_id or 0, row.platform_code, row.account_username)


def _account_session_summary_from_payload(row: QuotePlatformAccountProfile) -> Dict[str, Any]:
    payload = _json_obj(_loaded_value(row, "credential_payload"))
    summary = _json_obj(payload.get("session_summary"))
    if not summary:
        return {}
    blocked = {"cookies", "authorization", "user_token", "jsession_id", "session_snapshot"}
    safe = {str(k): v for k, v in summary.items() if str(k) not in blocked}
    if "last_error_message" in safe:
        safe["last_error_message"] = sanitize_quote_user_message(safe.get("last_error_message"))
    if "last_error" in safe:
        safe["last_error"] = sanitize_quote_user_message(safe.get("last_error"))
    return safe


def _account_password_for_management(row: QuotePlatformAccountProfile) -> str:
    password_ciphertext = _to_str(_loaded_value(row, "password_ciphertext")).strip()
    if not password_ciphertext:
        return ""
    try:
        return decrypt_text(password_ciphertext, aad=_account_profile_aad(row)) or ""
    except Exception:
        return ""


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


def _normalize_quota_period_type(value: Any) -> str:
    text = _to_str(value).strip().lower()
    if text in {"", "day", "daily", "d", "日"}:
        return ACCOUNT_QUOTA_PERIOD_DAY
    if text in {"week", "weekly", "w", "周"}:
        return ACCOUNT_QUOTA_PERIOD_WEEK
    if text in {"month", "monthly", "m", "月"}:
        return ACCOUNT_QUOTA_PERIOD_MONTH
    raise ValueError("查询额度周期只能选择日、周或月")


def _normalize_quota_limit_value(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = int(value)
    except Exception:
        raise ValueError("查询额度必须是非负整数")
    if number < 0:
        raise ValueError("查询额度不能小于 0")
    if number > 1_000_000:
        raise ValueError("查询额度不能超过 1000000")
    return number


def _quota_period_bounds(period_type: str, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    current = now or _now()
    day_start = datetime(current.year, current.month, current.day)
    period = _normalize_quota_period_type(period_type)
    if period == ACCOUNT_QUOTA_PERIOD_WEEK:
        start = day_start - timedelta(days=day_start.weekday())
        return start, start + timedelta(days=7)
    if period == ACCOUNT_QUOTA_PERIOD_MONTH:
        start = datetime(current.year, current.month, 1)
        if current.month == 12:
            end = datetime(current.year + 1, 1, 1)
        else:
            end = datetime(current.year, current.month + 1, 1)
        return start, end
    return day_start, day_start + timedelta(days=1)


def _reset_account_quota_if_needed(row: Optional[QuotePlatformAccountQuota], now: Optional[datetime] = None) -> bool:
    if row is None:
        return False
    current = now or _now()
    period = _normalize_quota_period_type(_loaded_value(row, "period_type") or ACCOUNT_QUOTA_PERIOD_DAY)
    start, end = _quota_period_bounds(period, current)
    period_start_at = _loaded_value(row, "period_start_at")
    period_end_at = _loaded_value(row, "period_end_at")
    needs_reset = not period_start_at or not period_end_at or current >= period_end_at
    if not needs_reset:
        return False
    row.period_type = period
    row.used_count = 0
    row.period_start_at = start
    row.period_end_at = end
    row.updated_at = current
    return True


def _quota_remaining_count(row: Optional[QuotePlatformAccountQuota]) -> Optional[int]:
    if row is None:
        return None
    limit = max(0, _safe_int(_loaded_value(row, "quota_limit"), 0))
    used = max(0, _safe_int(_loaded_value(row, "used_count"), 0))
    return max(0, limit - used)


def _sync_account_quota_status(
    account: Optional[QuotePlatformAccountProfile],
    quota: Optional[QuotePlatformAccountQuota],
    *,
    reset_happened: bool = False,
    now: Optional[datetime] = None,
) -> None:
    if account is None or quota is None:
        return
    current = now or _now()
    remaining = _quota_remaining_count(quota)
    account.quota_reset_at = _loaded_value(quota, "period_end_at")
    if remaining is not None and remaining <= 0:
        account.quota_status = ACCOUNT_QUOTA_FULL
        account.updated_at = current
        return
    reset_at = _loaded_value(account, "quota_reset_at")
    if (
        reset_happened
        or _loaded_value(account, "quota_status") in {ACCOUNT_QUOTA_UNKNOWN, ACCOUNT_QUOTA_RESET, ACCOUNT_QUOTA_WARNING}
        or (_loaded_value(account, "quota_status") == ACCOUNT_QUOTA_FULL and reset_at and current >= reset_at)
    ):
        account.quota_status = ACCOUNT_QUOTA_AVAILABLE
        account.updated_at = current


def _quota_public_payload(row: Optional[QuotePlatformAccountQuota]) -> Dict[str, Any]:
    if row is None:
        return {
            "configured": False,
            "quota_limit": None,
            "period_type": "",
            "period_label": "",
            "used_count": None,
            "remaining_count": None,
            "remaining_display": "\u672a\u8bbe\u7f6e",
            "period_start_at": None,
            "period_end_at": None,
            "last_consumed_at": None,
        }
    period = _normalize_quota_period_type(_loaded_value(row, "period_type") or ACCOUNT_QUOTA_PERIOD_DAY)
    label = ACCOUNT_QUOTA_PERIOD_LABELS.get(period, "\u65e5")
    remaining = _quota_remaining_count(row)
    return {
        "configured": True,
        "quota_limit": max(0, _safe_int(_loaded_value(row, "quota_limit"), 0)),
        "period_type": period,
        "period_label": label,
        "used_count": max(0, _safe_int(_loaded_value(row, "used_count"), 0)),
        "remaining_count": remaining,
        "remaining_display": f"{remaining}/{label}",
        "period_start_at": _fmt_dt(_loaded_value(row, "period_start_at")),
        "period_end_at": _fmt_dt(_loaded_value(row, "period_end_at")),
        "last_consumed_at": _fmt_dt(_loaded_value(row, "last_consumed_at")),
    }


def _account_inspection_notice_from_payload(row: QuotePlatformAccountProfile) -> Dict[str, Any]:
    payload = _json_obj(_loaded_value(row, "credential_payload"))
    notice = _json_obj(payload.get("inspection_notice"))
    return notice if notice else {}


def _set_account_inspection_notice(
    row: QuotePlatformAccountProfile,
    *,
    notice_type: str,
    message: str,
    task_id: Optional[int] = None,
    level: str = "warning",
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    base = _json_obj(_loaded_value(row, "credential_payload"))
    base["inspection_notice"] = {
        "type": _to_str(notice_type).strip() or "inspection",
        "level": _to_str(level).strip() or "warning",
        "message": _to_str(message).strip(),
        "task_id": int(task_id) if task_id else None,
        "payload": _json_obj(payload),
        "created_at": _fmt_dt(_now()),
    }
    row.credential_payload = base
    row.updated_at = _now()


def _clear_account_inspection_notice(row: QuotePlatformAccountProfile) -> None:
    base = _json_obj(_loaded_value(row, "credential_payload"))
    if "inspection_notice" in base:
        base.pop("inspection_notice", None)
        row.credential_payload = base
        row.updated_at = _now()


def _sync_account_status_from_session_state(
    row: QuotePlatformAccountProfile,
    session_state: Optional[QuotePlatformAccountSessionState],
    *,
    now: Optional[datetime] = None,
) -> None:
    if row is None or session_state is None or not bool(_loaded_value(row, "enabled")):
        return
    status = _to_str(_loaded_value(session_state, "status")).strip().lower()
    current_login_status = _to_str(_loaded_value(row, "login_status")).strip().lower()
    ts = now or _now()
    if status == SESSION_STATUS_AUTHENTICATED:
        if current_login_status != ACCOUNT_LOGIN_AUTHENTICATED or _to_str(_loaded_value(row, "last_error")).strip():
            row.login_status = ACCOUNT_LOGIN_AUTHENTICATED
            row.last_error = None
            row.last_check_at = ts
            row.updated_at = ts
            _clear_account_inspection_notice(row)
        return
    if status in RUNTIME_SESSION_EXPIRED_STATUSES and current_login_status in {
        ACCOUNT_LOGIN_AUTHENTICATED,
        ACCOUNT_LOGIN_DEGRADED,
    }:
        row.login_status = ACCOUNT_LOGIN_EXPIRED
        row.last_error = "平台会话已过期，请重新登录"
        row.last_check_at = ts
        row.updated_at = ts
        _set_account_inspection_notice(
            row,
            notice_type="session_expired",
            message=row.last_error,
            level="warning",
            payload={"source": "account_list_session_sync"},
        )


async def _expire_account_active_login_tasks(
    db: AsyncSession,
    account: QuotePlatformAccountProfile,
    *,
    reason: str,
) -> int:
    rows = (
        await db.execute(
            select(QuotePlatformAccountLoginTask).where(
                QuotePlatformAccountLoginTask.account_id == int(account.id),
                QuotePlatformAccountLoginTask.status.in_([LOGIN_TASK_RUNNING, LOGIN_TASK_NEEDS_CODE]),
            )
        )
    ).scalars().all()
    if not rows:
        return 0
    now = _now()
    for task in rows:
        task.status = LOGIN_TASK_EXPIRED
        task.error_detail = reason
        task.finished_at = now
        task.updated_at = now
    return len(rows)


async def _clear_group_resolved_inspection_attention(
    db: AsyncSession,
    rows: List[QuotePlatformAccountProfile],
    *,
    keep_account_id: int,
) -> None:
    """Clear stale inspection-only notices once the group has a healthy account."""
    for account in rows:
        if int(_loaded_value(account, "id") or 0) == int(keep_account_id):
            continue
        notice = _account_inspection_notice_from_payload(account)
        source = _to_str(_json_obj(notice.get("payload")).get("source")).strip()
        if source not in {"daily_inspection", "account_list"}:
            continue
        task_id = _safe_int(notice.get("task_id"), 0)
        if task_id:
            task = (
                await db.execute(
                    select(QuotePlatformAccountLoginTask)
                    .where(
                        QuotePlatformAccountLoginTask.id == int(task_id),
                        QuotePlatformAccountLoginTask.status.in_([LOGIN_TASK_RUNNING, LOGIN_TASK_NEEDS_CODE]),
                    )
                    .limit(1)
                )
            ).scalars().first()
            if task is not None:
                now = _now()
                task.status = LOGIN_TASK_EXPIRED
                task.error_detail = "同组已有账号登录保活成功，巡检任务已关闭"
                task.finished_at = now
                task.updated_at = now
        _clear_account_inspection_notice(account)


async def _load_account_quota_map(
    db: AsyncSession,
    account_ids: Iterable[int],
    *,
    accounts_by_id: Optional[Dict[int, QuotePlatformAccountProfile]] = None,
) -> Dict[int, QuotePlatformAccountQuota]:
    ids = sorted({int(x) for x in account_ids if x})
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(QuotePlatformAccountQuota).where(QuotePlatformAccountQuota.account_id.in_(ids))
        )
    ).scalars().all()
    now = _now()
    out: Dict[int, QuotePlatformAccountQuota] = {}
    for quota in rows:
        reset_happened = _reset_account_quota_if_needed(quota, now)
        account_id = int(_loaded_value(quota, "account_id") or 0)
        if accounts_by_id:
            _sync_account_quota_status(accounts_by_id.get(account_id), quota, reset_happened=reset_happened, now=now)
        out[account_id] = quota
    return out


async def _save_account_quota_config(
    db: AsyncSession,
    *,
    account: QuotePlatformAccountProfile,
    incoming: Dict[str, Any],
) -> Optional[QuotePlatformAccountQuota]:
    if not incoming.get("quota_limit_provided") and not incoming.get("quota_period_type_provided"):
        return (
            await db.execute(
                select(QuotePlatformAccountQuota)
                .where(QuotePlatformAccountQuota.account_id == int(account.id))
                .limit(1)
            )
        ).scalars().first()

    row = (
        await db.execute(
            select(QuotePlatformAccountQuota)
            .where(QuotePlatformAccountQuota.account_id == int(account.id))
            .limit(1)
        )
    ).scalars().first()
    limit = incoming.get("quota_limit")
    if limit is None:
        if row is not None:
            await db.delete(row)
        account.quota_reset_at = None
        if account.quota_status == ACCOUNT_QUOTA_FULL:
            account.quota_status = ACCOUNT_QUOTA_UNKNOWN
        account.updated_at = _now()
        return None

    now = _now()
    period = _normalize_quota_period_type(incoming.get("quota_period_type") or ACCOUNT_QUOTA_PERIOD_DAY)
    start, end = _quota_period_bounds(period, now)
    if row is None:
        row = QuotePlatformAccountQuota(
            account_id=int(account.id),
            owner_user_id=int(account.owner_user_id),
            platform_code=_to_str(account.platform_code).strip().upper(),
            period_type=period,
            quota_limit=int(limit),
            used_count=0,
            period_start_at=start,
            period_end_at=end,
        )
        db.add(row)
        await db.flush()
    else:
        old_period = _normalize_quota_period_type(row.period_type or ACCOUNT_QUOTA_PERIOD_DAY)
        row.owner_user_id = int(account.owner_user_id)
        row.platform_code = _to_str(account.platform_code).strip().upper()
        row.period_type = period
        row.quota_limit = int(limit)
        if old_period != period:
            row.used_count = 0
            row.period_start_at = start
            row.period_end_at = end
        else:
            reset_happened = _reset_account_quota_if_needed(row, now)
            if reset_happened:
                row.period_start_at = start
                row.period_end_at = end
        row.updated_at = now
    _sync_account_quota_status(account, row, reset_happened=True, now=now)
    return row


async def _consume_account_quota_on_success(
    db: AsyncSession,
    *,
    account: QuotePlatformAccountProfile,
    operator_user_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    reservation = await _reserve_account_quota_for_quote(db, account=account)
    if not reservation.get("reserved"):
        return reservation.get("after") if reservation.get("configured") else None
    return await _record_account_quota_consumed(
        db,
        account=account,
        reservation=reservation,
        operator_user_id=operator_user_id,
    )


async def _reserve_account_quota_for_quote(
    db: AsyncSession,
    *,
    account: QuotePlatformAccountProfile,
) -> Dict[str, Any]:
    row = (
        await db.execute(
            select(QuotePlatformAccountQuota)
            .where(QuotePlatformAccountQuota.account_id == int(account.id))
            .with_for_update()
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return {"configured": False, "available": True, "reserved": False}
    now = _now()
    before = _quota_public_payload(row)
    previous_used_count = max(0, _safe_int(row.used_count, 0))
    previous_last_consumed_at = row.last_consumed_at
    reset_happened = _reset_account_quota_if_needed(row, now)
    remaining = _quota_remaining_count(row)
    if remaining is not None and remaining <= 0:
        _sync_account_quota_status(account, row, reset_happened=reset_happened, now=now)
        await db.flush()
        return {
            "configured": True,
            "available": False,
            "reserved": False,
            "before": before,
            "after": _quota_public_payload(row),
        }
    row.used_count = max(0, _safe_int(row.used_count, 0)) + 1
    row.last_consumed_at = now
    row.updated_at = now
    _sync_account_quota_status(account, row, reset_happened=reset_happened, now=now)
    after = _quota_public_payload(row)
    await db.flush()
    return {
        "configured": True,
        "available": True,
        "reserved": True,
        "before": before,
        "after": after,
        "previous_used_count": previous_used_count,
        "previous_last_consumed_at": previous_last_consumed_at,
    }


async def _release_account_quota_reservation(
    db: AsyncSession,
    *,
    account: Optional[QuotePlatformAccountProfile],
    reservation: Optional[Dict[str, Any]],
) -> None:
    if not account or not reservation or not reservation.get("reserved"):
        return
    row = (
        await db.execute(
            select(QuotePlatformAccountQuota)
            .where(QuotePlatformAccountQuota.account_id == int(account.id))
            .with_for_update()
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return
    now = _now()
    previous_used_count = max(0, _safe_int(reservation.get("previous_used_count"), 0))
    current_used_count = max(0, _safe_int(row.used_count, 0))
    row.used_count = max(previous_used_count, current_used_count - 1)
    if current_used_count <= previous_used_count + 1:
        row.last_consumed_at = reservation.get("previous_last_consumed_at")
    row.updated_at = now
    _sync_account_quota_status(account, row, reset_happened=True, now=now)
    await db.flush()


async def _record_account_quota_consumed(
    db: AsyncSession,
    *,
    account: QuotePlatformAccountProfile,
    reservation: Dict[str, Any],
    operator_user_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if not reservation.get("reserved"):
        return reservation.get("after") if reservation.get("configured") else None
    await _add_account_event(
        db,
        account=account,
        event_type="quota",
        operator_user_id=operator_user_id,
        before={"quota": reservation.get("before") or {}},
        after={"quota": reservation.get("after") or {}},
        message="报价成功，扣减一次查询额度",
    )
    return reservation.get("after")


def _runtime_platform_usage_payload(result: Optional[PlatformRuntimeResult]) -> Dict[str, Any]:
    if result is None:
        return {}
    data = _json_obj(result.data)
    usage = _json_obj(data.get("platform_usage"))
    if usage:
        return usage
    return _json_obj(_json_obj(data.get("quote_result")).get("platform_usage"))


def _platform_today_used_count(usage: Mapping[str, Any]) -> Optional[int]:
    value = _json_obj(usage).get("today_used_count")
    if value is None or _to_str(value).strip() == "":
        return None
    try:
        return max(0, int(Decimal(_to_str(value).strip())))
    except Exception:
        return None


async def _reconcile_account_quota_with_platform_usage(
    db: AsyncSession,
    *,
    account: Optional[QuotePlatformAccountProfile],
    runtime_result: Optional[PlatformRuntimeResult],
    operator_user_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if account is None:
        return None
    usage = _runtime_platform_usage_payload(runtime_result)
    today_used_count = _platform_today_used_count(usage)
    if today_used_count is None:
        return None

    now = _now()
    credential_payload = _json_obj(_loaded_value(account, "credential_payload"))
    previous_usage = _json_obj(credential_payload.get("platform_quote_usage"))
    usage_snapshot = {
        "platform_code": _loaded_value(account, "platform_code") or "",
        "source": _to_str(usage.get("source")).strip() or "queryQuoteTimes",
        "today_used_count": today_used_count,
        "platform_status": usage.get("platform_status"),
        "platform_status_text": sanitize_quote_user_message(usage.get("platform_status_text")),
        "synced_at": _fmt_dt(now),
    }
    credential_payload["platform_quote_usage"] = usage_snapshot
    account.credential_payload = credential_payload
    account.last_check_at = now
    account.updated_at = now

    row = (
        await db.execute(
            select(QuotePlatformAccountQuota)
            .where(QuotePlatformAccountQuota.account_id == int(account.id))
            .with_for_update()
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        await _add_account_event(
            db,
            account=account,
            event_type="quota_sync",
            operator_user_id=operator_user_id,
            before={"platform_usage": previous_usage},
            after={"platform_usage": usage_snapshot},
            message="已记录平台今日报价次数；当前账号未配置本地查询额度",
        )
        await db.flush()
        return None

    before = _quota_public_payload(row)
    reset_happened = _reset_account_quota_if_needed(row, now)
    period = _normalize_quota_period_type(_loaded_value(row, "period_type") or ACCOUNT_QUOTA_PERIOD_DAY)
    current_used = max(0, _safe_int(_loaded_value(row, "used_count"), 0))
    message = "已记录平台今日报价次数"
    if period == ACCOUNT_QUOTA_PERIOD_DAY:
        reconciled_used = max(current_used, today_used_count)
        if reconciled_used != current_used:
            row.used_count = reconciled_used
            row.last_consumed_at = now
            row.updated_at = now
            message = "已按平台今日报价次数矫正本地日额度"
        else:
            message = "平台今日报价次数已核对，本地日额度无需调整"
    else:
        message = "已记录平台今日报价次数；本地周/月额度不按日次数自动矫正"

    _sync_account_quota_status(account, row, reset_happened=reset_happened, now=now)
    after = _quota_public_payload(row)
    await _add_account_event(
        db,
        account=account,
        event_type="quota_sync",
        operator_user_id=operator_user_id,
        before={"quota": before, "platform_usage": previous_usage},
        after={"quota": after, "platform_usage": usage_snapshot},
        message=message,
    )
    await db.flush()
    return after


def _quote_account_label(account: Optional[QuotePlatformAccountProfile]) -> str:
    if not account:
        return "当前账号"
    account_type = _normalize_account_type_name(_loaded_value(account, "account_type_name")) or "未标记"
    username = _to_str(_loaded_value(account, "account_username")).strip()
    if username:
        return f"{account_type}账号（{username}）"
    return f"{account_type}账号"


def _quote_account_needs_admin_contact(role_name: Any) -> bool:
    role = _to_str(role_name).strip()
    return bool(role) and role != ROLE_SUPER_ADMIN


def _quote_account_action_text(role_name: Any, admin_text: str, contact_text: str = "请联系管理员处理。") -> str:
    return contact_text if _quote_account_needs_admin_contact(role_name) else admin_text


def _quote_platform_account_manage_actions(
    role_name: Any,
    *,
    platform_code: str,
    platform_name: str,
) -> List[Dict[str, Any]]:
    if _quote_account_needs_admin_contact(role_name):
        return []
    return [
        _mk_action(
            "平台账号管理",
            "open_account_manager",
            "quote_platform_accounts",
            platform_code=platform_code,
            platform_name=platform_name,
        )
    ]


async def _quote_platform_login_gate_response(
    db: AsyncSession,
    *,
    owner_user_id: int,
    role_name: Any,
    platform_code: str,
    platform_name: str,
    account_type_name: Optional[str] = None,
    entities: Optional[Dict[str, Any]] = None,
    case: Optional[QuoteCase] = None,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    code = _to_str(platform_code).strip().upper()
    name = _to_str(platform_name).strip() or _platform_display_name(code)
    if owner_user_id <= 0 or not code or not name:
        return None
    logged_account = await _select_logged_quote_platform_account(
        db,
        owner_user_id=owner_user_id,
        platform_code=code,
        account_type_name=account_type_name,
    )
    if logged_account is not None:
        return None

    has_enabled = await _has_enabled_quote_platform_account(
        db,
        owner_user_id=owner_user_id,
        platform_code=code,
        account_type_name=None,
    )
    type_hint = f"（类型：{_normalize_account_type_name(account_type_name)}）" if _normalize_account_type_name(account_type_name) else ""
    if has_enabled:
        admin_text = f"{name}{type_hint}当前没有已登录可用账号，请先登录平台账号后再报价。"
        contact_text = f"{name}平台账号当前没有已登录可用会话，请联系管理员处理。"
        message_key = "平台账号没有已登录可用会话"
    else:
        admin_text = f"{name}{type_hint}当前没有可用平台账号，请先新增、启用并登录账号后再报价。"
        contact_text = f"{name}平台账号暂不可用，请联系管理员处理。"
        message_key = "平台账号暂不可用"
    message = _quote_account_action_text(role_name, admin_text, contact_text)
    payload: Dict[str, Any] = {
        "platform_account_login_gate": {
            "platform_code": code,
            "platform_name": name,
            "account_type_name": _normalize_account_type_name(account_type_name),
            "has_enabled_account": bool(has_enabled),
            "has_logged_account": False,
        }
    }
    if case is not None:
        payload["quote_case"] = {
            "id": case.id,
            "case_no": case.case_no,
            "status": case.status,
            "order_id": case.order_id,
            "session_id": case.session_id,
            "platform_code": case.platform_code,
            "platform_name": case.platform_name,
        }
    return (
        message,
        {
            "status": "success",
            "intent": "quote",
            "trace_id": _new_trace_id(),
            "data": _mk_data(
                result_status=RESULT_NEED_MORE,
                message=message_key,
                entities={**(entities or {}), "platform_code": code, "platform_name": name},
                payload=payload,
            ),
            "actions": _quote_platform_account_manage_actions(
                role_name,
                platform_code=code,
                platform_name=name,
            ),
        },
    )


def _quote_quota_exhausted_message(
    platform_name: str,
    account: Optional[QuotePlatformAccountProfile],
    *,
    operator_role_name: Any = "",
) -> str:
    admin_text = f"{platform_name}{_quote_account_label(account)}查询额度已用完，请切换同平台其他账号，或在右上角“平台账号管理”调整额度。"
    return _quote_account_action_text(
        operator_role_name,
        admin_text,
        f"{platform_name}{_quote_account_label(account)}查询额度已用完，请联系管理员处理。",
    )


def _credential_public_payload(
    row: Optional[QuotePlatformAccountProfile],
    *,
    session_state: Optional[QuotePlatformAccountSessionState] = None,
    quota: Optional[QuotePlatformAccountQuota] = None,
    active_login_task: Optional[QuotePlatformAccountLoginTask] = None,
    include_password: bool = False,
) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    login_phone = _loaded_value(row, "login_phone")
    login_phone_mask = _loaded_value(row, "login_phone_mask") or (_mask_phone(login_phone) if _to_str(login_phone).strip() else "")
    password_ciphertext = _loaded_value(row, "password_ciphertext")
    session_summary = _account_session_summary_from_payload(row)
    if session_state is not None:
        session_summary = {
            **session_summary,
            "status": _loaded_value(session_state, "status") or session_summary.get("status") or "",
            "session_version": _loaded_value(session_state, "session_version") or 0,
            "session_generation": _loaded_value(session_state, "session_generation") or "",
            "jwt_issued_at": _loaded_value(session_state, "jwt_issued_at"),
            "jwt_expires_at": _loaded_value(session_state, "jwt_expires_at"),
            "last_login_at": _fmt_dt(_loaded_value(session_state, "last_login_at")),
            "last_authenticated_at": _fmt_dt(_loaded_value(session_state, "last_authenticated_at")),
            "last_keepalive_at": _fmt_dt(_loaded_value(session_state, "last_keepalive_at")),
            "last_business_at": _fmt_dt(_loaded_value(session_state, "last_business_at")),
            "last_refresh_at": _fmt_dt(_loaded_value(session_state, "last_refresh_at")),
            "last_validation_at": _fmt_dt(_loaded_value(session_state, "last_validation_at")),
            "last_error_code": _loaded_value(session_state, "last_error_code") or "",
            "last_error_message": sanitize_quote_user_message(_loaded_value(session_state, "last_error_message")),
        }
    quota_payload = _quota_public_payload(quota)
    inspection_notice = _account_inspection_notice_from_payload(row)
    login_status = _loaded_value(row, "login_status") or ACCOUNT_LOGIN_NOT_LOGGED_IN
    if login_status in {ACCOUNT_LOGIN_AUTHENTICATED, ACCOUNT_LOGIN_DEGRADED}:
        inspection_notice = {}
    platform_quote_usage = _json_obj(_json_obj(_loaded_value(row, "credential_payload")).get("platform_quote_usage"))
    if platform_quote_usage:
        platform_quote_usage = {
            **platform_quote_usage,
            "platform_status_text": sanitize_quote_user_message(platform_quote_usage.get("platform_status_text")),
        }
    quota_remaining_display = quota_payload.get("remaining_display")
    if not quota_payload.get("configured"):
        today_used_count = _platform_today_used_count(platform_quote_usage)
        quota_remaining_display = f"已查询{today_used_count if today_used_count is not None else 0}/日"
    active_login_task_payload = _login_task_payload(active_login_task) if active_login_task is not None else None
    if active_login_task_payload and active_login_task_payload.get("status") == LOGIN_TASK_NEEDS_CODE:
        inspection_notice = {
            "type": "login_challenge",
            "level": "warning",
            "message": active_login_task_payload.get("challenge_prompt") or "账号巡检发现该账号登录需要安全码，请点击登录并输入验证码。",
            "task_id": active_login_task_payload.get("id"),
            "payload": {
                "challenge_type": active_login_task_payload.get("challenge_type"),
                "challenge_payload": active_login_task_payload.get("challenge_payload") or {},
            },
            "created_at": active_login_task_payload.get("updated_at") or active_login_task_payload.get("created_at"),
        }
    payload = {
        "id": _loaded_value(row, "id"),
        "platform_code": _loaded_value(row, "platform_code"),
        "platform_name": _loaded_value(row, "platform_name"),
        "account_type_id": _loaded_value(row, "account_type_id"),
        "account_type_name": _normalize_account_type_name(_loaded_value(row, "account_type_name")) or "",
        "account_username": _loaded_value(row, "account_username"),
        "has_password": bool(_to_str(password_ciphertext).strip()),
        "login_phone_mask": login_phone_mask,
        "has_login_phone": bool(_to_str(login_phone).strip()),
        "email": _loaded_value(row, "email") or "",
        "account_owner_user_id": _loaded_value(row, "account_owner_user_id"),
        "account_owner_name": _loaded_value(row, "account_owner_name") or "",
        "auto_login": bool(_loaded_value(row, "auto_login")),
        "enabled": bool(_loaded_value(row, "enabled")),
        "login_status": login_status,
        "quota_status": _loaded_value(row, "quota_status") or ACCOUNT_QUOTA_UNKNOWN,
        "quota_reset_at": _fmt_dt(_loaded_value(row, "quota_reset_at")),
        "quota_configured": bool(quota_payload.get("configured")),
        "quota_limit": quota_payload.get("quota_limit"),
        "quota_period_type": quota_payload.get("period_type"),
        "quota_period_label": quota_payload.get("period_label"),
        "quota_used_count": quota_payload.get("used_count"),
        "quota_remaining_count": quota_payload.get("remaining_count"),
        "quota_remaining_display": quota_remaining_display,
        "quota_period_start_at": quota_payload.get("period_start_at"),
        "quota_period_end_at": quota_payload.get("period_end_at"),
        "quota_last_consumed_at": quota_payload.get("last_consumed_at"),
        "quota_config": quota_payload,
        "platform_quote_usage": platform_quote_usage,
        "browser_env_key": _loaded_value(row, "browser_env_key"),
        "session": session_summary,
        "active_login_task": active_login_task_payload,
        "inspection_notice": {
            **_json_obj(inspection_notice),
            "message": sanitize_quote_user_message(_json_obj(inspection_notice).get("message")),
        } if inspection_notice else {},
        "last_login_at": _fmt_dt(_loaded_value(row, "last_login_at")),
        "last_check_at": _fmt_dt(_loaded_value(row, "last_check_at")),
        "last_used_at": _fmt_dt(_loaded_value(row, "last_used_at")),
        "last_error": sanitize_quote_user_message(_loaded_value(row, "last_error")),
        "created_at": _fmt_dt(_loaded_value(row, "created_at")),
        "updated_at": _fmt_dt(_loaded_value(row, "updated_at")),
    }
    if include_password:
        payload["account_password"] = _account_password_for_management(row)
        payload["login_phone"] = _loaded_value(row, "login_phone") or ""
    return payload


def _account_event_snapshot(row: Optional[QuotePlatformAccountProfile]) -> Dict[str, Any]:
    return _credential_public_payload(row) or {}


def _platform_default_config_payload(row: QuotePlatformDefaultConfig) -> Dict[str, Any]:
    platform_code = _loaded_value(row, "platform_code")
    account_type_name = _normalize_account_type_name(_loaded_value(row, "account_type_name")) or ""
    default_values = _platform_default_values_with_legacy_fixes(
        platform_code,
        account_type_name,
        _json_obj(_loaded_value(row, "default_values_json")),
    )
    return {
        "id": _loaded_value(row, "id"),
        "platform_code": platform_code,
        "platform_name": _loaded_value(row, "platform_name"),
        "account_type_name": account_type_name,
        "default_values": default_values,
        "enabled": bool(_loaded_value(row, "enabled")),
        "created_by": _loaded_value(row, "created_by"),
        "updated_by": _loaded_value(row, "updated_by"),
        "created_at": _fmt_dt(_loaded_value(row, "created_at")),
        "updated_at": _fmt_dt(_loaded_value(row, "updated_at")),
    }


def _normalize_default_values(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("默认参数必须是字段名和值组成的对象")
    out: Dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = re.sub(r"\s+", " ", _to_str(raw_key).strip())[:128]
        if not key:
            continue
        if isinstance(raw_value, str):
            val: Any = raw_value.strip()
            if not val:
                continue
            if len(val) > 4096:
                raise ValueError(f"默认参数“{key}”的值不能超过 4096 个字符")
        elif raw_value is None:
            continue
        elif isinstance(raw_value, (bool, int, float)):
            val = raw_value
        else:
            raise ValueError(f"默认参数“{key}”暂只支持文本、数字、布尔值或空值")
        _ensure_positive_numeric_config_value(key, val, context="默认参数")
        out[key] = val
    return out


def platform_builtin_default_values(platform_code: Any, account_type_name: Any) -> Dict[str, Any]:
    code = _to_str(platform_code).strip().upper()
    type_name = _normalize_account_type_name(account_type_name)
    if code == "PICC" and type_name:
        try:
            return _normalize_default_values(picc_motor_builtin_default_values(type_name))
        except Exception:
            return {}
    return {}


def _platform_default_values_with_legacy_fixes(
    platform_code: Any,
    account_type_name: Any,
    default_values: Any,
) -> Dict[str, Any]:
    values = dict(_json_obj(default_values))
    code = _to_str(platform_code).strip().upper()
    type_name = _normalize_account_type_name(account_type_name)
    if code != "PICC" or type_name != "新能源车-旧":
        return values

    builtin = platform_builtin_default_values(code, type_name)
    for key in (QUOTE_DRIVER_LABEL, QUOTE_PASSENGER_LABEL):
        raw = _to_str(values.get(key)).strip()
        if raw in {"1", "10000"} and _to_str(builtin.get(key)).strip() in {"4", "40000"}:
            values[key] = builtin[key]
    return values


def _build_quote_request_body(default_values: Dict[str, Any], normalized_data: Dict[str, Any]) -> Dict[str, Any]:
    request_body = dict(default_values or {})
    # Order/OCR/chat data must win when it uses the same key as a default value.
    for key, value in (normalized_data or {}).items():
        if value is not None and _to_str(value).strip() != "":
            request_body[key] = value
    return request_body


def _quote_detect_haystack(normalized_data: Dict[str, Any], images_by_slot: Dict[str, List[Dict[str, Any]]]) -> str:
    keys = (
        "quote_vehicle_type",
        "vehicle_usage_type",
        "vehicle_type",
        "use_nature",
        "energy_type",
        "vehicle_energy_type",
        "fuel_type",
        "fuel_kind",
        "vehicle_model",
        "car_name",
        "vehicle_name",
        "vehicle_brand_name",
        "manufacturer_name",
        "plate_no",
        "vehicle_cert_text",
        "vehicle_certificate_text",
        "generic_ocr_text",
        "ocr_text",
        "raw_text",
        "driving_license_text",
        "vehicle_license_text",
    )
    parts = [_to_str(normalized_data.get(key)).strip() for key in keys if _to_str(normalized_data.get(key)).strip()]
    if images_by_slot.get("vehicle_cert"):
        parts.append("车辆合格证")
        for image in images_by_slot.get("vehicle_cert") or []:
            sample = _to_str(image.get("ocr_text_sample")).strip()
            if sample:
                parts.append(sample)
            features = _json_obj(image.get("text_features"))
            generic_text = _to_str(features.get("generic_ocr_text")).strip()
            if generic_text:
                parts.append(generic_text)
    return " ".join(parts)


def detect_quote_vehicle_type(
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    data = _json_obj(normalized_data)
    transfer_state = _apply_transfer_vehicle_state(data)
    slots = images_by_slot or {}
    haystack = _quote_detect_haystack(data, slots)
    compact = re.sub(r"\s+", "", haystack).lower()
    has_vehicle_cert_material = bool(
        slots.get("vehicle_cert")
        or _to_str(data.get("vehicle_cert_text")).strip()
        or _to_str(data.get("vehicle_certificate_text")).strip()
    )
    has_driving_license_material = bool(
        slots.get("driving_license_main")
        or _to_str(data.get("driving_license_text")).strip()
        or _to_str(data.get("vehicle_license_text")).strip()
    )
    has_labeled_plate_text = _quote_labeled_plate_no_present(haystack)
    has_plate_material = _quote_plate_no_present(data.get("plate_no")) or has_labeled_plate_text
    has_new_energy_plate = _quote_new_energy_plate_no_present(data.get("plate_no")) or (
        has_labeled_plate_text and _quote_new_energy_plate_no_present(haystack)
    )
    field_license_type = _normalize_license_type_value(
        data.get("license_type") or data.get("licenseType") or data.get("licensePlateType") or data.get("license_color_code")
    )
    has_new_energy_license_type = field_license_type == LICENSE_TYPE_NEW_ENERGY
    has_meaningful_vehicle_text = bool(
        any(
            _to_str(data.get(key)).strip()
            for key in (
                "vehicle_type",
                "use_nature",
                "energy_type",
                "vehicle_energy_type",
                "fuel_type",
                "fuel_kind",
                "vehicle_model",
                "vehicle_brand_name",
                "manufacturer_name",
                "generic_ocr_text",
                "ocr_text",
                "raw_text",
            )
        )
        or any(
            _to_str(image.get("ocr_text_sample")).strip()
            or _to_str(_json_obj(image.get("text_features")).get("generic_ocr_text")).strip()
            for rows in slots.values()
            for image in (rows or [])
            if isinstance(image, dict)
        )
    )

    fuel_energy_text = re.sub(
        r"\s+",
        "",
        _to_str(data.get("fuel_type") or data.get("fuel_kind") or data.get("energy_type") or "").strip().lower(),
    )
    explicit_new_energy_fuel = bool(
        fuel_energy_text == "电"
        or _quote_new_energy_text_present(
            fuel_energy_text,
            data.get("vehicle_model"),
            data.get("vehicle_brand_name"),
            data.get("manufacturer_name"),
        )
    )

    energy_type = "unknown"
    energy_name = ""
    if has_new_energy_plate or has_new_energy_license_type:
        energy_type = "new_energy"
        energy_name = "新能源"
    elif _quote_fuel_text_present(compact) and re.search(r"非新能源|不是新能源", compact, flags=re.IGNORECASE):
        energy_type = "fuel"
        energy_name = "燃油"
    elif explicit_new_energy_fuel or _quote_new_energy_text_present(compact):
        energy_type = "new_energy"
        energy_name = "新能源"
    elif _quote_fuel_text_present(compact) or field_license_type == LICENSE_TYPE_FUEL:
        energy_type = "fuel"
        energy_name = "燃油"

    usage_type = "unknown"
    usage_name = ""
    source = "unknown"
    confidence = 0.0

    if has_plate_material:
        usage_type, usage_name, source, confidence = "used_car", "旧车", "plate_no", 0.96

    explicit = _to_str(
        data.get("quote_vehicle_type")
        or data.get("vehicle_usage_type")
        or data.get("vehicle_kind")
    ).strip()
    if explicit and usage_type == "unknown":
        if re.search(r"旧|二手|过户|转移|used", explicit, flags=re.IGNORECASE):
            usage_type, usage_name, source, confidence = "used_car", "旧车", "explicit_field", 0.92
        elif re.search(r"新|未上牌|新购|new", explicit, flags=re.IGNORECASE):
            usage_type, usage_name, source, confidence = "new_car", "新车", "explicit_field", 0.92

    if usage_type == "unknown":
        if re.search(r"旧车|二手车|过户车|转移登记|usedcar|used", compact, flags=re.IGNORECASE):
            usage_type, usage_name, source, confidence = "used_car", "旧车", "ocr_text", 0.84
        elif re.search(r"新车|新购|未上牌|国产新车|进口新车", compact, flags=re.IGNORECASE):
            usage_type, usage_name, source, confidence = "new_car", "新车", "ocr_text", 0.84
        elif has_driving_license_material:
            usage_type, usage_name, source, confidence = "used_car", "旧车", "driving_license_slot", 0.86
        elif has_vehicle_cert_material and not has_driving_license_material:
            usage_type, usage_name, source, confidence = "new_car", "新车", "vehicle_cert_slot", 0.82
        elif _to_str(data.get("first_register_date") or data.get("register_date")).strip():
            usage_type, usage_name, source, confidence = "used_car", "旧车", "first_register_date", 0.78
        elif has_vehicle_cert_material and not _to_str(data.get("first_register_date") or data.get("register_date")).strip():
            usage_type, usage_name, source, confidence = "new_car", "新车", "vehicle_cert_slot", 0.66

    if energy_type == "unknown" and usage_type == "used_car" and has_plate_material:
        energy_type = "fuel"
        energy_name = "燃油"
    elif energy_type == "unknown" and usage_type in {"new_car", "used_car"} and has_meaningful_vehicle_text:
        energy_type = "fuel"
        energy_name = "燃油"

    config_type_name = ""
    if energy_type == "new_energy" and usage_type == "used_car":
        config_type_name = "新能源车-旧"
    elif energy_type == "new_energy":
        config_type_name = "新能源车-新"
    elif energy_type == "fuel" and usage_type == "new_car":
        config_type_name = "油车-新"
    elif energy_type == "fuel" and usage_type == "used_car":
        config_type_name = "油车-旧"

    return {
        "vehicle_usage_type": usage_type,
        "vehicle_usage_type_name": usage_name,
        "vehicle_energy_type": energy_type,
        "vehicle_energy_type_name": energy_name,
        "config_type_name": config_type_name,
        "license_type_decision": _resolve_license_type_decision(
            data,
            slots,
            vehicle_type_detect={
                "vehicle_energy_type": energy_type,
                "config_type_name": config_type_name,
            },
        ),
        "is_transfer_vehicle": bool(transfer_state.get("is_transfer_vehicle")),
        "transfer_date": _to_str(transfer_state.get("transfer_date")).strip(),
        "transfer_vehicle_source": _to_str(transfer_state.get("transfer_vehicle_source")).strip(),
        "source": source,
        "confidence": round(confidence, 4),
    }


async def list_platform_default_configs(
    db: AsyncSession,
    *,
    platform_code: Optional[str] = None,
    account_type_name: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    stmt = select(QuotePlatformDefaultConfig)
    code = _to_str(platform_code).strip().upper()
    if code:
        stmt = stmt.where(QuotePlatformDefaultConfig.platform_code == code)
    type_name = _normalize_account_type_name(account_type_name)
    if account_type_name is not None:
        stmt = stmt.where(QuotePlatformDefaultConfig.account_type_name.in_(_account_type_db_names(type_name, allow_empty=True)))
    if enabled is not None:
        stmt = stmt.where(QuotePlatformDefaultConfig.enabled == bool(enabled))
    rows = (
        await db.execute(
            stmt.order_by(
                QuotePlatformDefaultConfig.platform_code.asc(),
                QuotePlatformDefaultConfig.account_type_name.asc(),
                desc(QuotePlatformDefaultConfig.updated_at),
                desc(QuotePlatformDefaultConfig.id),
            )
        )
    ).scalars().all()
    return {"total": len(rows), "items": [_platform_default_config_payload(row) for row in rows]}


async def save_platform_default_config(
    db: AsyncSession,
    *,
    values: Dict[str, Any],
    config_id: Optional[int] = None,
    operator_user_id: Optional[int] = None,
) -> QuotePlatformDefaultConfig:
    code, platform_name = _normalize_platform_code_name(values.get("platform_code"), values.get("platform_name"))
    type_name = _ensure_fixed_quote_account_type(values.get("account_type_name"), allow_empty=False)
    default_values = _normalize_default_values(values.get("default_values") or {})
    if not default_values:
        raise ValueError("请至少配置一个默认参数")

    row: Optional[QuotePlatformDefaultConfig] = None
    is_new_row = False
    if config_id:
        row = (
            await db.execute(
                select(QuotePlatformDefaultConfig)
                .where(QuotePlatformDefaultConfig.id == int(config_id))
                .limit(1)
            )
        ).scalars().first()
        if not row:
            raise ValueError("默认参数配置不存在或已删除")
        old_code = _to_str(row.platform_code).strip().upper()
        old_platform_name = _to_str(row.platform_name).strip() or _platform_display_name(old_code, None)
        old_type_name = _normalize_account_type_name(row.account_type_name)
        old_enabled = bool(row.enabled)
        removes_old_coverage = old_enabled and (
            old_code != code
            or old_type_name != type_name
            or not bool(values.get("enabled", True))
        )
        if removes_old_coverage:
            await _ensure_default_config_removal_safe(
                db,
                platform_code=old_code,
                platform_name=old_platform_name,
                account_type_name=old_type_name,
                action="停用或改为其他账号类型",
                exclude_config_id=row.id,
            )
    else:
        row = (
            await db.execute(
                select(QuotePlatformDefaultConfig)
                .where(
                    QuotePlatformDefaultConfig.platform_code == code,
                    QuotePlatformDefaultConfig.account_type_name.in_(_account_type_db_names(type_name, allow_empty=True)),
                )
                .limit(1)
            )
        ).scalars().first()
        if not row:
            row = QuotePlatformDefaultConfig(
                platform_code=code,
                platform_name=platform_name,
                account_type_name=type_name,
                default_values_json=default_values,
                enabled=bool(values.get("enabled", True)),
                created_by=operator_user_id,
                updated_by=operator_user_id,
            )
            db.add(row)
            is_new_row = True

    if not is_new_row:
        duplicate = (
            await db.execute(
                select(QuotePlatformDefaultConfig)
                .where(
                    QuotePlatformDefaultConfig.platform_code == code,
                    QuotePlatformDefaultConfig.account_type_name.in_(_account_type_db_names(type_name, allow_empty=True)),
                    QuotePlatformDefaultConfig.id != int(row.id or 0),
                )
                .limit(1)
            )
        ).scalars().first()
        if duplicate:
            label = type_name or "通用"
            raise ValueError(f"{platform_name}（{label}）默认参数配置已存在，请直接编辑原配置")

    row.platform_code = code
    row.platform_name = platform_name
    row.account_type_name = type_name
    row.default_values_json = default_values
    row.enabled = bool(values.get("enabled", True))
    row.updated_by = operator_user_id
    row.updated_at = _now()
    await db.flush()
    return row


async def delete_platform_default_config(
    db: AsyncSession,
    *,
    config_id: int,
) -> bool:
    row = (
        await db.execute(
            select(QuotePlatformDefaultConfig)
            .where(QuotePlatformDefaultConfig.id == int(config_id))
            .limit(1)
        )
    ).scalars().first()
    if not row:
        return False
    if bool(row.enabled):
        await _ensure_default_config_removal_safe(
            db,
            platform_code=row.platform_code,
            platform_name=row.platform_name,
            account_type_name=row.account_type_name,
            action="删除",
            exclude_config_id=row.id,
        )
    await db.delete(row)
    await db.flush()
    return True


async def resolve_platform_default_config(
    db: AsyncSession,
    *,
    platform_code: str,
    account_type_name: Optional[str] = None,
) -> Dict[str, Any]:
    code = _to_str(platform_code).strip().upper()
    if not code:
        return {"config": None, "default_values": {}, "builtin_default_values": {}, "matched": "none"}
    type_name = _normalize_account_type_name(account_type_name)
    builtin_defaults = platform_builtin_default_values(code, type_name)
    if not type_name:
        return {"config": None, "default_values": {}, "builtin_default_values": builtin_defaults, "matched": "none"}
    candidates = list(_account_type_db_names(type_name))
    for candidate in candidates:
        row = (
            await db.execute(
                select(QuotePlatformDefaultConfig)
                .where(
                    QuotePlatformDefaultConfig.platform_code == code,
                    QuotePlatformDefaultConfig.account_type_name == candidate,
                    QuotePlatformDefaultConfig.enabled == True,  # noqa: E712
                )
                .order_by(desc(QuotePlatformDefaultConfig.updated_at), desc(QuotePlatformDefaultConfig.id))
                .limit(1)
            )
        ).scalars().first()
        if row:
            payload = _platform_default_config_payload(row)
            return {
                "config": payload,
                "default_values": _json_obj(payload.get("default_values")),
                "builtin_default_values": builtin_defaults,
                "matched": "account_type" if candidate else "platform_common",
            }
    return {"config": None, "default_values": {}, "builtin_default_values": builtin_defaults, "matched": "none"}


async def _has_enabled_platform_default_config(
    db: AsyncSession,
    *,
    platform_code: Any,
    account_type_name: Any,
    exclude_config_id: Optional[int] = None,
) -> bool:
    code = _to_str(platform_code).strip().upper()
    type_name = _normalize_account_type_name(account_type_name)
    if not code or not type_name:
        return False
    stmt = select(QuotePlatformDefaultConfig.id).where(
        QuotePlatformDefaultConfig.platform_code == code,
        QuotePlatformDefaultConfig.account_type_name.in_(_account_type_db_names(type_name)),
        QuotePlatformDefaultConfig.enabled == True,  # noqa: E712
    )
    if exclude_config_id:
        stmt = stmt.where(QuotePlatformDefaultConfig.id != int(exclude_config_id))
    row_id = (await db.execute(stmt.limit(1))).scalar()
    return bool(row_id)


async def _enabled_account_count_for_default_config(
    db: AsyncSession,
    *,
    platform_code: Any,
    account_type_name: Any,
) -> int:
    code = _to_str(platform_code).strip().upper()
    type_name = _normalize_account_type_name(account_type_name)
    if not code or not type_name:
        return 0
    total = (
        await db.execute(
            select(func.count())
            .select_from(QuotePlatformAccountProfile)
            .where(
                QuotePlatformAccountProfile.platform_code == code,
                QuotePlatformAccountProfile.account_type_name.in_(_account_type_db_names(type_name)),
                QuotePlatformAccountProfile.enabled == True,  # noqa: E712
            )
        )
    ).scalar_one()
    return int(total or 0)


async def _ensure_default_config_removal_safe(
    db: AsyncSession,
    *,
    platform_code: Any,
    platform_name: Any,
    account_type_name: Any,
    action: str,
    exclude_config_id: Optional[int] = None,
) -> None:
    code = _to_str(platform_code).strip().upper()
    type_name = _normalize_account_type_name(account_type_name)
    if not code or not type_name:
        return
    if await _has_enabled_platform_default_config(
        db,
        platform_code=code,
        account_type_name=type_name,
        exclude_config_id=exclude_config_id,
    ):
        return
    enabled_count = await _enabled_account_count_for_default_config(
        db,
        platform_code=code,
        account_type_name=type_name,
    )
    if enabled_count <= 0:
        return
    name = _platform_display_name(code, _to_str(platform_name).strip() or None)
    verb = _to_str(action).strip() or "删除"
    raise ValueError(
        f"{name}（{type_name}）已有 {enabled_count} 个启用账号依赖这条默认参数，不能{verb}。"
        "请先停用对应账号，或保留该配置并直接编辑默认值。"
    )


async def _ensure_default_config_ready_for_enabled_account(db: AsyncSession, incoming: Mapping[str, Any]) -> None:
    if not bool(incoming.get("enabled")):
        return
    platform_code = _to_str(incoming.get("platform_code")).strip().upper()
    account_type_name = _normalize_account_type_name(incoming.get("account_type_name"))
    if await _has_enabled_platform_default_config(
        db,
        platform_code=platform_code,
        account_type_name=account_type_name,
    ):
        return
    platform_name = _platform_display_name(platform_code, _to_str(incoming.get("platform_name")).strip() or None)
    raise ValueError(
        f"{platform_name}（{account_type_name}）还没有启用的默认参数配置，"
        "请先在右上角“默认参数配置”中新增并启用该账号类型配置，再启用业务账号"
    )


async def apply_platform_default_config_to_snapshot(
    db: AsyncSession,
    *,
    snapshot: Dict[str, Any],
    platform_code: str,
    account_type_name: Optional[str] = None,
    config_type_name: Optional[str] = None,
) -> Dict[str, Any]:
    safe_snapshot = _json_obj(snapshot)
    normalized_data = _json_obj(safe_snapshot.get("normalized_data"))
    images_by_slot = _json_obj(safe_snapshot.get("images_by_slot"))
    vehicle_type_detect = detect_quote_vehicle_type(normalized_data, images_by_slot)
    resolved_type_name = _normalize_account_type_name(config_type_name) or _normalize_account_type_name(
        vehicle_type_detect.get("config_type_name")
    ) or _normalize_account_type_name(account_type_name)
    resolved = await resolve_platform_default_config(
        db,
        platform_code=platform_code,
        account_type_name=resolved_type_name,
    )
    builtin_defaults = _json_obj(resolved.get("builtin_default_values"))
    resolved_defaults = _json_obj(resolved.get("default_values"))
    quote_field_overrides = _merge_quote_config_overrides(normalized_data.get("quote_field_overrides"))
    default_values = dict(builtin_defaults)
    default_values.update(resolved_defaults)
    default_values.update(quote_field_overrides)
    quote_product_exclusions = _normalize_quote_product_exclusions(
        normalized_data.get(QUOTE_PRODUCT_EXCLUSIONS_KEY)
    )
    if quote_product_exclusions:
        default_values[QUOTE_PRODUCT_EXCLUSIONS_KEY] = quote_product_exclusions
    safe_snapshot["vehicle_type_detect"] = vehicle_type_detect
    safe_snapshot["vehicle_usage_type"] = vehicle_type_detect.get("vehicle_usage_type") or "unknown"
    safe_snapshot["vehicle_energy_type"] = vehicle_type_detect.get("vehicle_energy_type") or "unknown"
    safe_snapshot["default_config_json"] = default_values
    safe_snapshot["platform_default_config"] = {
        "matched": resolved.get("matched") or "none",
        "config": resolved.get("config"),
        "builtin_default_values": builtin_defaults,
        "resolved_type_name": resolved_type_name,
        "account_type_name": _normalize_account_type_name(account_type_name),
        "quote_field_overrides": quote_field_overrides,
        QUOTE_PRODUCT_EXCLUSIONS_KEY: quote_product_exclusions,
    }
    safe_snapshot["platform_default_params"] = default_values
    safe_snapshot["request_body"] = _build_quote_request_body(default_values, normalized_data)
    return _snapshot_with_quote_fingerprint(safe_snapshot)


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


def _platform_account_health_summary(row: Optional[QuotePlatformAccountProfile]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "account_type_name": _normalize_account_type_name(_loaded_value(row, "account_type_name")) or "",
        "login_status": _loaded_value(row, "login_status") or ACCOUNT_LOGIN_NOT_LOGGED_IN,
        "quota_status": _loaded_value(row, "quota_status") or ACCOUNT_QUOTA_UNKNOWN,
    }


async def get_quote_platform_account_health(
    db: AsyncSession,
    *,
    owner_user_id: int,
) -> Dict[str, Any]:
    if owner_user_id <= 0:
        return {"ok": False, "items": [], "missing": []}
    items: List[Dict[str, Any]] = []
    for code in PLATFORM_ALIASES.keys():
        if code not in DEVELOPED_QUOTE_PLATFORM_CODES:
            continue
        platform_name = _platform_display_name(code)
        account = await _select_logged_quote_platform_account(
            db,
            owner_user_id=owner_user_id,
            platform_code=code,
            account_type_name=None,
        )
        has_enabled = await _has_enabled_quote_platform_account(
            db,
            owner_user_id=owner_user_id,
            platform_code=code,
            account_type_name=None,
        )
        healthy = account is not None
        if healthy:
            status = "ok"
            message = f"{platform_name}已有已登录可用账号"
        elif has_enabled:
            status = "no_alive_account"
            message = f"{platform_name}暂无存活可用账号，请确认账号已登录、未等待验证码且额度未满"
        else:
            status = "no_enabled_account"
            message = f"{platform_name}暂无可用平台账号，请先新增、启用并登录账号"
        items.append(
            {
                "platform_code": code,
                "platform_name": platform_name,
                "developed": True,
                "ok": healthy,
                "status": status,
                "message": message,
                "has_enabled_account": bool(has_enabled),
                "account": _platform_account_health_summary(account),
            }
        )
    missing = [item for item in items if not item.get("ok")]
    return {
        "ok": not missing,
        "items": items,
        "missing": missing,
    }


async def list_platform_account_types(
    db: AsyncSession,
    *,
    owner_user_id: int,
    platform_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if owner_user_id <= 0:
        return []
    code = _to_str(platform_code).strip().upper()
    platform_codes = [code] if code else [item_code for item_code in PLATFORM_ALIASES.keys()]
    items: List[Dict[str, Any]] = []
    for platform in platform_codes:
        platform_name = _platform_display_name(platform)
        for index, type_name in enumerate(QUOTE_ACCOUNT_TYPE_OPTIONS):
            items.append(
                {
                    "id": None,
                    "platform_code": platform,
                    "platform_name": platform_name,
                    "type_name": type_name,
                    "description": "系统固定账号类型",
                    "match_rules": {"fixed": True},
                    "is_default": index == 0,
                    "enabled": True,
                    "created_at": None,
                    "updated_at": None,
                }
            )
    return items


async def _get_or_create_account_type(
    db: AsyncSession,
    *,
    owner_user_id: int,
    platform_code: str,
    platform_name: str,
    type_name: str,
) -> Optional[QuotePlatformAccountType]:
    name = _ensure_fixed_quote_account_type(type_name, allow_empty=True)
    if not name:
        return None
    row = (
        await db.execute(
            select(QuotePlatformAccountType)
            .where(
                QuotePlatformAccountType.owner_user_id == int(owner_user_id),
                QuotePlatformAccountType.platform_code == platform_code,
                QuotePlatformAccountType.type_name.in_(_account_type_db_names(name)),
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
    type_name = _ensure_fixed_quote_account_type(values.get("type_name"), allow_empty=False)
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


async def _get_platform_account_profile_by_id(
    db: AsyncSession,
    *,
    account_id: int,
) -> Optional[QuotePlatformAccountProfile]:
    if account_id <= 0:
        return None
    return (
        await db.execute(
            select(QuotePlatformAccountProfile)
            .where(QuotePlatformAccountProfile.id == int(account_id))
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
        stmt = stmt.where(QuotePlatformAccountProfile.account_type_name.in_(_account_type_db_names(account_type)))
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
    state_by_account: Dict[int, QuotePlatformAccountSessionState] = {}
    account_ids = [int(row.id) for row in rows if getattr(row, "id", None)]
    account_by_id = {int(row.id): row for row in rows if getattr(row, "id", None)}
    quota_by_account: Dict[int, QuotePlatformAccountQuota] = {}
    active_task_by_account: Dict[int, QuotePlatformAccountLoginTask] = {}
    if account_ids:
        try:
            states = (
                await db.execute(
                    select(QuotePlatformAccountSessionState).where(
                        QuotePlatformAccountSessionState.account_id.in_(account_ids)
                    )
                )
            ).scalars().all()
            state_by_account = {int(item.account_id): item for item in states}
        except Exception:
            state_by_account = {}
        try:
            quota_by_account = await _load_account_quota_map(db, account_ids, accounts_by_id=account_by_id)
        except Exception:
            quota_by_account = {}
        try:
            tasks = (
                await db.execute(
                    select(QuotePlatformAccountLoginTask)
                    .where(
                        QuotePlatformAccountLoginTask.account_id.in_(account_ids),
                        QuotePlatformAccountLoginTask.status.in_([LOGIN_TASK_RUNNING, LOGIN_TASK_NEEDS_CODE]),
                    )
                    .order_by(desc(QuotePlatformAccountLoginTask.id))
                )
            ).scalars().all()
            now = _now()
            for task in tasks:
                account_id = int(task.account_id or 0)
                account = account_by_id.get(account_id)
                if account is not None and not bool(_loaded_value(account, "enabled")):
                    task.status = LOGIN_TASK_EXPIRED
                    task.error_detail = "账号已停用，登录任务已作废"
                    task.finished_at = now
                    task.updated_at = now
                    _clear_account_inspection_notice(account)
                    continue
                if task.expires_at and now > task.expires_at:
                    task.status = LOGIN_TASK_EXPIRED
                    task.error_detail = "登录验证码已过期，请重新点击登录"
                    task.finished_at = now
                    task.updated_at = now
                    if account is not None and _loaded_value(account, "login_status") in {ACCOUNT_LOGIN_LOGGING_IN, ACCOUNT_LOGIN_NEEDS_CODE}:
                        account.login_status = ACCOUNT_LOGIN_EXPIRED
                        account.last_error = task.error_detail
                        account.last_check_at = now
                        account.updated_at = now
                        _set_account_inspection_notice(
                            account,
                            notice_type="login_expired",
                            message=task.error_detail,
                            task_id=task.id,
                            level="warning",
                            payload={"source": "account_list"},
                        )
                    continue
                if account_id and account_id not in active_task_by_account:
                    active_task_by_account[account_id] = task
        except Exception:
            active_task_by_account = {}
    sync_now = _now()
    for account_id, row in account_by_id.items():
        if account_id in active_task_by_account:
            continue
        _sync_account_status_from_session_state(row, state_by_account.get(account_id), now=sync_now)
    return {
        "total": int(total or 0),
        "items": [
            _credential_public_payload(
                row,
                session_state=state_by_account.get(int(row.id)),
                quota=quota_by_account.get(int(row.id)),
                active_login_task=active_task_by_account.get(int(row.id)),
            )
            for row in rows
        ],
    }


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
    quota_limit_provided = "quota_limit" in raw
    quota_period_type_provided = "quota_period_type" in raw
    quota_period_type = _normalize_quota_period_type(raw.get("quota_period_type") or ACCOUNT_QUOTA_PERIOD_DAY)
    account_type_name = _ensure_fixed_quote_account_type(raw.get("account_type_name") or raw.get("account_type"), allow_empty=True)
    return {
        "platform_code": code,
        "platform_name": platform_name,
        "account_type_name": account_type_name,
        "account_username": username,
        "account_password": password or None,
        "login_phone": login_phone,
        "login_phone_mask": login_phone_mask,
        "email": _normalize_email(raw.get("email")),
        "account_owner_user_id": _safe_int(raw.get("account_owner_user_id"), 0) or None,
        "account_owner_name": _to_str(raw.get("account_owner_name")).strip()[:64] or None,
        "auto_login": bool(raw.get("auto_login", True)),
        "enabled": bool(raw.get("enabled", True)),
        "quota_limit_provided": quota_limit_provided,
        "quota_period_type_provided": quota_period_type_provided,
        "quota_limit": _normalize_quota_limit_value(raw.get("quota_limit")) if quota_limit_provided else None,
        "quota_period_type": quota_period_type,
    }


def _enabled_account_sensitive_changes(row: QuotePlatformAccountProfile, incoming: Dict[str, Any]) -> List[str]:
    changes: List[str] = []
    for key, old_value, new_value in (
        ("platform_code", row.platform_code, incoming["platform_code"]),
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
    await _save_account_quota_config(db, account=row, incoming=incoming)
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
    old_aad = _account_profile_aad(row)
    old_password_ciphertext = _to_str(row.password_ciphertext).strip()
    needs_password_rewrap = (
        not incoming.get("account_password")
        and old_password_ciphertext
        and (
            _to_str(row.platform_code).strip().upper() != incoming["platform_code"]
            or _to_str(row.account_username).strip() != incoming["account_username"]
        )
    )
    password_for_rewrap: Optional[str] = None
    if needs_password_rewrap:
        try:
            password_for_rewrap = decrypt_text(old_password_ciphertext, aad=old_aad) or ""
        except Exception:
            raise ValueError("账号平台或账号名发生变化时，需要重新输入密码以重建安全密文")
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
    elif password_for_rewrap:
        row.password_ciphertext = encrypt_text(password_for_rewrap, aad=_account_profile_aad(row))
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
    session_reset_changes = set(sensitive_changes).intersection({"platform_code", "account_username", "account_password", "login_phone"})
    if session_reset_changes or not row.enabled:
        row.login_status = ACCOUNT_LOGIN_DISABLED if not row.enabled else ACCOUNT_LOGIN_NOT_LOGGED_IN
        row.last_error = None
        await _expire_account_active_login_tasks(
            db,
            row,
            reason="账号信息已变更，原登录任务已作废" if row.enabled else "账号已停用，原登录任务已作废",
        )
        _clear_account_inspection_notice(row)
        await quote_platform_session_manager.clear(
            db,
            row,
            status=SESSION_STATUS_DISABLED if not row.enabled else SESSION_STATUS_OFFLINE,
        )
    await _save_account_quota_config(db, account=row, incoming=incoming)
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
    account_password = ""
    credential_error = ""
    password_ciphertext = _to_str(account.password_ciphertext).strip()
    if password_ciphertext:
        try:
            account_password = decrypt_text(password_ciphertext, aad=_account_profile_aad(account)) or ""
        except Exception as exc:
            credential_error = f"账号密码密文解密失败：{str(exc) or exc.__class__.__name__}"
    profile_dir = account_profile_dir(
        storage_root=getattr(settings, "STORAGE_ROOT", "./storage"),
        platform_code=_to_str(account.platform_code).strip().upper() or "STUB",
        account_id=int(account.id or 0),
    )
    login_phone = _to_str(account.login_phone).strip()
    login_phone_mask = account.login_phone_mask or (_mask_phone(login_phone) if login_phone else "")
    return PlatformAccountContext(
        platform_code=_to_str(account.platform_code).strip().upper() or "STUB",
        platform_name=_to_str(account.platform_name).strip() or _to_str(account.platform_code).strip().upper() or "STUB",
        account_id=int(account.id or 0),
        account_username=_to_str(account.account_username).strip(),
        owner_user_id=int(account.owner_user_id or 0),
        account_password=account_password,
        account_type_name=_normalize_account_type_name(account.account_type_name),
        browser_env_key=_to_str(account.browser_env_key).strip(),
        profile_dir=profile_dir,
        payload={
            "login_phone": login_phone,
            "login_phone_mask": login_phone_mask,
            "email": _to_str(account.email).strip(),
            "account_owner_name": _to_str(account.account_owner_name).strip(),
            "browser_profile_path": str(profile_dir),
            "credential_error": credential_error,
            "credential_payload": _json_obj(account.credential_payload),
        },
    )


def _platform_account_quote_context(
    account: QuotePlatformAccountProfile,
    *,
    account_type_name: Optional[str] = None,
) -> PlatformAccountContext:
    ctx = _platform_account_context(account)
    selected_type = _normalize_account_type_name(account_type_name)
    if not selected_type:
        return ctx
    profile_type = ctx.account_type_name
    ctx.account_type_name = selected_type
    ctx.payload = {
        **_json_obj(ctx.payload),
        "profile_account_type_name": profile_type,
        "quote_account_type_name": selected_type,
    }
    return ctx


def _runtime_result_payload(result: Optional[PlatformRuntimeResult]) -> Dict[str, Any]:
    if result is None:
        return {}
    blocked = {
        "cookies",
        "cookie",
        "authorization",
        "user_token",
        "USER_TOKEN",
        "jsession_id",
        "JSESSIONID",
        "jwt",
        "session_snapshot",
        "browser_runtime",
        "browser_cdp_url",
        "browser_profile_path",
        "login_artifact_path",
        "account_password",
        "password",
    }

    def sensitive_key(key: Any) -> bool:
        text = _to_str(key).strip()
        low = text.lower()
        return (
            text in blocked
            or "token" in low
            or "cookie" in low
            or "authorization" in low
            or "session_snapshot" in low
            or "jsession" in low
            or low == "jwt"
            or "browser_cdp" in low
            or "password" in low
            or "secret" in low
            or "密码" in text
            or "口令" in text
        )

    def scrub(value: Any, *, depth: int = 0, inside_failed_runtime: bool = False) -> Any:
        if depth > 8:
            return None
        if isinstance(value, dict):
            safe: Dict[str, Any] = {}
            runtime_status = _to_str(value.get("status")).strip().lower()
            failed_runtime = inside_failed_runtime or runtime_status not in {
                "",
                *RUNTIME_QUOTE_SUCCESS_STATUSES,
            }
            for key, item in value.items():
                if sensitive_key(key):
                    continue
                # A failed runtime may retain diagnostics from an adapter. Do
                # not let a nested candidate quote escape through that
                # diagnostic payload and become renderable by a future caller.
                if failed_runtime and _to_str(key).strip().lower() in {
                    "quote_result",
                    "quoteresult",
                }:
                    continue
                safe[str(key)] = scrub(
                    item,
                    depth=depth + 1,
                    inside_failed_runtime=failed_runtime,
                )
            return safe
        if isinstance(value, list):
            return [
                scrub(item, depth=depth + 1, inside_failed_runtime=inside_failed_runtime)
                for item in value
            ]
        return value

    data = _json_obj(result.data)
    safe_data = scrub(data, inside_failed_runtime=_runtime_status(result) not in {
        "",
        *RUNTIME_QUOTE_SUCCESS_STATUSES,
    })
    return {
        "status": result.status,
        "message": sanitize_quote_user_message(result.message),
        "data": safe_data,
        "challenge_type": result.challenge_type,
        "challenge_prompt": sanitize_quote_user_message(result.challenge_prompt),
    }


def _runtime_status(result: Optional[PlatformRuntimeResult]) -> str:
    if result is None:
        return ""
    return _to_str(result.status).strip().lower()


def _login_challenge_ttl_seconds(platform_code: Any, challenge_type: Any = "") -> int:
    code = _to_str(platform_code).strip().upper()
    challenge = _to_str(challenge_type).strip().lower()
    if code == "PICC" and (not challenge or "security" in challenge):
        return PICC_SECURITY_CODE_TTL_SECONDS
    return QUOTE_SMS_CODE_TTL_SECONDS


def _runtime_detail(result: Optional[PlatformRuntimeResult], default_message: str) -> str:
    status = _runtime_status(result)
    platform_message = _runtime_platform_dialog_message(result, "")
    if platform_message:
        return sanitize_quote_user_message(platform_message, default_message)
    message = _to_str(getattr(result, "message", "") if result is not None else "").strip()
    if message:
        return sanitize_quote_user_message(message, default_message)
    if status:
        return f"{default_message}：{RUNTIME_STATUS_USER_LABELS.get(status, '平台返回异常')}"
    return f"{default_message}：平台未返回状态"


def _is_runtime_login_success(status: str) -> bool:
    return status in RUNTIME_LOGIN_SUCCESS_STATUSES


def _is_runtime_challenge(status: str) -> bool:
    return status in RUNTIME_LOGIN_CHALLENGE_STATUSES


def _is_runtime_quote_success(status: str) -> bool:
    return status in RUNTIME_QUOTE_SUCCESS_STATUSES


def _quote_result_real_data_error(result: Any) -> str:
    """Keep the historical private name while sharing one validation rule."""
    return quote_result_real_data_error(result)


def _quote_runtime_result_or_failure(result: Optional[PlatformRuntimeResult]) -> PlatformRuntimeResult:
    """Downgrade a transport-success response that contains no real quote."""
    if result is None or not _is_runtime_quote_success(_runtime_status(result)):
        return result or PlatformRuntimeResult(status="failed", message="平台未返回报价结果")

    payload = _json_obj(getattr(result, "data", None))
    candidate = _json_obj(payload.get("quote_result"))
    validation_error = _quote_result_real_data_error(candidate)
    if not validation_error:
        return result

    # Do not expose an untrusted candidate as a quote result in the failure
    # payload. Keep only the validation reason and the surrounding diagnostics.
    safe_payload = dict(payload)
    safe_payload.pop("quote_result", None)
    safe_payload["quote_result_validation_error"] = validation_error
    safe_payload["original_runtime_status"] = _runtime_status(result)
    return PlatformRuntimeResult(
        status="failed",
        message=validation_error,
        data=safe_payload,
        challenge_type=result.challenge_type,
        challenge_prompt=result.challenge_prompt,
    )


def _is_runtime_quota_full(status: str) -> bool:
    return status in RUNTIME_QUOTA_FULL_STATUSES


def _runtime_business_status(result: Optional[PlatformRuntimeResult]) -> str:
    if result is None:
        return ""
    return _to_str(_json_obj(result.data).get("business_status")).strip().lower()


def _is_runtime_quota_full_result(result: Optional[PlatformRuntimeResult]) -> bool:
    if result is None:
        return False
    status = _runtime_status(result)
    business_status = _runtime_business_status(result)
    if _is_runtime_quota_full(status) or _is_runtime_quota_full(business_status):
        return True
    data = _json_obj(result.data)
    for key in ("quota_status", "limit_status", "error_code", "code", "reason"):
        if _is_runtime_quota_full(_to_str(data.get(key)).strip().lower()):
            return True
    raw = " ".join(
        _to_str(x)
        for x in (
            getattr(result, "message", ""),
            data.get("message"),
            data.get("platform_status_text"),
            data.get("error_message"),
        )
        if _to_str(x).strip()
    )
    return bool(
        re.search(r"(?:额度|次数|查询).{0,12}(?:用完|已满|满了|不足|超限|限制)", raw)
        or re.search(r"(?:quota|limit).{0,24}(?:full|exceed|exhaust|insufficient)", raw, flags=re.IGNORECASE)
    )


def _is_runtime_duplicate_quote_result(result: Optional[PlatformRuntimeResult]) -> bool:
    if result is None:
        return False
    status = _runtime_status(result)
    business_status = _runtime_business_status(result)
    if status in {"duplicate_quote", "duplicate_quote_confirm_required"} or business_status in {
        "duplicate_quote",
        "duplicate_quote_confirm_required",
    }:
        return True
    data = _json_obj(result.data)
    for key in ("error_code", "code", "reason", "business_status"):
        if _to_str(data.get(key)).strip().lower() in {"duplicate_quote", "duplicate_quote_confirm_required"}:
            return True
    platform_dialog = _json_obj(data.get("platform_dialog"))
    if _to_str(platform_dialog.get("subtype")).strip().lower() == "insurance_date_adjust":
        return False
    raw = " ".join(
        _to_str(x)
        for x in (
            getattr(result, "message", ""),
            data.get("message"),
            data.get("platform_status_text"),
            data.get("error_message"),
        )
        if _to_str(x).strip()
    )
    compact = re.sub(r"\s+", "", raw)
    if (
        "重复投保" in compact
        and ("保险期间" in compact or "起保" in compact or "起期" in compact)
        and re.search(r"(?:调整为|调整至|改为|改至|变更为|同步至|建议).{0,32}\d{4}", compact)
    ):
        return False
    return bool(re.search(r"(?:重复|已报价|已经报价|不能重复).{0,12}(?:报价|投保)?", raw))


def _duplicate_quote_warning_from_runtime(result: Optional[PlatformRuntimeResult]) -> str:
    runtime_payload = _runtime_result_payload(result)
    data = _json_obj(runtime_payload.get("data"))
    duplicate = _json_obj(data.get("duplicateVin"))
    warning = _to_str(
        data.get("duplicate_quote_warning")
        or duplicate.get("warning")
        or duplicate.get("message")
        or getattr(result, "message", "")
    ).strip()
    return _sanitize_duplicate_quote_warning(warning, "平台提示该车辆可能重复投保，请核实后再继续报价。")


# Chat polarity whitelist only. Never fuzzy-match or guess user intent.
# Platform API confirm/modify prompts stay auto-handled elsewhere to reduce user ops.
QUOTE_CHAT_POLARITY_AFFIRM = "affirm"
QUOTE_CHAT_POLARITY_NEGATE = "negate"

QUOTE_CHAT_AFFIRM_EXACT = frozenset(
    {
        "好",
        "好的",
        "行",
        "可以",
        "要的",
        "没问题",
        "确认",
        "同意",
        "是",
        "是的",
        "对",
        "继续",
        "继续报",
        "继续报价",
        "继续投保",
        "确认继续",
        "确认继续报价",
        "确认继续投保",
        "确认报价",
        "确认投保",
    }
)
QUOTE_CHAT_NEGATE_EXACT = frozenset(
    {
        "不",
        "否",
        "不要",
        "不用",
        "不需要",
        "不要了",
        "不用了",
        "算了",
        "取消",
        "放弃",
        "停止",
        "中止",
        "不继续",
        "不要继续",
        "不用继续",
        "不报了",
        "不报价",
        "取消报价",
        "放弃报价",
        "停止报价",
        "中止报价",
        "取消重复报价",
        "放弃重复报价",
        "中止重复报价",
        "取消重复投保",
        "放弃重复投保",
        "中止重复投保",
    }
)
# Object-scoped negate ("不要车损"); keep tight and shared across parsers.
QUOTE_CHAT_NEGATE_OBJECT_WORDS = (
    "去掉",
    "不要",
    "取消",
    "不买",
    "不投",
    "不保",
    "去除",
    "删除",
    "关闭",
    "不需要",
    "不用",
    "非",
)
QUOTE_DUPLICATE_CONFIRM_HINT = (
    "没识别到明确指令。继续请直接发「继续报价」；取消请发「取消」。"
)


def _quote_chat_compact(text: Any) -> str:
    return re.sub(r"\s+", "", _norm_text(text))


def _quote_chat_polarity_exact(text: Any) -> Optional[str]:
    """Return affirm/negate only for exact whitelist utterances; never guess."""
    compact = _quote_chat_compact(text)
    if not compact:
        return None
    if compact in QUOTE_CHAT_NEGATE_EXACT:
        return QUOTE_CHAT_POLARITY_NEGATE
    if compact in QUOTE_CHAT_AFFIRM_EXACT:
        return QUOTE_CHAT_POLARITY_AFFIRM
    if re.fullmatch(r"(确认)?继续(报价|投保)?", compact):
        return QUOTE_CHAT_POLARITY_AFFIRM
    if re.fullmatch(r"确认(报价|投保|继续报价|继续投保)", compact):
        return QUOTE_CHAT_POLARITY_AFFIRM
    if re.fullmatch(r"(不|不用|不要)?(继续|确认)?(中止|取消|放弃|停止)(重复)?(报价|投保)?", compact):
        return QUOTE_CHAT_POLARITY_NEGATE
    return None


def _looks_like_unclear_chat_polarity_attempt(text: Any) -> bool:
    """Short utterances that seem yes/no-ish but are not whitelist → ask again."""
    compact = _quote_chat_compact(text)
    if not compact or len(compact) > 16:
        return False
    if re.search(r"\d{4,}", compact):
        return False
    if _quote_chat_polarity_exact(compact) is not None:
        return False
    if compact in {"看着办", "随便", "这样吧", "先这样", "先这样吧", "再说", "都行", "你定", "随便吧"}:
        return True
    return bool(
        re.search(
            r"好|行|可|要|确认|继续|同意|取消|放弃|不要|不用|算了|停止|中止|否|嗯|ok|yes|no",
            compact,
            flags=re.IGNORECASE,
        )
    )


def _duplicate_quote_unclear_command_response(
    *,
    case: QuoteCase,
    task: QuoteTask,
    trace_id: str = "",
) -> Tuple[str, Dict[str, Any]]:
    return _build_quote_user_failure_response(
        reply=QUOTE_DUPLICATE_CONFIRM_HINT,
        case=case,
        task=task,
        trace_id=trace_id or task.trace_id or _new_trace_id(),
        failure_code=FAILURE_CODE_DUPLICATE_QUOTE,
        failure_reason="重复投保确认指令不明确",
        next_action="继续请发「继续报价」；取消请发「取消」",
        result_status=RESULT_NEED_MORE,
        response_status="success",
        actions=[
            _mk_action("继续报价"),
            _mk_action("取消"),
        ],
        payload={
            "duplicate_quote_confirm_hint": True,
            "quote_case": {
                "id": case.id,
                "case_no": case.case_no,
                "status": case.status,
                "order_id": case.order_id,
            },
            "quote_task": {
                "id": task.id,
                "status": task.status,
                "trace_id": task.trace_id,
            },
        },
    )


def _is_duplicate_quote_confirmation_text(text: Any) -> bool:
    return _quote_chat_polarity_exact(text) == QUOTE_CHAT_POLARITY_AFFIRM


def _is_duplicate_quote_cancel_text(text: Any) -> bool:
    return _quote_chat_polarity_exact(text) == QUOTE_CHAT_POLARITY_NEGATE


def looks_like_duplicate_quote_confirmation(text: Any) -> bool:
    return _is_duplicate_quote_confirmation_text(text)


def looks_like_duplicate_quote_cancel(text: Any) -> bool:
    return _is_duplicate_quote_cancel_text(text)


def _duplicate_quote_confirmed_snapshot(snapshot: Dict[str, Any], *, next_day: Any = "") -> Dict[str, Any]:
    safe_snapshot = dict(_json_obj(snapshot))
    safe_snapshot["confirm_duplicate_quote"] = True
    safe_snapshot["duplicate_quote_confirmed"] = True
    request_body = dict(_json_obj(safe_snapshot.get("request_body")))
    preflight = dict(_json_obj(request_body.get("preflight")))
    preflight["confirmDuplicateQuote"] = True
    preflight["duplicateQuoteConfirmed"] = True
    request_body["preflight"] = preflight
    safe_snapshot["request_body"] = request_body
    adjustments = _duplicate_quote_next_day_adjustments(request_body, next_day=next_day)
    if adjustments:
        safe_snapshot = _quote_snapshot_with_auto_adjusted_dates(
            safe_snapshot,
            adjustments,
            adjusted_request_body=request_body,
        )
    return _snapshot_with_quote_fingerprint(safe_snapshot)


def _snapshot_has_duplicate_quote_confirmation(snapshot: Dict[str, Any]) -> bool:
    safe_snapshot = _json_obj(snapshot)
    request_body = _json_obj(safe_snapshot.get("request_body"))
    preflight = _json_obj(request_body.get("preflight"))
    candidates = (
        safe_snapshot.get("confirm_duplicate_quote"),
        safe_snapshot.get("confirmDuplicateQuote"),
        safe_snapshot.get("duplicate_quote_confirmed"),
        safe_snapshot.get("duplicateQuoteConfirmed"),
        safe_snapshot.get("allow_duplicate_quote"),
        safe_snapshot.get("allowDuplicateQuote"),
        preflight.get("confirmDuplicateQuote"),
        preflight.get("duplicateQuoteConfirmed"),
        preflight.get("allowDuplicateQuote"),
    )
    for value in candidates:
        if value is True:
            return True
        text = _to_str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "是", "确认", "继续", "继续报价", "确认报价"}:
            return True
    return False


def _is_runtime_session_expired_result(result: Optional[PlatformRuntimeResult]) -> bool:
    status = _runtime_status(result)
    business_status = _runtime_business_status(result)
    return status in RUNTIME_SESSION_EXPIRED_STATUSES or business_status == "16"


def _quote_platform_dialog_response(
    *,
    case: QuoteCase,
    task: QuoteTask,
    platform_account: Optional[QuotePlatformAccountProfile],
    runtime_result: Optional[PlatformRuntimeResult],
    platform_code: str,
    platform_name: str,
    trace_id: str,
    error_detail: str,
    result_status: str = RESULT_FAILED,
    response_status: str = "failed",
    title: str = "",
    subtype: str = "",
    severity: str = "",
    actions: Optional[List[Dict[str, Any]]] = None,
    extra_payload: Optional[Dict[str, Any]] = None,
    operator_role_name: Any = "",
) -> Tuple[str, Dict[str, Any]]:
    if not subtype:
        if _is_runtime_session_expired_result(runtime_result):
            subtype = "session_expired"
        elif _is_runtime_quota_full_result(runtime_result):
            subtype = "quota_full"
        else:
            subtype = "quote_business_error"
    if not title:
        if subtype == "session_expired":
            title = f"{platform_name or '平台'}登录已过期"
        elif subtype == "quota_full":
            title = "查询额度已用完"
        else:
            title = f"{platform_name or '平台'}报价提示"
    if not severity:
        severity = "error" if result_status == RESULT_FAILED else "warning"
    runtime_data = _json_obj(getattr(runtime_result, "data", None) if runtime_result is not None else None)
    source_dialog = _json_obj(runtime_data.get("platform_dialog"))
    source_confirm_action = _json_obj(source_dialog.get("confirm_action"))
    source_cancel_action = _json_obj(source_dialog.get("cancel_action"))
    source_confirm_required = (
        source_dialog.get("confirm_required") is True
        or _to_str(source_dialog.get("type")).strip().lower() == "confirm"
    )
    message = _runtime_platform_dialog_message(
        runtime_result,
        error_detail or "平台返回报价提示，请核实后再继续。",
        platform_code=platform_code,
        platform_name=platform_name,
    )
    renewal_lookup_failure_text = _extract_renewal_lookup_failure_text(runtime_result)
    if renewal_lookup_failure_text:
        error_detail = renewal_lookup_failure_text
        message = renewal_lookup_failure_text
        source_confirm_required = False
    if _quote_account_needs_admin_contact(operator_role_name) and subtype in {"session_expired", "quota_full"}:
        if subtype == "session_expired":
            message = f"{platform_name or '平台'}账号登录已过期，请联系管理员处理。"
        else:
            message = f"{platform_name or '平台'}平台账号查询额度已用完，请联系管理员处理。"
        actions = []
    dialog = _make_platform_dialog(
        message=message,
        title=_to_str(source_dialog.get("title")).strip() or title,
        subtype=_to_str(source_dialog.get("subtype")).strip() or subtype,
        severity=_to_str(source_dialog.get("severity")).strip() or severity,
        confirm_required=source_confirm_required,
        trace_id=trace_id,
        task_id=getattr(task, "id", None),
        case_id=getattr(case, "id", None),
        confirm_text=_to_str(source_dialog.get("confirm_text")).strip(),
        cancel_text=_to_str(source_dialog.get("cancel_text")).strip(),
        close_text=_to_str(source_dialog.get("close_text")).strip() or "关闭",
        confirm_command=_to_str(source_confirm_action.get("command") or source_dialog.get("confirm_command")).strip(),
        cancel_command=_to_str(source_cancel_action.get("command") or source_dialog.get("cancel_command")).strip(),
        platform_code=platform_code,
        platform_name=platform_name,
    )
    # The assistant no longer waits on frontend platform-dialog popups. Any
    # platform prompt that reaches this response path must be visible as chat
    # text, otherwise quote-command failures look like a silent no-op.
    visible_to_chat = True
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
        "platform_dialog": dialog,
        "quote_runtime": _runtime_result_payload(runtime_result),
        "ui_visible": visible_to_chat,
    }
    payload.update(_json_obj(extra_payload))
    data = _mk_data(
        result_status=result_status,
        message=message,
        entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
        payload=payload,
    )
    data["silent"] = not visible_to_chat
    data["ui_visible"] = visible_to_chat
    if visible_to_chat:
        _attach_quote_failure(
            data,
            code=_failure_code_for_platform_dialog_subtype(subtype),
            reason=error_detail or message,
            next_action=_quote_failure_next_action(_failure_code_for_platform_dialog_subtype(subtype)),
        )
    return (
        message if visible_to_chat else "",
        {
            "status": response_status,
            "intent": "quote",
            "trace_id": trace_id,
            "silent": not visible_to_chat,
            "ui_visible": visible_to_chat,
            "data": data,
            "actions": actions or [],
        },
    )


def _quote_platform_text_notice_response(
    *,
    case: QuoteCase,
    task: QuoteTask,
    runtime_result: Optional[PlatformRuntimeResult],
    platform_code: str,
    platform_name: str,
    trace_id: str,
    message: str,
    result_status: str = RESULT_NOT_READY,
    response_status: str = "success",
    notice_type: str = "platform_notice",
    extra_payload: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Return a platform prompt as normal chat text without creating a modal state."""
    safe_message = sanitize_quote_user_message(
        message,
        f"{platform_name or '平台'}返回报价提示，请核实后重试。",
        platform_code=platform_code,
        platform_name=platform_name,
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
            "error_detail": safe_message,
        },
        "quote_runtime": _runtime_result_payload(runtime_result),
        "platform_notice": {
            "type": notice_type,
            "message": safe_message,
            "platform_code": platform_code,
            "platform_name": platform_name,
        },
        "ui_visible": True,
    }
    payload.update(_json_obj(extra_payload))
    data = _mk_data(
        result_status=result_status,
        message=safe_message,
        entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
        payload=payload,
    )
    data["silent"] = False
    data["ui_visible"] = True
    notice_failure_code = (
        FAILURE_CODE_DUPLICATE_QUOTE
        if _to_str(notice_type).strip().lower() in {"duplicate_quote_notice", "duplicate_quote"}
        else FAILURE_CODE_PLATFORM
    )
    _attach_quote_failure(
        data,
        code=notice_failure_code,
        reason=safe_message,
    )
    return (
        safe_message,
        {
            "status": response_status,
            "intent": "quote",
            "trace_id": trace_id,
            "silent": False,
            "ui_visible": True,
            "data": data,
            "actions": [],
        },
    )


def _apply_platform_account_runtime_status(
    account: Optional[QuotePlatformAccountProfile],
    result: Optional[PlatformRuntimeResult],
    *,
    default_error: str,
) -> None:
    if account is None:
        return
    status = _runtime_status(result)
    detail = _runtime_detail(result, default_error)
    if _is_runtime_session_expired_result(result):
        account.login_status = ACCOUNT_LOGIN_EXPIRED
        account.last_error = detail or "平台登录已过期，请重新登录"
        account.last_check_at = _now()
        account.updated_at = _now()
        _set_account_inspection_notice(
            account,
            notice_type="session_expired",
            message=account.last_error,
            level="warning",
            payload={"source": "quote_runtime", "runtime_status": status, "business_status": _runtime_business_status(result)},
        )
        return
    if status in RUNTIME_SESSION_DEGRADED_STATUSES:
        account.login_status = ACCOUNT_LOGIN_DEGRADED
        account.last_error = detail
        account.last_check_at = _now()
        account.updated_at = _now()
        _set_account_inspection_notice(
            account,
            notice_type="runtime_degraded",
            message=account.last_error,
            level="warning",
            payload={"source": "quote_runtime", "runtime_status": status},
        )


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
        account_type_name=_normalize_account_type_name(safe.get("account_type_name")),
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
        "challenge_prompt": sanitize_quote_user_message(_loaded_value(row, "challenge_prompt")),
        "challenge_payload": _json_obj(_loaded_value(row, "challenge_payload")),
        "trace_id": _loaded_value(row, "trace_id"),
        "error_detail": sanitize_quote_user_message(_loaded_value(row, "error_detail")),
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
    active_task = (
        await db.execute(
            select(QuotePlatformAccountLoginTask)
            .where(
                QuotePlatformAccountLoginTask.account_id == int(account.id),
                QuotePlatformAccountLoginTask.owner_user_id == int(owner_user_id),
                QuotePlatformAccountLoginTask.status.in_([LOGIN_TASK_RUNNING, LOGIN_TASK_NEEDS_CODE]),
            )
            .order_by(desc(QuotePlatformAccountLoginTask.id))
            .limit(1)
        )
    ).scalars().first()
    if active_task:
        if active_task.expires_at and _now() > active_task.expires_at:
            active_task.status = LOGIN_TASK_EXPIRED
            active_task.error_detail = "登录任务已过期，请重新点击登录"
            active_task.finished_at = _now()
            active_task.updated_at = _now()
            account.login_status = ACCOUNT_LOGIN_EXPIRED
            account.last_error = active_task.error_detail
            account.last_check_at = _now()
            account.updated_at = _now()
            await db.flush()
        else:
            return {"account": _credential_public_payload(account), "login_task": _login_task_payload(active_task)}
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
        expires_at=now + timedelta(seconds=_login_challenge_ttl_seconds(account.platform_code)),
    )
    db.add(task)
    account.login_status = ACCOUNT_LOGIN_LOGGING_IN
    account.last_error = None
    account.last_check_at = now
    account.updated_at = now
    await db.flush()

    runtime_result = await quote_platform_runtime.login(_platform_account_context(account), db=db)
    status = _runtime_status(runtime_result)
    if _is_runtime_challenge(status):
        task.status = LOGIN_TASK_NEEDS_CODE
        task.challenge_type = runtime_result.challenge_type or "sms"
        phone_mask = account.login_phone_mask or _mask_phone(account.login_phone)
        prompt = sanitize_quote_user_message(
            runtime_result.challenge_prompt or f"请输入{account.platform_name or account.platform_code}的验证码",
            platform_code=account.platform_code,
            platform_name=account.platform_name,
        )
        if phone_mask and phone_mask not in prompt:
            prompt = f"{prompt}（发送至 {phone_mask}）"
        task.challenge_prompt = prompt
        challenge_payload = _json_obj(_json_obj(runtime_result.data).get("challenge_payload"))
        code_length = _safe_int(challenge_payload.get("code_length"), 0)
        task.challenge_payload = {
            "phone_mask": phone_mask or "",
            "code_length": code_length or "4-8",
            "platform_runtime": _runtime_result_payload(runtime_result),
        }
        task.expires_at = _now() + timedelta(
            seconds=_login_challenge_ttl_seconds(account.platform_code, task.challenge_type)
        )
        account.login_status = ACCOUNT_LOGIN_NEEDS_CODE
        _set_account_inspection_notice(
            account,
            notice_type="login_challenge",
            message=prompt,
            task_id=task.id,
            payload={"source": "login_flow", "challenge_type": task.challenge_type},
        )
    elif _is_runtime_login_success(status):
        task.status = LOGIN_TASK_SUCCESS
        task.finished_at = _now()
        account.login_status = ACCOUNT_LOGIN_AUTHENTICATED
        account.last_login_at = task.finished_at
        account.quota_status = ACCOUNT_QUOTA_AVAILABLE if account.quota_status == ACCOUNT_QUOTA_UNKNOWN else account.quota_status
        _clear_account_inspection_notice(account)
    elif status in RUNTIME_SESSION_DEGRADED_STATUSES and bool(_json_obj(runtime_result.data).get("preserved_previous_session")):
        task.status = LOGIN_TASK_FAILED
        task.error_detail = _runtime_detail(runtime_result, "平台登录未完成")
        task.finished_at = _now()
        account.login_status = ACCOUNT_LOGIN_DEGRADED
        account.last_error = task.error_detail
        _set_account_inspection_notice(
            account,
            notice_type="login_preserved_session",
            message=task.error_detail or "新登录失败，已保留原有可用会话",
            task_id=task.id,
            level="warning",
            payload={"source": "login_flow", "preserved_previous_session": True},
        )
    else:
        task.status = LOGIN_TASK_FAILED
        task.error_detail = _runtime_detail(runtime_result, "平台登录失败")
        task.finished_at = _now()
        account.login_status = ACCOUNT_LOGIN_FAILED
        account.last_error = task.error_detail
        _set_account_inspection_notice(
            account,
            notice_type="login_failed",
            message=task.error_detail,
            task_id=task.id,
            level="danger",
            payload={"source": "login_flow"},
        )
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

    runtime_result = await quote_platform_runtime.submit_challenge(_platform_account_context(account), clean_code, db=db)
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
        _clear_account_inspection_notice(account)
    elif _is_runtime_challenge(status):
        task.status = LOGIN_TASK_NEEDS_CODE
        task.challenge_type = runtime_result.challenge_type or task.challenge_type or "sms"
        task.challenge_prompt = sanitize_quote_user_message(
            runtime_result.challenge_prompt or runtime_result.message or task.challenge_prompt,
            "账号登录仍需要安全码，请继续输入验证码。",
            platform_code=account.platform_code,
            platform_name=account.platform_name,
        )
        challenge_payload = _json_obj(_json_obj(runtime_result.data).get("challenge_payload"))
        code_length = _safe_int(challenge_payload.get("code_length"), 0)
        task.challenge_payload = {
            **_json_obj(task.challenge_payload),
            "code_length": code_length or _json_obj(task.challenge_payload).get("code_length") or "4-8",
            "platform_runtime": _runtime_result_payload(runtime_result),
        }
        task.expires_at = _now() + timedelta(
            seconds=_login_challenge_ttl_seconds(account.platform_code, task.challenge_type)
        )
        task.error_detail = task.challenge_prompt
        account.login_status = ACCOUNT_LOGIN_NEEDS_CODE
        account.last_error = task.error_detail
        _set_account_inspection_notice(
            account,
            notice_type="login_challenge",
            message=task.challenge_prompt or "账号登录仍需要安全码，请继续输入验证码。",
            task_id=task.id,
            payload={"source": "challenge_flow", "challenge_type": task.challenge_type},
        )
    elif status in RUNTIME_SESSION_DEGRADED_STATUSES and bool(_json_obj(runtime_result.data).get("preserved_previous_session")):
        task.status = LOGIN_TASK_FAILED
        task.error_detail = _runtime_detail(runtime_result, "验证码校验未完成")
        account.login_status = ACCOUNT_LOGIN_DEGRADED
        account.last_error = task.error_detail
        _set_account_inspection_notice(
            account,
            notice_type="login_preserved_session",
            message=task.error_detail or "验证码校验未完成，已恢复原有可用会话",
            task_id=task.id,
            level="warning",
            payload={"source": "challenge_flow", "preserved_previous_session": True},
        )
    elif status in RUNTIME_SESSION_EXPIRED_STATUSES:
        task.status = LOGIN_TASK_EXPIRED
        task.error_detail = _runtime_detail(runtime_result, "验证码校验失败")
        account.login_status = ACCOUNT_LOGIN_EXPIRED
        account.last_error = task.error_detail
        _set_account_inspection_notice(
            account,
            notice_type="login_expired",
            message=task.error_detail,
            task_id=task.id,
            level="warning",
            payload={"source": "challenge_flow"},
        )
    else:
        task.status = LOGIN_TASK_FAILED
        task.error_detail = _runtime_detail(runtime_result, "验证码校验失败")
        account.login_status = ACCOUNT_LOGIN_FAILED
        account.last_error = task.error_detail
        _set_account_inspection_notice(
            account,
            notice_type="login_failed",
            message=task.error_detail,
            task_id=task.id,
            level="danger",
            payload={"source": "challenge_flow"},
        )
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
    exclude_account_ids: Optional[Iterable[int]] = None,
) -> Optional[QuotePlatformAccountProfile]:
    code = _to_str(platform_code).strip().upper()
    if owner_user_id <= 0 or not code:
        return None
    excluded = {int(x) for x in (exclude_account_ids or []) if _safe_int(x, 0)}
    stmt = select(QuotePlatformAccountProfile).where(
        QuotePlatformAccountProfile.platform_code == code,
        QuotePlatformAccountProfile.enabled == True,  # noqa: E712
    )
    type_name = _normalize_account_type_name(account_type_name)
    if excluded:
        stmt = stmt.where(QuotePlatformAccountProfile.id.notin_(excluded))
    rows = (
        await db.execute(
            stmt.order_by(
                (QuotePlatformAccountProfile.login_status == ACCOUNT_LOGIN_AUTHENTICATED).desc(),
                (QuotePlatformAccountProfile.login_status == ACCOUNT_LOGIN_DEGRADED).desc(),
                QuotePlatformAccountProfile.auto_login.desc(),
                (QuotePlatformAccountProfile.login_status == ACCOUNT_LOGIN_LOGGING_IN).asc(),
                (QuotePlatformAccountProfile.login_status == ACCOUNT_LOGIN_NEEDS_CODE).asc(),
                (QuotePlatformAccountProfile.login_status == ACCOUNT_LOGIN_FAILED).asc(),
                QuotePlatformAccountProfile.last_used_at.asc(),
                QuotePlatformAccountProfile.id.asc(),
            )
        )
    ).scalars().all()
    if not rows:
        return None
    if type_name:
        preferred_type_names = set(_account_type_db_names(type_name))

        def account_rank(row: QuotePlatformAccountProfile) -> Tuple[int, int, int, int, str, int]:
            status = _to_str(_loaded_value(row, "login_status")).strip()
            if status == ACCOUNT_LOGIN_AUTHENTICATED:
                login_rank = 0
            elif status == ACCOUNT_LOGIN_DEGRADED:
                login_rank = 1
            else:
                login_rank = 2
            row_type = _normalize_account_type_name(_loaded_value(row, "account_type_name"))
            if row_type in preferred_type_names:
                type_rank = 0
            elif row_type:
                type_rank = 1
            else:
                type_rank = 2
            owner_rank = 0 if _safe_int(_loaded_value(row, "owner_user_id"), 0) == int(owner_user_id) else 1
            auto_rank = 0 if bool(_loaded_value(row, "auto_login")) else 1
            last_used = _to_str(_loaded_value(row, "last_used_at")).strip()
            return (login_rank, type_rank, owner_rank, auto_rank, last_used, int(_loaded_value(row, "id") or 0))

        rows.sort(key=account_rank)
    account_ids = [int(row.id) for row in rows if getattr(row, "id", None)]
    account_by_id = {int(row.id): row for row in rows if getattr(row, "id", None)}
    quota_by_account = await _load_account_quota_map(db, account_ids, accounts_by_id=account_by_id)
    now = _now()
    for row in rows:
        login_status = _to_str(_loaded_value(row, "login_status")).strip()
        if login_status in {ACCOUNT_LOGIN_LOGGING_IN, ACCOUNT_LOGIN_NEEDS_CODE}:
            continue
        if (
            login_status not in {ACCOUNT_LOGIN_AUTHENTICATED, ACCOUNT_LOGIN_DEGRADED}
            and not bool(_loaded_value(row, "auto_login"))
            and _safe_int(_loaded_value(row, "owner_user_id"), 0) != int(owner_user_id)
        ):
            continue
        quota = quota_by_account.get(int(row.id))
        if quota is not None and (_quota_remaining_count(quota) or 0) <= 0:
            row.quota_status = ACCOUNT_QUOTA_FULL
            row.quota_reset_at = quota.period_end_at
            row.updated_at = now
            continue
        if quota is None and row.quota_status == ACCOUNT_QUOTA_FULL:
            continue
        if quota is not None and row.quota_status == ACCOUNT_QUOTA_FULL:
            reset_at = row.quota_reset_at
            if not reset_at or now < reset_at:
                continue
        return row
    return None


async def _select_logged_quote_platform_account(
    db: AsyncSession,
    *,
    owner_user_id: int,
    platform_code: str,
    account_type_name: Optional[str] = None,
    exclude_account_ids: Optional[Iterable[int]] = None,
) -> Optional[QuotePlatformAccountProfile]:
    code = _to_str(platform_code).strip().upper()
    if owner_user_id <= 0 or not code:
        return None
    excluded = {int(x) for x in (exclude_account_ids or []) if _safe_int(x, 0)}
    stmt = select(QuotePlatformAccountProfile).where(
        QuotePlatformAccountProfile.platform_code == code,
        QuotePlatformAccountProfile.enabled == True,  # noqa: E712
        QuotePlatformAccountProfile.login_status.in_([ACCOUNT_LOGIN_AUTHENTICATED, ACCOUNT_LOGIN_DEGRADED]),
    )
    if excluded:
        stmt = stmt.where(QuotePlatformAccountProfile.id.notin_(excluded))
    rows = (
        await db.execute(
            stmt.order_by(
                (QuotePlatformAccountProfile.login_status == ACCOUNT_LOGIN_AUTHENTICATED).desc(),
                QuotePlatformAccountProfile.last_used_at.asc(),
                QuotePlatformAccountProfile.id.asc(),
            )
        )
    ).scalars().all()
    if not rows:
        return None

    type_name = _normalize_account_type_name(account_type_name)
    if type_name:
        preferred_type_names = set(_account_type_db_names(type_name))

        def account_rank(row: QuotePlatformAccountProfile) -> Tuple[int, int, int, str, int]:
            row_type = _normalize_account_type_name(_loaded_value(row, "account_type_name"))
            if row_type in preferred_type_names:
                type_rank = 0
            elif row_type:
                type_rank = 1
            else:
                type_rank = 2
            login_rank = 0 if _loaded_value(row, "login_status") == ACCOUNT_LOGIN_AUTHENTICATED else 1
            owner_rank = 0 if _safe_int(_loaded_value(row, "owner_user_id"), 0) == int(owner_user_id) else 1
            return (login_rank, type_rank, owner_rank, _to_str(_loaded_value(row, "last_used_at")).strip(), int(_loaded_value(row, "id") or 0))

        rows.sort(key=account_rank)

    account_ids = [int(row.id) for row in rows if getattr(row, "id", None)]
    account_by_id = {int(row.id): row for row in rows if getattr(row, "id", None)}
    quota_by_account = await _load_account_quota_map(db, account_ids, accounts_by_id=account_by_id)
    now = _now()
    for row in rows:
        quota = quota_by_account.get(int(row.id))
        if quota is not None and (_quota_remaining_count(quota) or 0) <= 0:
            row.quota_status = ACCOUNT_QUOTA_FULL
            row.quota_reset_at = quota.period_end_at
            row.updated_at = now
            continue
        if quota is None and row.quota_status == ACCOUNT_QUOTA_FULL:
            continue
        if quota is not None and row.quota_status == ACCOUNT_QUOTA_FULL:
            reset_at = row.quota_reset_at
            if not reset_at or now < reset_at:
                continue
        if not await _account_has_usable_session_snapshot(row):
            continue
        return row
    return None


async def _account_has_usable_session_snapshot(account: QuotePlatformAccountProfile) -> bool:
    snapshot = await quote_platform_session_manager.store.load(account)
    status = _to_str(getattr(snapshot, "status", "")).strip().lower() if snapshot is not None else ""
    return status in {SESSION_STATUS_AUTHENTICATED, SESSION_STATUS_DEGRADED}


async def _has_enabled_quote_platform_account(
    db: AsyncSession,
    *,
    owner_user_id: int,
    platform_code: str,
    account_type_name: Optional[str] = None,
) -> bool:
    code = _to_str(platform_code).strip().upper()
    if owner_user_id <= 0 or not code:
        return False
    stmt = select(func.count()).select_from(QuotePlatformAccountProfile).where(
        QuotePlatformAccountProfile.platform_code == code,
        QuotePlatformAccountProfile.enabled == True,  # noqa: E712
    )
    type_name = _normalize_account_type_name(account_type_name)
    if type_name:
        stmt = stmt.where(QuotePlatformAccountProfile.account_type_name.in_(_account_type_db_names(type_name)))
    total = (await db.execute(stmt)).scalar_one()
    return int(total or 0) > 0


def _account_group_key(row: QuotePlatformAccountProfile) -> Tuple[int, str]:
    return (
        int(_loaded_value(row, "owner_user_id") or 0),
        _to_str(_loaded_value(row, "platform_code")).strip().upper(),
    )


def _account_group_label(row: QuotePlatformAccountProfile) -> str:
    platform = _to_str(_loaded_value(row, "platform_name")).strip() or _to_str(_loaded_value(row, "platform_code")).strip()
    return platform


def _runtime_keepalive_ok(status: str) -> bool:
    return _to_str(status).strip().lower() in {"success", "ok", "authenticated", "available", "skipped"}


async def _try_keepalive_account(db: AsyncSession, account: QuotePlatformAccountProfile) -> bool:
    try:
        result = await quote_platform_runtime.keepalive(_platform_account_context(account), db=db)
        status = _runtime_status(result)
        account.last_check_at = _now()
        account.updated_at = _now()
        if _runtime_keepalive_ok(status):
            account.login_status = ACCOUNT_LOGIN_AUTHENTICATED
            account.last_error = None
            _clear_account_inspection_notice(account)
            return True
        if status in {"expired", "session_expired", "not_authenticated", "unauthorized", "status_16"}:
            account.login_status = ACCOUNT_LOGIN_EXPIRED
        else:
            account.login_status = ACCOUNT_LOGIN_DEGRADED
        account.last_error = _runtime_detail(result, "账号保活失败")
        _set_account_inspection_notice(
            account,
            notice_type="keepalive_failed",
            message=account.last_error,
            level="warning",
            payload={"source": "daily_inspection", "runtime_status": status},
        )
        return False
    except Exception as exc:
        account.login_status = ACCOUNT_LOGIN_DEGRADED
        account.last_error = f"账号保活异常：{sanitize_quote_user_message(str(exc) or exc.__class__.__name__, '平台处理异常')}"
        account.last_check_at = _now()
        account.updated_at = _now()
        _set_account_inspection_notice(
            account,
            notice_type="keepalive_failed",
            message=account.last_error,
            level="danger",
            payload={"source": "daily_inspection", "error": exc.__class__.__name__},
        )
        return False


async def _inspect_account_group(
    db: AsyncSession,
    *,
    rows: List[QuotePlatformAccountProfile],
) -> Dict[str, Any]:
    if not rows:
        return {"status": "empty"}
    group_label = _account_group_label(rows[0])
    sorted_rows = sorted(
        rows,
        key=lambda item: (
            0 if _loaded_value(item, "login_status") == ACCOUNT_LOGIN_AUTHENTICATED else 1,
            0 if _loaded_value(item, "login_status") == ACCOUNT_LOGIN_DEGRADED else 1,
            0 if bool(_loaded_value(item, "auto_login")) else 1,
            _loaded_value(item, "last_check_at") or datetime(1970, 1, 1),
            int(_loaded_value(item, "id") or 0),
        ),
    )

    for account in sorted_rows:
        if _loaded_value(account, "login_status") in {ACCOUNT_LOGIN_AUTHENTICATED, ACCOUNT_LOGIN_DEGRADED}:
            if await _try_keepalive_account(db, account):
                await _clear_group_resolved_inspection_attention(db, rows, keep_account_id=int(account.id))
                await _add_account_event(
                    db,
                    account=account,
                    event_type="inspection",
                    operator_user_id=None,
                    before={},
                    after=_account_event_snapshot(account),
                    message=f"每日巡检：{group_label} 已登录账号保活成功",
                )
                return {"status": "ok", "account_id": int(account.id), "action": "keepalive"}

    challenge_count = 0
    failed_count = 0
    skipped_manual_count = 0
    for account in sorted_rows:
        if not bool(_loaded_value(account, "auto_login")):
            skipped_manual_count += 1
            _set_account_inspection_notice(
                account,
                notice_type="manual_login_required",
                message=f"每日巡检发现 {group_label} 没有已登录保活账号，该账号未开启自动登录，请管理员手动点击登录。",
                level="warning",
                payload={"source": "daily_inspection"},
            )
            continue
        try:
            result = await start_platform_account_login(
                db,
                owner_user_id=int(account.owner_user_id),
                account_id=int(account.id),
                operator_user_id=None,
            )
            task = _json_obj(result.get("login_task"))
            status = _to_str(task.get("status")).strip()
            if status == LOGIN_TASK_SUCCESS:
                _clear_account_inspection_notice(account)
                await _clear_group_resolved_inspection_attention(db, rows, keep_account_id=int(account.id))
                await _add_account_event(
                    db,
                    account=account,
                    event_type="inspection",
                    operator_user_id=None,
                    before={},
                    after=_account_event_snapshot(account),
                    message=f"每日巡检：{group_label} 自动登录成功",
                )
                return {"status": "ok", "account_id": int(account.id), "action": "auto_login"}
            if status == LOGIN_TASK_NEEDS_CODE:
                challenge_count += 1
                _set_account_inspection_notice(
                    account,
                    notice_type="login_challenge",
                    message=task.get("challenge_prompt") or f"每日巡检发现 {group_label} 登录需要安全码，请点击登录并输入验证码。",
                    task_id=_safe_int(task.get("id"), 0) or None,
                    payload={"source": "daily_inspection", "challenge_type": task.get("challenge_type")},
                )
            else:
                failed_count += 1
        except Exception as exc:
            failed_count += 1
            account.login_status = ACCOUNT_LOGIN_FAILED
            account.last_error = f"每日巡检自动登录异常：{sanitize_quote_user_message(str(exc) or exc.__class__.__name__, '平台处理异常')}"
            account.last_check_at = _now()
            account.updated_at = _now()
            _set_account_inspection_notice(
                account,
                notice_type="login_failed",
                message=account.last_error,
                level="danger",
                payload={"source": "daily_inspection", "error": exc.__class__.__name__},
            )

    for account in sorted_rows:
        if bool(_loaded_value(account, "auto_login")) or _account_inspection_notice_from_payload(account):
            continue
        _set_account_inspection_notice(
            account,
            notice_type="manual_login_required",
            message=f"每日巡检发现 {group_label} 没有已登录保活账号，请管理员手动登录至少一个账号。",
            level="warning",
            payload={"source": "daily_inspection"},
        )

    return {
        "status": "needs_attention",
        "challenge_count": challenge_count,
        "failed_count": failed_count,
        "skipped_manual_count": skipped_manual_count,
    }


async def inspect_quote_platform_accounts_once(
    db: AsyncSession,
    *,
    max_groups: Optional[int] = None,
) -> Dict[str, Any]:
    stmt = (
        select(QuotePlatformAccountProfile)
        .where(QuotePlatformAccountProfile.enabled == True)  # noqa: E712
        .order_by(
            QuotePlatformAccountProfile.owner_user_id.asc(),
            QuotePlatformAccountProfile.platform_code.asc(),
            QuotePlatformAccountProfile.account_type_name.asc(),
            QuotePlatformAccountProfile.id.asc(),
        )
    )
    rows = (await db.execute(stmt)).scalars().all()
    groups: Dict[Tuple[int, str], List[QuotePlatformAccountProfile]] = {}
    for row in rows:
        key = _account_group_key(row)
        if key[0] <= 0 or not key[1]:
            continue
        groups.setdefault(key, []).append(row)

    summary = {
        "groups_total": len(groups),
        "groups_checked": 0,
        "groups_ok": 0,
        "groups_needs_attention": 0,
        "accounts_total": len(rows),
        "details": [],
    }
    limit = max(0, int(max_groups or 0))
    for key, items in groups.items():
        if limit and summary["groups_checked"] >= limit:
            break
        result = await _inspect_account_group(db, rows=items)
        summary["groups_checked"] += 1
        if result.get("status") == "ok":
            summary["groups_ok"] += 1
        else:
            summary["groups_needs_attention"] += 1
        summary["details"].append(
            {
                "owner_user_id": key[0],
                "platform_code": key[1],
                "account_type_names": sorted(
                    {
                        _normalize_account_type_name(_loaded_value(item, "account_type_name")) or "未标记"
                        for item in items
                    }
                ),
                "account_count": len(items),
                **result,
            }
        )
    return summary


def _parse_session_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = _to_str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _platform_keepalive_interval_seconds(platform_code: str) -> int:
    code = _to_str(platform_code).strip().upper()
    if code == "PICC":
        return max(30, int(getattr(settings, "PICC_KEEPALIVE_SECONDS", 300) or 300))
    return max(60, int(os.getenv("QUOTE_PLATFORM_KEEPALIVE_SECONDS", "300") or "300"))


def _snapshot_due_for_keepalive(snapshot: Any, *, now: Optional[datetime] = None) -> bool:
    if snapshot is None:
        return False
    current = now or _now()
    status = _to_str(getattr(snapshot, "status", "")).strip().lower()
    if status not in {SESSION_STATUS_AUTHENTICATED, SESSION_STATUS_DEGRADED}:
        return False

    next_keepalive_at = _parse_session_dt(getattr(snapshot, "next_keepalive_at", None))
    if next_keepalive_at and current >= next_keepalive_at:
        return True

    jwt_expires_at = _safe_int(getattr(getattr(snapshot, "jwt", None), "expires_at", None), 0)
    if jwt_expires_at > 0:
        try:
            jwt_expires_dt = datetime.fromtimestamp(jwt_expires_at, TZ_BJ).replace(tzinfo=None)
            platform_code = _to_str(getattr(snapshot, "platform_code", "")).strip().upper()
            refresh_before = max(60, int(getattr(settings, "PICC_JWT_REFRESH_BEFORE_SECONDS", 25 * 60) or 25 * 60))
            if platform_code == "PICC" and jwt_expires_dt <= current + timedelta(seconds=refresh_before):
                return True
        except Exception:
            return True

    active_times = [
        item
        for item in (
            _parse_session_dt(getattr(snapshot, "last_authenticated_at", None)),
            _parse_session_dt(getattr(snapshot, "last_business_at", None)),
            _parse_session_dt(getattr(snapshot, "last_keepalive_at", None)),
        )
        if item is not None
    ]
    last_active = max(active_times) if active_times else None
    if last_active is None:
        return True
    return current >= last_active + timedelta(seconds=_platform_keepalive_interval_seconds(getattr(snapshot, "platform_code", "")))


async def maintain_quote_platform_sessions_once(
    db: AsyncSession,
    *,
    startup_restore: bool = False,
    max_accounts: Optional[int] = None,
) -> Dict[str, Any]:
    """Restore and keep authenticated quote-platform sessions alive.

    This keeps Dingchang's async actor model while preserving the auto_business
    contract: a stored session is restored and validated on startup, then
    refreshed independently from user quote traffic.
    """
    stmt = (
        select(QuotePlatformAccountProfile)
        .where(
            QuotePlatformAccountProfile.enabled == True,  # noqa: E712
        )
        .order_by(
            QuotePlatformAccountProfile.platform_code.asc(),
            QuotePlatformAccountProfile.account_type_name.asc(),
            QuotePlatformAccountProfile.last_check_at.asc(),
            QuotePlatformAccountProfile.id.asc(),
        )
    )
    limit = max(0, int(max_accounts or 0))
    if limit:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    summary = {
        "accounts_total": len(rows),
        "checked": 0,
        "kept_alive": 0,
        "skipped_not_due": 0,
        "expired": 0,
        "degraded": 0,
        "missing_snapshot": 0,
    }
    now = _now()
    for account in rows:
        snapshot = await quote_platform_session_manager.store.load(account)
        if snapshot is None:
            had_usable_login = bool(
                _loaded_value(account, "last_login_at")
                or _loaded_value(account, "last_used_at")
                or _to_str(_loaded_value(account, "login_status")).strip() in {ACCOUNT_LOGIN_AUTHENTICATED, ACCOUNT_LOGIN_DEGRADED}
            )
            if not had_usable_login:
                summary["skipped_not_due"] += 1
                continue
            summary["missing_snapshot"] += 1
            account.login_status = ACCOUNT_LOGIN_EXPIRED if startup_restore else ACCOUNT_LOGIN_DEGRADED
            account.last_error = "服务启动后未找到可恢复的平台会话，请重新登录" if startup_restore else "未找到可用平台会话，等待重新登录"
            account.last_check_at = now
            account.updated_at = now
            _set_account_inspection_notice(
                account,
                notice_type="session_missing",
                message=account.last_error,
                level="warning",
                payload={"source": "startup_restore" if startup_restore else "keepalive_scheduler"},
            )
            continue

        status = _to_str(getattr(snapshot, "status", "")).strip().lower()
        if status in {SESSION_STATUS_EXPIRED, "session_expired", "not_authenticated", "unauthorized", "status_16"}:
            summary["expired"] += 1
            account.login_status = ACCOUNT_LOGIN_EXPIRED
            account.last_error = "平台会话已过期，请重新登录"
            account.last_check_at = now
            account.updated_at = now
            _set_account_inspection_notice(
                account,
                notice_type="session_expired",
                message=account.last_error,
                level="warning",
                payload={"source": "startup_restore" if startup_restore else "keepalive_scheduler"},
            )
            continue
        if status not in {SESSION_STATUS_AUTHENTICATED, SESSION_STATUS_DEGRADED}:
            summary["skipped_not_due"] += 1
            continue
        if not startup_restore and not _snapshot_due_for_keepalive(snapshot, now=now):
            if _to_str(_loaded_value(account, "login_status")).strip() not in {ACCOUNT_LOGIN_AUTHENTICATED, ACCOUNT_LOGIN_DEGRADED}:
                account.login_status = ACCOUNT_LOGIN_AUTHENTICATED if status == SESSION_STATUS_AUTHENTICATED else ACCOUNT_LOGIN_DEGRADED
                account.last_error = None if status == SESSION_STATUS_AUTHENTICATED else account.last_error
                account.last_check_at = now
                account.updated_at = now
                if status == SESSION_STATUS_AUTHENTICATED:
                    _clear_account_inspection_notice(account)
            summary["skipped_not_due"] += 1
            continue

        summary["checked"] += 1
        if await _try_keepalive_account(db, account):
            summary["kept_alive"] += 1
        else:
            if account.login_status == ACCOUNT_LOGIN_EXPIRED:
                summary["expired"] += 1
            else:
                summary["degraded"] += 1
    await db.flush()
    return summary


async def _mark_platform_account_used(
    db: AsyncSession,
    *,
    account_id: Optional[int],
    owner_user_id: int,
    login_state: str,
    sms_at: bool = False,
    consume_quota: bool = False,
) -> None:
    if not account_id:
        return
    row = await _get_platform_account_profile_by_id(db, account_id=int(account_id))
    if not row:
        return
    row.login_status = login_state or row.login_status
    row.last_used_at = _now()
    if sms_at:
        row.last_check_at = _now()
    if login_state == ACCOUNT_LOGIN_AUTHENTICATED:
        row.last_login_at = _now()
        row.last_error = None
        _clear_account_inspection_notice(row)
        if row.quota_status == ACCOUNT_QUOTA_UNKNOWN:
            row.quota_status = ACCOUNT_QUOTA_AVAILABLE
    if consume_quota:
        await _consume_account_quota_on_success(db, account=row, operator_user_id=owner_user_id)
    row.updated_at = _now()
    await db.flush()


def _extract_account_type_from_quote_text(text: Any, platform_name: str, platform_code: str = "") -> Optional[str]:
    t = _norm_text(text)
    platform = _to_str(platform_name).strip()
    if not t:
        return None
    body = t.replace(" ", "")
    aliases: set[str] = set()
    code = _to_str(platform_code).strip().upper()
    if code and code in PLATFORM_ALIASES:
        name, alias_values = PLATFORM_ALIASES[code]
        aliases.update([code, name, *alias_values])
    if platform:
        aliases.add(platform)
        for candidate_code, (name, alias_values) in PLATFORM_ALIASES.items():
            if platform == name or platform in alias_values:
                aliases.update([candidate_code, name, *alias_values])
                break
    for alias in sorted({x.replace(" ", "") for x in aliases if _to_str(x).strip()}, key=len, reverse=True):
        flags = re.IGNORECASE if re.search(r"[A-Za-z0-9]", alias) else 0
        body = re.sub(re.escape(alias), "", body, flags=flags)
    body = re.sub(r"(?:重新|再次|再)?报价.*$", "", body)
    body = re.sub(r"重报.*$", "", body)
    body = re.sub(r"(?:全保|交三|单商业|单商)$", "", body)
    body = body.strip("，。；;:")
    normalized = _normalize_account_type_name(body)
    return normalized if normalized in QUOTE_ACCOUNT_TYPE_SET else None


def _quote_account_type_from_material_text(text: Any, extracted: Optional[Mapping[str, Any]] = None) -> str:
    raw = _norm_text(text)
    compact = re.sub(r"[\s,，。.;；:：=+\-_/\\]+", "", raw)
    if not compact:
        return ""

    data = _json_obj(extracted)
    has_plate = bool(_to_str(data.get("plate_no")).strip()) or bool(
        re.search(r"(?:号牌号码|车牌号码|车牌号|号牌|车牌)[:：=]?[\u4e00-\u9fff][A-Z][A-Z0-9]{4,7}", compact, flags=re.IGNORECASE)
    )
    has_register_date = bool(_to_str(data.get("first_register_date")).strip())
    field_license_type = _normalize_license_type_value(
        data.get("license_type") or data.get("licenseType") or data.get("licensePlateType") or data.get("license_color_code")
    )
    old_hint = bool(re.search(r"旧车|二手车|过户车|转移登记|行驶证|初登|初次登记|注册日期|登记日期", compact, flags=re.IGNORECASE))
    new_hint = bool(re.search(r"新车|新购|未上牌|车辆合格证|合格证|国产新车|进口新车", compact, flags=re.IGNORECASE))
    new_energy_hint = bool(
        _quote_new_energy_plate_no_present(data.get("plate_no"))
        or field_license_type == LICENSE_TYPE_NEW_ENERGY
        or _quote_new_energy_text_present(
            compact,
            data.get("fuel_type"),
            data.get("fuel_kind"),
            data.get("energy_type"),
            data.get("vehicle_energy_type"),
            data.get("vehicle_model"),
            data.get("vehicle_brand_name"),
            data.get("manufacturer_name"),
        )
    )
    fuel_hint = bool(_quote_fuel_text_present(compact, data.get("fuel_type"), data.get("fuel_kind"), data.get("energy_type")))

    generic_usage_aliases = {"新车", "旧车", "二手车", "过户车"}

    candidates = sorted(
        (*QUOTE_ACCOUNT_TYPE_OPTIONS, *QUOTE_ACCOUNT_TYPE_ALIASES.keys()),
        key=lambda item: len(re.sub(r"[\s,，。.;；:：=+\-_/\\]+", "", _to_str(item))),
        reverse=True,
    )
    for candidate in candidates:
        normalized = _normalize_account_type_name(candidate)
        compact_candidate = re.sub(r"[\s,，。.;；:：=+\-_/\\]+", "", candidate)
        if normalized not in QUOTE_ACCOUNT_TYPE_SET or compact_candidate not in compact:
            continue
        # “新车/旧车”只是使用性质，不能抢在“纯电/新能源”等能源特征前面定成油车。
        if compact_candidate in generic_usage_aliases and new_energy_hint and not fuel_hint:
            break
        else:
            return normalized

    usage = ""
    if has_plate or has_register_date or old_hint:
        usage = "旧"
    elif new_hint:
        usage = "新"

    energy = ""
    if new_energy_hint and not re.search(r"非新能源|不是新能源", compact, flags=re.IGNORECASE):
        energy = "新能源"
    elif fuel_hint:
        energy = "油"

    if usage and not energy:
        # 没有新能源特征时按燃油车处理；新能源车通常能从绿牌或文本命中。
        energy = "油"
    if energy == "新能源" and not usage:
        usage = "新"
    if energy == "油" and not usage:
        usage = "旧" if has_plate else "新"

    if energy == "新能源" and usage == "旧":
        return "新能源车-旧"
    if energy == "新能源":
        return "新能源车-新"
    if energy == "油" and usage == "旧":
        return "油车-旧"
    if energy == "油" and usage == "新":
        return "油车-新"
    return ""


def _quote_vehicle_type_text_data(text: Any, extracted: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    type_name = _quote_account_type_from_material_text(text, extracted)
    if not type_name:
        return {}
    usage = "used_car" if type_name.endswith("-旧") else "new_car"
    energy = "new_energy" if type_name.startswith("新能源") else "fuel"
    return {
        "account_type_name": type_name,
        "quote_vehicle_type": "旧车" if usage == "used_car" else "新车",
        "vehicle_usage_type": usage,
        "vehicle_energy_type": energy,
        "energy_type": energy,
    }


def _looks_like_quote_vehicle_type_followup_command(text: Any) -> bool:
    compact = re.sub(r"\s+", "", _norm_text(text))
    if not compact:
        return False
    body = (
        r"(?:新能源车?(?:新车|旧车)?|绿牌|"
        r"油车(?:新车|旧车)?|燃油车?(?:新车|旧车)?|蓝牌|"
        r"新车|旧车|非新能源车?|不是新能源车?)"
    )
    prefix = (
        r"(?:这(?:个|辆|台)?车?(?:是|为)?|这是|这个是|类型(?:是|为)?|"
        r"按|改成|改为|调整成|调整为|调成|设为|设置为|更正为|修正为|纠正为|改|调)?"
    )
    return bool(re.fullmatch(prefix + body, compact, flags=re.IGNORECASE))


def _quote_text_material_field_count(data: Mapping[str, Any]) -> int:
    keys = {
        "owner_name",
        "owner_phone",
        "id_number",
        "plate_no",
        "vin",
        "engine_no",
        "vehicle_model",
        "first_register_date",
    }
    return sum(1 for key in keys if _to_str(_json_obj(data).get(key)).strip())


def _quote_text_only_material_mode(
    normalized_data: Mapping[str, Any],
    images_by_slot: Optional[Mapping[str, List[Dict[str, Any]]]] = None,
) -> bool:
    if any(rows for rows in (images_by_slot or {}).values()):
        return False
    data = _json_obj(normalized_data)
    if _to_str(data.get("raw_text")).strip():
        return True
    return _quote_text_material_field_count(data) > 0


def _looks_like_quote_text_material(text: Any, extracted: Optional[Mapping[str, Any]] = None) -> bool:
    raw = _norm_text(text)
    if not raw or looks_like_sms_code(raw):
        return False
    if re.match(r"^(查找|查询|查一下|查|搜索|搜|找)", re.sub(r"\s+", "", raw)):
        return False
    data = _json_obj(extracted)
    field_count = _quote_text_material_field_count(data)
    if field_count >= 2:
        return True
    type_name = _quote_account_type_from_material_text(raw, data)
    if type_name and field_count >= 1:
        return True
    material_terms = (
        "车主",
        "被保险人",
        "投保人",
        "手机号",
        "手机",
        "电话",
        "身份证",
        "车牌",
        "号牌",
        "车架",
        "车辆识别代号",
        "VIN",
        "发动机",
        "车型",
        "品牌型号",
        "初登",
        "合格证",
        "行驶证",
        "新能源",
        "新车",
        "旧车",
    )
    return bool(field_count and any(term.lower() in raw.lower() for term in material_terms))


def detect_quote_signal(text: Any) -> Dict[str, Any]:
    t = _norm_text(text)
    low = t.lower()
    platform_low = _redact_platform_credentials_for_signal(t).lower()
    entities: Dict[str, Any] = {}
    professional = _detect_professional_quote_command(t)

    for code, (name, aliases) in PLATFORM_ALIASES.items():
        if any(_alias_matches_text(alias, platform_low) for alias in aliases):
            entities["platform_code"] = code
            entities["platform_name"] = name
            break
    entities.update(_json_obj(professional.get("entities")))

    order_id = _extract_order_id(t)
    if order_id:
        entities["order_id"] = order_id

    extracted = extract_quote_fields(t)
    entities.update({k: v for k, v in extracted.items() if v})
    entities.update({k: v for k, v in _quote_vehicle_type_text_data(t, extracted).items() if v})

    is_quote = bool(professional.get("is_quote") or re.search(r"报价|重报|\bquote\b", low) or looks_like_short_quote_command(t))
    return {"is_quote": bool(is_quote), "entities": entities}


def looks_like_short_quote_command(text: Any) -> bool:
    """Allow experienced users to type a terse "报" after materials/platform context exists."""

    compact = re.sub(r"\s+", "", _norm_text(text))
    if not compact:
        return False
    return compact in {
        "报",
        "报价",
        "开始报",
        "开始报价",
        "直接报",
        "直接报价",
        "现在报",
        "现在报价",
        "提交报价",
        "继续报",
        "继续报价",
        "继续投保",
        "确认报价",
        "确认投保",
        "确认继续",
        "确认继续报价",
        "确认继续投保",
        "全保",
        "人保全保",
        "全保报价",
        "人保全保报价",
        "交三",
        "人保交三",
        "交三报价",
        "人保交三报价",
        "单商",
        "人保单商",
        "单商报价",
        "人保单商报价",
    }


def _is_explicit_platform_quote_command(text: Any, platform_code: str = "", platform_name: str = "") -> bool:
    raw_text = _norm_text(text)
    professional = _detect_professional_quote_command(raw_text)
    professional_entities = _json_obj(professional.get("entities"))
    if professional.get("is_quote"):
        professional_code = _to_str(professional_entities.get("platform_code")).strip().upper()
        code = _to_str(platform_code).strip().upper()
        if professional_code and (not code or professional_code == code):
            return True
    compact_candidates = [re.sub(r"\s+", "", raw_text)]
    compact_candidates.extend(
        re.sub(r"\s+", "", line)
        for line in re.split(r"[\r\n]+", raw_text)
        if _to_str(line).strip()
    )
    compact_candidates = [item for item in dict.fromkeys(compact_candidates) if item]
    if not compact_candidates:
        return False

    aliases: set[str] = set()
    code = _to_str(platform_code).strip().upper()
    if code and code in PLATFORM_ALIASES:
        name, values = PLATFORM_ALIASES[code]
        aliases.update([code, name, *values])
    name_text = _to_str(platform_name).strip()
    if name_text:
        aliases.add(name_text)
        for candidate_code, (candidate_name, values) in PLATFORM_ALIASES.items():
            if name_text == candidate_name or name_text in values:
                aliases.update([candidate_code, candidate_name, *values])
                break

    compact_aliases = sorted({re.sub(r"\s+", "", x) for x in aliases if _to_str(x).strip()}, key=len, reverse=True)
    for line in [raw_text, *re.split(r"[\r\n]+", raw_text)]:
        compact_line = re.sub(r"\s+", "", _to_str(line))
        if not compact_line or not ("报价" in compact_line or "重报" in compact_line):
            continue
        if not any(
            re.search(re.escape(alias), compact_line, flags=re.IGNORECASE if re.search(r"[A-Za-z0-9]", alias) else 0)
            for alias in compact_aliases
        ):
            continue
        line_type = _extract_account_type_from_quote_text(line, name_text, code)
        if line_type in QUOTE_ACCOUNT_TYPE_SET:
            return True
        if any(
            re.fullmatch(
                rf"{re.escape(alias)}(?:重新|再次|再)?报价|{re.escape(alias)}重报",
                compact_line,
                flags=re.IGNORECASE if re.search(r"[A-Za-z0-9]", alias) else 0,
            )
            for alias in compact_aliases
        ):
            return True

    for alias in compact_aliases:
        flags = re.IGNORECASE if re.search(r"[A-Za-z0-9]", alias) else 0
        alias_re = re.escape(alias)
        for compact in compact_candidates:
            for pattern in (
                rf"^{alias_re}(?P<body>.*?)(?:重新|再次|再)?报价$",
                rf"^{alias_re}(?P<body>.*?)重报$",
            ):
                m = re.fullmatch(pattern, compact, flags=flags)
                if not m:
                    continue
                body = m.group("body").strip("，。；;:：")
                body_type = _normalize_account_type_name(body)
                if not body or body_type in QUOTE_ACCOUNT_TYPE_SET:
                    return True
    return False


def _quote_check_context_is_active(case: QuoteCase) -> bool:
    if not case or not _to_str(case.platform_code).strip() or not _to_str(case.platform_name).strip():
        return False
    if case.status in {CASE_STATUS_READY, CASE_STATUS_WAITING_SMS, CASE_STATUS_WAITING_DUPLICATE_CONFIRM}:
        return True
    if case.status == CASE_STATUS_COLLECTING:
        return True
    return bool(_json_list(case.missing_requirements))


def _quote_case_has_pending_quote_check(case: Optional[QuoteCase]) -> bool:
    if not case or not _to_str(getattr(case, "platform_code", "")).strip():
        return False
    if _json_list(getattr(case, "missing_requirements", None)):
        return True
    if _safe_int(getattr(case, "current_task_id", 0), 0) > 0:
        return True
    return _to_str(getattr(case, "status", "")).strip() in {
        CASE_STATUS_READY,
        CASE_STATUS_WAITING_SMS,
        CASE_STATUS_WAITING_DUPLICATE_CONFIRM,
        CASE_STATUS_FAILED,
    }


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


def _normalize_quote_date_text(value: Any) -> str:
    text = _to_str(value).strip()
    if not text:
        return ""
    m = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", text)
    if not m:
        return ""
    year, month, day = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _quote_end_date_text(start_date: Any) -> str:
    text = _normalize_quote_date_text(start_date)
    if not text:
        return ""
    try:
        start = datetime.strptime(text, "%Y-%m-%d")
        try:
            next_year = start.replace(year=start.year + 1)
        except ValueError:
            next_year = start.replace(year=start.year + 1, day=28)
        return (next_year - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _quote_period_time_texts(hour: Any = "", minute: Any = "") -> Tuple[str, str]:
    parsed_hour = _safe_int(hour, -1)
    parsed_minute = _safe_int(minute, 0) if _to_str(minute).strip() else 0
    if 0 <= parsed_hour <= 23 and 0 <= parsed_minute <= 59:
        return str(parsed_hour), str(parsed_minute)
    return "0", "0"


def _quote_period_time_explicit(hour: Any = "", minute: Any = "") -> bool:
    if not _to_str(hour).strip():
        return False
    parsed_hour = _safe_int(hour, -1)
    parsed_minute = _safe_int(minute, 0) if _to_str(minute).strip() else 0
    return 0 <= parsed_hour <= 23 and 0 <= parsed_minute <= 59


def _quote_ci_end_date_text(start_date: Any, start_hour: Any = "", start_minute: Any = "") -> str:
    text = _normalize_quote_date_text(start_date)
    if not text:
        return ""
    hour, minute = _quote_period_time_texts(start_hour, start_minute)
    if hour == "0" and minute == "0":
        return _quote_end_date_text(text)
    try:
        start = datetime.strptime(text, "%Y-%m-%d")
        try:
            next_year = start.replace(year=start.year + 1)
        except ValueError:
            next_year = start.replace(year=start.year + 1, day=28)
        return next_year.strftime("%Y-%m-%d")
    except ValueError:
        return ""


def extract_quote_fields(text: Any) -> Dict[str, Any]:
    t = _norm_text(text)
    up = t.upper()
    out: Dict[str, Any] = {}

    owner_phone = _extract_labeled_value(t, _OWNER_PHONE_HINTS, max_len=32)
    if owner_phone:
        digits = re.sub(r"\D+", "", _trim_quote_text_value_at_next_label(owner_phone))
        if len(digits) == 11:
            out["owner_phone"] = digits
    elif not _has_login_phone_hint(t):
        phone = re.search(r"\b(1\d{10})\b", t)
        if phone:
            out["owner_phone"] = phone.group(1)

    id_no = re.search(
        r"(?:身份证号|身份证号码|身份证|证件号|证件号码)\s*(?:[:：=]|是|为)?\s*([0-9A-Za-z\u00d7Xx]{18})",
        t,
    )
    if not id_no:
        id_no = re.search(r"(?<![0-9A-Za-z])([0-9A-Za-z\u00d7Xx]{18})(?![0-9A-Za-z])", t)
    if id_no:
        out["id_number"] = id_no.group(1).upper()

    plate = re.search(r"(?:号牌号码|车牌号码|车牌号|号牌|车牌)\s*(?:[:：=]|是|为)?\s*([\u4e00-\u9fa5][A-Z][A-Z0-9]{4,7})", up)
    if not plate:
        plate = re.search(r"(?<![\u4e00-\u9fffA-Z0-9])([\u4e00-\u9fa5][A-Z][A-Z0-9]{4,7})(?![A-Z0-9])", up)
    if plate:
        out["plate_no"] = plate.group(1)

    vin = re.search(r"(?:VIN|车架号|车架|车辆识别代号|车辆识别代码|车辆识别码)\s*(?:[:：=]|是|为)?\s*([A-Z0-9]{11,20})", up, flags=re.IGNORECASE)
    if not vin:
        vin = re.search(r"\b([A-Z0-9]{17})\b", up)
    if vin:
        out["vin"] = vin.group(1).upper()

    engine = re.search(r"(?:发动机号|发动机号码|发动机)\s*(?:[:：=]|是|为)?\s*([A-Z0-9\-]{4,32})", up, flags=re.IGNORECASE)
    if engine:
        out["engine_no"] = engine.group(1).upper()

    owner_name = _extract_labeled_value(t, _OWNER_NAME_HINTS, max_len=64)
    if owner_name and _quote_owner_name_value_blocked(owner_name):
        owner_name = None
    if not owner_name:
        owner_label_block = (
            r"(?!手机号|手机号码|手机|电话|身份证号|身份证号码|身份证|证件号|证件号码|"
            r"车牌号码|车牌号|车牌|号牌号码|号牌|VIN|车架号|车架|车辆识别代号|"
            r"发动机号|发动机号码|发动机|车型名称|车型|品牌型号)"
        )
        name = re.search(
            rf"(?:车主|姓名|被保人|被保险人|投保人|联系人){owner_label_block}\s*[:：]\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{{2,40}})",
            t,
            flags=re.IGNORECASE,
        )
        if not name:
            name = re.search(
                rf"(?:车主|姓名){owner_label_block}\s+([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{{2,40}})(?=\s|$)",
                t,
                flags=re.IGNORECASE,
            )
        if not name:
            name = re.search(
                rf"(?:车主|被保人|被保险人|投保人|联系人){owner_label_block}([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{{2,40}})",
                t,
                flags=re.IGNORECASE,
            )
        owner_name = name.group(1).strip() if name else None
    if owner_name:
        owner_name = _trim_quote_text_value_at_next_label(owner_name)
        owner_name = re.sub(
            r"^(?:改成|改为|改到|调整成|调整为|调整到|调成|调到|调至|设置成|设置为|设成|设为|变成|变为|变到|修改成|修改为|修改到|更正成|更正为|更正到|修正成|修正为|修正到|纠正成|纠正为|纠正到|改|变|是|为)",
            "",
            owner_name,
        ).strip()
        if _quote_owner_name_value_blocked(owner_name):
            owner_name = ""
    if owner_name and owner_name not in {"姓名", "车主", "手机号", "电话", "车牌号", "身份证号"}:
        out["owner_name"] = owner_name.strip()

    model = re.search(r"(?:车辆品牌/车辆名称|车辆品牌/车辆型号|车型名称|品牌型号|车辆型号|车型)\s*(?:[:：=]|是|为)?\s*([^，,;；。\n\r]{2,90})", t)
    if model:
        out["vehicle_model"] = _trim_quote_text_value_at_next_label(model.group(1)).strip()

    quote_date_pattern = r"(\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2})"

    exp_date = re.search(r"(?:保险到期|到期日|保险止期)\s*(?:[:：=]|是|为)?\s*" + quote_date_pattern, t)
    if exp_date:
        value = _normalize_quote_date_text(exp_date.group(1))
        if value:
            out["insurance_expire_date"] = value

    first_register = re.search(r"(?:初登日期|初登|初次登记日期|注册日期|登记日期)\s*(?:[:：=]|是|为)?\s*" + quote_date_pattern, t)
    if first_register:
        value = _normalize_quote_date_text(first_register.group(1))
        if value:
            out["first_register_date"] = value

    issue_date = re.search(r"(?:行驶证发证日期|发证日期|发证时间)\s*(?:[:：=]|是|为)?\s*" + quote_date_pattern, t)
    if issue_date:
        value = _normalize_quote_date_text(issue_date.group(1))
        if value:
            out["issue_date"] = value

    commercial_start = re.search(r"(?:商业起保日期|商业险起保日期|商业起保|商业险起期)\s*(?:[:：=]|是|为)?\s*" + quote_date_pattern, t)
    if commercial_start:
        value = _normalize_quote_date_text(commercial_start.group(1))
        if value:
            out["commercial_start_date"] = value

    compulsory_start = re.search(r"(?:交强起保日期|交强险起保日期|交强起保|交强险起期)\s*(?:[:：=]|是|为)?\s*" + quote_date_pattern, t)
    if compulsory_start:
        value = _normalize_quote_date_text(compulsory_start.group(1))
        if value:
            out["compulsory_start_date"] = value

    quote_overrides: Dict[str, Any] = {}
    purchase = re.search(r"(?:新车购置价|购置价)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*元?", t)
    if purchase:
        quote_overrides["新车购置价"] = purchase.group(1)

    seat = re.search(r"(?:核定载客量\s*[（(]包括司机[）)]|核定载客量|座位数)\s*[:：]?\s*(\d{1,2})\s*人?", t)
    if seat:
        out["approved_passenger_count"] = seat.group(1)

    product_aliases: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
        ("机动车损失保险", ("机动车损失保险", "车辆损失险", "车损险", "车损")),
        ("医保外医疗费用责任险（第三者责任险）", ("附加医保外医疗费用责任险（机动车第三者责任保险）", "医保外医疗费用责任险（第三者责任险）", "医保外医疗费用责任险(第三者责任险)", "医保外三者", "医保外")),
        ("第三者责任险", ("机动车第三者责任保险", "第三者责任险", "第三责任险", "第三者", "三者险", "三者")),
        ("车上人员责任险（司机）", ("机动车车上人员责任保险（司机）", "车上人员责任险（司机）", "车上人员责任险(司机)", "司机责任险", "司机险")),
        ("车上人员责任险（乘客）", ("机动车车上人员责任保险（乘客）", "车上人员责任险（乘客）", "车上人员责任险(乘客)", "乘客责任险", "乘客险")),
        ("交强", ("交强险", "交强")),
    )
    consumed_spans: List[Tuple[int, int]] = []
    for canonical, aliases in product_aliases:
        for alias in sorted(aliases, key=len, reverse=True):
            pattern = rf"{_quote_config_alias_pattern(alias)}\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(万|元)?"
            m = next(
                (
                    item
                    for item in re.finditer(pattern, t, flags=re.IGNORECASE)
                    if not any(not (item.end() <= start or item.start() >= end) for start, end in consumed_spans)
                ),
                None,
            )
            if not m:
                continue
            value = _normalize_quote_config_override_value(m.group(1), m.group(2))
            if value:
                quote_overrides[canonical] = value
                consumed_spans.append(m.span())
            break
    if "共享主险限额" in t:
        compact_shared = re.sub(r"\s+", "", t)
        if re.search(r"(?:取消|不要|不用|不使用|关闭|去掉|不勾选|非).{0,8}共享主险限额|共享主险限额.{0,8}(?:取消|不要|不用|不使用|关闭|去掉|不勾选|非)", compact_shared):
            quote_overrides[QUOTE_SHARED_LIMIT_LABEL] = _quote_false_text()
        else:
            quote_overrides.setdefault(QUOTE_SHARED_LIMIT_LABEL, "true")
    if quote_overrides:
        out["quote_field_overrides"] = _merge_quote_config_overrides(
            out.get("quote_field_overrides"),
            quote_overrides,
            validate_positive=False,
        )
    transfer_command = _extract_transfer_vehicle_command(t)
    if transfer_command:
        out["transfer_vehicle_override"] = transfer_command.get("transfer_vehicle_override")
        out["is_transfer_vehicle"] = transfer_command.get("is_transfer_vehicle")
        if _to_str(transfer_command.get("transfer_date")).strip():
            out["transfer_date"] = transfer_command.get("transfer_date")

    out.update({k: v for k, v in _quote_vehicle_type_text_data(t, out).items() if v})

    return _clean_quote_dynamic_data(out, derive_owner_name=False)


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


def _ensure_quote_flow_access(ctx: Dict[str, Any]) -> None:
    try:
        require_quote_assistant_quote_use_access(role_name=_ctx_role_name(ctx))
    except Exception as exc:
        detail = _to_str(getattr(exc, "detail", "")).strip()
        raise ValueError(detail or "当前账号无权发起报价或上传报价材料") from exc


def _order_acl_clause_for_ctx(ctx: Dict[str, Any]):
    role_name = _ctx_role_name(ctx)
    if role_name == ROLE_SUPER_ADMIN:
        return None

    if role_name == ROLE_SALES:
        team_names = _ctx_team_names(ctx)
        if not team_names:
            return sql_false()
        team_user_ids = select(User.id).where(user_team_match_expr(team_names))
        return Order.salesperson_id.in_(team_user_ids)

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


def _quote_order_not_accessible_response(
    *,
    order_id: int,
    platform_name: str = "",
    inherited: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    order_label = f"订单 {int(order_id)}" if _safe_int(order_id, 0) else "该订单"
    if inherited:
        reply = f"当前会话绑定的{order_label}已不在你的权限范围内，我没有继续使用这条订单资料。请确认权限后重新进入订单，或新建会话重新上传材料。"
        message = "当前会话绑定订单无权访问"
    else:
        reply = f"没有在你的权限范围内找到{order_label}，我没有继续发起报价。请确认订单号是否正确，或切换到有权限的账号后重试。"
        message = "订单不存在或无权访问"
    return reply, {
        "status": "success",
        "intent": "quote",
        "trace_id": _new_trace_id(),
        "data": _mk_data(
            result_status=RESULT_NEED_MORE,
            message=message,
            entities={"order_id": int(order_id)} if _safe_int(order_id, 0) else {},
            payload={"order_acl_blocked": True, "order_id": int(order_id) if _safe_int(order_id, 0) else None},
        ),
        "actions": [_mk_action(f"{platform_name}报价") if platform_name else _mk_action("查看当前材料状态")],
    }


async def _case_order_is_readable(db: AsyncSession, *, ctx: Dict[str, Any], case: QuoteCase) -> bool:
    order_id = _safe_int(getattr(case, "order_id", 0), 0)
    if order_id <= 0:
        return True
    return await _find_order(db, ctx=ctx, order_id=order_id) is not None


async def _latest_reusable_session_case(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    ctx: Optional[Dict[str, Any]] = None,
    reuse_quoted: bool = True,
    exclude_case_ids: Optional[Iterable[int]] = None,
) -> Optional[QuoteCase]:
    if owner_user_id <= 0 or not session_id:
        return None
    excluded = {_safe_int(x, 0) for x in (exclude_case_ids or []) if _safe_int(x, 0) > 0}
    stmt = (
        select(QuoteCase)
        .where(
            QuoteCase.owner_user_id == owner_user_id,
            QuoteCase.session_id == session_id,
            QuoteCase.status.in_(ACTIVE_CASE_STATUSES),
        )
    )
    if not reuse_quoted:
        stmt = stmt.where(QuoteCase.status != CASE_STATUS_QUOTED)
    if excluded:
        stmt = stmt.where(QuoteCase.id.notin_(excluded))
    rows = (await db.execute(stmt.order_by(desc(QuoteCase.id)).limit(10))).scalars().all()
    fallback_case: Optional[QuoteCase] = None
    for case in rows:
        if ctx is None or await _case_order_is_readable(db, ctx=ctx, case=case):
            if fallback_case is None:
                fallback_case = case
            if await _case_has_reusable_material_state(db, case):
                return case
    return fallback_case


async def _case_has_reusable_material_state(db: AsyncSession, case: QuoteCase) -> bool:
    if case is None:
        return False
    if _safe_int(getattr(case, "order_id", 0), 0) > 0:
        return True
    if _safe_int(getattr(case, "quote_count", 0), 0) > 0:
        return True
    if _safe_int(getattr(case, "current_task_id", 0), 0) > 0:
        return True
    if _to_str(getattr(case, "status", "")).strip().lower() == CASE_STATUS_QUOTED:
        return True
    draft = _json_obj(getattr(case, "draft_order_data", None))
    normalized = _json_obj(getattr(case, "normalized_data", None))
    if any(_to_str(v).strip() for v in draft.values()):
        return True
    if any(_to_str(v).strip() for v in normalized.values()):
        return True
    active_count = await db.scalar(
        select(func.count())
        .select_from(QuoteCaseImage)
        .where(QuoteCaseImage.quote_case_id == case.id, QuoteCaseImage.status == ACTIVE_IMAGE_STATUS)
    )
    return _safe_int(active_count, 0) > 0


async def _get_or_create_case(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    order: Optional[Order],
    platform_code: str,
    platform_name: str,
    ctx: Optional[Dict[str, Any]] = None,
    reuse_quoted: bool = True,
    exclude_case_ids: Optional[Iterable[int]] = None,
) -> QuoteCase:
    order_id = _safe_int(getattr(order, "id", 0), 0) or None
    excluded = {_safe_int(x, 0) for x in (exclude_case_ids or []) if _safe_int(x, 0) > 0}

    base_stmt = select(QuoteCase).where(
        QuoteCase.owner_user_id == owner_user_id,
        QuoteCase.status.in_(ACTIVE_CASE_STATUSES),
    )
    if not reuse_quoted:
        base_stmt = base_stmt.where(QuoteCase.status != CASE_STATUS_QUOTED)
    if excluded:
        base_stmt = base_stmt.where(QuoteCase.id.notin_(excluded))

    case = None
    if order_id:
        exact_stmt = base_stmt.where(QuoteCase.order_id == order_id)
        case = (await db.execute(exact_stmt.order_by(desc(QuoteCase.id)).limit(1))).scalars().first()
        if not case and session_id:
            draft_stmt = base_stmt.where(QuoteCase.session_id == session_id, QuoteCase.order_id.is_(None))
            case = (await db.execute(draft_stmt.order_by(desc(QuoteCase.id)).limit(1))).scalars().first()
    elif session_id:
        if ctx is not None:
            case = await _latest_reusable_session_case(
                db,
                owner_user_id=owner_user_id,
                session_id=session_id,
                ctx=ctx,
                reuse_quoted=reuse_quoted,
                exclude_case_ids=excluded,
            )
        else:
            stmt = base_stmt.where(QuoteCase.session_id == session_id, QuoteCase.order_id.is_(None))
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
        status=CASE_STATUS_COLLECTING,
        quote_count=0,
        draft_order_data={},
        normalized_data={},
        missing_requirements=[],
    )
    db.add(case)
    await db.flush()
    return case


async def _latest_active_case(db: AsyncSession, *, owner_user_id: int, session_id: Optional[str]) -> Optional[QuoteCase]:
    if not session_id:
        return None
    stmt = select(QuoteCase).where(QuoteCase.owner_user_id == owner_user_id)
    stmt = stmt.where(QuoteCase.session_id == session_id)
    stmt = stmt.where(QuoteCase.status.in_(ACTIVE_CASE_STATUSES))
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


async def _set_single_active(db: AsyncSession, *, case_id: int, slot_key: str, keep_image_id: int) -> List[Dict[str, Any]]:
    if not is_single_slot(slot_key):
        return []

    rows = (
        await db.execute(
            select(QuoteCaseImage).where(
                QuoteCaseImage.quote_case_id == case_id,
                QuoteCaseImage.confirmed_slot_key == slot_key,
                QuoteCaseImage.status == ACTIVE_IMAGE_STATUS,
                QuoteCaseImage.id != keep_image_id,
            )
        )
    ).scalars().all()
    if not rows:
        return []

    replaced = [
        {
            "id": row.id,
            "storage_key": row.storage_key,
            "confirmed_slot_key": row.confirmed_slot_key,
            "extracted_fields": _quote_image_extracted_fields_from_features(row.text_features, row.ocr_text_sample),
        }
        for row in rows
    ]
    await db.execute(
        update(QuoteCaseImage)
        .where(QuoteCaseImage.id.in_([row.id for row in rows]))
        .values(status="replaced", updated_at=_now())
    )
    return replaced


def _image_meta_key(image: Dict[str, Any]) -> str:
    return _to_str(image.get("storage_key")).strip().lstrip("/")


def _image_meta_md5(image: Dict[str, Any]) -> str:
    md5 = _to_str(image.get("md5")).strip().lower()
    if len(md5) != 32 or any(ch not in "0123456789abcdef" for ch in md5):
        return ""
    return md5


def _is_valid_quote_upload_image_meta(image: Dict[str, Any]) -> bool:
    storage_key = _image_meta_key(image)
    md5 = _image_meta_md5(image)
    if not storage_key or not md5:
        return False
    for slot_key in SLOT_KEYS:
        try:
            if storage.validate_b1_key(scene=slot_key, storage_key=storage_key, md5_hex=md5):
                return True
        except Exception:
            continue
    return False


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


def quote_message_may_interrupt_running_task(text: Any, ctx: Optional[Mapping[str, Any]] = None) -> bool:
    """Whether a new chat request can invalidate an already-running quote.

    Ordinary chat remains serialized.  Only commands/material submissions that
    can change the quote snapshot are allowed to pass the process-local chat
    lock while a platform request is running; database task status and snapshot
    fingerprints remain the cross-process source of truth.
    """

    safe_ctx = dict(ctx) if isinstance(ctx, Mapping) else {}
    page_ctx = _json_obj(safe_ctx.get("page_context"))
    if _collect_context_images(safe_ctx) or page_ctx.get("quote_material_form_submit") is True:
        return True

    value = _to_str(text).strip()
    if not value:
        return False
    if detect_quote_signal(value).get("is_quote"):
        return True
    if _json_obj(detect_quote_config_override_signal(value).get("overrides")):
        return True
    if _json_obj(detect_quote_data_override_signal(value).get("overrides")):
        return True
    if _extract_transfer_vehicle_command(value) or _extract_quote_product_exclusions(value):
        return True
    return bool(extract_quote_fields(value))


def _image_url_for_ocr(image: Dict[str, Any], storage_key: str) -> str:
    if not storage_key:
        return ""
    try:
        return storage.object_url_for_display(storage_key, signed=True, expires_in=900, allow_fallback_public=True)
    except Exception:
        try:
            return storage.object_public_url(storage_key)
        except Exception:
            return _to_str(image.get("url") or image.get("preview_url") or image.get("image_url")).strip()


def _ocr_candidates_for_image(
    provided_slot: str,
    storage_key: str,
    predicted_slot: str = "",
) -> Tuple[Tuple[str, str, Optional[str]], ...]:
    provided = _to_str(provided_slot).strip()
    predicted = _to_str(predicted_slot).strip()
    key = "/" + _to_str(storage_key).strip().lstrip("/").lower()
    preferred = provided if provided in SLOT_KEYS and provided != "related" else ""
    if not preferred and predicted in SLOT_KEYS and predicted != "related":
        preferred = predicted

    if preferred == "vehicle_cert" or "/cert/" in key:
        return (("vehicle_cert", "vehicle_certificate", None), ("vehicle_cert", "accurate_basic", None))
    if preferred in ("idcard_front", "idcard_back") or "/idcard/" in key:
        return (
            ("idcard_front", "idcard", "front"),
            ("idcard_back", "idcard", "back"),
        )
    if preferred in ("driving_license_main", "driving_license_sub") or "/dl/" in key:
        return (
            ("driving_license_main", "vehicle_license", "front"),
            ("driving_license_sub", "vehicle_license", "back"),
        )
    return UNKNOWN_IMAGE_OCR_CANDIDATES


def _prioritize_cached_ocr_candidates(
    candidates: Tuple[Tuple[str, str, Optional[str]], ...],
    *,
    storage_key: str,
    ocr_cache: Optional[Dict[Tuple[str, str, str], Dict[str, Any]]] = None,
) -> Tuple[Tuple[str, str, Optional[str]], ...]:
    if not candidates or not ocr_cache:
        return candidates

    ranked: List[Tuple[int, float, int, Tuple[str, str, Optional[str]]]] = []
    for idx, item in enumerate(candidates):
        slot_key, api_type, side = item
        raw = ocr_cache.get(_quote_ocr_cache_key(storage_key, api_type, _to_str(side).strip()))
        if raw is None:
            ranked.append((1, 0.0, idx, item))
            continue
        try:
            extracted = _extract_by_type(api_type, raw)
        except Exception:
            extracted = {}
        extracted_slot, extracted_confidence, _ = _slot_from_ocr_extracted(api_type, side, extracted)
        score = extracted_confidence
        if extracted_slot == slot_key:
            score += 0.04
        if not score and any(_to_str(v).strip() for v in _json_obj(extracted).values()):
            score = 0.2
        ranked.append((0, -score, idx, item))

    if not any(group == 0 and score < 0 for group, score, _, _ in ranked):
        return candidates
    return tuple(item for _, _, _, item in sorted(ranked, key=lambda row: (row[0], row[1], row[2])))


def _quote_ocr_cache_key(storage_key: str, api_type: str, side: Optional[str]) -> Tuple[str, str, str]:
    return (_to_str(storage_key).strip(), _to_str(api_type).strip(), _to_str(side).strip())


async def _load_quote_ocr_cache(
    db: AsyncSession,
    image_list: Iterable[Dict[str, Any]],
) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    storage_keys = sorted({_image_meta_key(image) for image in image_list if isinstance(image, dict) and _image_meta_key(image)})
    if not storage_keys:
        return {}
    rows = (
        await db.execute(
            select(OcrImageCache).where(
                OcrImageCache.storage_key.in_(storage_keys),
                OcrImageCache.provider == "baidu",
            )
        )
    ).scalars().all()
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in rows:
        result = _json_obj(getattr(row, "result", None))
        if result:
            out[_quote_ocr_cache_key(row.storage_key, row.api_type, row.side)] = result
    return out


async def _persist_quote_ocr_cache_writes(db: AsyncSession, writes: Iterable[Dict[str, Any]]) -> None:
    seen: set[Tuple[str, str, str]] = set()
    for item in writes or []:
        if not isinstance(item, dict):
            continue
        storage_key = _to_str(item.get("storage_key")).strip()
        api_type = _to_str(item.get("api_type")).strip()
        side = _to_str(item.get("side")).strip()
        raw = _json_obj(item.get("raw"))
        if not storage_key or not api_type or not raw:
            continue
        key = _quote_ocr_cache_key(storage_key, api_type, side)
        if key in seen:
            continue
        seen.add(key)
        await _cache_put(
            db,
            storage_key=storage_key,
            api_type=api_type,
            side=side,
            provider="baidu",
            result=raw,
        )


def _ocr_raw_text(raw: Any, *, limit: int = 3000) -> str:
    parts: List[str] = []

    def push(value: Any) -> None:
        if len("\n".join(parts)) >= limit:
            return
        if isinstance(value, str):
            text = re.sub(r"\s+", " ", value.replace("\u3000", " ")).strip()
            if text:
                parts.append(text)
            return
        if isinstance(value, dict):
            words = value.get("words")
            if isinstance(words, str):
                push(words)
                return
            for key, item in value.items():
                if key in {"location", "probability", "vertexes_location"}:
                    continue
                if isinstance(item, (dict, list, tuple)):
                    push(item)
                elif isinstance(item, str):
                    label = _to_str(key).strip()
                    push(f"{label}{item}" if label and label not in item else item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                push(item)

    if isinstance(raw, dict) and "words_result" in raw:
        push(raw.get("words_result"))
    else:
        push(raw)
    return "\n".join(parts)[:limit].strip()


def _looks_like_quote_reference_text(value: Any) -> bool:
    text = _norm_text(_ocr_raw_text(value) if isinstance(value, dict) else value)
    if not text:
        return False
    core_hits = sum(1 for marker in ("险别名称", "保额", "保费") if marker in text)
    if core_hits < 2:
        return False
    markers = (
        "投保人姓名",
        "被保险人姓名",
        "车主姓名",
        "商业险起保日期",
        "交强险起保日期",
        "商业车险合计",
        "保费合计",
        "代收车船税",
        "机动车损失保险",
        "机动车第三者责任保险",
    )
    return sum(1 for marker in markers if marker in text) >= 2


async def _call_accurate_basic_text(
    *,
    image_url: str,
    storage_key: str = "",
    ocr_cache: Optional[Dict[Tuple[str, str, str], Dict[str, Any]]] = None,
    cache_writes: Optional[List[Dict[str, Any]]] = None,
    deadline: float,
) -> Tuple[Optional[Dict[str, Any]], str, Dict[str, Any], Dict[str, Any]]:
    global _QUOTE_ACCURATE_BASIC_DISABLED_REASON
    global _QUOTE_ACCURATE_BASIC_DISABLED_UNTIL

    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return None, "", {}, {"type": "timeout_budget_exhausted", "message": "通用文字识别兜底未获得可用调用时间"}
    cache_key = _quote_ocr_cache_key(storage_key, "accurate_basic", None)
    cached_raw = (ocr_cache or {}).get(cache_key)
    if cached_raw is not None:
        text = _ocr_raw_text(cached_raw)
        try:
            extracted = _extract_by_type("accurate_basic", cached_raw)
        except Exception:
            extracted = {}
        return cached_raw, text, extracted, {}
    loop = asyncio.get_running_loop()
    now = loop.time()
    if _QUOTE_ACCURATE_BASIC_DISABLED_UNTIL > now:
        reason = _QUOTE_ACCURATE_BASIC_DISABLED_REASON or "上次通用文字识别调用失败"
        return None, "", {}, {
            "type": "ocr_temporarily_unavailable",
            "message": f"通用文字识别兜底临时跳过：{sanitize_quote_user_message(reason, '上次通用文字识别调用失败')}",
        }
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(call_ocr, "accurate_basic", image_url, None, True),
            timeout=min(float(QUOTE_IMAGE_OCR_CALL_TIMEOUT_SECONDS), remaining),
        )
    except OcrNotConfigured as exc:
        return None, "", {}, {"type": "ocr_not_configured", "message": _to_str(exc)[:300]}
    except OcrCallError as exc:
        message = _to_str(exc)[:300]
        if (
            QUOTE_ACCURATE_BASIC_ERROR_COOLDOWN_SECONDS > 0
            and ("error_code=6" in message or "No permission" in message or "no permission" in message.lower())
        ):
            _QUOTE_ACCURATE_BASIC_DISABLED_UNTIL = loop.time() + float(QUOTE_ACCURATE_BASIC_ERROR_COOLDOWN_SECONDS)
            _QUOTE_ACCURATE_BASIC_DISABLED_REASON = message
        return None, "", {}, {"type": "ocr_call_error", "message": message}
    except asyncio.TimeoutError:
        return None, "", {}, {"type": "ocr_timeout", "message": "通用文字识别兜底调用超时"}
    except (ValueError, RuntimeError) as exc:
        return None, "", {}, {"type": exc.__class__.__name__, "message": _to_str(exc)[:300]}
    except Exception as exc:
        return None, "", {}, {"type": exc.__class__.__name__, "message": _to_str(exc)[:300]}

    text = _ocr_raw_text(raw)
    try:
        extracted = _extract_by_type("accurate_basic", raw)
    except Exception:
        extracted = {}
    if cache_writes is not None and storage_key:
        cache_writes.append(
            {
                "storage_key": storage_key,
                "api_type": "accurate_basic",
                "side": "",
                "raw": raw,
            }
        )
    return raw, text, extracted, {}


def _slot_from_ocr_extracted(api_type: str, side: Optional[str], extracted: Dict[str, Any]) -> Tuple[str, float, str]:
    data = _json_obj(extracted)
    if not data:
        return "", 0.0, ""

    def has_any(*keys: str) -> bool:
        return any(_to_str(data.get(key)).strip() for key in keys)

    api = _to_str(api_type).strip()
    side_norm = _to_str(side).strip().lower()
    if api == "vehicle_certificate" and has_any(
        "vin",
        "engine_no",
        "vehicle_model",
        "manufacturer_name",
        "vehicle_brand_name",
        "approved_passenger_count",
    ):
        return "vehicle_cert", 0.94, "车辆合格证识别返回了车架号/发动机号/车型/制造厂等核心字段"
    if api == "vehicle_license" and has_any("plate_no", "owner_name", "use_nature", "first_register_date", "issuer_org"):
        slot = "driving_license_sub" if side_norm == "back" else "driving_license_main"
        return slot, 0.92, "行驶证识别返回了号牌/所有人/使用性质等核心字段"
    if api == "idcard" and has_any("id_number", "id_name", "id_address", "id_birth_date"):
        slot = "idcard_back" if side_norm == "back" and has_any("id_issuer", "id_validity") else "idcard_front"
        return slot, 0.92, "身份证识别返回了身份证号/姓名等核心字段"
    if api == "accurate_basic":
        if has_any("manufacturer_name", "vehicle_brand_name", "vehicle_model", "approved_passenger_count") and has_any("vin", "engine_no"):
            return "vehicle_cert", 0.86, "通用文字识别命中合格证核心字段"
        if has_any("plate_no", "owner_name", "use_nature", "first_register_date", "issuer_org"):
            return "driving_license_main", 0.84, "通用文字识别命中行驶证主页字段"
        if has_any("id_number", "id_name", "id_address", "id_birth_date"):
            return "idcard_front", 0.84, "通用文字识别命中身份证正面字段"
    return "", 0.0, ""


def _slot_family(slot_key: str) -> str:
    slot = _to_str(slot_key).strip()
    if slot in {"idcard_front", "idcard_back"}:
        return "idcard"
    if slot in {"driving_license_main", "driving_license_sub"}:
        return "driving_license"
    if slot == "vehicle_cert":
        return "vehicle_cert"
    if slot == "related":
        return "related"
    return ""


async def _classify_image_with_optional_ocr(
    *,
    image: Dict[str, Any],
    provided_slot: str,
    storage_key: str,
    ocr_cache: Optional[Dict[Tuple[str, str, str], Dict[str, Any]]] = None,
    cache_writes: Optional[List[Dict[str, Any]]] = None,
):
    context_hint = _to_str(image.get("context_hint")).strip()
    raw_ocr_text = image.get("ocr_text") or image.get("ocr_text_sample")
    text_for_classification = raw_ocr_text or context_hint
    classification = classify_image_slot(
        provided_slot_key=provided_slot,
        original_name=image.get("original_name"),
        storage_key=storage_key,
        ocr_text=text_for_classification,
        raw_payload=image.get("raw") or image.get("ocr_raw"),
    )
    storage_key_norm = "/" + _to_str(storage_key).strip().lstrip("/").lower()
    likely_vehicle_cert = (
        provided_slot == "vehicle_cert"
        or classification.predicted_slot_key == "vehicle_cert"
        or "/cert/" in storage_key_norm
    )
    explicit_material_hint = (
        (provided_slot in SLOT_KEYS and provided_slot != "related")
        or "/cert/" in storage_key_norm
        or "/idcard/" in storage_key_norm
        or "/dl/" in storage_key_norm
        or (bool(context_hint) and classification.method == "context_hint_rule")
    )
    if not QUOTE_IMAGE_OCR_CLASSIFY_ENABLED:
        features = dict(classification.text_features or {})
        features["ocr_classify"] = {
            "enabled": bool(QUOTE_IMAGE_OCR_CLASSIFY_ENABLED),
            "used": False,
            "skip_reason": "disabled",
        }
        object.__setattr__(classification, "text_features", features)
        return classification, None, {}

    image_url = _image_url_for_ocr(image, storage_key)
    if not image_url:
        features = dict(classification.text_features or {})
        features["ocr_classify"] = {
            "enabled": True,
            "used": False,
            "skip_reason": "missing_ocr_url",
        }
        object.__setattr__(classification, "text_features", features)
        return classification, None, {}

    deadline = asyncio.get_running_loop().time() + float(QUOTE_IMAGE_OCR_TOTAL_TIMEOUT_SECONDS)
    generic_raw: Optional[Dict[str, Any]] = None
    generic_text = ""
    generic_extracted: Dict[str, Any] = {}
    generic_error: Dict[str, Any] = {}
    ocr_attempts: List[Dict[str, Any]] = []

    generic_raw, generic_text, generic_extracted, generic_error = await _call_accurate_basic_text(
        image_url=image_url,
        storage_key=storage_key,
        ocr_cache=ocr_cache,
        cache_writes=cache_writes,
        deadline=deadline,
    )
    generic_seed_text = generic_text or (_ocr_raw_text(generic_raw) if generic_raw is not None else "")

    best = classification
    best_raw: Optional[Dict[str, Any]] = None
    best_extracted: Dict[str, Any] = {}
    best_score = float(classification.confidence or 0.0)
    if generic_raw is not None or generic_seed_text or generic_error:
        generic_candidate = classify_image_slot(
            provided_slot_key=provided_slot,
            original_name=image.get("original_name"),
            storage_key=storage_key,
            ocr_text=generic_seed_text or text_for_classification,
            raw_payload=generic_raw,
        )
        extracted_slot, extracted_confidence, extracted_reason = _slot_from_ocr_extracted("accurate_basic", None, generic_extracted)
        if extracted_slot:
            features = dict(generic_candidate.text_features or {})
            features["ocr_extracted_slot"] = {
                "api_type": "accurate_basic",
                "side": None,
                "slot_key": extracted_slot,
                "matched_fields": sorted(k for k, v in generic_extracted.items() if _to_str(v).strip()),
            }
            generic_candidate = SlotClassification(
                predicted_slot_key=extracted_slot,
                confidence=max(float(generic_candidate.confidence or 0.0), extracted_confidence),
                method="ocr_extracted_rule",
                reason=extracted_reason,
                text_features=features,
                ocr_text_sample=generic_candidate.ocr_text_sample,
            )
        features = dict(generic_candidate.text_features or {})
        generic_payload = {"api_type": "accurate_basic", "used": bool(generic_raw)}
        if generic_error:
            generic_payload["error"] = generic_error
        features["generic_ocr"] = generic_payload
        if generic_seed_text:
            features["generic_ocr_text"] = generic_seed_text
        if generic_candidate.predicted_slot_key == "vehicle_cert":
            features["vehicle_type_rule_hook"] = {
                "reserved": True,
                "description": "future vehicle certificate vehicle-type rule hook",
            }
        object.__setattr__(generic_candidate, "text_features", features)
        if generic_seed_text:
            object.__setattr__(generic_candidate, "ocr_text_sample", generic_seed_text[:3000])
        if generic_raw is not None or generic_seed_text or generic_error:
            best_raw = generic_raw
            best_extracted = generic_extracted
        if generic_candidate.predicted_slot_key in SLOT_KEYS and generic_candidate.predicted_slot_key != "related":
            best = generic_candidate
            best_score = float(generic_candidate.confidence or 0.0)

    candidates = _prioritize_cached_ocr_candidates(
        _ocr_candidates_for_image(
            provided_slot,
            storage_key,
            best.predicted_slot_key or classification.predicted_slot_key,
        ),
        storage_key=storage_key,
        ocr_cache=ocr_cache,
    )
    for slot_key, api_type, side in candidates:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        side_key = _to_str(side).strip()
        attempt: Dict[str, Any] = {
            "slot_key": slot_key,
            "api_type": api_type,
            "side": side,
            "primary": not ocr_attempts,
        }
        cached_raw = (ocr_cache or {}).get(_quote_ocr_cache_key(storage_key, api_type, side_key))
        if cached_raw is not None:
            raw = cached_raw
            attempt["cached"] = True
        else:
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(call_ocr, api_type, image_url, side, True),
                    timeout=min(float(QUOTE_IMAGE_OCR_CALL_TIMEOUT_SECONDS), remaining),
                )
                attempt["cached"] = False
                if cache_writes is not None:
                    cache_writes.append(
                        {
                            "storage_key": storage_key,
                            "api_type": api_type,
                            "side": side_key,
                            "raw": raw,
                        }
                    )
            except (OcrNotConfigured, OcrCallError, ValueError, RuntimeError) as exc:
                attempt["error"] = {"type": exc.__class__.__name__, "message": _to_str(exc)[:300]}
                ocr_attempts.append(attempt)
                continue
            except asyncio.TimeoutError:
                attempt["error"] = {"type": "ocr_timeout", "message": "图片识别调用超时"}
                ocr_attempts.append(attempt)
                break
            except Exception as exc:
                attempt["error"] = {"type": exc.__class__.__name__, "message": _to_str(exc)[:300]}
                ocr_attempts.append(attempt)
                continue

        try:
            extracted_candidate = _extract_by_type(api_type, raw)
        except Exception:
            extracted_candidate = {}
        quote_reference_material = _looks_like_quote_reference_text(raw)
        if quote_reference_material:
            reference_fields = extract_quote_fields(_ocr_raw_text(raw))
            if reference_fields:
                extracted_candidate = _merge_data(extracted_candidate, reference_fields)
                attempt["quote_reference_field_keys"] = sorted(
                    key for key, value in reference_fields.items() if _to_str(value).strip()
                )[:20]
        attempt["raw_field_count"] = len(extracted_candidate or {})
        attempt["raw_field_keys"] = sorted(k for k, v in (extracted_candidate or {}).items() if _to_str(v).strip())[:20]

        candidate = classify_image_slot(
            provided_slot_key=provided_slot,
            original_name=image.get("original_name"),
            storage_key=storage_key,
            ocr_text=raw,
        )
        extracted_slot, extracted_confidence, extracted_reason = _slot_from_ocr_extracted(api_type, side, extracted_candidate)
        if quote_reference_material:
            features = dict(candidate.text_features or {})
            features["quote_reference_material"] = True
            features["quote_reference_matched"] = "报价/投保明细表"
            candidate = SlotClassification(
                predicted_slot_key="related",
                confidence=max(float(candidate.confidence or 0.0), 0.91),
                method="quote_reference_rule",
                reason="识别到报价/投保明细表，仅作为相关资料补充字段",
                text_features=features,
                ocr_text_sample=candidate.ocr_text_sample,
            )
        elif extracted_slot:
            features = dict(candidate.text_features or {})
            features["ocr_extracted_slot"] = {
                "api_type": api_type,
                "side": side,
                "slot_key": extracted_slot,
                "matched_fields": sorted(k for k, v in extracted_candidate.items() if _to_str(v).strip()),
            }
            candidate = SlotClassification(
                predicted_slot_key=extracted_slot,
                confidence=max(float(candidate.confidence or 0.0), extracted_confidence),
                method="ocr_extracted_rule",
                reason=extracted_reason,
                text_features=features,
                ocr_text_sample=candidate.ocr_text_sample,
            )
        score = float(candidate.confidence or 0.0)
        if candidate.predicted_slot_key == slot_key:
            score += 0.04
        if quote_reference_material and candidate.predicted_slot_key == "related":
            score += 0.08
        attempt["predicted_slot_key"] = candidate.predicted_slot_key
        attempt["confidence"] = round(float(candidate.confidence or 0.0), 4)
        attempt["score"] = round(float(score or 0.0), 4)
        ocr_attempts.append(attempt)
        should_replace = False
        if score > best_score:
            best_family = _slot_family(best.predicted_slot_key)
            candidate_family = _slot_family(candidate.predicted_slot_key)
            if best.predicted_slot_key == "related":
                should_replace = True
            elif candidate_family and candidate_family == best_family:
                should_replace = score >= best_score + 0.02
            else:
                should_replace = score >= best_score + 0.08
        if should_replace:
            best = candidate
            best_raw = raw
            best_score = score
            best_extracted = extracted_candidate
        if best.predicted_slot_key == slot_key and best.confidence >= 0.82 and (quote_reference_material or explicit_material_hint):
            break

    if ocr_attempts:
        features = dict(best.text_features or {})
        features["ocr_classify_attempts"] = ocr_attempts[:10]
        object.__setattr__(best, "text_features", features)

    generic_raw_seen = generic_raw
    generic_text_seen = generic_seed_text
    generic_extracted_seen = generic_extracted
    generic_error_seen = generic_error

    best_is_vehicle_cert = (
        likely_vehicle_cert
        or best.predicted_slot_key == "vehicle_cert"
        or provided_slot == "vehicle_cert"
    )
    if best_is_vehicle_cert:
        if generic_raw_seen is not None or generic_text_seen:
            generic_raw = generic_raw_seen
            generic_text = generic_text_seen
            generic_extracted = generic_extracted_seen
            generic_error = generic_error_seen
        else:
            generic_deadline = deadline
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining < 1:
                generic_deadline = asyncio.get_running_loop().time() + float(QUOTE_IMAGE_OCR_CALL_TIMEOUT_SECONDS)
            generic_raw, generic_text, generic_extracted, generic_error = await _call_accurate_basic_text(
                image_url=image_url,
                storage_key=storage_key,
                ocr_cache=ocr_cache,
                cache_writes=cache_writes,
                deadline=generic_deadline,
            )
        if generic_raw is not None or generic_text or generic_error:
            features = dict(best.text_features or {})
            generic_payload = {"api_type": "accurate_basic", "used": bool(generic_raw)}
            if generic_error:
                generic_payload["error"] = generic_error
            features["generic_ocr"] = generic_payload
            if generic_text:
                features["generic_ocr_text"] = generic_text
            features["vehicle_type_rule_hook"] = {
                "reserved": True,
                "description": "未来车辆合格证新车/旧车/新能源规则入口",
            }
            object.__setattr__(best, "text_features", features)
            if generic_text:
                merged_sample = "\n".join(x for x in (best.ocr_text_sample, generic_text) if x).strip()
                object.__setattr__(best, "ocr_text_sample", merged_sample[:3000])
            if generic_extracted:
                best_extracted = _merge_data(generic_extracted, best_extracted)
            if best_raw is None:
                best_raw = generic_raw

    if best_raw is not None:
        features = dict(best.text_features or {})
        features["ocr_classify"] = {
            "enabled": True,
            "api_type": next((api for slot, api, _ in OCR_SLOT_CANDIDATES if slot == best.predicted_slot_key), None),
            "used": True,
        }
        object.__setattr__(best, "text_features", features)
    elif ocr_attempts:
        features = dict(best.text_features or {})
        features["ocr_classify"] = {
            "enabled": True,
            "used": False,
            "attempted": True,
        }
        object.__setattr__(best, "text_features", features)
    return best, best_raw, best_extracted


async def _classify_uploaded_image_for_quote(
    image: Dict[str, Any],
    *,
    ocr_cache: Optional[Dict[Tuple[str, str, str], Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    storage_key = _image_meta_key(image)
    if not storage_key:
        return None

    provided_slot = _to_str(image.get("provided_slot_key") or image.get("slot_key")).strip()
    ocr_cache_writes: List[Dict[str, Any]] = []
    try:
        classification, ocr_raw, extracted_fields = await _classify_image_with_optional_ocr(
            image=image,
            provided_slot=provided_slot,
            storage_key=storage_key,
            ocr_cache=ocr_cache,
            cache_writes=ocr_cache_writes,
        )
    except Exception as exc:
        classification = classify_image_slot(
            provided_slot_key=provided_slot,
            original_name=image.get("original_name"),
            storage_key=storage_key,
            ocr_text=image.get("ocr_text") or image.get("ocr_text_sample") or image.get("context_hint"),
            raw_payload=image.get("raw") or image.get("ocr_raw"),
        )
        features = dict(classification.text_features or {})
        features["ocr_classify"] = {
            "enabled": bool(QUOTE_IMAGE_OCR_CLASSIFY_ENABLED),
            "used": False,
            "error": {"type": exc.__class__.__name__, "message": _to_str(exc)[:300]},
        }
        object.__setattr__(classification, "text_features", features)
        ocr_raw = None
        extracted_fields = {}

    predicted = classification.predicted_slot_key
    confirmed = predicted
    provided_is_explicit_slot = provided_slot in SLOT_KEYS and provided_slot != "related"
    needs_review = False
    if classification.confidence < QUOTE_IMAGE_AUTO_CONFIRM_MIN_CONFIDENCE:
        needs_review = bool(predicted and predicted != "related")
        confirmed = provided_slot if provided_is_explicit_slot else "related"
    if confirmed not in SLOT_KEYS:
        confirmed = "related"
    stored_features, cleaned_extracted_fields = _quote_image_features(
        classification.text_features or {},
        extracted_fields or {},
    )
    if needs_review and confirmed == "related":
        stored_features = dict(stored_features or {})
        stored_features["needs_manual_review"] = True
        stored_features["review_predicted_slot_key"] = predicted
        stored_features["review_confidence"] = round(classification.confidence, 4)
    return {
        "image": image,
        "storage_key": storage_key,
        "provided_slot": provided_slot,
        "classification": classification,
        "ocr_raw": ocr_raw,
        "extracted_fields": extracted_fields or {},
        "cleaned_extracted_fields": cleaned_extracted_fields,
        "stored_features": stored_features,
        "predicted": predicted,
        "confirmed": confirmed,
        "ocr_cache_writes": ocr_cache_writes,
    }


async def _attach_uploaded_images(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    images: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    attached: List[Dict[str, Any]] = []
    image_list = [image for image in images if isinstance(image, dict) and _image_meta_key(image)]
    if not image_list:
        return attached
    invalid_count = sum(1 for image in image_list if not _is_valid_quote_upload_image_meta(image))
    if invalid_count:
        raise ValueError(f"有 {invalid_count} 张图片上传凭证校验失败，请重新上传图片")

    ocr_cache = await _load_quote_ocr_cache(db, image_list)
    semaphore = asyncio.Semaphore(int(QUOTE_IMAGE_OCR_CONCURRENCY))

    async def run_classify(image: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with semaphore:
            return await _classify_uploaded_image_for_quote(image, ocr_cache=ocr_cache)

    classified_images = await asyncio.gather(*(run_classify(image) for image in image_list))
    await _persist_quote_ocr_cache_writes(
        db,
        (
            write
            for item in classified_images
            if isinstance(item, dict)
            for write in item.get("ocr_cache_writes") or []
        ),
    )

    for item in classified_images:
        if not item:
            continue
        image = item["image"]
        storage_key = item["storage_key"]
        provided_slot = item["provided_slot"]
        classification = item["classification"]
        ocr_raw = item["ocr_raw"]
        cleaned_extracted_fields = item["cleaned_extracted_fields"]
        stored_features = item["stored_features"]
        predicted = item["predicted"]
        confirmed = item["confirmed"]

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
            existing.text_features = stored_features
            existing.updated_at = _now()
            await db.flush()
            replaced_images = await _set_single_active(db, case_id=case.id, slot_key=confirmed, keep_image_id=existing.id)
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
                text_features=stored_features,
                created_by=owner_user_id,
            )
            db.add(row)
            await db.flush()
            replaced_images = await _set_single_active(db, case_id=case.id, slot_key=confirmed, keep_image_id=row.id)

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
                "extracted_fields": cleaned_extracted_fields,
                "needs_manual_review": bool(_json_obj(stored_features).get("needs_manual_review")),
                "review_predicted_slot_key": _json_obj(stored_features).get("review_predicted_slot_key") or "",
                "replaced_images": replaced_images,
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


QUOTE_ENTITY_FIELD_KEYS = tuple(
    sorted(
        {
            *(key for key, _ in REQUIRED_FIELDS),
            *QUOTE_IMAGE_MANAGED_FIELDS,
            "commercial_start_date",
            "compulsory_start_date",
            "first_register_date",
            "insurance_expire_date",
            "quote_vehicle_type",
            "vehicle_usage_type",
            "vehicle_energy_type",
            "energy_type",
            "fuel_type",
            "vehicle_kind",
            "raw_text",
            "is_transfer_vehicle",
            "transfer_date",
            "transfer_vehicle_override",
            QUOTE_DATA_OVERRIDES_KEY,
            QUOTE_PRODUCT_EXCLUSIONS_KEY,
            QUOTE_FLOW_TYPE_KEY,
            "quote_command_mode",
        }
    )
)


def _quote_text_data_from_entities(
    extracted: Dict[str, Any],
    entities: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    entity_data = {
        key: _json_obj(entities or {}).get(key)
        for key in QUOTE_ENTITY_FIELD_KEYS
        if _json_obj(entities or {}).get(key) not in (None, "")
    }
    # Fresh extraction wins over inherited intent entities when both exist.
    return _merge_data(entity_data, extracted or {})


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
        text_features = _json_obj(row.text_features)
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
                "ocr_text_sample": row.ocr_text_sample or "",
                "text_features": text_features,
                "extracted_fields": _quote_image_extracted_fields_from_features(text_features, row.ocr_text_sample),
            }
        )
    return out


def _missing_requirements(
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
    *,
    platform_code: str = "",
    account_type_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    missing: List[Dict[str, Any]] = []
    data = _clean_quote_dynamic_data(_json_obj(normalized_data))

    for key, label in _required_fields_for_quote(
        data,
        images_by_slot,
        platform_code=platform_code,
        account_type_name=account_type_name,
    ):
        if not _to_str(data.get(key)).strip():
            item = {"type": "field", "key": key, "label": label}
            if key == "vin":
                detail = _vehicle_cert_vin_failure_detail(images_by_slot)
                if detail:
                    item["detail"] = detail
            missing.append(item)

    for slot_key in _required_slots_for_quote(
        data,
        images_by_slot,
        platform_code=platform_code,
        account_type_name=account_type_name,
    ):
        if not images_by_slot.get(slot_key):
            missing.append({"type": "image", "key": slot_key, "label": slot_label(slot_key)})

    missing.extend(_quote_material_issues(data, images_by_slot))
    return missing


def _renewal_lookup_missing_requirements(normalized_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = _clean_quote_dynamic_data(_json_obj(normalized_data))
    missing: List[Dict[str, Any]] = []
    if not _to_str(data.get("plate_no")).strip():
        missing.append({"type": "field", "key": "plate_no", "label": "号牌号码"})
    if not _to_str(data.get("engine_no")).strip() and not _to_str(data.get("vin")).strip():
        missing.append({"type": "field", "key": "engine_or_vin", "label": "发动机号或车架号"})
    return missing


def _missing_requirements_for_quote_flow(
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
    *,
    platform_code: str,
    account_type_name: Optional[str],
    quote_flow_type: Any = "",
) -> List[Dict[str, Any]]:
    """Resolve material requirements from the case's effective quote flow.

    Renewal lookup deliberately starts with only the plate number and either
    engine number or VIN. All material/status entry points must use that same
    rule; otherwise a renewal case can oscillate between ready and collecting
    depending on whether it was inspected, supplemented, or recalled.
    """

    flow_type = _to_str(quote_flow_type).strip() or _quote_flow_type_from_case_data(normalized_data)
    if flow_type == QUOTE_FLOW_RENEWAL:
        return _renewal_lookup_missing_requirements(normalized_data)
    return _missing_requirements(
        normalized_data,
        images_by_slot,
        platform_code=platform_code,
        account_type_name=account_type_name,
    )


def _missing_item_text(item: Dict[str, Any]) -> str:
    label = _to_str(item.get("label") or item.get("key")).strip() or "未知项目"
    detail = _format_missing_detail(item.get("detail"))
    return f"{label}（{detail}）" if detail else label


def _quote_preflight_item(
    *,
    code: str,
    category: str,
    label: str,
    detail: str = "",
    failure_code: str = "",
) -> Dict[str, str]:
    return {
        "code": _to_str(code).strip(),
        "category": _to_str(category).strip(),
        "label": _to_str(label).strip() or "未命名项",
        "detail": _to_str(detail).strip(),
        "failure_code": _to_str(failure_code).strip(),
    }


def _material_preflight_items(missing: Iterable[Mapping[str, Any]]) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for raw in missing or []:
        item = dict(raw or {})
        item_type = _to_str(item.get("type")).strip() or "field"
        if item_type == "image":
            category = "material_image"
        elif item_type == "data_conflict":
            category = "material_conflict"
        else:
            category = "material_field"
        items.append(
            _quote_preflight_item(
                code=_to_str(item.get("key") or item_type).strip() or category,
                category=category,
                label=_missing_item_text(item),
                detail=_format_missing_detail(item.get("detail")),
                failure_code=FAILURE_CODE_MATERIAL_MISSING,
            )
        )
    return items


def _primary_preflight_failure_code(items: Iterable[Mapping[str, Any]]) -> str:
    codes = {_to_str(item.get("failure_code")).strip() for item in (items or []) if _to_str(item.get("failure_code")).strip()}
    for preferred in (
        FAILURE_CODE_MATERIAL_MISSING,
        FAILURE_CODE_ACCOUNT_MISSING,
        FAILURE_CODE_ACCOUNT_LOGIN,
        FAILURE_CODE_DEFAULT_CONFIG_MISSING,
    ):
        if preferred in codes:
            return preferred
    return FAILURE_CODE_PREFLIGHT if codes else FAILURE_CODE_PREFLIGHT


def _format_quote_preflight_reply(
    *,
    platform_name: str,
    items: Iterable[Mapping[str, Any]],
    override_summary: str = "",
    attached_images: Optional[List[Dict[str, Any]]] = None,
) -> str:
    safe_items = [dict(item or {}) for item in (items or []) if _to_str((item or {}).get("label")).strip()]
    platform_label = _to_str(platform_name).strip() or "平台"
    lines = [f"{platform_label}报价前检查未通过，请先处理以下事项："]
    if _to_str(override_summary).strip():
        lines.append(f"已记录本次调整：{_to_str(override_summary).strip()}")

    field_labels = [_to_str(x.get("label")).strip() for x in safe_items if x.get("category") == "material_field"]
    image_labels = [_to_str(x.get("label")).strip() for x in safe_items if x.get("category") == "material_image"]
    conflict_labels = [_to_str(x.get("label")).strip() for x in safe_items if x.get("category") == "material_conflict"]
    account_labels = [_to_str(x.get("label")).strip() for x in safe_items if x.get("category") == "account"]
    config_labels = [_to_str(x.get("label")).strip() for x in safe_items if x.get("category") == "default_config"]

    step = 1
    if field_labels:
        lines.append(f"{step}. 缺少字段：" + "、".join(field_labels[:12]))
        step += 1
    if image_labels:
        lines.append(f"{step}. 缺少图片：" + "、".join(image_labels[:12]))
        step += 1
    if conflict_labels:
        lines.append(f"{step}. 资料冲突：" + "、".join(conflict_labels[:8]))
        step += 1
    for label in account_labels:
        lines.append(f"{step}. 账号：{label}")
        step += 1
    for label in config_labels:
        lines.append(f"{step}. 默认参数：{label}")
        step += 1

    if attached_images:
        moved = [
            f"{slot_label(x.get('provided_slot_key') or '')}->{slot_label(x.get('confirmed_slot_key') or '')}"
            for x in attached_images
            if x.get("provided_slot_key") != x.get("confirmed_slot_key")
        ]
        if moved:
            lines.append("已自动识别并归位图片：" + "、".join(moved[:5]))
        review_count = sum(1 for x in attached_images if x.get("needs_manual_review"))
        if review_count:
            lines.append(
                f"{review_count} 张图片特征不够明确，已先放入相关图片，不会覆盖关键材料；"
                "如需修正，请补一句图片说明后重新拖入。"
            )
    lines.append(f"下一步：{_quote_failure_next_action(_primary_preflight_failure_code(safe_items))}")
    return "\n".join(lines)


async def _collect_quote_command_preflight_items(
    db: AsyncSession,
    *,
    missing: Iterable[Mapping[str, Any]],
    platform_account: Optional[QuotePlatformAccountProfile],
    platform_has_enabled_account: bool,
    platform_code: str,
    platform_name: str,
    selected_account_type_name: Optional[str],
    operator_role_name: Any = "",
) -> List[Dict[str, str]]:
    """Gather material + account + default-config blockers for one quote command."""
    material_items = _material_preflight_items(missing)
    items: List[Dict[str, str]] = list(material_items)
    type_name = _normalize_account_type_name(selected_account_type_name)
    type_hint = f"（类型：{type_name}）" if type_name else ""
    name = _to_str(platform_name).strip() or _platform_display_name(platform_code) or "平台"

    if platform_account is None:
        if platform_has_enabled_account:
            items.append(
                _quote_preflight_item(
                    code="account_login",
                    category="account",
                    label=_quote_account_action_text(
                        operator_role_name,
                        f"{name}{type_hint}没有已登录可用账号（请确认已登录、未等待验证码且额度未满）",
                        f"{name}{type_hint}没有已登录可用账号，请联系管理员处理",
                    ),
                    failure_code=FAILURE_CODE_ACCOUNT_LOGIN,
                )
            )
        else:
            items.append(
                _quote_preflight_item(
                    code="account_missing",
                    category="account",
                    label=_quote_account_action_text(
                        operator_role_name,
                        f"{name}{type_hint}还没有可用平台账号，请先新增、启用并登录",
                        f"{name}{type_hint}还没有可用平台账号，请联系管理员新增、启用或登录",
                    ),
                    failure_code=FAILURE_CODE_ACCOUNT_MISSING,
                )
            )

    # Default-config depends on account type; only check after materials are complete
    # so incomplete OCR/vehicle type does not raise a misleading config blocker.
    if type_name and not material_items:
        resolved = await resolve_platform_default_config(
            db,
            platform_code=platform_code,
            account_type_name=type_name,
        )
        matched = _to_str(resolved.get("matched")).strip()
        if matched != "account_type":
            items.append(
                _quote_preflight_item(
                    code="default_config_missing",
                    category="default_config",
                    label=_quote_account_action_text(
                        operator_role_name,
                        f"{name}（{type_name}）尚未启用默认参数配置，请先在“默认参数配置”中新增并启用",
                        f"{name}（{type_name}）尚未启用默认参数配置，请联系管理员处理",
                    ),
                    failure_code=FAILURE_CODE_DEFAULT_CONFIG_MISSING,
                )
            )
    return items


def _quote_preflight_actions(
    *,
    items: Iterable[Mapping[str, Any]],
    platform_code: str,
    platform_name: str,
    selected_account_type_name: Optional[str],
    operator_role_name: Any = "",
) -> List[Dict[str, Any]]:
    codes = {_to_str(item.get("failure_code")).strip() for item in (items or [])}
    actions: List[Dict[str, Any]] = []
    if codes & {FAILURE_CODE_ACCOUNT_MISSING, FAILURE_CODE_ACCOUNT_LOGIN}:
        actions.extend(
            _quote_platform_account_manage_actions(
                operator_role_name,
                platform_code=platform_code,
                platform_name=platform_name,
            )
        )
    if FAILURE_CODE_DEFAULT_CONFIG_MISSING in codes and not _quote_account_needs_admin_contact(operator_role_name):
        actions.append(
            _mk_action(
                "默认参数配置",
                "open_default_config_manager",
                "quote_platform_default_configs",
                platform_code=platform_code,
                platform_name=platform_name,
                account_type_name=_normalize_account_type_name(selected_account_type_name),
            )
        )
    actions.append(_mk_action("查看当前材料状态"))
    if not (
        codes & {FAILURE_CODE_ACCOUNT_MISSING, FAILURE_CODE_ACCOUNT_LOGIN}
        and _quote_account_needs_admin_contact(operator_role_name)
    ):
        actions.append(_mk_action(f"{_to_str(platform_name).strip() or '平台'}报价"))
    return actions


def _build_quote_preflight_blocked_response(
    *,
    case: QuoteCase,
    platform_code: str,
    platform_name: str,
    selected_account_type_name: Optional[str],
    items: Iterable[Mapping[str, Any]],
    merged_entities: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    override_summary: str = "",
    attached_images: Optional[List[Dict[str, Any]]] = None,
    operator_role_name: Any = "",
    trace_id: str = "",
) -> Tuple[str, Dict[str, Any]]:
    safe_items = [dict(item or {}) for item in (items or [])]
    failure_code = _primary_preflight_failure_code(safe_items)
    reply = _format_quote_preflight_reply(
        platform_name=platform_name,
        items=safe_items,
        override_summary=override_summary,
        attached_images=attached_images,
    )
    data_payload = dict(payload or {})
    data_payload["preflight_checklist"] = safe_items
    data_payload["preflight_blocked"] = True
    return _build_quote_user_failure_response(
        reply=reply,
        case=case,
        task=None,
        trace_id=trace_id or _new_trace_id(),
        failure_code=failure_code,
        failure_reason="报价前检查未通过",
        result_status=RESULT_NEED_MORE,
        response_status="success",
        actions=_quote_preflight_actions(
            items=safe_items,
            platform_code=platform_code,
            platform_name=platform_name,
            selected_account_type_name=selected_account_type_name,
            operator_role_name=operator_role_name,
        ),
        payload=data_payload,
        entities={**(merged_entities or {}), "quote_case_id": case.id, "order_id": case.order_id},
    )


def _quote_material_form_command_mode(text: Any) -> str:
    compact = re.sub(r"[\s,，。.;；:：]+", "", _norm_text(text))
    if not compact:
        return ""
    manual_commands = {
        "手工",
        "手工录入",
        "手工填写",
        "手动",
        "手动录入",
        "手动填写",
        "人工",
        "人工录入",
        "人工填写",
    }
    supplement_commands = {
        "补资料",
        "补充资料",
        "补全资料",
        "补材料",
        "补充材料",
        "补全材料",
        "缺什么",
        "缺少什么",
        "改资料",
        "修改资料",
    }
    if compact in manual_commands:
        return "manual"
    if compact in supplement_commands:
        return "supplement"
    return ""


def looks_like_quote_material_form_command(text: Any) -> bool:
    return bool(_quote_material_form_command_mode(text))


def _quote_material_form_required_types_for_key(key: str) -> List[str]:
    out: List[str] = []
    for type_name in QUOTE_ACCOUNT_TYPE_OPTIONS:
        required_keys = set(QUOTE_MATERIAL_FORM_REQUIRED_BY_TYPE.get(type_name) or ())
        if key in required_keys:
            out.append(type_name)
    return out


def _quote_material_form_value(data: Mapping[str, Any], key: str, *, account_type_name: str = "") -> str:
    if key == "account_type_name":
        return _normalize_account_type_name(account_type_name or _json_obj(data).get(key)) or ""
    value = _to_str(_json_obj(data).get(key)).strip()
    if not value and _to_str(key).strip() in QUOTE_MANUAL_EXTRA_CONFIG_FIELD_KEYS:
        quote_field_overrides = _json_obj(_json_obj(data).get("quote_field_overrides"))
        value = _to_str(quote_field_overrides.get(_canonical_quote_config_override_label(key) or key)).strip()
    if key.endswith("_date"):
        return _normalize_quote_date_text(value) or value
    return value


def _quote_material_form_field(
    key: str,
    label: str,
    field_type: str,
    *,
    data: Mapping[str, Any],
    account_type_name: str,
    required: bool = False,
) -> Dict[str, Any]:
    field: Dict[str, Any] = {
        "key": key,
        "label": label,
        "type": field_type,
        "required": bool(required),
        "value": _quote_material_form_value(data, key, account_type_name=account_type_name),
    }
    required_for = _quote_material_form_required_types_for_key(key)
    if required_for:
        field["required_for_account_types"] = required_for
    if key == "account_type_name":
        field["required"] = True
        field["options"] = [{"label": item, "value": item} for item in QUOTE_ACCOUNT_TYPE_OPTIONS]
        field["placeholder"] = "请选择报价类型"
    elif key == "license_type":
        field["options"] = [
            {"label": "02-小型汽车号牌", "value": LICENSE_TYPE_FUEL},
            {"label": "52-小型新能源汽车", "value": LICENSE_TYPE_NEW_ENERGY},
        ]
        field["placeholder"] = "请选择号牌种类"
    elif key == QUOTE_ROAD_RESCUE_LABEL:
        field["placeholder"] = "请输入道路救援次数"
    elif key == QUOTE_EXTERNAL_GRID_LABEL:
        field["placeholder"] = "请输入外部电网故障损失险金额"
    elif field_type == "date":
        field["placeholder"] = "请选择或输入日期"
    else:
        field["placeholder"] = f"请输入{label}"
    return field


def _quote_material_conflict_edit_keys(item: Mapping[str, Any]) -> Tuple[str, ...]:
    key = _to_str(_json_obj(item).get("key")).strip()
    if key == "owner_name_id_name_conflict":
        return ("owner_name", "id_number")
    if key == "vehicle_cert_license_vin_conflict":
        return ("vin",)
    if key == "vehicle_cert_license_engine_conflict":
        return ("engine_no",)
    return ()


def _quote_material_form_payload(
    *,
    mode: str,
    normalized_data: Dict[str, Any],
    missing: List[Dict[str, Any]],
    platform_code: str,
    platform_name: str,
    account_type_name: str,
    session_id: Optional[str] = None,
    quote_case_id: Optional[int] = None,
) -> Dict[str, Any]:
    data = _json_obj(normalized_data)
    selected_type = _normalize_account_type_name(account_type_name or data.get("account_type_name")) or ""
    order_map = {key: (label, field_type) for key, label, field_type in QUOTE_MANUAL_MATERIAL_FIELD_ORDER}
    missing_field_keys: List[str] = []
    conflict_field_keys: List[str] = []
    for item in missing or []:
        item_type = _to_str(item.get("type")).strip()
        item_key = _to_str(item.get("key")).strip()
        if item_type == "field" and item_key:
            missing_field_keys.append(item_key)
        elif item_type == "data_conflict":
            conflict_field_keys.extend(_quote_material_conflict_edit_keys(item))

    core_field_union: List[str] = []
    for type_name in QUOTE_ACCOUNT_TYPE_OPTIONS:
        for key in QUOTE_MATERIAL_FORM_REQUIRED_BY_TYPE.get(type_name, ()):
            if key not in core_field_union:
                core_field_union.append(key)

    if mode == "manual":
        field_keys = [key for key, _, _ in QUOTE_MANUAL_MATERIAL_FIELD_ORDER]
    elif not selected_type or "account_type_name" in missing_field_keys:
        field_keys = [key for key, _, _ in QUOTE_MANUAL_MATERIAL_FIELD_ORDER]
    else:
        field_keys = ["account_type_name"]
        for key in [*core_field_union, *conflict_field_keys]:
            if key not in field_keys:
                field_keys.append(key)

    fields: List[Dict[str, Any]] = []
    missing_set = set(missing_field_keys)
    for key in field_keys:
        label, field_type = order_map.get(key, (key, "text"))
        required = key == "account_type_name" or key in missing_set
        fields.append(
            _quote_material_form_field(
                key,
                label,
                field_type,
                data=data,
                account_type_name=selected_type,
                required=required,
            )
        )

    field_key_set = set(field_keys)
    optional_fields: List[Dict[str, Any]] = []
    selected_extra_field_keys: List[str] = []
    for key, label, field_type in (
        *QUOTE_MANUAL_MATERIAL_FIELD_ORDER,
        *QUOTE_MANUAL_EXTRA_CONFIG_FIELD_ORDER,
    ):
        if key in field_key_set:
            continue
        field = _quote_material_form_field(
            key,
            label,
            field_type,
            data=data,
            account_type_name=selected_type,
            required=False,
        )
        optional_fields.append(field)
        if _to_str(field.get("value")).strip():
            selected_extra_field_keys.append(key)

    missing_texts = [_missing_item_text(item) for item in missing or []]
    title = "手工填写报价资料" if mode == "manual" else "补充报价资料"
    if mode == "supplement" and missing_texts:
        description = "请补齐下列表单字段；提交后会继续沿用当前资料。"
    elif mode == "supplement":
        description = "当前核心字段已基本齐全，如需纠正可直接修改后提交。"
    else:
        description = "请填写客户提供的资料；提交后先保存资料，发起报价时再校验最低必填字段。"
    return {
        "mode": mode,
        "title": title,
        "description": description,
        "platform_code": platform_code or "PICC",
        "platform_name": platform_name or "人保",
        "account_type_name": selected_type,
        "session_id": _to_str(session_id).strip(),
        "quote_case_id": _safe_int(quote_case_id, 0) or None,
        "fields": fields,
        "optional_fields": optional_fields,
        "selected_extra_field_keys": selected_extra_field_keys,
        "missing": missing or [],
        "missing_texts": missing_texts,
        "submit_prefix": "手工资料" if mode == "manual" else "补充资料",
    }


def _quote_material_form_values_from_context(ctx: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(ctx, Mapping) or not ctx.get("quote_material_form_submit"):
        return {}
    values = _json_obj(ctx.get("quote_material_form_values"))
    if not values:
        values = _json_obj(ctx.get("quoteMaterialFormValues"))
    if not values:
        return {}
    allowed = {key for key, _, _ in QUOTE_MANUAL_MATERIAL_FIELD_ORDER} | QUOTE_MANUAL_EXTRA_CONFIG_FIELD_KEYS
    return {
        key: value
        for key, value in values.items()
        if key in allowed and _to_str(value).strip()
    }


def _quote_material_form_overrides_from_values(values: Mapping[str, Any]) -> Dict[str, Any]:
    raw = {
        key: value
        for key, value in _json_obj(values).items()
        if key != "account_type_name" and _to_str(value).strip()
    }
    overrides = _clean_quote_dynamic_data(raw, derive_owner_name=False) if raw else {}
    overrides = _backfill_quote_sales_model_fields(overrides)
    license_type = _normalize_license_type_value(raw.get("license_type"))
    if license_type:
        overrides["license_type"] = license_type
        overrides["license_type_override"] = license_type
    return overrides


def _quote_material_form_config_overrides_from_values(values: Mapping[str, Any]) -> Dict[str, Any]:
    raw: Dict[str, Any] = {}
    for key, value in _json_obj(values).items():
        canonical = _canonical_quote_config_override_label(key)
        if canonical not in QUOTE_MANUAL_EXTRA_CONFIG_FIELD_KEYS:
            continue
        if not _to_str(value).strip():
            continue
        raw[canonical] = value
    return _merge_quote_config_overrides(raw) if raw else {}


async def _quote_case_by_id_for_form(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    quote_case_id: int,
) -> Optional[QuoteCase]:
    if owner_user_id <= 0 or quote_case_id <= 0:
        return None
    stmt = select(QuoteCase).where(
        QuoteCase.id == int(quote_case_id),
        QuoteCase.owner_user_id == int(owner_user_id),
        QuoteCase.status.in_(ACTIVE_CASE_STATUSES),
    )
    if session_id:
        stmt = stmt.where(QuoteCase.session_id == session_id)
    return (await db.execute(stmt.limit(1))).scalars().first()


async def handle_quote_material_form_message(
    db: AsyncSession,
    *,
    ctx: Dict[str, Any],
    entities: Dict[str, Any],
    text: str,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    mode = _quote_material_form_command_mode(text)
    if not mode:
        return None
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        return None
    _ensure_quote_flow_access(ctx)

    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    signal = detect_quote_signal(text)
    merged_entities = {**(entities or {}), **_json_obj(signal.get("entities"))}
    platform_code = _to_str(merged_entities.get("platform_code")).strip().upper()
    platform_name = _to_str(merged_entities.get("platform_name")).strip()

    case = await _latest_active_case(db, owner_user_id=owner_user_id, session_id=session_id)
    if case:
        case = await _lock_quote_case(db, case)
        if not platform_code:
            platform_code = _to_str(case.platform_code).strip().upper()
        if not platform_name:
            platform_name = _to_str(case.platform_name).strip()
    if not platform_code:
        platform_code = "PICC"
    if not platform_name:
        platform_name = _platform_display_name(platform_code) or "人保"

    if not case:
        case = await _get_or_create_case(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            order=None,
            platform_code=platform_code,
            platform_name=platform_name,
            ctx=ctx,
        )
        case = await _lock_quote_case(db, case)
    else:
        changed = False
        if platform_code and _to_str(case.platform_code).strip().upper() != platform_code:
            case.platform_code = platform_code
            changed = True
        if platform_name and _to_str(case.platform_name).strip() != platform_name:
            case.platform_name = platform_name
            changed = True
        if changed:
            case.updated_at = _now()

    images_by_slot = await _active_images_by_slot(db, int(case.id))
    normalized_data = _normalize_quote_case_data(
        base_data=_json_obj(case.normalized_data) or _json_obj(case.draft_order_data),
        order_data={},
        text_data={},
        images_by_slot=images_by_slot,
    )
    vehicle_type_detect = detect_quote_vehicle_type(normalized_data, images_by_slot)
    selected_account_type_name = _normalize_account_type_name(
        merged_entities.get("account_type_name")
        or normalized_data.get("account_type_name")
        or vehicle_type_detect.get("config_type_name")
    )
    missing = _missing_requirements_for_quote_flow(
        normalized_data,
        images_by_slot,
        platform_code=platform_code,
        account_type_name=selected_account_type_name,
    )
    # Opening the form while a wait is active must not rewrite case data: the
    # waiting task is bound to submitted_snapshot fingerprint. Persist only when
    # the case is not waiting for SMS / legacy duplicate confirm.
    if case.status in {CASE_STATUS_WAITING_SMS, CASE_STATUS_WAITING_DUPLICATE_CONFIRM}:
        normalized_data = _json_obj(case.normalized_data) or _json_obj(case.draft_order_data) or normalized_data
        missing = _json_list(case.missing_requirements) or missing
        selected_account_type_name = _normalize_account_type_name(
            merged_entities.get("account_type_name")
            or normalized_data.get("account_type_name")
            or selected_account_type_name
        )
    else:
        case.normalized_data = normalized_data
        case.draft_order_data = normalized_data
        case.missing_requirements = missing
        if case.status not in {CASE_STATUS_QUOTED}:
            case.status = CASE_STATUS_READY if not missing else CASE_STATUS_COLLECTING
    case.updated_at = _now()

    form_payload = _quote_material_form_payload(
        mode=mode,
        normalized_data=normalized_data,
        missing=missing,
        platform_code=platform_code,
        platform_name=platform_name,
        account_type_name=selected_account_type_name,
        session_id=session_id,
        quote_case_id=_safe_int(case.id, 0),
    )
    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=missing,
        attached_images=[],
        platform_account=None,
    )
    payload.update(
        {
            "quote_material_form": form_payload,
            "ui_visible": False,
            "silent": True,
        }
    )
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="chat",
        role="user",
        content=text,
        payload={"quote_material_form_requested": True, "mode": mode, "missing": missing},
    )
    await db.flush()

    data = _mk_data(
        result_status=RESULT_NEED_MORE if form_payload.get("missing") else RESULT_NOT_READY,
        message="请填写报价资料表单",
        entities={"quote_case_id": case.id, "order_id": case.order_id},
        payload=payload,
    )
    data["silent"] = True
    data["ui_visible"] = False
    return (
        "",
        {
            "status": "success",
            "intent": "quote_material_form",
            "trace_id": _new_trace_id(),
            "silent": True,
            "ui_visible": False,
            "data": data,
            "actions": [],
        },
    )


_MATERIAL_DETAIL_LABELS = {
    "owner_name": "行驶证所有人",
    "id_name": "身份证姓名",
    "vehicle_cert_vin": "车辆合格证车架号",
    "driving_license_vin": "行驶证车架号",
    "vehicle_cert_engine_no": "车辆合格证发动机号",
    "driving_license_engine_no": "行驶证发动机号",
}


def _format_missing_detail(detail: Any) -> str:
    if isinstance(detail, dict):
        parts: List[str] = []
        for key, value in detail.items():
            text = _to_str(value).strip()
            if not text:
                continue
            parts.append(f"{_MATERIAL_DETAIL_LABELS.get(str(key), str(key))}：{text}")
        # 材料详情里的 VIN/发动机号/车牌可能包含字母数字，不能走通用错误脱敏，
        # 否则会把真实业务值误清成空。
        return "；".join(parts)
    if isinstance(detail, list):
        parts = [_format_missing_detail(item) for item in detail]
        return sanitize_quote_user_message("；".join(part for part in parts if part))
    return sanitize_quote_user_message(detail)


def _material_image_identity(image: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "storage_key": _to_str(image.get("storage_key")).strip(),
        "md5": _to_str(image.get("md5")).strip(),
        "confirmed_slot_key": _to_str(image.get("confirmed_slot_key")).strip(),
        "predicted_slot_key": _to_str(image.get("predicted_slot_key")).strip(),
        "method": _to_str(image.get("method")).strip(),
    }


def _quote_material_fingerprint(
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
) -> str:
    data = _compact_quote_data(_clean_quote_dynamic_data(_json_obj(normalized_data)))
    image_payload: Dict[str, List[Dict[str, Any]]] = {}
    for slot_key, rows in sorted((images_by_slot or {}).items(), key=lambda item: str(item[0])):
        identities = [_material_image_identity(_json_obj(row)) for row in (rows or [])]
        identities = [item for item in identities if item.get("storage_key") or item.get("md5")]
        identities.sort(key=lambda item: (item.get("confirmed_slot_key") or "", item.get("storage_key") or "", item.get("md5") or ""))
        if identities:
            image_payload[_to_str(slot_key).strip()] = identities
    return _sha256_json({"normalized_data": data, "images_by_slot": image_payload})


def _snapshot_material_fingerprint(snapshot: Dict[str, Any]) -> str:
    snap = _json_obj(snapshot)
    existing = _to_str(snap.get("material_fingerprint")).strip()
    if existing:
        return existing
    return _quote_material_fingerprint(
        _json_obj(snap.get("normalized_data")),
        _json_obj(snap.get("images_by_slot")),
    )


def _snapshot_with_material_fingerprint(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    safe_snapshot = dict(_json_obj(snapshot))
    safe_snapshot["material_fingerprint"] = _snapshot_material_fingerprint(safe_snapshot)
    return safe_snapshot


def _compute_snapshot_quote_fingerprint(snapshot: Dict[str, Any]) -> str:
    snap = _json_obj(snapshot)
    quote_case = _json_obj(snap.get("quote_case"))
    platform_default = _json_obj(snap.get("platform_default_config"))
    default_config = _json_obj(platform_default.get("config"))
    quote_params = {
        "material_fingerprint": _snapshot_material_fingerprint(snap),
        "platform_code": _to_str(quote_case.get("platform_code")).strip().upper(),
        "platform_name": _to_str(quote_case.get("platform_name")).strip(),
        "account_type_name": _to_str(platform_default.get("account_type_name")).strip(),
        "resolved_type_name": _to_str(platform_default.get("resolved_type_name")).strip(),
        "default_config_id": default_config.get("id"),
        "default_config_matched": _to_str(platform_default.get("matched")).strip(),
        "default_config_json": _compact_quote_data(_json_obj(snap.get("default_config_json"))),
        "quote_field_overrides": _merge_quote_config_overrides(
            platform_default.get("quote_field_overrides"),
            validate_positive=False,
        ),
        "request_body": _compact_quote_data(_json_obj(snap.get("request_body"))),
    }
    return _sha256_json(quote_params)


def _snapshot_quote_fingerprint(snapshot: Dict[str, Any]) -> str:
    snap = _json_obj(snapshot)
    existing = _to_str(snap.get("quote_fingerprint")).strip()
    if existing:
        return existing
    return _compute_snapshot_quote_fingerprint(snap)


def _snapshot_with_quote_fingerprint(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    safe_snapshot = _snapshot_with_material_fingerprint(snapshot)
    safe_snapshot["quote_fingerprint"] = _compute_snapshot_quote_fingerprint(safe_snapshot)
    return safe_snapshot


async def _current_case_material_fingerprint(db: AsyncSession, case: QuoteCase) -> str:
    images_by_slot = await _active_images_by_slot(db, int(case.id))
    normalized_data = _normalize_quote_case_data(
        base_data=_json_obj(case.normalized_data),
        order_data={},
        text_data={},
        images_by_slot=images_by_slot,
    )
    return _quote_material_fingerprint(normalized_data, images_by_slot)


async def _lock_quote_case(db: AsyncSession, case: QuoteCase) -> QuoteCase:
    case_id = _safe_int(getattr(case, "id", 0), 0)
    if case_id <= 0:
        return case
    locked = (
        await db.execute(
            select(QuoteCase)
            .where(QuoteCase.id == case_id)
            .with_for_update()
        )
    ).scalars().first()
    return locked or case


async def _lock_quote_task(db: AsyncSession, task: QuoteTask) -> QuoteTask:
    task_id = _safe_int(getattr(task, "id", 0), 0)
    if task_id <= 0:
        return task
    locked = (
        await db.execute(
            select(QuoteTask)
            .where(QuoteTask.id == task_id)
            .with_for_update()
        )
    ).scalars().first()
    return locked or task


def _snapshot_payload(
    *,
    case: QuoteCase,
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    snapshot = {
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
    return _snapshot_with_material_fingerprint(snapshot)


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
    vehicle_type_detect = _quote_vehicle_type_detect_safe(normalized_data, images_by_slot)
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
        "vehicle_type_detect": vehicle_type_detect,
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
    for_update: bool = False,
) -> Optional[Tuple[QuoteCase, QuoteTask]]:
    stmt = (
        select(QuoteCase, QuoteTask)
        .join(QuoteTask, QuoteTask.quote_case_id == QuoteCase.id)
        .where(
            QuoteCase.owner_user_id == owner_user_id,
            QuoteTask.status == TASK_STATUS_WAITING_SMS,
            QuoteTask.login_state == "sms_required",
        )
    )
    if not include_expired:
        stmt = stmt.where(QuoteCase.status == CASE_STATUS_WAITING_SMS)
    if session_id:
        stmt = stmt.where(QuoteCase.session_id == session_id)
    stmt = stmt.order_by(desc(QuoteTask.id)).limit(1)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).first()
    if not row:
        return None
    task = row[1]
    if not include_expired and _is_sms_task_expired(task):
        return None
    return row[0], row[1]


async def _find_waiting_duplicate_quote_confirm_task(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    for_update: bool = False,
) -> Optional[Tuple[QuoteCase, QuoteTask]]:
    stmt = (
        select(QuoteCase, QuoteTask)
        .join(QuoteTask, QuoteTask.quote_case_id == QuoteCase.id)
        .where(
            QuoteCase.owner_user_id == owner_user_id,
            QuoteCase.status == CASE_STATUS_WAITING_DUPLICATE_CONFIRM,
            QuoteTask.status == TASK_STATUS_WAITING_DUPLICATE_CONFIRM,
        )
    )
    if session_id:
        stmt = stmt.where(QuoteCase.session_id == session_id)
    stmt = stmt.order_by(desc(QuoteTask.id)).limit(1)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).first()
    return (row[0], row[1]) if row else None


async def _cancel_orphaned_waiting_duplicate_confirm_tasks(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    for_update: bool = False,
) -> int:
    """Cancel legacy duplicate-confirm tasks whose Case is no longer waiting.

    New quotes no longer create waiting_duplicate_confirm. Older rows can drift
    when Case status was rewritten without cancelling the Task; the paired
    finder then never recovers them.
    """

    if owner_user_id <= 0:
        return 0
    stmt = (
        select(QuoteCase, QuoteTask)
        .join(QuoteTask, QuoteTask.quote_case_id == QuoteCase.id)
        .where(
            QuoteCase.owner_user_id == owner_user_id,
            QuoteTask.status == TASK_STATUS_WAITING_DUPLICATE_CONFIRM,
            QuoteCase.status != CASE_STATUS_WAITING_DUPLICATE_CONFIRM,
        )
        .order_by(desc(QuoteTask.id))
    )
    if session_id:
        stmt = stmt.where(QuoteCase.session_id == session_id)
    if for_update:
        stmt = stmt.with_for_update()
    rows = (await db.execute(stmt)).all()
    cancelled = 0
    ts = _now()
    for case, task in rows:
        await _mark_quote_task_cancelled(
            db,
            task=task,
            reason="遗留重复投保确认已失效",
            now=ts,
            response_extra={"orphaned_duplicate_confirm_cancelled": True},
        )
        if case.current_task_id == task.id:
            case.current_task_id = None
            case.updated_at = ts
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={
                "task_id": task.id,
                "status": TASK_STATUS_CANCELLED,
                "orphaned_duplicate_confirm_cancelled": True,
                "case_status": case.status,
            },
        )
        cancelled += 1
    if cancelled:
        await db.flush()
    return cancelled


def _is_sms_task_expired(task: QuoteTask) -> bool:
    base = getattr(task, "started_at", None) or getattr(task, "created_at", None)
    if not isinstance(base, datetime):
        return False
    return (_now() - base).total_seconds() > QUOTE_SMS_CODE_TTL_SECONDS


def _quote_task_platform_account_id(task: QuoteTask) -> int:
    return _safe_int(_json_obj(_json_obj(task.request_payload).get("platform_account")).get("id"), 0)


async def _refresh_quote_case_material_state(
    db: AsyncSession,
    case: QuoteCase,
    *,
    preserve_quoted: bool = True,
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    ts = now or _now()
    images_by_slot = await _active_images_by_slot(db, int(case.id))
    normalized_data = _normalize_quote_case_data(
        base_data=_json_obj(case.normalized_data),
        order_data={},
        text_data={},
        images_by_slot=images_by_slot,
    )
    vehicle_type_detect = detect_quote_vehicle_type(normalized_data, images_by_slot)
    missing = _missing_requirements_for_quote_flow(
        normalized_data,
        images_by_slot,
        platform_code=case.platform_code or "",
        account_type_name=vehicle_type_detect.get("config_type_name"),
    )
    case.normalized_data = normalized_data
    case.draft_order_data = normalized_data
    case.missing_requirements = missing
    if not (preserve_quoted and case.status == CASE_STATUS_QUOTED):
        case.status = CASE_STATUS_READY if not missing else CASE_STATUS_COLLECTING
    case.updated_at = ts
    return normalized_data, images_by_slot, missing


def _quote_task_stale_base_time(task: QuoteTask) -> Optional[datetime]:
    """Clock for stale running cleanup.

    Prefer started_at (then created_at). Do not use updated_at: long platform
    calls may keep bumping updated_at and indefinitely delay expiry.
    """

    for value in (
        getattr(task, "started_at", None),
        getattr(task, "created_at", None),
    ):
        if isinstance(value, datetime):
            return value
    return None


def _quote_task_is_stale(task: QuoteTask, *, now: Optional[datetime] = None) -> bool:
    base = _quote_task_stale_base_time(task)
    if not base:
        return False
    return ((now or _now()) - base).total_seconds() > QUOTE_RUNNING_TASK_STALE_SECONDS


async def _expire_stale_running_quote_tasks_for_owner_session(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    for_update: bool = False,
) -> int:
    """Recover quote attempts left running after a crash/thread stall.

    Quota is intentionally not decremented here: once a platform call may have
    been sent, the safest accounting choice is to reconcile with platform usage
    later rather than undercount a real successful query.
    """

    if owner_user_id <= 0:
        return 0
    stmt = (
        select(QuoteCase, QuoteTask)
        .join(QuoteTask, QuoteTask.quote_case_id == QuoteCase.id)
        .where(
            QuoteCase.owner_user_id == owner_user_id,
            QuoteTask.status.in_(("pending", TASK_STATUS_RUNNING)),
        )
        .order_by(desc(QuoteTask.id))
    )
    if session_id:
        stmt = stmt.where(QuoteCase.session_id == session_id)
    if for_update:
        stmt = stmt.with_for_update()
    rows = (await db.execute(stmt)).all()
    if not rows:
        return 0

    now = _now()
    expired = 0
    for case, task in rows:
        if not _quote_task_is_stale(task, now=now):
            continue
        task.status = TASK_STATUS_FAILED
        task.error_detail = QUOTE_STALE_TIMEOUT_MESSAGE
        task.response_payload = {
            **_json_obj(task.response_payload),
            "stale_running_task_cleaned": True,
            "stale_seconds": QUOTE_RUNNING_TASK_STALE_SECONDS,
            "quota_not_adjusted": True,
            **_quote_failure_fields(
                code=FAILURE_CODE_STALE_TIMEOUT,
                reason=QUOTE_STALE_TIMEOUT_MESSAGE,
            ),
        }
        task.finished_at = now
        task.updated_at = now
        if case.current_task_id == task.id:
            case.current_task_id = None
        await _refresh_quote_case_material_state(db, case, preserve_quoted=True, now=now)
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={
                "task_id": task.id,
                "status": TASK_STATUS_FAILED,
                "reason": task.error_detail,
                "stale_running_task_cleaned": True,
                "failure_code": FAILURE_CODE_STALE_TIMEOUT,
            },
        )
        platform_label = task.platform_name or case.platform_name or case.platform_code or "平台"
        notice = (
            f"{platform_label}报价超时已中止，请重新发起报价；"
            "若反复超时请检查账号会话。"
        )
        await _persist_platform_text_notice_if_recently_absent(
            db,
            case=case,
            owner_user_id=owner_user_id,
            message=notice,
            trace_id=task.trace_id or _new_trace_id(),
            task_id=task.id,
            platform_code=task.platform_code or case.platform_code or "",
            platform_name=platform_label,
            notice_type="stale_timeout_notice",
        )
        expired += 1
    if expired:
        await db.flush()
    return expired


async def has_running_quote_task(db: AsyncSession, ctx: Mapping[str, Any]) -> bool:
    owner_user_id = _ctx_current_user_id(dict(ctx or {}))
    session_id = _to_str((ctx or {}).get("session_id")).strip()
    if owner_user_id <= 0 or not session_id:
        return False
    stmt = (
        select(QuoteTask.id)
        .join(QuoteCase, QuoteTask.quote_case_id == QuoteCase.id)
        .where(
            QuoteCase.owner_user_id == owner_user_id,
            QuoteCase.session_id == session_id,
            QuoteTask.status.in_(("pending", TASK_STATUS_RUNNING)),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _expire_waiting_sms_task(
    db: AsyncSession,
    *,
    case: QuoteCase,
    task: QuoteTask,
    owner_user_id: int,
    reason: str = QUOTE_SMS_EXPIRED_MESSAGE,
    now: Optional[datetime] = None,
) -> None:
    ts = now or _now()
    task.status = TASK_STATUS_FAILED
    task.login_state = "failed"
    task.error_detail = reason
    task.finished_at = ts
    task.updated_at = ts
    if case.current_task_id == task.id:
        case.current_task_id = None
    await _refresh_quote_case_material_state(db, case, preserve_quoted=True, now=ts)
    account_id = _quote_task_platform_account_id(task)
    if account_id:
        account = await _get_platform_account_profile_by_id(db, account_id=account_id)
        if account and account.login_status in {ACCOUNT_LOGIN_LOGGING_IN, ACCOUNT_LOGIN_NEEDS_CODE}:
            account.login_status = ACCOUNT_LOGIN_EXPIRED
            account.last_error = reason
            account.last_check_at = ts
            account.updated_at = ts
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={"task_id": task.id, "status": TASK_STATUS_FAILED, "reason": reason},
    )


async def _expire_stale_waiting_sms_tasks_for_owner_session(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    for_update: bool = False,
) -> int:
    if owner_user_id <= 0:
        return 0
    stmt = (
        select(QuoteCase, QuoteTask)
        .join(QuoteTask, QuoteTask.quote_case_id == QuoteCase.id)
        .where(
            QuoteCase.owner_user_id == owner_user_id,
            QuoteTask.status == TASK_STATUS_WAITING_SMS,
            QuoteTask.login_state == "sms_required",
        )
        .order_by(desc(QuoteTask.id))
    )
    if session_id:
        stmt = stmt.where(QuoteCase.session_id == session_id)
    if for_update:
        stmt = stmt.with_for_update()
    rows = (await db.execute(stmt)).all()
    expired = 0
    ts = _now()
    for case, task in rows:
        if not _is_sms_task_expired(task):
            continue
        await _expire_waiting_sms_task(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            reason=QUOTE_SMS_EXPIRED_MESSAGE,
            now=ts,
        )
        platform_label = task.platform_name or case.platform_name or case.platform_code or "平台"
        await _persist_platform_text_notice_if_recently_absent(
            db,
            case=case,
            owner_user_id=owner_user_id,
            message=(
                f"{platform_label}验证码已过期，旧验证码已作废。"
                f"请重新发送“{platform_label}报价”获取新验证码。"
            ),
            trace_id=task.trace_id or _new_trace_id(),
            task_id=task.id,
            platform_code=task.platform_code or case.platform_code or "",
            platform_name=platform_label,
            notice_type="sms_expired_notice",
        )
        expired += 1
    if expired:
        await db.flush()
    return expired


def _is_recent_invalid_sms_task(task: QuoteTask) -> bool:
    base = getattr(task, "finished_at", None) or getattr(task, "updated_at", None) or getattr(task, "started_at", None)
    if not isinstance(base, datetime):
        return True
    return (_now() - base).total_seconds() <= max(QUOTE_SMS_CODE_TTL_SECONDS * 2, 600)


async def _find_recent_invalid_sms_task(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
) -> Optional[Tuple[QuoteCase, QuoteTask]]:
    if owner_user_id <= 0:
        return None
    stmt = (
        select(QuoteCase, QuoteTask)
        .join(QuoteTask, QuoteTask.quote_case_id == QuoteCase.id)
        .where(
            QuoteCase.owner_user_id == owner_user_id,
            QuoteTask.status.in_((TASK_STATUS_FAILED, TASK_STATUS_CANCELLED)),
            QuoteTask.login_state == "failed",
            QuoteTask.error_detail.in_(
                (
                    QUOTE_SMS_EXPIRED_MESSAGE,
                    "短信验证码已过期",
                    "cancelled_by_material_change",
                    "cancelled_by_image_recall",
                    QUOTE_MATERIAL_CHANGED_MESSAGE,
                )
            ),
        )
        .order_by(desc(QuoteTask.id))
        .limit(5)
    )
    if session_id:
        stmt = stmt.where(QuoteCase.session_id == session_id)
    rows = (await db.execute(stmt)).all()
    for case, task in rows:
        if _is_recent_invalid_sms_task(task):
            return case, task
    return None


async def _quote_snapshot_material_is_current(
    db: AsyncSession,
    *,
    case: QuoteCase,
    snapshot: Dict[str, Any],
) -> bool:
    submitted = _snapshot_material_fingerprint(snapshot)
    if not submitted:
        return True
    try:
        await db.refresh(case)
    except Exception:
        pass
    current = await _current_case_material_fingerprint(db, case)
    return current == submitted


async def _quote_snapshot_default_config_is_current(
    db: AsyncSession,
    *,
    snapshot: Dict[str, Any],
    platform_code: str = "",
) -> bool:
    snap = _json_obj(snapshot)
    platform_default = _json_obj(snap.get("platform_default_config"))
    if not platform_default and not _json_obj(snap.get("default_config_json")):
        return True
    quote_case = _json_obj(snap.get("quote_case"))
    code = _to_str(platform_code or quote_case.get("platform_code")).strip().upper()
    if not code:
        return True
    resolved_type = _normalize_account_type_name(
        platform_default.get("resolved_type_name") or platform_default.get("account_type_name")
    )
    if not resolved_type:
        return True
    try:
        refreshed = await apply_platform_default_config_to_snapshot(
            db,
            snapshot=snap,
            platform_code=code,
            account_type_name=platform_default.get("account_type_name"),
            config_type_name=resolved_type,
        )
    except ValueError:
        return False
    current_platform_default = _json_obj(refreshed.get("platform_default_config"))
    if _to_str(current_platform_default.get("matched")).strip() != _to_str(platform_default.get("matched")).strip():
        return False
    return _compute_snapshot_quote_fingerprint(snap) == _compute_snapshot_quote_fingerprint(refreshed)


async def _stop_quote_for_material_change(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    platform_name: str,
    trace_id: str,
    task: Optional[QuoteTask] = None,
    platform_account: Optional[QuotePlatformAccountProfile] = None,
    quota_reservation: Optional[Dict[str, Any]] = None,
    response_payload: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    if platform_account is None and task is not None:
        account_id = _quote_task_platform_account_id(task)
        if account_id > 0:
            platform_account = await _get_platform_account_profile_by_id(db, account_id=account_id)
    await _release_account_quota_reservation(db, account=platform_account, reservation=quota_reservation or {})
    normalized_data, images_by_slot, missing = await _refresh_quote_case_material_state(
        db,
        case,
        preserve_quoted=False,
    )
    case.current_task_id = task.id if task is not None else None
    if task is not None:
        task.status = TASK_STATUS_FAILED
        if task.login_state != "authenticated":
            task.login_state = "failed"
        task.error_detail = QUOTE_MATERIAL_CHANGED_MESSAGE
        task.response_payload = {
            **_json_obj(task.response_payload),
            **_json_obj(response_payload),
            "material_changed": True,
        }
        task.result_payload = {}
        task.finished_at = _now()
        task.updated_at = _now()
    if platform_account and platform_account.login_status in {ACCOUNT_LOGIN_LOGGING_IN, ACCOUNT_LOGIN_NEEDS_CODE}:
        platform_account.login_status = ACCOUNT_LOGIN_EXPIRED
        platform_account.last_error = QUOTE_MATERIAL_CHANGED_MESSAGE
        platform_account.last_check_at = _now()
        platform_account.updated_at = _now()
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id if task is not None else None,
            "status": TASK_STATUS_FAILED,
            "trace_id": trace_id,
            "reason": QUOTE_MATERIAL_CHANGED_MESSAGE,
        },
    )
    await db.flush()
    platform_label = platform_name or case.platform_name or case.platform_code or "平台"
    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=missing,
        task=task,
        platform_account=platform_account,
    )
    payload["material_changed"] = True
    return _build_quote_user_failure_response(
        reply=f"{platform_label}报价材料已更新，我已停止本次报价；请确认材料后重新发起报价。",
        case=case,
        task=task,
        trace_id=trace_id,
        failure_code=FAILURE_CODE_MATERIAL_CHANGED,
        failure_reason=QUOTE_MATERIAL_CHANGED_MESSAGE,
        result_status=RESULT_NOT_READY,
        response_status="success",
        actions=[_mk_action(f"{platform_label}报价"), _mk_action("查看当前材料状态")],
        payload=payload,
    )


async def _stop_quote_for_default_config_change(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    platform_name: str,
    trace_id: str,
    task: Optional[QuoteTask] = None,
    platform_account: Optional[QuotePlatformAccountProfile] = None,
    response_payload: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    if platform_account is None and task is not None:
        account_id = _quote_task_platform_account_id(task)
        if account_id > 0:
            platform_account = await _get_platform_account_profile_by_id(db, account_id=account_id)
    normalized_data, images_by_slot, missing = await _refresh_quote_case_material_state(
        db,
        case,
        preserve_quoted=False,
    )
    case.status = CASE_STATUS_READY
    case.current_task_id = None
    case.updated_at = _now()
    if task is not None:
        task.status = TASK_STATUS_FAILED
        task.login_state = "failed"
        task.error_detail = QUOTE_DEFAULT_CONFIG_CHANGED_MESSAGE
        task.response_payload = {
            **_json_obj(task.response_payload),
            **_json_obj(response_payload),
            "default_config_changed": True,
        }
        task.result_payload = {}
        task.finished_at = _now()
        task.updated_at = _now()
    if platform_account and platform_account.login_status in {ACCOUNT_LOGIN_LOGGING_IN, ACCOUNT_LOGIN_NEEDS_CODE}:
        platform_account.login_status = ACCOUNT_LOGIN_EXPIRED
        platform_account.last_error = QUOTE_DEFAULT_CONFIG_CHANGED_MESSAGE
        platform_account.last_check_at = _now()
        platform_account.updated_at = _now()
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id if task is not None else None,
            "status": TASK_STATUS_FAILED,
            "trace_id": trace_id,
            "reason": QUOTE_DEFAULT_CONFIG_CHANGED_MESSAGE,
        },
    )
    await db.flush()
    platform_label = platform_name or case.platform_name or case.platform_code or "平台"
    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=missing,
        task=task,
        platform_account=platform_account,
    )
    payload["default_config_changed"] = True
    return _build_quote_user_failure_response(
        reply=(
            f"{platform_label}默认参数已更新，我已停止本次报价；"
            f"{QUOTE_DEFAULT_CONFIG_CHANGED_MESSAGE}。"
        ),
        case=case,
        task=task,
        trace_id=trace_id,
        failure_code=FAILURE_CODE_DEFAULT_CONFIG_CHANGED,
        failure_reason=QUOTE_DEFAULT_CONFIG_CHANGED_MESSAGE,
        result_status=RESULT_NOT_READY,
        response_status="success",
        actions=[_mk_action(f"{platform_label}报价"), _mk_action("查看当前材料状态")],
        payload=payload,
    )


def _is_explicit_requote(text: Any) -> bool:
    compact = re.sub(r"\s+", "", _to_str(text))
    if not compact:
        return False
    return bool(re.search(r"(重新报价|再次报价|再报价|重报|重新发起报价|强制报价)", compact))


def _task_material_fingerprint(task: QuoteTask) -> str:
    return _snapshot_material_fingerprint(_json_obj(getattr(task, "submitted_snapshot", None)))


def _task_quote_fingerprint(task: QuoteTask) -> str:
    snapshot = _json_obj(getattr(task, "submitted_snapshot", None))
    if not snapshot:
        return ""
    if not (
        _to_str(snapshot.get("quote_fingerprint")).strip()
        or snapshot.get("default_config_json")
        or snapshot.get("platform_default_config")
        or snapshot.get("request_body")
    ):
        return ""
    return _snapshot_quote_fingerprint(snapshot)


async def _latest_quote_task_for_material(
    db: AsyncSession,
    *,
    case: QuoteCase,
    platform_code: str,
    material_fingerprint: str,
    quote_fingerprint: str = "",
    statuses: Iterable[str] = (TASK_STATUS_SUCCESS,),
) -> Optional[QuoteTask]:
    case_id = _safe_int(getattr(case, "id", 0), 0)
    code = _to_str(platform_code or case.platform_code).strip().upper()
    fingerprint = _to_str(material_fingerprint).strip()
    if case_id <= 0 or not code or not fingerprint:
        return None
    rows = (
        await db.execute(
            select(QuoteTask)
            .where(
                QuoteTask.quote_case_id == case_id,
                QuoteTask.platform_code == code,
                QuoteTask.status.in_(tuple(statuses)),
            )
            .order_by(desc(QuoteTask.id))
            .limit(30)
        )
    ).scalars().all()
    for task in rows:
        if not await _stored_quote_task_has_real_result(db, task):
            continue
        task_quote_fingerprint = _task_quote_fingerprint(task)
        if quote_fingerprint and task_quote_fingerprint:
            if task_quote_fingerprint == quote_fingerprint:
                return task
            continue
        if _task_material_fingerprint(task) == fingerprint:
            return task
    return None


def _quote_result_lines(result: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    plate_no = _to_str(result.get("plate_no")).strip()
    owner_name = _to_str(result.get("owner_name")).strip()
    if plate_no:
        lines.append(f"车牌：{plate_no}")
    if owner_name:
        lines.append(f"车主：{owner_name}")
    for item in _json_list(result.get("price_items"))[:5]:
        row = _json_obj(item)
        name = _to_str(row.get("name")).strip()
        amount = row.get("amount")
        if not name or amount in (None, ""):
            continue
        try:
            amount_text = f"{float(amount):.2f}"
        except Exception:
            amount_text = _to_str(amount)
        lines.append(f"{name}：{amount_text}")
    total = result.get("premium_total")
    if total not in (None, ""):
        try:
            total_text = f"{float(total):.2f}"
        except Exception:
            total_text = _to_str(total)
        lines.append(f"合计：{total_text}")
    return lines


def _quote_result_reply_text(result: Dict[str, Any], *, platform_name: str, account_label: str = "") -> str:
    card = _json_obj(result.get("result_card"))
    result_platform_code = _to_str(result.get("platform_code") or card.get("platform_code")).strip().upper()
    result_platform_name = _to_str(result.get("platform_name") or card.get("platform_name") or platform_name).strip()
    risk_score = _to_str(result.get("risk_score")).strip() or _to_str(card.get("risk_score")).strip()
    display_name = result_platform_name or platform_name or "平台"
    if result_platform_code == "PICC" or display_name in {"人保", "中国人保", "PICC"}:
        display_name = "人保"
    return f"{display_name}风险水平：{risk_score or '-'} 分"


def _quote_result_insurance_date_auto_adjustments(result: Mapping[str, Any]) -> Dict[str, str]:
    """Return platform-auto-adjusted start dates that should persist for later requotes."""
    adjustments: Dict[str, str] = {}

    def collect(notice_any: Any) -> None:
        notice = _json_obj(notice_any)
        if _to_str(notice.get("type")).strip() != "insurance_date_adjust":
            return
        bi_day = _normalize_quote_date_text(notice.get("commercial_start_date"))
        ci_day = _normalize_quote_date_text(notice.get("compulsory_start_date"))
        if bi_day:
            adjustments["commercial_start_date"] = bi_day
            if _quote_period_time_explicit(notice.get("commercial_start_hour"), notice.get("commercial_start_minute")):
                hour, minute = _quote_period_time_texts(notice.get("commercial_start_hour"), notice.get("commercial_start_minute"))
                adjustments["commercial_start_hour"] = hour
                adjustments["commercial_start_minute"] = minute
        if ci_day:
            adjustments["compulsory_start_date"] = ci_day
            if _quote_period_time_explicit(notice.get("compulsory_start_hour"), notice.get("compulsory_start_minute")):
                hour, minute = _quote_period_time_texts(notice.get("compulsory_start_hour"), notice.get("compulsory_start_minute"))
                adjustments["compulsory_start_hour"] = hour
                adjustments["compulsory_start_minute"] = minute

    for notice_any in _json_list(_json_obj(result).get("platform_auto_notices")):
        collect(notice_any)

    request_body = _json_obj(_json_obj(result).get("request_body"))
    preflight = _json_obj(request_body.get("preflight"))
    collect(preflight.get("insuranceDateAutoAdjusted"))
    return adjustments


def _quote_snapshot_optional_renewal_defaults(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    defaults = _json_obj(_json_obj(snapshot).get("default_config_json"))
    overrides: Dict[str, Any] = {}

    road_rescue_raw = _to_str(defaults.get("机动车增值服务特约条款（道路救援服务）")).strip()
    if road_rescue_raw:
        road_rescue_quantity = _safe_int(road_rescue_raw, 0)
        if road_rescue_quantity > 0:
            overrides["机动车增值服务特约条款（道路救援服务）"] = str(road_rescue_quantity)

    external_grid_raw = _to_str(defaults.get("附加外部电网故障损失险")).strip()
    if external_grid_raw:
        numeric = external_grid_raw.replace(",", "").replace("元", "").strip()
        try:
            amount = Decimal(numeric)
        except Exception:
            amount = None
        if amount is not None and amount > 0:
            overrides["附加外部电网故障损失险"] = _quote_money_text(amount)
        elif amount is None and external_grid_raw not in {"0", "0.0", "0.00"}:
            overrides["附加外部电网故障损失险"] = external_grid_raw

    return overrides


def _quote_snapshot_with_auto_adjusted_dates(
    snapshot: Mapping[str, Any],
    adjustments: Mapping[str, Any],
    *,
    adjusted_request_body: Any = None,
) -> Dict[str, Any]:
    safe_snapshot = deepcopy(_json_obj(snapshot))
    normalized = dict(_json_obj(safe_snapshot.get("normalized_data")))
    changed = False
    normalized_updates = {
        "commercial_start_date": _normalize_quote_date_text(adjustments.get("commercial_start_date")),
        "compulsory_start_date": _normalize_quote_date_text(adjustments.get("compulsory_start_date")),
    }
    if _quote_period_time_explicit(adjustments.get("commercial_start_hour"), adjustments.get("commercial_start_minute")):
        hour, minute = _quote_period_time_texts(adjustments.get("commercial_start_hour"), adjustments.get("commercial_start_minute"))
        normalized_updates["commercial_start_hour"] = hour
        normalized_updates["commercial_start_minute"] = minute
    if _quote_period_time_explicit(adjustments.get("compulsory_start_hour"), adjustments.get("compulsory_start_minute")):
        hour, minute = _quote_period_time_texts(adjustments.get("compulsory_start_hour"), adjustments.get("compulsory_start_minute"))
        normalized_updates["compulsory_start_hour"] = hour
        normalized_updates["compulsory_start_minute"] = minute
    for key, value in normalized_updates.items():
        if value and _to_str(normalized.get(key)).strip() != value:
            normalized[key] = value
            changed = True
    request_body = deepcopy(_json_obj(adjusted_request_body)) or dict(_json_obj(safe_snapshot.get("request_body")))
    if adjusted_request_body and request_body != _json_obj(safe_snapshot.get("request_body")):
        changed = True
    quote_form = dict(_json_obj(request_body.get("quoteForm")))
    vehicle = dict(_json_obj(request_body.get("vehicleForm")))

    request_body_updates = {
        "commercial_start_date": _normalize_quote_date_text(_quote_first_text(quote_form.get("prpCmain.startDate"), vehicle.get("startDateBI"))),
        "compulsory_start_date": _normalize_quote_date_text(_quote_first_text(quote_form.get("prpCmain.startDateCI"), vehicle.get("startDateCI"))),
    }
    if _quote_period_time_explicit(quote_form.get("prpCmain.starthourbi"), quote_form.get("prpCmain.startminutebi")):
        hour, minute = _quote_period_time_texts(quote_form.get("prpCmain.starthourbi"), quote_form.get("prpCmain.startminutebi"))
        request_body_updates["commercial_start_hour"] = hour
        request_body_updates["commercial_start_minute"] = minute
    elif _quote_period_time_explicit(vehicle.get("startHourBI"), vehicle.get("startMinuteBI")):
        hour, minute = _quote_period_time_texts(vehicle.get("startHourBI"), vehicle.get("startMinuteBI"))
        request_body_updates["commercial_start_hour"] = hour
        request_body_updates["commercial_start_minute"] = minute
    if _quote_period_time_explicit(quote_form.get("prpCmain.starthourci"), quote_form.get("prpCmain.startminuteci")):
        hour, minute = _quote_period_time_texts(quote_form.get("prpCmain.starthourci"), quote_form.get("prpCmain.startminuteci"))
        request_body_updates["compulsory_start_hour"] = hour
        request_body_updates["compulsory_start_minute"] = minute
    elif _quote_period_time_explicit(vehicle.get("startHourCI"), vehicle.get("startMinuteCI")):
        hour, minute = _quote_period_time_texts(vehicle.get("startHourCI"), vehicle.get("startMinuteCI"))
        request_body_updates["compulsory_start_hour"] = hour
        request_body_updates["compulsory_start_minute"] = minute
    for key, value in request_body_updates.items():
        if value and key not in normalized_updates and _to_str(normalized.get(key)).strip() != value:
            normalized[key] = value
            changed = True

    optional_overrides = _quote_snapshot_optional_renewal_defaults(safe_snapshot)
    if optional_overrides:
        merged_overrides = _merge_quote_config_overrides(
            normalized.get("quote_field_overrides"),
            optional_overrides,
            validate_positive=False,
        )
        if merged_overrides != _json_obj(normalized.get("quote_field_overrides")):
            normalized["quote_field_overrides"] = merged_overrides
            changed = True

    if changed:
        safe_snapshot["normalized_data"] = _clean_quote_dynamic_data(normalized)
    if adjustments.get("commercial_start_date"):
        day = _normalize_quote_date_text(adjustments.get("commercial_start_date"))
        if _to_str(quote_form.get("prpCmain.startDate")).strip() != day:
            quote_form["prpCmain.startDate"] = day
            changed = True
        if _to_str(vehicle.get("startDateBI")).strip() != day:
            vehicle["startDateBI"] = day
            changed = True
        if _quote_period_time_explicit(adjustments.get("commercial_start_hour"), adjustments.get("commercial_start_minute")):
            hour, minute = _quote_period_time_texts(adjustments.get("commercial_start_hour"), adjustments.get("commercial_start_minute"))
            if _safe_int(quote_form.get("prpCmain.starthourbi"), 0) != _safe_int(hour, 0):
                quote_form["prpCmain.starthourbi"] = hour
                changed = True
            if _safe_int(quote_form.get("prpCmain.startminutebi"), 0) != _safe_int(minute, 0):
                quote_form["prpCmain.startminutebi"] = minute
                changed = True
            vehicle["startHourBI"] = hour
            vehicle["startMinuteBI"] = minute
    if adjustments.get("compulsory_start_date"):
        day = _normalize_quote_date_text(adjustments.get("compulsory_start_date"))
        if _to_str(quote_form.get("prpCmain.startDateCI")).strip() != day:
            quote_form["prpCmain.startDateCI"] = day
            changed = True
        if _to_str(vehicle.get("startDateCI")).strip() != day:
            vehicle["startDateCI"] = day
            changed = True
        ci_hour = adjustments.get("compulsory_start_hour")
        ci_minute = adjustments.get("compulsory_start_minute")
        has_ci_time = _quote_period_time_explicit(ci_hour, ci_minute)
        if has_ci_time:
            ci_hour, ci_minute = _quote_period_time_texts(ci_hour, ci_minute)
        else:
            ci_hour, ci_minute = "", ""
        end_day = _quote_ci_end_date_text(day, ci_hour, ci_minute) if has_ci_time else _quote_end_date_text(day)
        if end_day and _to_str(quote_form.get("prpCmain.endDateCI")).strip() != end_day:
            quote_form["prpCmain.endDateCI"] = end_day
            changed = True
        if has_ci_time:
            expected_end_hour = "24" if ci_hour == "0" and ci_minute == "0" else ci_hour
            if _safe_int(quote_form.get("prpCmain.starthourci"), 0) != _safe_int(ci_hour, 0):
                quote_form["prpCmain.starthourci"] = ci_hour
                changed = True
            if _safe_int(quote_form.get("prpCmain.startminuteci"), 0) != _safe_int(ci_minute, 0):
                quote_form["prpCmain.startminuteci"] = ci_minute
                changed = True
            if _safe_int(quote_form.get("prpCmain.endhourci"), 24) != _safe_int(expected_end_hour, 24):
                quote_form["prpCmain.endhourci"] = expected_end_hour
                changed = True
            if _safe_int(quote_form.get("prpCmain.endminuteci"), 0) != _safe_int(ci_minute, 0):
                quote_form["prpCmain.endminuteci"] = ci_minute
                changed = True
            vehicle["startHourCI"] = ci_hour
            vehicle["startMinuteCI"] = ci_minute
    if not changed:
        return safe_snapshot
    if quote_form:
        request_body["quoteForm"] = quote_form
    if vehicle:
        request_body["vehicleForm"] = vehicle
    if request_body:
        safe_snapshot["request_body"] = request_body
    safe_snapshot.pop("material_fingerprint", None)
    safe_snapshot.pop("quote_fingerprint", None)
    return _snapshot_with_quote_fingerprint(safe_snapshot)


def _quote_task_with_final_request_body(task: QuoteTask, result: Mapping[str, Any]) -> bool:
    final_request_body = _json_obj(_json_obj(result).get("request_body"))
    if not final_request_body:
        return False

    changed = False
    request_payload = dict(_json_obj(task.request_payload))
    if _json_obj(request_payload.get("request_body")) != final_request_body:
        request_payload["request_body"] = final_request_body
        task.request_payload = request_payload
        changed = True

    snapshot = deepcopy(_json_obj(task.submitted_snapshot))
    if snapshot and _json_obj(snapshot.get("request_body")) != final_request_body:
        snapshot["request_body"] = final_request_body
        snapshot.pop("quote_fingerprint", None)
        task.submitted_snapshot = _snapshot_with_quote_fingerprint(snapshot)
        changed = True

    if changed:
        task.updated_at = _now()
    return changed


async def _persist_quote_auto_adjusted_dates_to_case(
    db: AsyncSession,
    *,
    case: QuoteCase,
    task: QuoteTask,
    result: Mapping[str, Any],
) -> Dict[str, str]:
    adjustments = _quote_result_insurance_date_auto_adjustments(result)
    request_body_changed = _quote_task_with_final_request_body(task, result)
    next_snapshot = _quote_snapshot_with_auto_adjusted_dates(
        task.submitted_snapshot,
        adjustments,
        adjusted_request_body=_json_obj(_json_obj(result).get("request_body")),
    )
    changed = False
    if next_snapshot != _json_obj(task.submitted_snapshot):
        task.submitted_snapshot = next_snapshot
        final_request_body = _json_obj(next_snapshot.get("request_body"))
        if final_request_body:
            task.request_payload = {
                **_json_obj(task.request_payload),
                "request_body": final_request_body,
            }
        task.updated_at = _now()
        changed = True
    next_normalized = _clean_quote_dynamic_data(_json_obj(next_snapshot.get("normalized_data")))
    if next_normalized and (
        next_normalized != _json_obj(case.normalized_data)
        or next_normalized != _json_obj(case.draft_order_data)
    ):
        case.normalized_data = next_normalized
        case.draft_order_data = deepcopy(next_normalized)
        case.updated_at = _now()
        changed = True

    if changed or request_body_changed:
        await db.flush()
    return adjustments


async def _persist_unemitted_quote_auto_notices(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    result: Mapping[str, Any],
    trace_id: str,
    task_id: Any,
    platform_code: str,
    platform_name: str,
) -> int:
    emitted = 0
    seen: Set[str] = set()
    seen_messages: List[str] = []
    from app.services.ai_assistant_service import db_append_message

    for notice_any in _json_list(_json_obj(result).get("platform_auto_notices"))[:3]:
        notice = _json_obj(notice_any)
        notice_type = _to_str(notice.get("type")).strip() or "platform_notice"
        if notice_type not in {"insurance_date_adjust", "duplicate_quote_notice", "platform_notice"}:
            continue
        message = _sanitize_duplicate_quote_warning(notice.get("message"), "")
        if not message:
            continue
        dedupe_key = _quote_auto_notice_compact_message(message)
        if notice.get("emitted_to_chat") is True:
            if dedupe_key:
                seen.add(dedupe_key)
                seen_messages.append(dedupe_key)
            continue
        if dedupe_key in seen:
            continue
        if any(_quote_auto_notice_message_overlaps(dedupe_key, previous) for previous in seen_messages):
            notice["emitted_to_chat"] = True
            continue
        seen.add(dedupe_key)
        seen_messages.append(dedupe_key)
        stable_key = _quote_auto_notice_dedupe_key(
            trace_id=trace_id,
            task_id=task_id,
            notice_type=notice_type,
            message=message,
        )
        if await _quote_auto_notice_already_persisted(
            db,
            owner_user_id=owner_user_id,
            session_id=case.session_id,
            dedupe_key=stable_key,
            message=message,
            trace_id=trace_id,
        ):
            notice["emitted_to_chat"] = True
            continue
        auto_notice_payload = {
            "type": notice_type,
            "message": message,
            "source": _to_str(notice.get("source")).strip() or "platform_prompt",
            "fallback_persisted": True,
            "dedupe_key": stable_key,
        }
        if notice_type == "insurance_date_adjust":
            auto_notice_payload.update(
                {
                    "commercial_start_date": _to_str(notice.get("commercial_start_date")).strip(),
                    "compulsory_start_date": _to_str(notice.get("compulsory_start_date")).strip(),
                    "adjustment_kinds": [
                        item
                        for item in _json_list(notice.get("adjustment_kinds"))
                        if _to_str(item).strip() in {"bi", "ci"}
                    ],
                }
            )
        elif notice_type == "duplicate_quote_notice":
            auto_notice_payload.update(
                {
                    "duplicateVin": _json_obj(notice.get("duplicateVin")),
                }
            )
        payload = {
            "quote_case": {"id": case.id, "status": CASE_STATUS_READY},
            "quote_task": {"id": _safe_int(task_id, 0), "status": TASK_STATUS_RUNNING, "trace_id": trace_id},
            "platform_code": _to_str(platform_code).strip().upper(),
            "platform_name": _to_str(platform_name).strip() or _platform_display_name(platform_code),
            "platform_auto_notice": auto_notice_payload,
            "ui_visible": True,
        }
        metadata = {
            "status": "success",
            "intent": "quote",
            "trace_id": trace_id,
            "data": _mk_data(
                result_status=RESULT_NOT_READY,
                message=message,
                entities={"quote_case_id": case.id, "quote_task_id": _safe_int(task_id, 0), "order_id": case.order_id},
                payload=payload,
            ),
            "actions": [],
        }
        try:
            async with db.begin_nested():
                await db_append_message(
                    db,
                    owner_user_id=owner_user_id,
                    session_id=case.session_id,
                    role="assistant",
                    content=message,
                    metadata=metadata,
                    message_id=_quote_auto_notice_message_id(stable_key),
                )
                await _add_event(
                    db,
                    case=case,
                    owner_user_id=owner_user_id,
                    event_type="platform_notice",
                    role="assistant",
                    content=redact_quote_sensitive_text(message),
                    payload=payload,
                )
        except IntegrityError as exc:
            if not _is_quote_auto_notice_duplicate_error(exc):
                raise
        notice["emitted_to_chat"] = True
        emitted += 1
    return emitted


def _quote_auto_notice_dedupe_key(
    *,
    trace_id: Any,
    task_id: Any,
    notice_type: Any,
    message: Any,
) -> str:
    source = "|".join(
        (
            _to_str(trace_id).strip(),
            _to_str(task_id).strip(),
            _to_str(notice_type).strip().lower(),
            _quote_auto_notice_compact_message(message),
        )
    )
    return hashlib.sha1(source.encode("utf-8", errors="ignore")).hexdigest()


def _quote_auto_notice_compact_message(message: Any) -> str:
    return re.sub(r"\s+", "", _to_str(message))


def _quote_auto_notice_message_overlaps(left: Any, right: Any) -> bool:
    """Treat a platform prompt summary and its full text as the same chat notice."""
    a = _quote_auto_notice_compact_message(left)
    b = _quote_auto_notice_compact_message(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 24:
        return False
    return a in b or b in a


def _quote_auto_notice_payload_from_metadata(metadata: Any) -> Dict[str, Any]:
    data = _json_obj(_json_obj(metadata).get("data"))
    payload = _json_obj(data.get("payload"))
    return _json_obj(payload.get("platform_auto_notice"))


def _quote_auto_notice_key_from_metadata(metadata: Any) -> str:
    notice = _quote_auto_notice_payload_from_metadata(metadata)
    return _to_str(notice.get("dedupe_key")).strip()


def _quote_auto_notice_message_id(dedupe_key: Any) -> str:
    return f"qa-notice-{_to_str(dedupe_key).strip()}"[:64]


def _is_quote_auto_notice_duplicate_error(exc: IntegrityError) -> bool:
    text = _to_str(exc).lower()
    is_unique_violation = (
        "duplicate" in text
        or "1062" in text
        or "unique constraint" in text
        or "unique violation" in text
    )
    is_auto_notice_message_key = (
        "message_id" in text
        or "uq_quote_assistant_message_id" in text
    )
    return is_unique_violation and is_auto_notice_message_key


async def _quote_auto_notice_already_persisted(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Any,
    dedupe_key: str,
    message: str,
    trace_id: str,
) -> bool:
    """Check durable chat history before the fallback writes a duplicate notice."""
    owner = _safe_int(owner_user_id, 0)
    sid = _to_str(session_id).strip()
    if owner <= 0 or not sid:
        return False
    rows = (
        await db.execute(
            select(
                QuoteAssistantMessage.content,
                QuoteAssistantMessage.metadata_json,
            )
            .where(
                QuoteAssistantMessage.owner_user_id == owner,
                QuoteAssistantMessage.session_id == sid,
                QuoteAssistantMessage.role == "assistant",
            )
            .order_by(desc(QuoteAssistantMessage.id))
            .limit(40)
        )
    ).all()
    compact_message = _quote_auto_notice_compact_message(message)
    if not compact_message:
        return False
    for content, metadata in rows:
        if dedupe_key and _quote_auto_notice_key_from_metadata(metadata) == dedupe_key:
            return True
        metadata_obj = _json_obj(metadata)
        metadata_trace = _to_str(metadata_obj.get("trace_id")).strip()
        same_trace = bool(trace_id and metadata_trace == trace_id)
        row_notice = _quote_auto_notice_payload_from_metadata(metadata_obj)
        if same_trace:
            row_message = _to_str(row_notice.get("message")).strip() or _to_str(content)
            row_compact = _quote_auto_notice_compact_message(row_message)
            if row_compact == compact_message:
                return True
            if row_notice and _quote_auto_notice_message_overlaps(row_compact, compact_message):
                return True
    return False


async def _persist_runtime_auto_notices(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    runtime_result: Optional[PlatformRuntimeResult],
    trace_id: str,
    task_id: Any,
    platform_code: str,
    platform_name: str,
) -> int:
    """Persist notices emitted before an automatic retry even when the retry later fails."""
    runtime_data = _json_obj(getattr(runtime_result, "data", None) if runtime_result is not None else None)
    notices = _json_list(runtime_data.get("platform_auto_notices"))
    if not notices:
        return 0
    return await _persist_unemitted_quote_auto_notices(
        db,
        case=case,
        owner_user_id=owner_user_id,
        result={"platform_auto_notices": notices},
        trace_id=trace_id,
        task_id=task_id,
        platform_code=platform_code,
        platform_name=platform_name,
    )


def _runtime_platform_auto_notice_messages(result: Optional[PlatformRuntimeResult]) -> List[str]:
    data = _json_obj(getattr(result, "data", None) if result is not None else None)
    messages: List[str] = []
    for notice_any in _json_list(data.get("platform_auto_notices"))[:3]:
        notice = _json_obj(notice_any)
        notice_type = _to_str(notice.get("type")).strip() or "platform_notice"
        if notice_type not in {"insurance_date_adjust", "duplicate_quote_notice", "platform_notice"}:
            continue
        message = _sanitize_duplicate_quote_warning(notice.get("message"), "")
        if message:
            messages.append(message)
    return messages


def _runtime_platform_auto_notice_message_for_failure(
    result: Optional[PlatformRuntimeResult],
    error_detail: Any,
) -> str:
    detail_key = _quote_auto_notice_compact_message(error_detail)
    messages = _runtime_platform_auto_notice_messages(result)
    if not messages:
        return ""
    for message in messages:
        message_key = _quote_auto_notice_compact_message(message)
        if detail_key and _quote_auto_notice_message_overlaps(detail_key, message_key):
            return message
    if (
        len(messages) == 1
        and detail_key
        and ("没有返回真实保费明细" in detail_key or "未生成报价结果" in detail_key)
    ):
        return messages[0]
    return ""


async def _persist_platform_text_notice_if_recently_absent(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    message: str,
    trace_id: str,
    task_id: Any,
    platform_code: str,
    platform_name: str,
    notice_type: str = "platform_notice",
) -> bool:
    safe_message = sanitize_quote_user_message(message, "")
    if not safe_message or not case.session_id:
        return False
    stable_key = _quote_auto_notice_dedupe_key(
        trace_id=trace_id,
        task_id=task_id,
        notice_type=notice_type,
        message=safe_message,
    )
    if await _quote_auto_notice_already_persisted(
        db,
        owner_user_id=owner_user_id,
        session_id=case.session_id,
        dedupe_key=stable_key,
        message=safe_message,
        trace_id=trace_id,
    ):
        return False
    from app.services.ai_assistant_service import db_append_message

    payload = {
        "quote_case": {"id": case.id, "status": CASE_STATUS_READY},
        "quote_task": {"id": _safe_int(task_id, 0), "status": TASK_STATUS_RUNNING, "trace_id": trace_id},
        "platform_code": _to_str(platform_code).strip().upper(),
        "platform_name": _to_str(platform_name).strip() or _platform_display_name(platform_code),
        "platform_auto_notice": {
            "type": notice_type,
            "message": safe_message,
            "source": "quote_runtime_retry",
            "dedupe_key": stable_key,
        },
        "ui_visible": True,
    }
    metadata = {
        "status": "success",
        "intent": "quote",
        "trace_id": trace_id,
        "data": _mk_data(
            result_status=RESULT_NOT_READY,
            message=safe_message,
            entities={"quote_case_id": case.id, "quote_task_id": _safe_int(task_id, 0), "order_id": case.order_id},
            payload=payload,
        ),
        "actions": [],
    }
    notice_key = _to_str(notice_type).strip().lower()
    notice_failure_code = FAILURE_CODE_PLATFORM
    if notice_key in {"stale_timeout_notice", "stale_timeout"}:
        notice_failure_code = FAILURE_CODE_STALE_TIMEOUT
    elif notice_key in {"sms_expired_notice", "sms_expired"}:
        notice_failure_code = FAILURE_CODE_SMS_EXPIRED
    elif notice_key in {"duplicate_quote_notice", "duplicate_quote"}:
        notice_failure_code = FAILURE_CODE_DUPLICATE_QUOTE
    _attach_quote_failure(
        metadata["data"],
        code=notice_failure_code,
        reason=safe_message,
    )
    try:
        async with db.begin_nested():
            await db_append_message(
                db,
                owner_user_id=owner_user_id,
                session_id=case.session_id,
                role="assistant",
                content=safe_message,
                metadata=metadata,
                message_id=_quote_auto_notice_message_id(stable_key),
            )
            await _add_event(
                db,
                case=case,
                owner_user_id=owner_user_id,
                event_type="platform_notice",
                role="assistant",
                content=redact_quote_sensitive_text(safe_message),
                payload=payload,
            )
    except IntegrityError as exc:
        if _is_quote_auto_notice_duplicate_error(exc):
            return False
        raise
    await db.flush()
    return True


def _attach_quote_auto_notice_callback(
    platform_ctx: PlatformAccountContext,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    case_id: Any,
    task_id: Any,
    trace_id: str,
    platform_code: str,
    platform_name: str,
) -> PlatformAccountContext:
    """Publish a platform prompt before the worker continues an automatic retry.

    Both quote entry paths commit the case, task, and user message before the
    synchronous PICC worker starts. A short independent transaction can
    therefore persist the raw prompt in chat first; if it ever fails, the main
    quote transaction retains the notice in the runtime result as a fallback.
    """
    owner = _safe_int(owner_user_id, 0)
    sid = _to_str(session_id).strip()
    if owner <= 0 or not sid or _safe_int(case_id, 0) <= 0 or _safe_int(task_id, 0) <= 0:
        return platform_ctx
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return platform_ctx

    emitted_keys: Set[str] = set()

    async def persist_notice(notice_any: Any) -> bool:
        notice = _json_obj(notice_any)
        notice_type = _to_str(notice.get("type")).strip() or "platform_notice"
        if notice_type not in {"insurance_date_adjust", "duplicate_quote_notice", "platform_notice"}:
            return False
        message = _sanitize_duplicate_quote_warning(notice.get("message"), "")
        if not message:
            return False
        stable_key = _quote_auto_notice_dedupe_key(
            trace_id=trace_id,
            task_id=task_id,
            notice_type=notice_type,
            message=message,
        )

        from app.core.db import async_session_factory
        from app.services.ai_assistant_service import db_append_message
        async with async_session_factory() as notice_db:
            try:
                if await _quote_auto_notice_already_persisted(
                    notice_db,
                    owner_user_id=owner,
                    session_id=sid,
                    dedupe_key=stable_key,
                    message=message,
                    trace_id=trace_id,
                ):
                    return True
                auto_notice_payload = {
                    "type": notice_type,
                    "message": message,
                    "source": _to_str(notice.get("source")).strip() or "platform_prompt",
                    "dedupe_key": stable_key,
                }
                if notice_type == "insurance_date_adjust":
                    auto_notice_payload.update(
                        {
                            "commercial_start_date": _to_str(notice.get("commercial_start_date")).strip(),
                            "compulsory_start_date": _to_str(notice.get("compulsory_start_date")).strip(),
                            "adjustment_kinds": [
                                item
                                for item in _json_list(notice.get("adjustment_kinds"))
                                if _to_str(item).strip() in {"bi", "ci"}
                            ],
                        }
                    )
                elif notice_type == "duplicate_quote_notice":
                    auto_notice_payload["duplicateVin"] = _json_obj(notice.get("duplicateVin"))

                payload = {
                    "quote_case": {"id": _safe_int(case_id, 0), "status": CASE_STATUS_READY},
                    "quote_task": {"id": _safe_int(task_id, 0), "status": TASK_STATUS_RUNNING, "trace_id": trace_id},
                    "platform_code": _to_str(platform_code).strip().upper(),
                    "platform_name": _to_str(platform_name).strip() or _platform_display_name(platform_code),
                    "platform_auto_notice": auto_notice_payload,
                    "ui_visible": True,
                }
                metadata = {
                    "status": "success",
                    "intent": "quote",
                    "trace_id": trace_id,
                    "data": _mk_data(
                        result_status=RESULT_NOT_READY,
                        message=message,
                        entities={"quote_case_id": _safe_int(case_id, 0), "quote_task_id": _safe_int(task_id, 0)},
                        payload=payload,
                    ),
                    "actions": [],
                }
                await db_append_message(
                    notice_db,
                    owner_user_id=owner,
                    session_id=sid,
                    role="assistant",
                    content=message,
                    metadata=metadata,
                    message_id=_quote_auto_notice_message_id(stable_key),
                )
                notice_db.add(
                    QuoteCaseEvent(
                        quote_case_id=_safe_int(case_id, 0),
                        owner_user_id=owner,
                        session_id=sid,
                        event_type="platform_notice",
                        role="assistant",
                        content=redact_quote_sensitive_text(message),
                        payload=payload,
                    )
                )
                await notice_db.commit()
                return True
            except IntegrityError as exc:
                await notice_db.rollback()
                if _is_quote_auto_notice_duplicate_error(exc):
                    return True
                raise
            except Exception:
                await notice_db.rollback()
                raise

    def auto_notice_callback(notice_any: Any) -> bool:
        notice = _json_obj(notice_any)
        message = sanitize_quote_user_message(notice.get("message"), "")
        if not message:
            return False
        key = hashlib.sha1(
            "|".join(
                [
                    _to_str(case_id),
                    _to_str(task_id),
                    _to_str(notice.get("type")).strip(),
                    re.sub(r"\s+", "", message),
                ]
            ).encode("utf-8", errors="ignore")
        ).hexdigest()
        if key in emitted_keys:
            return True
        try:
            future = asyncio.run_coroutine_threadsafe(persist_notice(notice), loop)
            # The worker must publish the raw platform prompt before retrying.
            # The completed runtime result also carries the notice for durable
            # fallback persistence if this independent write ever fails.
            ok = bool(future.result(timeout=3))
            if ok:
                emitted_keys.add(key)
            return ok
        except Exception as exc:
            logger.warning(
                "quote auto notice emit failed: trace_id=%s case_id=%s task_id=%s error=%s",
                trace_id,
                case_id,
                task_id,
                str(exc) or exc.__class__.__name__,
            )
            return False

    payload = dict(_json_obj(platform_ctx.payload))
    payload["auto_notice_callback"] = auto_notice_callback
    return PlatformAccountContext(
        platform_code=platform_ctx.platform_code,
        platform_name=platform_ctx.platform_name,
        account_id=platform_ctx.account_id,
        account_username=platform_ctx.account_username,
        owner_user_id=platform_ctx.owner_user_id,
        account_password=platform_ctx.account_password,
        account_type_name=platform_ctx.account_type_name,
        browser_env_key=platform_ctx.browser_env_key,
        profile_dir=platform_ctx.profile_dir,
        payload=payload,
    )


def _quote_card_time_text(value: Optional[datetime] = None) -> str:
    ts = value or _now()
    try:
        return ts.strftime("%Y年%m月%d日 %H:%M:%S")
    except Exception:
        return _to_str(ts)


def _quote_result_normalized_amount(result: Mapping[str, Any], name: str) -> Any:
    provenance = _json_obj(result.get("quote_provenance"))
    amounts = _json_obj(provenance.get("normalized_amounts"))
    entry = _json_obj(amounts.get(name))
    if "value" not in entry or entry.get("value") in (None, ""):
        return ""
    return entry.get("value")


def _quote_money_decimal(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(_to_str(value).replace(",", "").replace("元", "").strip())
    except Exception:
        return Decimal(default)


def _quote_money_text(value: Any, default: str = "0.00") -> str:
    try:
        return str(_quote_money_decimal(value, default).quantize(Decimal("0.01")))
    except Exception:
        return default


def _quote_has_text(value: Any) -> bool:
    return _to_str(value).strip() != ""


def _quote_first_text(*values: Any) -> str:
    for value in values:
        text = _to_str(value).strip()
        if text:
            return text
    return ""


def _quote_money_text_or_empty(value: Any) -> str:
    return _quote_money_text(value) if _quote_has_text(value) else ""


def _quote_decimal_if_present(value: Any) -> Optional[Decimal]:
    if not _quote_has_text(value):
        return None
    return _quote_money_decimal(value)


def _quote_sum_if_present(*values: Any) -> str:
    total = Decimal("0")
    seen = False
    for value in values:
        amount = _quote_decimal_if_present(value)
        if amount is None:
            continue
        total += amount
        seen = True
    return _quote_money_text(total) if seen else ""


def _picc_code_label(code: Any, labels: Mapping[str, str]) -> str:
    text = _to_str(code).strip()
    if not text:
        return ""
    label = labels.get(text)
    return f"{text}-{label}" if label else text


PICC_DISPLAY_CAR_KIND_LABELS = {
    "A01": "客车",
    "A02": "货车",
    "A03": "客货两用车",
    "A04": "挂车",
    "A05": "特种车",
}


PICC_DISPLAY_USE_NATURE_LABELS = {
    "21": "家庭自用汽车",
    "211": "家庭自用汽车",
    "212": "非营业企业客车",
    "213": "非营业机关客车",
    "220": "营业出租租赁",
    "230": "营业城市公交",
    "240": "营业公路客运",
    "250": "营业货运",
}


def _quote_compact_money_text(value: Any, default: str = "0") -> str:
    text = _quote_money_text(value, default=f"{default}.00" if "." not in default else default)
    return text.rstrip("0").rstrip(".") if "." in text else text


def _quote_compact_money_text_or_empty(value: Any) -> str:
    if not _quote_has_text(value):
        return ""
    return _quote_compact_money_text(value)


def _picc_start_datetime_from_request(quote_form: Mapping[str, Any], vehicle: Mapping[str, Any], *, kind: str) -> str:
    if kind == "ci":
        date_value = _to_str(
            quote_form.get("prpCmain.startDateCI")
            or vehicle.get("startDateCI")
            or quote_form.get("startDateCI")
        ).strip()
        hour_value = _to_str(quote_form.get("prpCmain.starthourci") or quote_form.get("starthourci")).strip()
        minute_value = _to_str(quote_form.get("prpCmain.startminuteci") or quote_form.get("startminuteci")).strip()
    else:
        date_value = _to_str(
            quote_form.get("prpCmain.startDate")
            or vehicle.get("startDateBI")
            or quote_form.get("startDateBI")
        ).strip()
        hour_value = _to_str(quote_form.get("prpCmain.starthourbi") or quote_form.get("starthourbi")).strip()
        minute_value = _to_str(quote_form.get("prpCmain.startminutebi") or quote_form.get("startminutebi")).strip()
    if not date_value:
        return ""
    if re.search(r"\d{1,2}:\d{2}", date_value):
        return date_value
    if not hour_value and not minute_value:
        return date_value
    return f"{date_value} {_safe_int(hour_value, 0):02d}:{_safe_int(minute_value, 0):02d}"


def _quote_value_is_false(value: Any) -> bool:
    text = _to_str(value).strip().lower()
    return text in {"0", "false", "no", "n", "否", "不", "不要", "不用", "关闭"}


def _quote_first_positive_money_text(*values: Any) -> str:
    for value in values:
        if not _quote_has_text(value):
            continue
        amount = _quote_money_decimal(value)
        if amount > 0:
            return _quote_money_text(amount)
    return ""


def _quote_cert_display_text(field_name: str, *values: Any) -> str:
    raw = _quote_first_text(*values)
    return correct_vehicle_cert_field(field_name, raw) or raw


def _picc_result_coverage_items_for_display(
    result: Mapping[str, Any],
    *,
    seat_count: Any = "",
    vehicle_energy_type: Any = "",
) -> list[Dict[str, Any]]:
    rows = _json_list(result.get("coverage_items"))
    if not rows:
        rows = _json_list(result.get("proposal_coverage_items"))
    is_new_energy = picc_is_new_energy_vehicle(
        energy_type=vehicle_energy_type or result.get("vehicle_energy_type"),
        account_type_name=result.get("account_type_name"),
    )
    output: list[Dict[str, Any]] = []
    for item in rows:
        row = _json_obj(item)
        name = picc_result_kind_name(
            row.get("code"),
            platform_name=row.get("platform_name"),
            fallback_name=row.get("name"),
            is_new_energy=is_new_energy,
        )
        if not name:
            continue
        display_row = {
            key: row.get(key)
            for key in ("code", "platform_name", "amount", "unit_amount", "quantity", "shared_amount_flag")
            if row.get(key) not in (None, "")
        }
        display_row.update(
            {
                "name": name,
                "amount_text": picc_result_amount_text(row, seat_count=seat_count),
                "premium": _quote_money_text_or_empty(row.get("premium")),
            }
        )
        output.append(display_row)
    return output


def _picc_existing_proposal_table_card_for_display(result: Mapping[str, Any], card: Mapping[str, Any]) -> Dict[str, Any]:
    safe_card = dict(_json_obj(card))
    request_body = _json_obj(result.get("request_body"))
    vehicle = _json_obj(request_body.get("vehicleForm"))
    quote_form = _json_obj(request_body.get("quoteForm"))
    proposal_info = dict(_json_obj(safe_card.get("proposal_info")))
    seat_count = _quote_first_text(vehicle.get("seatCount"), quote_form.get("prpCitemCar.seatCount"))
    joint_sales = _json_obj(result.get("joint_sales"))
    joint_premium = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "joint_sales")
    )
    joint_amount = _quote_money_text_or_empty(
        result.get("joint_sales_amount")
    ) if joint_premium else ""
    commercial = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "commercial")
    )
    compulsory = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "compulsory")
    )
    vehicle_tax = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "vehicle_tax")
    )
    total_without_tax = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "total_without_vehicle_tax")
    )
    total_with_tax = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "total_with_vehicle_tax")
    )
    safe_card["joint_sales_label"] = "途家安顺"
    safe_card["joint_sales_display_label"] = "途顺家安组合保险"
    safe_card["joint_sales_premium"] = joint_premium
    safe_card["joint_sales_amount"] = joint_amount
    safe_card["commercial_premium"] = commercial
    safe_card["compulsory_premium"] = compulsory
    safe_card["vehicle_tax"] = vehicle_tax
    safe_card["total_without_vehicle_tax"] = total_without_tax
    safe_card["total_with_vehicle_tax"] = total_with_tax
    safe_card["total_premium"] = total_with_tax
    safe_card["risk_score"] = _quote_first_text(result.get("risk_score"), "-")
    safe_card["driver_accident_premium"] = _quote_money_text_or_empty(
        result.get("driver_accident_premium")
    )
    safe_card["claim_business_count"] = result.get("claim_business_count", "")
    safe_card["claim_compulsory_count"] = result.get("claim_compulsory_count", "")
    safe_card["vehicle_tax_detail"] = {
        key: _quote_money_text_or_empty(value)
        for key, value in _json_obj(result.get("vehicle_tax_detail")).items()
        if _quote_has_text(value)
    }
    vehicle_energy_type = _quote_first_text(
        result.get("vehicle_energy_type"),
        safe_card.get("vehicle_energy_type"),
    )
    coverage_source = result if _json_list(result.get("coverage_items")) else safe_card
    coverage_items = _picc_result_coverage_items_for_display(
        coverage_source,
        seat_count=seat_count,
        vehicle_energy_type=vehicle_energy_type,
    )
    if not coverage_items:
        coverage_items = [
            dict(row)
            for row in _json_list(safe_card.get("proposal_coverage_items"))
            if isinstance(row, Mapping)
        ]
    if vehicle_energy_type:
        safe_card["vehicle_energy_type"] = vehicle_energy_type
    if not _json_list(safe_card.get("coverage_items")):
        safe_card["coverage_items"] = coverage_items
    safe_card["proposal_coverage_items"] = coverage_items

    bi_start = _quote_first_text(
        result.get("bi_start_date"),
        result.get("commercial_start_date"),
        proposal_info.get("bi_start_date"),
        _picc_start_datetime_from_request(quote_form, vehicle, kind="bi"),
    )
    ci_start = _quote_first_text(
        result.get("ci_start_date"),
        result.get("compulsory_start_date"),
        proposal_info.get("ci_start_date"),
        _picc_start_datetime_from_request(quote_form, vehicle, kind="ci"),
    )
    if bi_start:
        proposal_info["bi_start_date"] = bi_start
    if ci_start:
        proposal_info["ci_start_date"] = ci_start
    if proposal_info:
        proposal_info["plate_no"] = _quote_cert_display_text(
            "plate_no",
            proposal_info.get("plate_no"),
            vehicle.get("licenseNo"),
            quote_form.get("prpCitemCar.licenseNo"),
        )
        proposal_info["engine_no"] = _quote_cert_display_text(
            "engine_no",
            proposal_info.get("engine_no"),
            vehicle.get("engineNo"),
            quote_form.get("prpCitemCar.engineNo"),
        )
        proposal_info["vin"] = _quote_cert_display_text(
            "vin",
            proposal_info.get("vin"),
            vehicle.get("vin"),
            quote_form.get("prpCitemCar.vinNo"),
        )
    if proposal_info:
        safe_card["proposal_info"] = proposal_info
    return safe_card


def _picc_result_card_for_display(result: Mapping[str, Any], card: Mapping[str, Any]) -> Dict[str, Any]:
    safe_card = dict(_json_obj(card))
    if _to_str(safe_card.get("style")).strip() == "picc_proposal_table":
        return _picc_existing_proposal_table_card_for_display(result, safe_card)

    platform_code = _to_str(result.get("platform_code")).strip().upper()
    platform_name = _to_str(result.get("platform_name")).strip()
    if platform_code != "PICC" and platform_name not in {"人保", "中国人保", "PICC"}:
        return safe_card

    request_body = _json_obj(result.get("request_body"))
    vehicle = _json_obj(request_body.get("vehicleForm"))
    owner = _json_obj(request_body.get("ownerForm"))
    quote_form = _json_obj(request_body.get("quoteForm"))
    seat_count = _quote_first_text(vehicle.get("seatCount"), quote_form.get("prpCitemCar.seatCount"))
    proposal_coverage_items = _picc_result_coverage_items_for_display(
        result,
        seat_count=seat_count,
        vehicle_energy_type=result.get("vehicle_energy_type"),
    )

    commercial = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "commercial")
    )
    compulsory = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "compulsory")
    )
    vehicle_tax = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "vehicle_tax")
    )
    joint_sales = _json_obj(result.get("joint_sales"))
    joint_premium = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "joint_sales")
    )
    joint_amount = _quote_money_text_or_empty(
        result.get("joint_sales_amount")
    )
    if _quote_money_decimal(joint_premium) <= 0:
        joint_premium = ""
        joint_amount = ""
    total_without_tax = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "total_without_vehicle_tax")
    )
    total_with_tax = _quote_money_text_or_empty(
        _quote_result_normalized_amount(result, "total_with_vehicle_tax")
    )

    car_kind_code = _quote_first_text(vehicle.get("carKindCode"), quote_form.get("prpCitemCar.carKindCode"))
    use_nature_code = _quote_first_text(vehicle.get("useNatureCode"), quote_form.get("prpCitemCar.useNatureCode"))
    claim_bi = _quote_first_text(result.get("claim_business_count"))
    claim_ci = _quote_first_text(result.get("claim_compulsory_count"))
    claim_parts = []
    if claim_bi:
        claim_parts.append(f"连续承保期间出险次数{claim_bi}次")
    if claim_ci:
        claim_parts.append(f"交强险{claim_ci}次")
    claim_summary = "，".join(claim_parts)
    tax_detail = {
        key: _quote_money_text_or_empty(value)
        for key, value in _json_obj(result.get("vehicle_tax_detail")).items()
        if _quote_has_text(value)
    }

    upgraded = {
        **safe_card,
        "style": "picc_proposal_table",
        "title": "中国人保投保方案",
        "total_premium": total_with_tax,
        "total_without_vehicle_tax": total_without_tax,
        "total_with_vehicle_tax": total_with_tax,
        "commercial_premium": commercial,
        "compulsory_premium": compulsory,
        "vehicle_tax": vehicle_tax,
        "vehicle_tax_detail": tax_detail,
        "joint_sales_label": "途家安顺",
        "joint_sales_display_label": "途顺家安组合保险",
        "joint_sales_premium": joint_premium,
        "joint_sales_amount": joint_amount,
        "driver_accident_premium": _quote_money_text_or_empty(result.get("driver_accident_premium")),
        "claim_business_count": _quote_first_text(result.get("claim_business_count"), claim_bi),
        "claim_compulsory_count": _quote_first_text(result.get("claim_compulsory_count"), claim_ci),
        "risk_score": _to_str(result.get("risk_score") or "-").strip() or "-",
        "vehicle_energy_type": _quote_first_text(result.get("vehicle_energy_type"), safe_card.get("vehicle_energy_type")),
        "proposal_info": {
            "insured_name": _quote_first_text(owner.get("ownerName"), result.get("owner_name"), safe_card.get("owner_name")),
            "plate_no": _quote_cert_display_text("plate_no", result.get("plate_no"), vehicle.get("licenseNo"), safe_card.get("plate_no")),
            "engine_no": _quote_cert_display_text("engine_no", vehicle.get("engineNo"), quote_form.get("prpCitemCar.engineNo"), safe_card.get("engine_no")),
            "vin": _quote_cert_display_text("vin", vehicle.get("vin"), quote_form.get("prpCitemCar.vinNo"), safe_card.get("vin")),
            "vehicle_type": _picc_code_label(car_kind_code, PICC_DISPLAY_CAR_KIND_LABELS),
            "vehicle_usage": _picc_code_label(use_nature_code, PICC_DISPLAY_USE_NATURE_LABELS),
            "vehicle_model": _quote_first_text(
                result.get("vehicle_model"),
                vehicle.get("selectedModelName"),
                vehicle.get("modelName"),
                quote_form.get("prpCitemCar.brandName"),
            ),
            "enroll_date": _quote_first_text(vehicle.get("enrollDate"), quote_form.get("prpCitemCar.enrollDate")),
            "ton_count": f"{_quote_compact_money_text_or_empty(_quote_first_text(vehicle.get('tonCount'), quote_form.get('prpCitemCar.tonCount')))}千克"
            if _quote_first_text(vehicle.get("tonCount"), quote_form.get("prpCitemCar.tonCount"))
            else "",
            "seat_count": f"{_safe_int(seat_count, 0)}人" if seat_count else "",
            "purchase_price": f"{_quote_compact_money_text_or_empty(_quote_first_text(vehicle.get('purchasePrice'), vehicle.get('actualValue'), result.get('vehicle_actual_value')))}元"
            if _quote_first_text(vehicle.get("purchasePrice"), vehicle.get("actualValue"), result.get("vehicle_actual_value"))
            else "",
            "claim_summary": claim_summary,
            "bi_start_date": _quote_first_text(
                result.get("bi_start_date"),
                result.get("commercial_start_date"),
                _picc_start_datetime_from_request(quote_form, vehicle, kind="bi"),
            ),
            "ci_start_date": _quote_first_text(
                result.get("ci_start_date"),
                result.get("compulsory_start_date"),
                _picc_start_datetime_from_request(quote_form, vehicle, kind="ci"),
            ),
        },
        "proposal_coverage_items": proposal_coverage_items,
    }
    if not _json_list(upgraded.get("coverage_items")):
        upgraded["coverage_items"] = proposal_coverage_items
    return upgraded


def _sync_quote_result_with_display_card(result: Dict[str, Any], card: Mapping[str, Any]) -> None:
    """Keep the stored quote payload consistent with the card used to render the image."""
    safe_card = _json_obj(card)
    if not safe_card:
        return

    joint_premium = _to_str(safe_card.get("joint_sales_premium")).strip()
    joint_amount = _to_str(safe_card.get("joint_sales_amount")).strip()
    if "joint_sales_premium" in safe_card:
        result["joint_sales_premium"] = joint_premium
    if "joint_sales_amount" in safe_card:
        result["joint_sales_amount"] = joint_amount

    if "joint_sales_premium" in safe_card or "joint_sales_amount" in safe_card:
        premium_decimal = _quote_money_decimal(joint_premium)
        joint_sales = deepcopy(_json_obj(result.get("joint_sales")))
        joint_sales["enabled"] = premium_decimal > 0
        joint_sales["premium"] = _quote_money_text(premium_decimal) if premium_decimal > 0 else "0.00"
        joint_sales["amount"] = joint_amount if premium_decimal > 0 else ""
        result["joint_sales"] = joint_sales
        _update_joint_sales_price_items(result, premium_decimal)

    proposal_info = _json_obj(safe_card.get("proposal_info"))
    bi_start = _quote_first_text(proposal_info.get("bi_start_date"), safe_card.get("bi_start_date"))
    ci_start = _quote_first_text(proposal_info.get("ci_start_date"), safe_card.get("ci_start_date"))
    if bi_start:
        result["bi_start_date"] = bi_start
        result["commercial_start_date"] = bi_start
    if ci_start:
        result["ci_start_date"] = ci_start
        result["compulsory_start_date"] = ci_start


def _enrich_quote_result_for_display(
    result: Dict[str, Any],
    *,
    platform_account: Optional[QuotePlatformAccountProfile] = None,
    platform_name: str = "",
    generate_image: bool = True,
) -> Dict[str, Any]:
    """Attach truthful display metadata for the chat quote card without leaking secrets."""
    safe_result = dict(_json_obj(result))
    validation_error = _quote_result_real_data_error(safe_result)
    if validation_error:
        raise ValueError(f"报价结果未通过真实性校验，不能生成结果图：{validation_error}")
    card = _json_obj(safe_result.get("result_card") or safe_result.get("resultCard"))
    if not card:
        raise ValueError("真实报价结果缺少结果卡片，不能生成结果图")
    card = _picc_result_card_for_display(safe_result, card)
    _sync_quote_result_with_display_card(safe_result, card)
    display_time = _quote_card_time_text()
    card.setdefault("quote_time", display_time)
    for key in ("watermark_account", "watermark_user", "watermark_name", "watermark_time", "watermark_text"):
        card.pop(key, None)
    safe_result["result_card"] = card
    if not generate_image:
        safe_result.pop("result_image", None)
        safe_result.pop("resultImage", None)
        safe_result["result_image_pending"] = True
        if platform_account:
            safe_result.setdefault("account_type_name", _normalize_account_type_name(_loaded_value(platform_account, "account_type_name")))
            safe_result.setdefault("account_username", _to_str(_loaded_value(platform_account, "account_username")).strip())
            safe_result.setdefault("account_owner_name", _to_str(_loaded_value(platform_account, "account_owner_name")).strip())
        return safe_result
    image_started = time.perf_counter()
    try:
        image_payload = save_quote_result_card_image(card, trace_id=_to_str(safe_result.get("trace_id")).strip())
    except Exception as exc:
        raise ValueError(
            sanitize_quote_user_message(exc, "报价结果图片生成失败")
        ) from exc
    safe_result["result_image_ms"] = _elapsed_ms(image_started)
    if not isinstance(image_payload, Mapping):
        raise ValueError("报价结果图片生成失败：OSS未返回有效图片信息")
    image_url = _to_str(
        image_payload.get("image_url")
        or image_payload.get("url")
        or image_payload.get("preview_url")
    ).strip()
    if not image_url:
        raise ValueError("报价结果图片生成失败：OSS未返回图片地址")
    try:
        image_size = int(image_payload.get("size") or 0)
        image_width = int(image_payload.get("width") or 0)
        image_height = int(image_payload.get("height") or 0)
    except (TypeError, ValueError):
        raise ValueError("报价结果图片生成失败：图片元数据无效")
    if image_size <= 0 or image_width <= 0 or image_height <= 0:
        raise ValueError("报价结果图片生成失败：图片内容或尺寸无效")
    safe_result["result_image"] = dict(image_payload)
    if platform_account:
        safe_result.setdefault("account_type_name", _normalize_account_type_name(_loaded_value(platform_account, "account_type_name")))
        safe_result.setdefault("account_username", _to_str(_loaded_value(platform_account, "account_username")).strip())
        safe_result.setdefault("account_owner_name", _to_str(_loaded_value(platform_account, "account_owner_name")).strip())
    return safe_result


async def _fail_quote_after_result_materialization(
    db: AsyncSession,
    *,
    case: QuoteCase,
    task: QuoteTask,
    owner_user_id: int,
    platform_name: str,
    trace_id: str,
    error: Any,
    platform_account: Optional[QuotePlatformAccountProfile] = None,
    quota_reservation: Optional[Dict[str, Any]] = None,
    response_payload: Optional[Dict[str, Any]] = None,
    preserve_existing_quote: bool = False,
    fallback_task: Optional[QuoteTask] = None,
    reply_prefix: str = "报价结果生成失败",
) -> Tuple[str, Dict[str, Any]]:
    """Close a quote attempt when a real result cannot be materialized.

    A platform response is not a successful quote until both the trusted
    result card and its OSS image have been created. This helper keeps that
    boundary explicit: no result payload, success event, quote count, or
    success reply may be persisted when materialization fails.
    """
    detail = sanitize_quote_user_message(error, "真实报价结果无法生成")
    reply = f"{platform_name or case.platform_name or '平台'}{reply_prefix}：{detail}。未生成报价结果图，请重试。"
    await _release_account_quota_reservation(
        db,
        account=platform_account,
        reservation=quota_reservation or {},
    )
    task = await _lock_quote_task(db, task)
    if await _quote_task_was_cancelled(db, task=task):
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)

    now = _now()
    task.status = TASK_STATUS_FAILED
    task.error_detail = reply
    task.response_payload = {
        **_json_obj(task.response_payload),
        **_json_obj(response_payload),
        "result_materialization_failed": True,
        "result_materialization_error": detail,
        "result_image_not_created": True,
    }
    task.result_payload = {}
    task.finished_at = now
    task.updated_at = now

    if preserve_existing_quote and fallback_task is not None:
        case.status = CASE_STATUS_QUOTED
        case.current_task_id = fallback_task.id
    else:
        case.status = CASE_STATUS_READY
        case.current_task_id = task.id
    case.updated_at = now
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": TASK_STATUS_FAILED,
            "trace_id": trace_id,
            "reason": reply,
            "result_materialization_failed": True,
            "result_materialization_error": detail,
            "source_task_id": fallback_task.id if fallback_task is not None else None,
        },
    )
    await db.flush()

    payload = {
        "quote_case": {
            "id": case.id,
            "case_no": case.case_no,
            "status": case.status,
            "order_id": case.order_id,
            "source_type": case.source_type,
            "quote_count": _safe_int(case.quote_count, 0),
            "current_task_id": case.current_task_id,
        },
        "quote_task": {
            "id": task.id,
            "status": task.status,
            "trace_id": trace_id,
            "error_detail": task.error_detail,
        },
        "result_materialization_failed": True,
        "result_materialization_error": detail,
    }
    if fallback_task is not None:
        payload["source_task_id"] = fallback_task.id
    return _build_quote_user_failure_response(
        reply=reply,
        case=case,
        task=task,
        trace_id=trace_id,
        failure_code=FAILURE_CODE_RESULT_MATERIALIZATION,
        failure_reason=detail,
        result_status=RESULT_FAILED,
        response_status="failed",
        actions=[_mk_action(f"{platform_name or case.platform_name or '平台'}报价")],
        payload=payload,
    )


def _extract_joint_sales_image_adjustment(text: Any) -> Dict[str, Any]:
    """Parse image-only non-car premium adjustments, without triggering platform requote."""
    raw = _norm_text(text)
    compact = re.sub(r"\s+", "", raw)
    if not compact or "报价" in compact or "重报" in compact:
        return {}
    aliases = (
        "途家安顺保费",
        "途家安顺非车保费",
        "途顺家安保费",
        "途家安顺",
        "途顺家安",
        "非车",
        "意外险",
        "驾乘意外",
        "意外",
    )
    alias_group = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    remove_words = QUOTE_CHAT_NEGATE_OBJECT_WORDS
    remove_group = "|".join(re.escape(word) for word in remove_words)
    remove_pattern = rf"^(?:请)?(?:(?:{remove_group})(?:{alias_group})(?:保费|金额|保额|险|保险)?|(?:{alias_group})(?:保费|金额|保额|险|保险)?(?:{remove_group}))$"
    if re.fullmatch(remove_pattern, compact, flags=re.IGNORECASE):
        return {"field_name": "途家安顺保费", "field_value": "0", "remove": True}
    number = r"(\d+(?:[,，]\d{3})*(?:\.\d+)?)"
    connector = r"(?:[:：=+＋]|改成|改为|改到|调整成|调整为|调整到|调成|调到|调至|设置为|设为|变成|变为|变到|调整|改|变|到|为)?"
    pattern = rf"^(?:请)?(?:把|将)?(?:{alias_group})(?:保费|金额|保额)?{connector}{number}(万|元)?$"
    match = re.fullmatch(pattern, compact, flags=re.IGNORECASE)
    if not match:
        return {}
    value = _normalize_quote_config_override_value(match.group(1), match.group(2))
    _ensure_positive_numeric_config_value("途家安顺保费", value, context="非车金额")
    return {"field_name": "途家安顺保费", "field_value": value}


def _result_has_quote_card(result: Any) -> bool:
    if _quote_result_real_data_error(result):
        return False
    return bool(_json_obj(result.get("result_card") or result.get("resultCard")))


async def _stored_quote_task_has_real_result(
    db: AsyncSession,
    task: Optional[QuoteTask],
    *,
    seen_task_ids: Optional[Set[int]] = None,
) -> bool:
    """Validate a stored success task before it can be reused or redrawn."""
    if task is None or _to_str(getattr(task, "status", "")).strip().lower() != TASK_STATUS_SUCCESS:
        return False

    task_id = _safe_int(getattr(task, "id", 0), 0)
    seen = set(seen_task_ids or set())
    if task_id > 0:
        if task_id in seen:
            return False
        seen.add(task_id)

    result = _json_obj(getattr(task, "result_payload", None))
    if _quote_result_real_data_error(result):
        return False
    if not _json_obj(result.get("result_card") or result.get("resultCard")):
        return False

    response_payload = _json_obj(getattr(task, "response_payload", None))
    runtime_payload = _json_obj(response_payload.get("quote"))
    runtime_data = _json_obj(runtime_payload.get("data"))
    runtime_result = _json_obj(runtime_data.get("quote_result"))
    if runtime_result and not _quote_result_real_data_error(runtime_result):
        return True

    # Image-only adjustments intentionally reuse a prior real quote. Follow
    # the provenance chain instead of trusting the derived card by itself.
    if response_payload.get("reused_quote_result") is True:
        source_task_id = _safe_int(response_payload.get("source_task_id"), 0)
        if source_task_id <= 0:
            return False
        source_task = await db.get(QuoteTask, source_task_id)
        return await _stored_quote_task_has_real_result(
            db,
            source_task,
            seen_task_ids=seen,
        )
    return False


async def _latest_success_quote_task_for_session(
    db: AsyncSession,
    *,
    owner_user_id: int,
    session_id: Optional[str],
    ctx: Optional[Dict[str, Any]] = None,
    for_update: bool = False,
) -> Optional[Tuple[QuoteCase, QuoteTask]]:
    if owner_user_id <= 0 or not session_id:
        return None
    stmt = (
        select(QuoteCase, QuoteTask)
        .join(QuoteTask, QuoteTask.quote_case_id == QuoteCase.id)
        .where(
            QuoteCase.owner_user_id == owner_user_id,
            QuoteCase.session_id == session_id,
            QuoteTask.status == TASK_STATUS_SUCCESS,
        )
        .order_by(desc(QuoteTask.id))
        .limit(30)
    )
    if for_update:
        stmt = stmt.with_for_update()
    rows = (await db.execute(stmt)).all()
    for case, task in rows:
        if ctx is not None and not await _case_order_is_readable(db, ctx=ctx, case=case):
            continue
        if await _stored_quote_task_has_real_result(db, task):
            return case, task
    return None


def _quote_adjusted_money_text(value: Any, delta: Decimal) -> str:
    return _quote_money_text(_quote_money_decimal(value) + delta)


def _update_joint_sales_price_items(result: Dict[str, Any], premium: Decimal) -> None:
    items = []
    found = False
    for item in _json_list(result.get("price_items")):
        row = dict(_json_obj(item))
        name = _to_str(row.get("name")).strip()
        if name and any(keyword in name for keyword in ("途家", "途顺", "非车")):
            if premium > 0:
                row["amount"] = float(premium)
                items.append(row)
                found = True
            continue
        items.append(row)
    if premium > 0 and not found:
        items.append({"name": "途家安顺", "amount": float(premium)})
    result["price_items"] = items


def _result_joint_sales_amount_source(result: Mapping[str, Any]) -> Any:
    card = _json_obj(result.get("result_card") or result.get("resultCard"))
    joint_sales = _json_obj(result.get("joint_sales"))
    return (
        result.get("joint_sales_original_amount")
        or result.get("joint_sales_base_amount")
        or card.get("joint_sales_original_amount")
        or card.get("joint_sales_amount")
        or result.get("joint_sales_amount")
        or joint_sales.get("amount")
    )


async def _resolve_joint_sales_base_amount(
    db: AsyncSession,
    *,
    case_id: int,
    source_task: QuoteTask,
    max_depth: int = 8,
) -> Any:
    """Find the original non-zero joint-sale coverage when the latest task is only an image redraw."""
    task: Optional[QuoteTask] = source_task
    seen: Set[int] = set()
    depth = 0
    while task is not None and depth < max_depth:
        task_id = int(getattr(task, "id", 0) or 0)
        if task_id and task_id in seen:
            break
        if task_id:
            seen.add(task_id)

        result = _json_obj(getattr(task, "result_payload", None))
        amount = _result_joint_sales_amount_source(result)
        if _quote_money_decimal(amount) > 0:
            return amount

        request_payload = _json_obj(getattr(task, "request_payload", None))
        source_task_id = _safe_int(request_payload.get("source_task_id") or result.get("source_quote_task_id"), 0)
        if source_task_id <= 0 or source_task_id in seen:
            break
        parent = await db.get(QuoteTask, source_task_id)
        if not parent or int(getattr(parent, "quote_case_id", 0) or 0) != int(case_id or 0):
            break
        task = parent
        depth += 1
    return None


def _quote_task_account_type_name(task: QuoteTask) -> str:
    snapshot = _json_obj(getattr(task, "submitted_snapshot", None))
    platform_default = _json_obj(snapshot.get("platform_default_config"))
    vehicle_type_detect = _json_obj(snapshot.get("vehicle_type_detect"))
    return _normalize_account_type_name(
        platform_default.get("resolved_type_name")
        or platform_default.get("account_type_name")
        or vehicle_type_detect.get("config_type_name")
    )


def _joint_sales_query_snapshot_payload(source_task: QuoteTask, premium_value: Any) -> Dict[str, Any]:
    snapshot = _json_obj(getattr(source_task, "submitted_snapshot", None))
    request_payload = _json_obj(getattr(source_task, "request_payload", None))
    result = _json_obj(getattr(source_task, "result_payload", None))
    request_body = _json_obj(
        result.get("request_body")
        or snapshot.get("request_body")
        or request_payload.get("request_body")
    )
    return {
        "mode": "query_joint_sales_plan",
        "source_task_id": _safe_int(getattr(source_task, "id", 0), 0),
        "premium": _quote_money_text(premium_value),
        "default_config_json": _json_obj(snapshot.get("default_config_json")),
        "platform_default_config": _json_obj(snapshot.get("platform_default_config")),
        "vehicle_type_detect": _json_obj(snapshot.get("vehicle_type_detect")),
        "request_body": request_body,
    }


async def _query_joint_sales_amount_for_image_adjustment(
    db: AsyncSession,
    *,
    owner_user_id: int,
    source_task: QuoteTask,
    premium_value: Any,
) -> Tuple[str, Dict[str, Any], Optional[QuotePlatformAccountProfile]]:
    premium = _quote_money_decimal(premium_value)
    if premium <= 0:
        return (
            "0.00",
            {
                "attempted": False,
                "success": True,
                "premium": "0.00",
                "amount": "0.00",
                "reason": "途家安顺保费为0，按规则不查询保额",
            },
            None,
        )

    platform_code = _to_str(source_task.platform_code).strip().upper() or "PICC"
    platform_name = _to_str(source_task.platform_name).strip() or platform_code
    if platform_code != "PICC":
        raise ValueError(f"{platform_name}暂不支持通过“非车 金额”重新查询途家安顺保额")

    account_id = _quote_task_platform_account_id(source_task)
    if account_id <= 0:
        raise ValueError("最近一次报价缺少平台账号留档，无法查询途家安顺保额")
    platform_account = await _get_platform_account_profile_by_id(db, account_id=account_id)
    if platform_account is None:
        raise ValueError("最近一次报价使用的平台账号已不存在")
    if not bool(_loaded_value(platform_account, "enabled")):
        raise ValueError("最近一次报价使用的平台账号已停用，无法查询途家安顺保额")

    account_type_name = _quote_task_account_type_name(source_task)
    platform_ctx = _platform_account_quote_context(platform_account, account_type_name=account_type_name)
    query_payload = _joint_sales_query_snapshot_payload(source_task, premium_value)
    runtime_result = await quote_platform_runtime.query_joint_sales_plan(platform_ctx, query_payload, db=db)
    if _is_runtime_session_expired_result(runtime_result):
        _apply_platform_account_runtime_status(platform_account, runtime_result, default_error="途家安顺保额查询失败")
        raise ValueError(_runtime_detail(runtime_result, "途家安顺保额查询失败"))

    runtime_status = _runtime_status(runtime_result)
    runtime_payload = _runtime_result_payload(runtime_result)
    data = _json_obj(runtime_payload.get("data"))
    plan = _json_obj(data.get("joint_sales_plan"))
    amount = data.get("amount") or plan.get("amount")
    if runtime_status not in {"success", "ok"} or not plan.get("success"):
        detail = _runtime_detail(runtime_result, "途家安顺保额查询失败")
        plan_message = _to_str(plan.get("message")).strip()
        raise ValueError(plan_message or detail)
    if _quote_money_decimal(amount) <= 0:
        raise ValueError(f"未查询到保费为{_quote_money_text(premium)}的途家安顺保额")

    return (
        _quote_money_text(amount),
        {
            "attempted": True,
            "success": True,
            "premium": _quote_money_text(data.get("premium") or plan.get("premium") or premium),
            "amount": _quote_money_text(amount),
            "candidate_count": _safe_int(plan.get("candidate_count"), 0),
            "match_count": _safe_int(plan.get("match_count"), 0),
            "selected_plan": _json_obj(plan.get("selected_plan")),
            "selection_rule": _to_str(plan.get("selection_rule")).strip(),
            "runtime": runtime_payload,
        },
        platform_account,
    )


def _apply_joint_sales_image_adjustment(
    result: Dict[str, Any],
    *,
    premium_value: Any,
    base_joint_sales_amount: Any = None,
) -> Dict[str, Any]:
    source_validation_error = _quote_result_real_data_error(result)
    if source_validation_error:
        raise ValueError(f"历史报价结果不完整，不能重绘结果图：{source_validation_error}")
    adjusted = deepcopy(_json_obj(result))
    card = deepcopy(_json_obj(adjusted.get("result_card") or adjusted.get("resultCard")))
    if not card:
        raise ValueError("最近一次报价结果缺少图片数据，无法只重绘报价图")

    premium = _quote_money_decimal(premium_value)
    old_premium = _quote_money_decimal(
        _quote_result_normalized_amount(adjusted, "joint_sales")
    )
    delta = premium - old_premium
    premium_text = _quote_money_text(premium)

    joint_sales = deepcopy(_json_obj(adjusted.get("joint_sales")))
    old_amount_source = (
        card.get("joint_sales_amount")
        or adjusted.get("joint_sales_amount")
        or joint_sales.get("amount")
    )
    original_amount_source = (
        base_joint_sales_amount
        or adjusted.get("joint_sales_original_amount")
        or card.get("joint_sales_original_amount")
        or adjusted.get("joint_sales_base_amount")
        or old_amount_source
    )
    original_amount_text = _quote_money_text(original_amount_source)
    enabled = premium > 0
    amount_text = original_amount_text if enabled else ""

    card["joint_sales_label"] = "途家安顺"
    card["joint_sales_display_label"] = "途顺家安组合保险"
    card["joint_sales_premium"] = premium_text if enabled else ""
    card["joint_sales_amount"] = amount_text
    card["joint_sales_original_amount"] = original_amount_text
    adjusted["joint_sales_premium"] = premium_text if enabled else ""
    adjusted["joint_sales_amount"] = amount_text
    adjusted["joint_sales_original_amount"] = original_amount_text
    if joint_sales:
        joint_sales["enabled"] = enabled
        joint_sales["premium"] = premium_text
        joint_sales["amount"] = amount_text
        joint_sales["original_amount"] = original_amount_text
        adjusted["joint_sales"] = joint_sales

    normalized_amounts = deepcopy(
        _json_obj(_json_obj(adjusted.get("quote_provenance")).get("normalized_amounts"))
    )
    if premium > 0:
        normalized_amounts["joint_sales"] = {
            "value": premium_text,
            "source": "joint_sales_plan_response.selected_plan.planPremium",
        }
    else:
        normalized_amounts.pop("joint_sales", None)

    for amount_name in ("total_without_vehicle_tax", "total_with_vehicle_tax"):
        entry = _json_obj(normalized_amounts.get(amount_name))
        if not entry or entry.get("value") in (None, ""):
            continue
        entry["value"] = _quote_adjusted_money_text(entry.get("value"), delta)
        entry["source"] = "derived_from_real_quote"
        normalized_amounts[amount_name] = entry

    provenance = deepcopy(_json_obj(adjusted.get("quote_provenance")))
    provenance["normalized_amounts"] = normalized_amounts
    if premium > 0:
        provenance["joint_sales_evidence"] = [
            {
                "name": "joint_sales",
                "source": "joint_sales_plan_response.selected_plan.planPremium",
                "value": premium_text,
            }
        ]
    else:
        provenance.pop("joint_sales_evidence", None)
    adjusted["quote_provenance"] = provenance

    card["commercial_premium"] = _quote_money_text_or_empty(
        _quote_result_normalized_amount(adjusted, "commercial")
    )
    card["compulsory_premium"] = _quote_money_text_or_empty(
        _quote_result_normalized_amount(adjusted, "compulsory")
    )
    card["vehicle_tax"] = _quote_money_text_or_empty(
        _quote_result_normalized_amount(adjusted, "vehicle_tax")
    )
    card["total_without_vehicle_tax"] = _quote_money_text_or_empty(
        _quote_result_normalized_amount(adjusted, "total_without_vehicle_tax")
    )
    card["total_with_vehicle_tax"] = _quote_money_text_or_empty(
        _quote_result_normalized_amount(adjusted, "total_with_vehicle_tax")
    )
    card["total_premium"] = card["total_with_vehicle_tax"]

    if adjusted.get("premium_total") not in (None, ""):
        adjusted["premium_total"] = float(_quote_money_decimal(adjusted.get("premium_total")) + delta)

    _update_joint_sales_price_items(adjusted, premium)
    adjusted["result_card"] = card
    adjusted.pop("result_image", None)
    adjusted.pop("resultImage", None)
    adjusted["joint_sales_image_adjustment"] = {
        "field_name": "途家安顺保费",
        "field_value": premium_text,
        "source": "chat_command",
    }
    return adjusted


async def _handle_joint_sales_image_adjustment_message(
    db: AsyncSession,
    *,
    ctx: Dict[str, Any],
    owner_user_id: int,
    session_id: Optional[str],
    text: str,
    adjustment: Dict[str, Any],
    source_pair: Optional[Tuple[QuoteCase, QuoteTask]] = None,
) -> Tuple[str, Dict[str, Any]]:
    if source_pair is None:
        source_pair = await _latest_success_quote_task_for_session(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            ctx=ctx,
            for_update=False,
        )
    if not source_pair:
        return (
            "请先完成一次报价后，再发送“非车 金额”调整报价结果图。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": _new_trace_id(),
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="没有可复用的报价结果",
                    entities={},
                    payload={"joint_sales_image_adjustment": adjustment},
                ),
                "actions": [],
            },
        )

    case, source_task = source_pair
    case = await _lock_quote_case(db, case)
    trace_id = _new_trace_id()
    premium_value = adjustment.get("field_value")
    premium_override = {"途家安顺保费": _quote_money_text(premium_value)}
    await _cancel_active_quote_tasks_for_case(
        db,
        case=case,
        reason=QUOTE_SUPERSEDED_MESSAGE,
        now=_now(),
    )
    images_by_slot = await _active_images_by_slot(db, case.id)
    draft_base = _json_obj(case.draft_order_data) or _json_obj(case.normalized_data)
    merged_overrides = _merge_quote_config_overrides(
        draft_base.get("quote_field_overrides"),
        premium_override,
    )
    next_normalized = _normalize_quote_case_data(
        base_data=_json_obj(case.normalized_data) or draft_base,
        order_data={},
        text_data={"quote_field_overrides": merged_overrides},
        images_by_slot=images_by_slot,
    )
    case.draft_order_data = next_normalized
    case.normalized_data = next_normalized

    submitted_snapshot = deepcopy(_json_obj(source_task.submitted_snapshot))
    submitted_normalized = _json_obj(submitted_snapshot.get("normalized_data"))
    submitted_normalized["quote_field_overrides"] = _merge_quote_config_overrides(
        submitted_normalized.get("quote_field_overrides"),
        premium_override,
    )
    submitted_snapshot["normalized_data"] = submitted_normalized
    submitted_defaults = _json_obj(submitted_snapshot.get("default_config_json"))
    if submitted_defaults:
        submitted_defaults["途家安顺保费"] = _quote_money_text(premium_value)
        submitted_snapshot["default_config_json"] = submitted_defaults
    submitted_snapshot["joint_sales_image_adjustment"] = {
        "source_task_id": source_task.id,
        "field_name": "途家安顺保费",
        "field_value": _quote_money_text(premium_value),
        "raw_text": text,
    }
    request_payload = {
        "mode": "result_image_adjustment",
        "source_task_id": source_task.id,
        "owner_user_id": owner_user_id,
        "platform_account": _json_obj(_json_obj(source_task.request_payload).get("platform_account")),
        "joint_sales_image_adjustment": submitted_snapshot["joint_sales_image_adjustment"],
    }
    now = _now()
    task = QuoteTask(
        quote_case_id=case.id,
        platform_code=source_task.platform_code or case.platform_code or "PICC",
        platform_name=source_task.platform_name or case.platform_name or source_task.platform_code or case.platform_code,
        status=TASK_STATUS_RUNNING,
        login_state=source_task.login_state or "authenticated",
        sms_phone_mask=source_task.sms_phone_mask,
        trace_id=trace_id,
        request_payload=request_payload,
        response_payload={"source_task_id": source_task.id},
        result_payload={},
        submitted_snapshot=submitted_snapshot,
        started_at=now,
    )
    db.add(task)
    await db.flush()
    case.current_task_id = task.id
    case.updated_at = now
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": TASK_STATUS_RUNNING,
            "trace_id": trace_id,
            "source_task_id": source_task.id,
            "joint_sales_image_adjustment": submitted_snapshot["joint_sales_image_adjustment"],
        },
    )
    await db.flush()
    await db.commit()

    source_result = _json_obj(source_task.result_payload)
    source_validation_error = _quote_result_real_data_error(source_result)
    if source_validation_error or not _json_obj(source_result.get("result_card") or source_result.get("resultCard")):
        detail = source_validation_error or "历史报价结果缺少结果卡片"
        reply = f"历史报价结果不完整，无法调整途家安顺并重绘结果图：{detail}。请重新发起报价。"
        task = await _lock_quote_task(db, task)
        task.status = TASK_STATUS_FAILED
        task.error_detail = reply
        task.response_payload = {
            **_json_obj(task.response_payload),
            "source_task_id": source_task.id,
            "quote_result_validation_error": detail,
        }
        task.result_payload = {}
        task.finished_at = _now()
        task.updated_at = _now()
        case.status = CASE_STATUS_READY
        case.current_task_id = task.id
        case.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={
                "task_id": task.id,
                "status": TASK_STATUS_FAILED,
                "trace_id": trace_id,
                "reason": detail,
                "source_task_id": source_task.id,
            },
        )
        await db.flush()
        return (
            reply,
            {
                "status": "success",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_FAILED,
                    message=reply,
                    entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                    payload={
                        "quote_case": {
                            "id": case.id,
                            "case_no": case.case_no,
                            "status": case.status,
                            "order_id": case.order_id,
                        },
                        "quote_task": {"id": task.id, "status": task.status},
                        "source_task_id": source_task.id,
                        "quote_result_validation_error": detail,
                    },
                ),
                "actions": [_mk_action(f"{case.platform_name or source_task.platform_name or '平台'}报价")],
            },
        )

    try:
        joint_sales_amount, joint_sales_amount_query, platform_account = await _query_joint_sales_amount_for_image_adjustment(
            db,
            owner_user_id=owner_user_id,
            source_task=source_task,
            premium_value=premium_value,
        )
    except Exception as exc:
        detail = sanitize_quote_user_message(exc, "途家安顺保额查询失败")
        reply = f"途家安顺保额查询失败：{detail}，本次没有更新报价结果图。"
        task = await _lock_quote_task(db, task)
        if await _quote_task_was_cancelled(db, task=task):
            return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)
        task.status = TASK_STATUS_FAILED
        task.error_detail = reply
        task.response_payload = {
            **_json_obj(task.response_payload),
            "joint_sales_amount_query_error": detail,
        }
        task.result_payload = {}
        task.finished_at = _now()
        task.updated_at = _now()
        if case.current_task_id == task.id:
            case.current_task_id = task.id
        case.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={
                "task_id": task.id,
                "status": TASK_STATUS_FAILED,
                "trace_id": trace_id,
                "reason": detail,
                "source_task_id": source_task.id,
            },
        )
        await db.flush()
        return (
            reply,
            {
                "status": "success",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_FAILED,
                    message=reply,
                    entities={"quote_case_id": case.id, "order_id": case.order_id},
                    payload={
                        "quote_case": {
                            "id": case.id,
                            "case_no": case.case_no,
                            "status": case.status,
                            "order_id": case.order_id,
                            "source_type": case.source_type,
                        },
                        "source_task_id": source_task.id,
                        "joint_sales_image_adjustment": adjustment,
                    },
                ),
                "actions": [],
            },
        )
    task = await _lock_quote_task(db, task)
    if await _quote_task_was_cancelled(db, task=task):
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)
    platform_name = _to_str(source_task.platform_name or case.platform_name or "平台").strip()
    try:
        adjusted_result = _apply_joint_sales_image_adjustment(
            _json_obj(source_task.result_payload),
            premium_value=premium_value,
            base_joint_sales_amount=joint_sales_amount,
        )
        adjusted_result["trace_id"] = trace_id
        adjusted_result["source_quote_task_id"] = source_task.id
        adjusted_result["joint_sales_amount_query"] = joint_sales_amount_query
        platform_name = _to_str(
            source_task.platform_name
            or case.platform_name
            or adjusted_result.get("platform_name")
            or "平台"
        ).strip()
        display_result = _enrich_quote_result_for_display(
            adjusted_result,
            platform_account=platform_account,
            platform_name=platform_name,
            generate_image=not _quote_result_image_async_enabled(),
        )
    except Exception as exc:
        return await _fail_quote_after_result_materialization(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            error=exc,
            platform_account=platform_account,
            preserve_existing_quote=True,
            fallback_task=source_task,
            response_payload={
                "source_task_id": source_task.id,
                "joint_sales_amount_query": joint_sales_amount_query,
            },
            reply_prefix="报价结果图更新失败",
        )
    task = await _lock_quote_task(db, task)
    if await _quote_task_was_cancelled(db, task=task):
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)

    submitted_snapshot["joint_sales_image_adjustment"]["resolved_amount"] = _quote_money_text(joint_sales_amount)
    request_payload = {
        **_json_obj(task.request_payload),
        "resolved_joint_sales_amount": _quote_money_text(joint_sales_amount),
        "joint_sales_amount_query": joint_sales_amount_query,
    }
    now = _now()
    task.status = TASK_STATUS_SUCCESS
    task.login_state = source_task.login_state or "authenticated"
    task.platform_name = platform_name or source_task.platform_name or case.platform_name or source_task.platform_code or case.platform_code
    task.request_payload = request_payload
    task.response_payload = {
        **_json_obj(task.response_payload),
        "reused_quote_result": True,
        "derived_from_real_quote": True,
        "source_task_id": source_task.id,
        "joint_sales_amount_query": joint_sales_amount_query,
    }
    task.result_payload = display_result
    task.submitted_snapshot = submitted_snapshot
    task.finished_at = now
    task.updated_at = now

    case.status = CASE_STATUS_QUOTED
    case.current_task_id = task.id
    case.updated_at = now
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": TASK_STATUS_SUCCESS,
            "trace_id": trace_id,
            "result": display_result,
            "source_task_id": source_task.id,
            "joint_sales_image_adjustment": request_payload["joint_sales_image_adjustment"],
            "joint_sales_amount_query": joint_sales_amount_query,
        },
    )
    await db.flush()

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
            "source_task_id": source_task.id,
        },
        "quote_result": display_result,
        "joint_sales_image_adjustment": request_payload["joint_sales_image_adjustment"],
        "joint_sales_amount_query": joint_sales_amount_query,
    }
    return _quote_result_reply_text(display_result, platform_name=platform_name), {
        "status": "success",
        "intent": "quote",
        "trace_id": trace_id,
        "data": _mk_data(
            result_status=RESULT_SUCCESS,
            message="报价结果图已更新",
            entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
            payload=payload,
        ),
        "actions": [],
    }


def _already_quoted_response(
    *,
    case: QuoteCase,
    task: QuoteTask,
    platform_name: str,
    material_fingerprint: str,
    quote_fingerprint: str = "",
) -> Tuple[str, Dict[str, Any]]:
    result = _json_obj(task.result_payload)
    validation_error = _quote_result_real_data_error(result)
    if validation_error:
        reply = f"{platform_name or '平台'}历史报价结果不完整，本次没有复用结果图：{validation_error}。请重新发起报价。"
        return reply, {
            "status": "success",
            "intent": "quote",
            "trace_id": task.trace_id or _new_trace_id(),
            "data": _mk_data(
                result_status=RESULT_FAILED,
                message=reply,
                entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                payload={
                    "duplicate_quote_blocked": False,
                    "quote_result_validation_error": validation_error,
                    "quote_case": {
                        "id": case.id,
                        "case_no": case.case_no,
                        "status": case.status,
                        "order_id": case.order_id,
                    },
                    "quote_task": {"id": task.id, "status": task.status},
                },
            ),
            "actions": [_mk_action(f"{platform_name or '平台'}报价")],
        }
    try:
        display_result = _enrich_quote_result_for_display(
            result,
            platform_account=None,
            platform_name=platform_name,
            generate_image=not _quote_result_image_async_enabled(),
        )
    except Exception as exc:
        detail = sanitize_quote_user_message(exc, "历史报价结果图生成失败")
        reply = f"{platform_name or '平台'}历史报价结果图生成失败：{detail}。请重新发起报价。"
        return reply, {
            "status": "success",
            "intent": "quote",
            "trace_id": task.trace_id or _new_trace_id(),
            "data": _mk_data(
                result_status=RESULT_FAILED,
                message=reply,
                entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                payload={
                    "duplicate_quote_blocked": False,
                    "result_materialization_failed": True,
                    "result_materialization_error": detail,
                    "quote_case": {
                        "id": case.id,
                        "case_no": case.case_no,
                        "status": case.status,
                        "order_id": case.order_id,
                    },
                    "quote_task": {"id": task.id, "status": task.status},
                },
            ),
            "actions": [_mk_action(f"{platform_name or '平台'}报价")],
        }
    reply = _quote_result_reply_text(display_result, platform_name=platform_name)
    payload = {
        "duplicate_quote_blocked": True,
        "material_fingerprint": material_fingerprint,
        "quote_fingerprint": quote_fingerprint,
        "quote_case": {
            "id": case.id,
            "case_no": case.case_no,
            "status": case.status,
            "order_id": case.order_id,
            "source_type": case.source_type,
            "quote_count": case.quote_count,
            "current_task_id": case.current_task_id,
        },
        "quote_task": {
            "id": task.id,
            "status": task.status,
            "login_state": task.login_state,
            "trace_id": task.trace_id,
            "finished_at": _fmt_dt(task.finished_at),
        },
        "quote_result": display_result,
    }
    return reply, {
        "status": "success",
        "intent": "quote",
        "trace_id": task.trace_id or _new_trace_id(),
        "data": _mk_data(
            result_status=RESULT_SUCCESS,
            message="该材料已完成报价，本次没有重复提交",
            entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
            payload=payload,
        ),
        "actions": [],
    }


def _quote_task_is_sms_wait(task: QuoteTask) -> bool:
    return task.status == TASK_STATUS_WAITING_SMS or task.login_state == "sms_required"


def _align_case_status_with_running_quote_task(case: QuoteCase) -> None:
    """Keep Case/Task paired while a quote attempt is running.

    waiting_sms is only valid when Task is also waiting for a code. Once Task
    moves to running (SMS submitted or reuse-authenticated quote), Case must
    leave waiting_sms so wait finders and active-case logic stay consistent.
    """

    case.status = CASE_STATUS_READY


async def _mark_quote_task_cancelled(
    db: AsyncSession,
    *,
    task: QuoteTask,
    reason: str,
    now: datetime,
    response_extra: Optional[Dict[str, Any]] = None,
) -> bool:
    """Mark one task cancelled. Returns whether it was an SMS wait task."""

    was_sms_task = _quote_task_is_sms_wait(task)
    task.status = TASK_STATUS_CANCELLED
    task.login_state = "failed" if was_sms_task else "authenticated"
    task.error_detail = reason
    if response_extra:
        task.response_payload = {**_json_obj(task.response_payload), **response_extra}
    task.finished_at = now
    task.updated_at = now
    return was_sms_task


async def _expire_account_after_sms_task_cancel(
    db: AsyncSession,
    *,
    task: QuoteTask,
    owner_user_id: int,
    reason: str,
    now: datetime,
) -> None:
    if owner_user_id <= 0:
        return
    account_id = _quote_task_platform_account_id(task)
    if account_id <= 0:
        return
    account = await _get_platform_account_profile_by_id(db, account_id=account_id)
    if account and account.login_status in {ACCOUNT_LOGIN_LOGGING_IN, ACCOUNT_LOGIN_NEEDS_CODE}:
        account.login_status = ACCOUNT_LOGIN_EXPIRED
        account.last_error = reason
        account.last_check_at = now
        account.updated_at = now


async def _cancel_waiting_tasks_for_case(
    db: AsyncSession,
    *,
    case: QuoteCase,
    reason: str,
    now: Optional[datetime] = None,
) -> int:
    ts = now or _now()
    owner_user_id = _safe_int(getattr(case, "owner_user_id", 0), 0)
    active_waiting_tasks = (
        await db.execute(
            select(QuoteTask).where(
                QuoteTask.quote_case_id == case.id,
                QuoteTask.status.in_((TASK_STATUS_WAITING_SMS, TASK_STATUS_WAITING_DUPLICATE_CONFIRM)),
            )
            .with_for_update()
        )
    ).scalars().all()

    cancelled = 0
    for task in active_waiting_tasks:
        was_sms_task = await _mark_quote_task_cancelled(db, task=task, reason=reason, now=ts)
        if was_sms_task:
            await _expire_account_after_sms_task_cancel(
                db,
                task=task,
                owner_user_id=owner_user_id,
                reason=QUOTE_MATERIAL_CHANGED_MESSAGE,
                now=ts,
            )
        cancelled += 1

    if cancelled:
        case.current_task_id = None
        case.updated_at = ts
    return cancelled


async def _cancel_active_quote_tasks_for_case(
    db: AsyncSession,
    *,
    case: QuoteCase,
    reason: str,
    now: Optional[datetime] = None,
    exclude_task_id: int = 0,
) -> int:
    ts = now or _now()
    owner_user_id = _safe_int(getattr(case, "owner_user_id", 0), 0)
    active_tasks = (
        await db.execute(
            select(QuoteTask).where(
                QuoteTask.quote_case_id == case.id,
                QuoteTask.status.in_(
                    (
                        "pending",
                        TASK_STATUS_RUNNING,
                        TASK_STATUS_WAITING_SMS,
                        TASK_STATUS_WAITING_DUPLICATE_CONFIRM,
                    )
                ),
            )
            .with_for_update()
        )
    ).scalars().all()

    cancelled = 0
    for task in active_tasks:
        task_id = _safe_int(getattr(task, "id", 0), 0)
        if exclude_task_id and task_id == exclude_task_id:
            continue
        was_sms_task = await _mark_quote_task_cancelled(
            db,
            task=task,
            reason=reason,
            now=ts,
            response_extra={
                "cancelled_by_new_quote_state": True,
                "cancel_reason": reason,
            },
        )
        if was_sms_task:
            await _expire_account_after_sms_task_cancel(
                db,
                task=task,
                owner_user_id=owner_user_id,
                reason=reason,
                now=ts,
            )
        cancelled += 1

    if cancelled:
        if not exclude_task_id or _safe_int(case.current_task_id, 0) != exclude_task_id:
            case.current_task_id = None
        case.updated_at = ts
    return cancelled


def _quote_superseded_silent_response(
    *,
    case: QuoteCase,
    task: Optional[QuoteTask],
    trace_id: str,
    reason: str = QUOTE_SUPERSEDED_MESSAGE,
) -> Tuple[str, Dict[str, Any]]:
    payload = {
        "quote_superseded": True,
        "reason": reason,
        "quote_case": {
            "id": case.id,
            "case_no": case.case_no,
            "status": case.status,
            "order_id": case.order_id,
            "source_type": case.source_type,
            "current_task_id": case.current_task_id,
        },
        "quote_task": {
            "id": task.id if task is not None else None,
            "status": task.status if task is not None else TASK_STATUS_CANCELLED,
            "trace_id": trace_id,
        },
    }
    data = _mk_data(
        result_status=RESULT_NOT_READY,
        message="",
        entities={"quote_case_id": case.id, "quote_task_id": task.id if task is not None else None, "order_id": case.order_id},
        payload=payload,
    )
    data["silent"] = True
    data["ui_visible"] = False
    return "", {
        "status": "success",
        "intent": "quote",
        "trace_id": trace_id,
        "silent": True,
        "ui_visible": False,
        "data": data,
        "actions": [],
    }


def _quote_notice_already_visible_silent_response(
    *,
    case: QuoteCase,
    task: Optional[QuoteTask],
    trace_id: str,
    message: Any,
) -> Tuple[str, Dict[str, Any]]:
    payload = {
        "quote_notice_already_visible": True,
        "message": sanitize_quote_user_message(message, ""),
        "quote_case": {
            "id": case.id,
            "case_no": case.case_no,
            "status": case.status,
            "order_id": case.order_id,
            "source_type": case.source_type,
            "current_task_id": case.current_task_id,
        },
        "quote_task": {
            "id": task.id if task is not None else None,
            "status": task.status if task is not None else TASK_STATUS_FAILED,
            "trace_id": trace_id,
        },
        "silent": True,
        "ui_visible": False,
    }
    data = _mk_data(
        result_status=RESULT_NOT_READY,
        message="平台提示已写入聊天窗",
        entities={"quote_case_id": case.id, "quote_task_id": task.id if task is not None else None, "order_id": case.order_id},
        payload=payload,
    )
    data["silent"] = True
    data["ui_visible"] = False
    return "", {
        "status": "success",
        "intent": "quote",
        "trace_id": trace_id,
        "silent": True,
        "ui_visible": False,
        "data": data,
        "actions": [],
    }


async def _quote_task_was_cancelled(
    db: AsyncSession,
    *,
    task: Optional[QuoteTask],
) -> bool:
    if task is None:
        return False
    try:
        await db.refresh(task)
    except Exception:
        pass
    return _to_str(getattr(task, "status", "")).strip() == TASK_STATUS_CANCELLED


async def has_waiting_sms_task(db: AsyncSession, ctx: Dict[str, Any]) -> bool:
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        return False
    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    return await _find_waiting_task(db, owner_user_id=owner_user_id, session_id=session_id) is not None


async def has_waiting_duplicate_quote_confirm_task(db: AsyncSession, ctx: Dict[str, Any]) -> bool:
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        return False
    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    return await _find_waiting_duplicate_quote_confirm_task(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
    ) is not None


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


async def has_recent_invalid_sms_task(db: AsyncSession, ctx: Dict[str, Any]) -> bool:
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        return False
    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    return await _find_recent_invalid_sms_task(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
    ) is not None


async def _start_sms_task(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    snapshot: Dict[str, Any],
    trace_id: str,
    platform_account: QuotePlatformAccountProfile,
    challenge_result: Optional[PlatformRuntimeResult] = None,
    challenge_prompt: str = "",
    operation: str = "",
) -> QuoteTask:
    normalized_operation = _to_str(operation).strip() or "quote"
    waiting = (
        await db.execute(
            select(QuoteTask)
            .where(
                QuoteTask.quote_case_id == case.id,
                QuoteTask.platform_code == (case.platform_code or "STUB"),
                QuoteTask.status == TASK_STATUS_WAITING_SMS,
                QuoteTask.login_state == "sms_required",
            )
            .order_by(desc(QuoteTask.id))
            .limit(1)
        )
    ).scalars().first()
    if waiting:
        waiting_operation = _to_str(_json_obj(waiting.request_payload).get("operation")).strip() or "quote"
        if _is_sms_task_expired(waiting):
            await _expire_waiting_sms_task(
                db,
                case=case,
                task=waiting,
                owner_user_id=owner_user_id,
                reason=QUOTE_SMS_EXPIRED_MESSAGE,
            )
            await db.flush()
        elif waiting_operation == normalized_operation:
            case.status = CASE_STATUS_WAITING_SMS
            case.current_task_id = waiting.id
            case.updated_at = _now()
            await db.flush()
            return waiting

    phone = _to_str(platform_account.login_phone).strip()
    phone_mask = platform_account.login_phone_mask or _mask_phone(phone)
    prompt = _to_str(challenge_prompt).strip()
    if not prompt and challenge_result is not None:
        prompt = sanitize_quote_user_message(
            challenge_result.challenge_prompt or challenge_result.message,
            "平台要求输入验证码",
            platform_code=platform_account.platform_code,
            platform_name=platform_account.platform_name,
        )
    if prompt and phone_mask and phone_mask not in prompt:
        prompt = f"{prompt}（发送至 {phone_mask}）"
    account_snapshot = _credential_public_payload(platform_account) or {}
    response_payload = {}
    if challenge_result is not None:
        response_payload["platform_challenge"] = _runtime_result_payload(challenge_result)
    task = QuoteTask(
        quote_case_id=case.id,
        platform_code=case.platform_code or "STUB",
        platform_name=case.platform_name,
        status=TASK_STATUS_WAITING_SMS,
        login_state="sms_required",
        sms_phone_mask=phone_mask,
        trace_id=trace_id,
        request_payload={
            "mode": "pending_sms_challenge",
            "operation": normalized_operation,
            "login": "sms_required",
            "owner_user_id": owner_user_id,
            "platform_account": account_snapshot,
            "platform_default_config": _json_obj(snapshot.get("platform_default_config")),
            "default_config_json": _json_obj(snapshot.get("default_config_json")),
            "vehicle_type_detect": _json_obj(snapshot.get("vehicle_type_detect")),
            "request_body": _json_obj(snapshot.get("request_body")),
        },
        response_payload=response_payload,
        result_payload={},
        submitted_snapshot=snapshot,
        error_detail=prompt or None,
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

    case.status = CASE_STATUS_WAITING_SMS
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


def _duplicate_quote_confirm_reply_text(warning: str, *, platform_name: str = "") -> str:
    safe_warning = _sanitize_duplicate_quote_warning(
        warning,
        f"{platform_name or '平台'}提示该车辆可能重复投保，请核实后再继续报价。",
    )
    return safe_warning


async def _complete_waiting_duplicate_quote_task(
    db: AsyncSession,
    *,
    case: QuoteCase,
    task: QuoteTask,
    owner_user_id: int,
    operator_role_name: Any = "",
) -> Tuple[str, Dict[str, Any]]:
    trace_id = task.trace_id or _new_trace_id()
    platform_code = task.platform_code or case.platform_code or "STUB"
    platform_name = task.platform_name or case.platform_name or platform_code
    perf_started = time.perf_counter()
    perf: Dict[str, Any] = {"login_mode": "sms_verified"}
    snapshot = _json_obj(task.submitted_snapshot)
    account_payload = _json_obj(_json_obj(task.request_payload).get("platform_account"))
    account_id = _safe_int(account_payload.get("id"), 0) or None
    platform_account = (
        await _get_platform_account_profile_by_id(db, account_id=account_id)
        if account_id
        else None
    )
    if platform_account is None:
        task.status = TASK_STATUS_FAILED
        task.login_state = "failed"
        task.error_detail = "平台账号不存在，请重新发起报价"
        task.finished_at = _now()
        task.updated_at = _now()
        case.status = CASE_STATUS_READY
        case.current_task_id = task.id
        case.updated_at = _now()
        await db.flush()
        return _build_quote_user_failure_response(
            reply=f"{platform_name}报价失败：平台账号不存在，请重新发起报价。",
            case=case,
            task=task,
            trace_id=trace_id,
            failure_code=FAILURE_CODE_ACCOUNT_MISSING,
            failure_reason="平台账号不存在",
            result_status=RESULT_FAILED,
            response_status="failed",
            actions=[_mk_action(f"{platform_name}报价")],
            payload={"quote_task": {"id": task.id, "status": task.status, "trace_id": trace_id}},
        )
    if not await _quote_snapshot_material_is_current(db, case=case, snapshot=snapshot):
        return await _stop_quote_for_material_change(
            db,
            case=case,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            task=task,
            platform_account=platform_account,
        )
    if not await _quote_snapshot_default_config_is_current(db, snapshot=snapshot, platform_code=platform_code):
        return await _stop_quote_for_default_config_change(
            db,
            case=case,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            task=task,
            platform_account=platform_account,
        )

    confirmed_snapshot = _duplicate_quote_confirmed_snapshot(snapshot)
    task.status = TASK_STATUS_CANCELLED
    task.login_state = "authenticated"
    task.error_detail = "用户已确认继续重复投保报价，转入正式报价"
    task.finished_at = _now()
    task.updated_at = _now()
    case.status = CASE_STATUS_READY
    case.current_task_id = None
    case.updated_at = _now()
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": TASK_STATUS_CANCELLED,
            "trace_id": trace_id,
            "duplicate_quote_confirmed": True,
        },
    )
    await db.flush()

    config_type_name = _normalize_account_type_name(
        _json_obj(confirmed_snapshot.get("platform_default_config")).get("resolved_type_name")
        or _json_obj(confirmed_snapshot.get("platform_default_config")).get("account_type_name")
        or _json_obj(confirmed_snapshot.get("vehicle_type_detect")).get("config_type_name")
        or platform_account.account_type_name
    )
    return await _continue_quote_with_platform_account(
        db,
        case=case,
        owner_user_id=owner_user_id,
        snapshot=confirmed_snapshot,
        trace_id=trace_id,
        platform_account=platform_account,
        merged_entities={"quote_case_id": case.id, "order_id": case.order_id, "platform_code": platform_code, "platform_name": platform_name},
        normalized_data=_json_obj(confirmed_snapshot.get("normalized_data")),
        images_by_slot=_json_obj(confirmed_snapshot.get("images_by_slot")),
        attached_images=[],
        config_type_name=config_type_name,
        attempted_account_ids={account_id} if account_id else None,
        operator_role_name=operator_role_name,
    )


async def _cancel_waiting_duplicate_quote_task(
    db: AsyncSession,
    *,
    case: QuoteCase,
    task: QuoteTask,
    owner_user_id: int,
    reason: str = "用户已中止重复投保报价",
) -> Tuple[str, Dict[str, Any]]:
    trace_id = task.trace_id or _new_trace_id()
    now = _now()
    task.status = TASK_STATUS_CANCELLED
    task.login_state = "authenticated"
    task.error_detail = reason
    task.finished_at = now
    task.updated_at = now
    if case.current_task_id == task.id:
        case.current_task_id = None

    normalized_data, images_by_slot, missing = await _refresh_quote_case_material_state(
        db,
        case,
        preserve_quoted=False,
        now=now,
    )
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": TASK_STATUS_CANCELLED,
            "trace_id": trace_id,
            "duplicate_quote_cancelled": True,
            "reason": reason,
        },
    )
    await db.flush()
    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=missing,
        task=task,
        platform_account=None,
    )
    payload["duplicate_quote_cancelled"] = True
    payload["ui_visible"] = False
    data = _mk_data(
        result_status=RESULT_NOT_READY,
        message=reason,
        entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
        payload=payload,
    )
    data["silent"] = True
    data["ui_visible"] = False
    return (
        "已中止本次重复报价。",
        {
            "status": "success",
            "intent": "quote",
            "trace_id": trace_id,
            "silent": True,
            "ui_visible": False,
            "data": data,
            "actions": [],
        },
    )


async def _mark_quote_account_challenge_attention(
    db: AsyncSession,
    *,
    account: QuotePlatformAccountProfile,
    runtime_result: Optional[PlatformRuntimeResult],
    operator_user_id: Optional[int],
    source: str = "quote_flow",
) -> str:
    before = _account_event_snapshot(account)
    platform_code = _to_str(account.platform_code).strip().upper()
    platform_name = _to_str(account.platform_name).strip() or platform_code
    phone_mask = account.login_phone_mask or _mask_phone(account.login_phone)
    prompt = sanitize_quote_user_message(
        getattr(runtime_result, "challenge_prompt", "") or getattr(runtime_result, "message", ""),
        f"{platform_name}要求输入验证码",
        platform_code=platform_code,
        platform_name=platform_name,
    )
    if phone_mask and phone_mask not in prompt:
        prompt = f"{prompt}（发送至 {phone_mask}）"

    now = _now()
    active_task = (
        await db.execute(
            select(QuotePlatformAccountLoginTask)
            .where(
                QuotePlatformAccountLoginTask.account_id == int(account.id),
                QuotePlatformAccountLoginTask.owner_user_id == int(account.owner_user_id or 0),
                QuotePlatformAccountLoginTask.status.in_([LOGIN_TASK_RUNNING, LOGIN_TASK_NEEDS_CODE]),
            )
            .order_by(desc(QuotePlatformAccountLoginTask.id))
            .limit(1)
        )
    ).scalars().first()
    if active_task and active_task.expires_at and now > active_task.expires_at:
        active_task.status = LOGIN_TASK_EXPIRED
        active_task.error_detail = "登录验证码已过期，请重新点击登录"
        active_task.finished_at = now
        active_task.updated_at = now
        active_task = None

    challenge_payload = _json_obj(_json_obj(getattr(runtime_result, "data", None)).get("challenge_payload"))
    code_length = _safe_int(challenge_payload.get("code_length"), 0)
    task_payload = {
        "phone_mask": phone_mask or "",
        "code_length": code_length or "4-8",
        "platform_runtime": _runtime_result_payload(runtime_result),
    }
    if active_task is None:
        active_task = QuotePlatformAccountLoginTask(
            account_id=int(account.id),
            owner_user_id=int(account.owner_user_id or 0),
            platform_code=platform_code,
            platform_name=platform_name,
            status=LOGIN_TASK_NEEDS_CODE,
            challenge_type=getattr(runtime_result, "challenge_type", None) or "sms",
            challenge_prompt=prompt,
            challenge_payload=task_payload,
            trace_id=_new_trace_id(),
            started_at=now,
            expires_at=now
            + timedelta(
                seconds=_login_challenge_ttl_seconds(
                    platform_code,
                    getattr(runtime_result, "challenge_type", None) or "sms",
                )
            ),
        )
        db.add(active_task)
        await db.flush()
    else:
        active_task.status = LOGIN_TASK_NEEDS_CODE
        active_task.challenge_type = getattr(runtime_result, "challenge_type", None) or active_task.challenge_type or "sms"
        active_task.challenge_prompt = prompt
        active_task.challenge_payload = task_payload
        active_task.error_detail = None
        active_task.updated_at = now
        if not active_task.expires_at:
            active_task.expires_at = now + timedelta(seconds=QUOTE_SMS_CODE_TTL_SECONDS)

    account.login_status = ACCOUNT_LOGIN_NEEDS_CODE
    account.last_error = prompt
    account.last_check_at = now
    account.updated_at = now
    _set_account_inspection_notice(
        account,
        notice_type="login_challenge",
        message=prompt,
        task_id=active_task.id,
        level="warning",
        payload={
            "source": source,
            "challenge_type": active_task.challenge_type,
            "platform_runtime": _runtime_result_payload(runtime_result),
        },
    )
    await _add_account_event(
        db,
        account=account,
        event_type="login",
        operator_user_id=operator_user_id,
        before=before,
        after=_account_event_snapshot(account),
        message=f"报价流程触发登录验证码：{prompt}",
    )
    await db.flush()
    return prompt


async def _sync_account_login_task_after_quote_challenge(
    db: AsyncSession,
    *,
    account: Optional[QuotePlatformAccountProfile],
    runtime_result: Optional[PlatformRuntimeResult],
    status: str,
    prompt: str = "",
    error_detail: str = "",
) -> None:
    if account is None or not getattr(account, "id", None):
        return
    active_task = (
        await db.execute(
            select(QuotePlatformAccountLoginTask)
            .where(
                QuotePlatformAccountLoginTask.account_id == int(account.id),
                QuotePlatformAccountLoginTask.owner_user_id == int(account.owner_user_id or 0),
                QuotePlatformAccountLoginTask.status.in_([LOGIN_TASK_RUNNING, LOGIN_TASK_NEEDS_CODE]),
            )
            .order_by(desc(QuotePlatformAccountLoginTask.id))
            .limit(1)
        )
    ).scalars().first()
    if active_task is None:
        return
    now = _now()
    runtime_payload = _runtime_result_payload(runtime_result)
    challenge_payload = _json_obj(_json_obj(getattr(runtime_result, "data", None)).get("challenge_payload"))
    code_length = _safe_int(challenge_payload.get("code_length"), 0)
    normalized_status = _to_str(status).strip()
    if normalized_status == LOGIN_TASK_NEEDS_CODE:
        phone_mask = account.login_phone_mask or _mask_phone(account.login_phone)
        active_task.status = LOGIN_TASK_NEEDS_CODE
        active_task.challenge_type = getattr(runtime_result, "challenge_type", None) or active_task.challenge_type or "sms"
        active_task.challenge_prompt = _to_str(prompt).strip() or sanitize_quote_user_message(
            getattr(runtime_result, "challenge_prompt", "") or getattr(runtime_result, "message", ""),
            "平台要求继续验证码校验",
            platform_code=account.platform_code,
            platform_name=account.platform_name,
        )
        active_task.challenge_payload = {
            "phone_mask": phone_mask or "",
            "code_length": code_length or "4-8",
            "platform_runtime": runtime_payload,
        }
        active_task.error_detail = None
        active_task.expires_at = now + timedelta(
            seconds=_login_challenge_ttl_seconds(account.platform_code, active_task.challenge_type)
        )
        active_task.updated_at = now
        return

    active_task.status = normalized_status if normalized_status in {LOGIN_TASK_SUCCESS, LOGIN_TASK_FAILED, LOGIN_TASK_EXPIRED} else LOGIN_TASK_FAILED
    active_task.error_detail = _to_str(error_detail).strip() or sanitize_quote_user_message(
        getattr(runtime_result, "message", ""),
        "验证码处理失败" if active_task.status != LOGIN_TASK_SUCCESS else "",
        platform_code=account.platform_code,
        platform_name=account.platform_name,
    )
    active_task.finished_at = now
    active_task.updated_at = now


async def _quote_snapshot_for_account(
    db: AsyncSession,
    *,
    case: QuoteCase,
    snapshot: Dict[str, Any],
    platform_account: QuotePlatformAccountProfile,
    config_type_name: Optional[str] = None,
) -> Dict[str, Any]:
    platform_code = case.platform_code or platform_account.platform_code or ""
    effective_config_type = _normalize_account_type_name(config_type_name) or _normalize_account_type_name(
        platform_account.account_type_name
    )
    return await apply_platform_default_config_to_snapshot(
        db,
        snapshot=snapshot,
        platform_code=platform_code,
        account_type_name=platform_account.account_type_name,
        config_type_name=effective_config_type,
    )


async def _continue_quote_with_platform_account(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    snapshot: Dict[str, Any],
    trace_id: str,
    platform_account: QuotePlatformAccountProfile,
    merged_entities: Dict[str, Any],
    normalized_data: Dict[str, Any],
    images_by_slot: Dict[str, List[Dict[str, Any]]],
    attached_images: Optional[List[Dict[str, Any]]] = None,
    config_type_name: Optional[str] = None,
    attempted_account_ids: Optional[Iterable[int]] = None,
    reply_prefix: str = "",
    operator_role_name: Any = "",
) -> Tuple[str, Dict[str, Any]]:
    platform_code = case.platform_code or platform_account.platform_code or "STUB"
    platform_name = case.platform_name or platform_account.platform_name or platform_code
    account_snapshot = await _quote_snapshot_for_account(
        db,
        case=case,
        snapshot=snapshot,
        platform_account=platform_account,
        config_type_name=config_type_name,
    )
    selected_type_name = _normalize_account_type_name(config_type_name) or _normalize_account_type_name(
        platform_account.account_type_name
    )
    platform_default_config = _json_obj(account_snapshot.get("platform_default_config"))
    if selected_type_name and _to_str(platform_default_config.get("matched")).strip() != "account_type":
        case.status = CASE_STATUS_READY
        case.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="status",
            role="assistant",
            payload={
                "status": "ready",
                "need_platform_default_config": True,
                "platform_code": platform_code,
                "account_type_name": selected_type_name,
                "platform_account_id": platform_account.id,
            },
        )
        await db.flush()
        payload = _case_payload(
            case=case,
            normalized_data=normalized_data,
            images_by_slot=images_by_slot,
            missing=[],
            attached_images=attached_images or [],
            platform_account=platform_account,
        )
        payload["platform_default_config"] = platform_default_config
        payload["default_config_json"] = _json_obj(account_snapshot.get("default_config_json"))
        config_action_text = _quote_account_action_text(
            operator_role_name,
            "请先在右上角“默认参数配置”中新增并启用该账号类型配置，再重新发起报价。",
            "请联系管理员在“默认参数配置”中新增并启用该账号类型配置后，再重新发起报价。",
        )
        preflight_items = [
            _quote_preflight_item(
                code="default_config_missing",
                category="default_config",
                label=f"{platform_name}（{selected_type_name}）尚未启用默认参数配置",
                detail=config_action_text,
                failure_code=FAILURE_CODE_DEFAULT_CONFIG_MISSING,
            )
        ]
        reply, body = _build_quote_preflight_blocked_response(
            case=case,
            platform_code=platform_code,
            platform_name=platform_name,
            selected_account_type_name=selected_type_name,
            items=preflight_items,
            merged_entities=merged_entities,
            payload=payload,
            attached_images=attached_images or [],
            operator_role_name=operator_role_name,
            trace_id=trace_id,
        )
        if reply_prefix:
            reply = f"{reply_prefix}{reply}"
            data = body.get("data")
            if isinstance(data, dict):
                data["message"] = reply
        return reply, body

    if platform_account.login_status in {ACCOUNT_LOGIN_AUTHENTICATED, ACCOUNT_LOGIN_DEGRADED} and await _account_has_usable_session_snapshot(platform_account):
        reply, meta = await _complete_quote_without_sms(
            db,
            case=case,
            owner_user_id=owner_user_id,
            snapshot=account_snapshot,
            trace_id=trace_id,
            platform_account=platform_account,
            login_mode="reuse_authenticated",
            attempted_account_ids=attempted_account_ids,
            account_type_name=config_type_name,
            operator_role_name=operator_role_name,
        )
        return reply, meta

    retry = await _retry_quote_with_next_platform_account(
        db,
        case=case,
        owner_user_id=owner_user_id,
        snapshot=account_snapshot,
        trace_id=trace_id,
        current_account=platform_account,
        config_type_name=selected_type_name,
        merged_entities=merged_entities,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        attached_images=attached_images,
        attempted_account_ids=attempted_account_ids,
        reason_text="当前账号没有已登录可用会话",
        operator_role_name=operator_role_name,
    )
    if retry is not None:
        return retry

    case.status = CASE_STATUS_READY
    case.current_task_id = None
    case.updated_at = _now()
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="status",
        role="assistant",
        payload={
            "status": "ready",
            "need_platform_account_login": True,
            "platform_account_id": platform_account.id,
            "platform_code": platform_code,
        },
    )
    await db.flush()
    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=[],
        attached_images=attached_images or [],
        platform_account=platform_account,
    )
    admin_text = f"{platform_name}报价资料已齐，但当前没有已登录且存活可用的平台账号。请先在右上角“平台账号管理”完成登录后再报价。"
    contact_text = f"{platform_name}报价资料已齐，但平台账号没有已登录可用会话，请联系管理员处理。"
    return (
        f"{reply_prefix}{_quote_account_action_text(operator_role_name, admin_text, contact_text)}",
        {
            "status": "success",
            "intent": "quote",
            "trace_id": trace_id,
            "data": _mk_data(
                result_status=RESULT_NEED_MORE,
                message="平台账号没有已登录可用会话",
                entities={**merged_entities, "quote_case_id": case.id, "order_id": case.order_id},
                payload=payload,
            ),
            "actions": _quote_platform_account_manage_actions(
                operator_role_name,
                platform_code=platform_code,
                platform_name=platform_name,
            ),
        },
    )


def _renewal_context_compare_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", _to_str(value).upper())


def _renewal_context_same_text(left: Any, right: Any) -> bool:
    lval = _renewal_context_compare_text(left)
    rval = _renewal_context_compare_text(right)
    return bool(lval and rval and lval == rval)


def _renewal_context_text_match_score(left: Any, right: Any, *, matched: int, mismatched: int) -> int:
    lval = _renewal_context_compare_text(left)
    rval = _renewal_context_compare_text(right)
    if not lval or not rval:
        return 0
    return matched if lval == rval else -mismatched


def _renewal_context_license_type(value: Any) -> str:
    data = _json_obj(value)
    decision = _json_obj(data.get(LICENSE_TYPE_DECISION_KEY))
    return _normalize_license_type_value(
        data.get("license_type")
        or data.get("licenseType")
        or decision.get("license_type")
        or decision.get("licenseType")
    )


def _renewal_candidate_score_for_current(row: Mapping[str, Any], current: Optional[Mapping[str, Any]] = None) -> int:
    current = _json_obj(current)
    score = 0
    score += _renewal_context_text_match_score(
        row.get("license_no") or row.get("licenseNo"),
        current.get("plate_no") or current.get("license_no"),
        matched=100,
        mismatched=160,
    )
    score += _renewal_context_text_match_score(
        row.get("vin") or row.get("vinNo") or row.get("frameNo"),
        current.get("vin"),
        matched=120,
        mismatched=420,
    )
    score += _renewal_context_text_match_score(
        row.get("engine_no") or row.get("engineNo"),
        current.get("engine_no"),
        matched=90,
        mismatched=220,
    )
    row_license_type = _normalize_license_type_value(row.get("license_type") or row.get("licenseType"))
    current_license_type = _renewal_context_license_type(current)
    if row_license_type and current_license_type and row_license_type == current_license_type:
        score += 50
    elif row_license_type and current_license_type:
        score -= 120
    if _to_str(row.get("renewal_or_copy_flag") or row.get("renewalOrCopyFlag")).strip() == "1":
        score += 30
    if _to_str(row.get("policy_no_encode") or row.get("policyNoEncode")).strip():
        score += 20
    if _to_str(row.get("relation_policy_no_encode") or row.get("relationPolicyNoEncode")).strip():
        score += 20
    if _to_str(row.get("risk_code") or row.get("riskCode")).strip().upper() == "DAA":
        score += 10
    return score


def _renewal_end_ord(value: Any) -> int:
    text = _normalize_quote_date_text(value)
    if not text:
        return 0
    try:
        return datetime.strptime(text, "%Y-%m-%d").toordinal()
    except Exception:
        return 0


def _renewal_lookup_primary_candidate(value: Any, current: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    candidates = [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda item: (
            _renewal_candidate_score_for_current(item, current),
            _renewal_end_ord(item.get("end_date") or item.get("endDate")),
            2 if _to_str(item.get("risk_code")).strip().upper() == "DAA" else 1,
        ),
    )


def _has_reusable_renewal_quote_context(value: Any) -> bool:
    data = _json_obj(value)
    lookup = _json_obj(data.get("renewal_lookup"))
    found = lookup.get("found")
    if found is not True and _to_str(found).strip().lower() not in {"1", "true", "yes"}:
        return False
    selected = _json_obj(lookup.get("selected")) or {
        "policy_no": _to_str(lookup.get("selected_policy_no")).strip(),
        "policy_no_encode": _to_str(lookup.get("selected_policy_no_encode")).strip(),
        "risk_code": _to_str(lookup.get("selected_risk_code")).strip(),
        "end_date": _to_str(lookup.get("selected_end_date")).strip(),
        "license_type": _normalize_license_type_value(lookup.get("selected_license_type")),
    }
    policy_no = (
        _to_str(selected.get("policy_no")).strip()
        or _to_str(selected.get("policyNo")).strip()
        or _to_str(lookup.get("selected_policy_no")).strip()
    )
    policy_no_encode = (
        _to_str(selected.get("policy_no_encode")).strip()
        or _to_str(selected.get("policyNoEncode")).strip()
        or _to_str(lookup.get("selected_policy_no_encode")).strip()
    )
    if not (policy_no and policy_no_encode):
        return False

    checks = (
        (selected.get("license_no") or selected.get("licenseNo"), data.get("plate_no") or data.get("license_no")),
        (selected.get("vin") or selected.get("vinNo") or selected.get("frameNo"), data.get("vin")),
        (selected.get("engine_no") or selected.get("engineNo"), data.get("engine_no")),
    )
    for left, right in checks:
        if _to_str(left).strip() and _to_str(right).strip() and not _renewal_context_same_text(left, right):
            return False
    selected_license_type = _normalize_license_type_value(selected.get("license_type") or selected.get("licenseType"))
    current_license_type = _renewal_context_license_type(data)
    if selected_license_type and current_license_type and selected_license_type != current_license_type:
        return False
    return True


def _should_auto_probe_renewal_before_normal_quote(
    *,
    platform_code: Any,
    quote_flow_type: Any,
    account_type_name: Any,
    normalized_data: Any,
) -> bool:
    if _to_str(platform_code).strip().upper() != "PICC":
        return False
    if _to_str(quote_flow_type).strip() != QUOTE_FLOW_NORMAL:
        return False
    type_name = _normalize_account_type_name(account_type_name)
    if type_name not in {"油车-旧", "新能源车-旧"}:
        return False
    data = _clean_quote_dynamic_data(_json_obj(normalized_data))
    if _has_reusable_renewal_quote_context(data):
        return False
    return bool(_to_str(data.get("plate_no")).strip() and (_to_str(data.get("engine_no")).strip() or _to_str(data.get("vin")).strip()))


def _is_silent_auto_renewal_not_found_response(response: Mapping[str, Any]) -> bool:
    """Whether an auto renewal probe should fall through to the normal quote path.

    Only the explicit auto_probe_fallthrough marker counts. Soft exits (not found,
    runtime failure, no session) must return _auto_renewal_probe_fallthrough_response.
    """

    payload = _json_obj(_json_obj(_json_obj(response).get("data")).get("payload"))
    lookup = _json_obj(payload.get("renewal_lookup"))
    return lookup.get("auto_probe_fallthrough") is True


def _auto_renewal_probe_fallthrough_response(
    *,
    case: QuoteCase,
    trace_id: str,
    reason: str = "auto_renewal_probe_fallthrough",
) -> Tuple[str, Dict[str, Any]]:
    return (
        "",
        {
            "status": "success",
            "intent": "quote",
            "trace_id": trace_id,
            "silent": True,
            "ui_visible": False,
            "data": _mk_data(
                result_status=RESULT_NOT_READY,
                message=reason,
                entities={"quote_case_id": case.id, "order_id": case.order_id},
                payload={
                    "renewal_lookup": {
                        "found": False,
                        "auto_probe_fallthrough": True,
                        "reason": reason,
                    },
                    "silent": True,
                    "ui_visible": False,
                },
            ),
            "actions": [],
        },
    )


async def _complete_renewal_lookup_without_sms(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    snapshot: Dict[str, Any],
    trace_id: str,
    platform_account: QuotePlatformAccountProfile,
    login_mode: str,
    task: Optional[QuoteTask] = None,
    operator_role_name: Any = "",
    auto_probe: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    platform_code = case.platform_code or platform_account.platform_code or "PICC"
    platform_name = case.platform_name or platform_account.platform_name or "人保"
    if not await _quote_snapshot_material_is_current(db, case=case, snapshot=snapshot):
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)

    if task is None:
        task = QuoteTask(
            quote_case_id=case.id,
            platform_code=platform_code,
            platform_name=platform_name,
            status=TASK_STATUS_RUNNING,
            login_state="authenticated",
            sms_phone_mask=platform_account.login_phone_mask,
            trace_id=trace_id,
            request_payload={
                "operation": RENEWAL_LOOKUP_OPERATION,
                "login": login_mode,
                "owner_user_id": owner_user_id,
                "platform_account": _credential_public_payload(platform_account),
                "vehicle_type_detect": _json_obj(snapshot.get("vehicle_type_detect")),
                "normalized_data": _json_obj(snapshot.get("normalized_data")),
            },
            response_payload={},
            result_payload={},
            submitted_snapshot=snapshot,
            started_at=_now(),
        )
        db.add(task)
        await db.flush()
    else:
        task.status = TASK_STATUS_RUNNING
        task.login_state = "authenticated"
        task.error_detail = None
        task.request_payload = {
            **_json_obj(task.request_payload),
            "operation": RENEWAL_LOOKUP_OPERATION,
            "login": login_mode,
            "platform_account": _credential_public_payload(platform_account),
            "vehicle_type_detect": _json_obj(snapshot.get("vehicle_type_detect")),
            "normalized_data": _json_obj(snapshot.get("normalized_data")),
        }
        task.updated_at = _now()

    case.status = CASE_STATUS_READY
    case.current_task_id = task.id
    case.updated_at = _now()
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": TASK_STATUS_RUNNING,
            "trace_id": trace_id,
            "operation": RENEWAL_LOOKUP_OPERATION,
            "login_mode": login_mode,
        },
    )
    await db.flush()
    await db.commit()

    platform_ctx = _platform_account_quote_context(
        platform_account,
        account_type_name=_normalize_account_type_name(
            _json_obj(snapshot.get("vehicle_type_detect")).get("config_type_name")
            or platform_account.account_type_name
        ),
    )
    started = time.perf_counter()
    async with release_chat_session_lock_for_platform_io():
        runtime_result = await quote_platform_runtime.query_renewal(platform_ctx, snapshot, db=db)
    perf = {"login_mode": login_mode, "renewal_lookup_ms": _elapsed_ms(started)}
    runtime_status = _runtime_status(runtime_result)
    runtime_data = _json_obj(getattr(runtime_result, "data", None))
    business_status = _to_str(runtime_data.get("business_status")).strip()
    task = await _lock_quote_task(db, task)
    if await _quote_task_was_cancelled(db, task=task):
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)

    task.response_payload = {
        "renewal_lookup": _runtime_result_payload(runtime_result),
        "perf": perf,
    }
    task.finished_at = _now()
    task.updated_at = _now()
    case.status = CASE_STATUS_READY
    case.current_task_id = task.id
    case.updated_at = _now()

    if runtime_status not in {"success", "ok"}:
        _apply_platform_account_runtime_status(platform_account, runtime_result, default_error="人保续保查询失败")
        detail = _runtime_detail(runtime_result, "人保续保查询失败")
        task.status = TASK_STATUS_FAILED
        task.login_state = "authenticated"
        task.error_detail = detail
        platform_account.last_error = detail
        platform_account.last_check_at = _now()
        platform_account.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={
                "task_id": task.id,
                "status": task.status,
                "trace_id": trace_id,
                "operation": RENEWAL_LOOKUP_OPERATION,
                "reason": detail,
            },
        )
        await db.flush()
        if auto_probe:
            return _auto_renewal_probe_fallthrough_response(
                case=case,
                trace_id=trace_id,
                reason=detail or "auto_renewal_probe_runtime_failed",
            )
        if _is_runtime_session_expired_result(runtime_result) and _quote_account_needs_admin_contact(operator_role_name):
            detail = f"{platform_name}账号登录已过期，请联系管理员处理。"
        failure_code = (
            FAILURE_CODE_SESSION_EXPIRED
            if _is_runtime_session_expired_result(runtime_result)
            else FAILURE_CODE_PLATFORM
        )
        return _build_quote_user_failure_response(
            reply=f"{platform_name}续保查询失败：{detail}",
            case=case,
            task=task,
            trace_id=trace_id,
            failure_code=failure_code,
            failure_reason=detail,
            result_status=RESULT_FAILED,
            response_status="failed",
            actions=[],
            payload={
                "quote_task": {"id": task.id, "status": task.status, "trace_id": trace_id},
                "platform_account": _credential_public_payload(platform_account),
                "renewal_lookup": _runtime_result_payload(runtime_result),
                "operation": RENEWAL_LOOKUP_OPERATION,
            },
        )

    lookup = _json_obj(runtime_data.get("renewal_lookup"))
    candidates = lookup.get("candidates") if isinstance(lookup.get("candidates"), list) else []
    lookup_vehicle = _json_obj(lookup.get("vehicle")) or _json_obj(case.normalized_data)
    primary = _json_obj(lookup.get("selected")) or _renewal_lookup_primary_candidate(candidates, lookup_vehicle)
    lookup_found = bool(runtime_data.get("renewal_found")) and bool(primary)
    if not lookup_found or business_status == "renewal_not_found":
        detail = _runtime_detail(runtime_result, "没有此车辆信息或不是可续保车辆")
        task.status = TASK_STATUS_SUCCESS
        task.login_state = "authenticated"
        task.error_detail = detail
        task.result_payload = {
            "renewal_lookup": {
                "found": False,
                "vehicle": _json_obj(lookup.get("vehicle")),
                "candidates": [],
            }
        }
        await _mark_platform_account_used(
            db,
            account_id=platform_account.id,
            owner_user_id=owner_user_id,
            login_state=ACCOUNT_LOGIN_AUTHENTICATED,
            consume_quota=False,
        )
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={
                "task_id": task.id,
                "status": task.status,
                "trace_id": trace_id,
                "operation": RENEWAL_LOOKUP_OPERATION,
                "renewal_found": False,
                "reason": detail,
            },
        )
        await db.flush()
        if auto_probe:
            return _auto_renewal_probe_fallthrough_response(
                case=case,
                trace_id=trace_id,
                reason=detail or "auto_renewal_probe_not_found",
            )
        return (
            detail,
            {
                "status": "success",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_NOT_READY,
                    message=detail,
                    entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                    payload={
                        "quote_task": {"id": task.id, "status": task.status, "trace_id": trace_id},
                        "renewal_lookup": task.result_payload["renewal_lookup"],
                    },
                ),
                "actions": [],
            },
        )

    updated_data = dict(_json_obj(case.normalized_data))
    updated_data[QUOTE_FLOW_TYPE_KEY] = QUOTE_FLOW_RENEWAL
    selected_license_type = _normalize_license_type_value(primary.get("license_type"))
    updated_data["renewal_lookup"] = {
        "found": True,
        # Keep the complete selected row. A later command such as "司乘改3万"
        # must be able to reuse this renewal context and call quotePolicy.do.
        "selected": primary,
        "candidates": candidates,
        "selected_policy_no": _to_str(primary.get("policy_no")).strip(),
        "selected_policy_no_encode": _to_str(primary.get("policy_no_encode")).strip(),
        "selected_risk_code": _to_str(primary.get("risk_code")).strip(),
        "selected_end_date": _to_str(primary.get("end_date")).strip(),
        "selected_license_type": selected_license_type,
        "candidate_count": len(candidates),
        "selected_score": lookup.get("selected_score"),
        "selected_reason": lookup.get("selected_reason"),
    }
    if selected_license_type:
        updated_data["license_type"] = selected_license_type
        updated_data["license_color_code"] = _license_color_for_type(selected_license_type)
        if selected_license_type == "52":
            updated_data["account_type_name"] = "新能源车-旧"
        elif selected_license_type == "02":
            updated_data["account_type_name"] = "油车-旧"
        updated_data[LICENSE_TYPE_DECISION_KEY] = _license_type_decision_payload(
            selected_license_type,
            source="renewal_lookup",
            reason="人保续保查询返回号牌种类",
        )
    case.normalized_data = _normalize_quote_case_data(
        base_data=updated_data,
        order_data={},
        text_data={},
        images_by_slot=_json_obj(snapshot.get("images_by_slot")),
    )
    case.draft_order_data = case.normalized_data
    task.status = TASK_STATUS_SUCCESS
    task.login_state = "authenticated"
    task.error_detail = None
    task.result_payload = {
        "renewal_lookup": {
            "found": True,
            "vehicle": _json_obj(lookup.get("vehicle")),
            "selected": primary,
            "candidate_count": len(candidates),
        }
    }
    await _mark_platform_account_used(
        db,
        account_id=platform_account.id,
        owner_user_id=owner_user_id,
        login_state=ACCOUNT_LOGIN_AUTHENTICATED,
        consume_quota=False,
    )
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": task.status,
            "trace_id": trace_id,
            "operation": RENEWAL_LOOKUP_OPERATION,
            "renewal_found": True,
            "selected": primary,
        },
    )
    await db.flush()
    await db.commit()

    refreshed_images = await _active_images_by_slot(db, int(case.id))
    refreshed_normalized = _normalize_quote_case_data(
        base_data=_json_obj(case.normalized_data),
        order_data={},
        text_data={},
        images_by_slot=refreshed_images,
    )
    case.normalized_data = refreshed_normalized
    case.draft_order_data = refreshed_normalized
    quote_snapshot = _snapshot_payload(case=case, normalized_data=refreshed_normalized, images_by_slot=refreshed_images)
    quote_snapshot = {
        **quote_snapshot,
        "vehicle_type_detect": detect_quote_vehicle_type(refreshed_normalized, refreshed_images),
        "quote_flow_type": QUOTE_FLOW_RENEWAL,
        QUOTE_PRODUCT_EXCLUSIONS_KEY: _normalize_quote_product_exclusions(
            refreshed_normalized.get(QUOTE_PRODUCT_EXCLUSIONS_KEY)
        ),
    }
    quote_trace_id = _new_trace_id()
    return await _continue_quote_with_platform_account(
        db,
        case=case,
        owner_user_id=owner_user_id,
        snapshot=quote_snapshot,
        trace_id=quote_trace_id,
        platform_account=platform_account,
        merged_entities={
            "platform_code": platform_code,
            "platform_name": platform_name,
            QUOTE_FLOW_TYPE_KEY: QUOTE_FLOW_RENEWAL,
            "quote_case_id": case.id,
            "renewal_lookup_task_id": task.id,
        },
        normalized_data=refreshed_normalized,
        images_by_slot=refreshed_images,
        attached_images=[],
        config_type_name=_normalize_account_type_name(
            _json_obj(quote_snapshot.get("vehicle_type_detect")).get("config_type_name")
            or refreshed_normalized.get("account_type_name")
            or "油车-旧"
        ),
        operator_role_name=operator_role_name,
    )


async def _continue_renewal_lookup_with_platform_account(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    snapshot: Dict[str, Any],
    trace_id: str,
    platform_account: QuotePlatformAccountProfile,
    operator_role_name: Any = "",
    auto_probe: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    platform_code = case.platform_code or platform_account.platform_code or "PICC"
    platform_name = case.platform_name or platform_account.platform_name or "人保"
    if platform_account.login_status in {ACCOUNT_LOGIN_AUTHENTICATED, ACCOUNT_LOGIN_DEGRADED} and await _account_has_usable_session_snapshot(platform_account):
        return await _complete_renewal_lookup_without_sms(
            db,
            case=case,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            trace_id=trace_id,
            platform_account=platform_account,
            login_mode="reuse_authenticated",
            operator_role_name=operator_role_name,
            auto_probe=auto_probe,
        )

    if auto_probe:
        return _auto_renewal_probe_fallthrough_response(
            case=case,
            trace_id=trace_id,
            reason="auto_renewal_probe_no_usable_session",
        )

    message = _quote_account_action_text(
        operator_role_name,
        f"{platform_name}续保资料已齐，但当前账号没有可用登录会话。请先完成平台账号登录后再发起续保查询。",
        f"{platform_name}续保资料已齐，但平台账号没有可用登录会话，请联系管理员处理。",
    )
    return (
        message,
        {
            "status": "success",
            "intent": "quote",
            "trace_id": trace_id,
            "data": _mk_data(
                result_status=RESULT_NEED_MORE,
                message="平台账号没有可用登录会话",
                entities={"quote_case_id": case.id, "order_id": case.order_id},
                payload={"platform_account": _credential_public_payload(platform_account)},
            ),
            "actions": _quote_platform_account_manage_actions(
                operator_role_name,
                platform_code=platform_code,
                platform_name=platform_name,
            ),
        },
    )


async def _retry_quote_with_next_platform_account(
    db: AsyncSession,
    *,
    case: QuoteCase,
    owner_user_id: int,
    snapshot: Dict[str, Any],
    trace_id: str,
    current_account: Optional[QuotePlatformAccountProfile],
    config_type_name: Optional[str],
    merged_entities: Optional[Dict[str, Any]] = None,
    normalized_data: Optional[Dict[str, Any]] = None,
    images_by_slot: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    attached_images: Optional[List[Dict[str, Any]]] = None,
    attempted_account_ids: Optional[Iterable[int]] = None,
    reason_text: str = "查询额度已用完",
    operator_role_name: Any = "",
) -> Optional[Tuple[str, Dict[str, Any]]]:
    if current_account is None:
        return None
    attempted = {int(x) for x in (attempted_account_ids or []) if _safe_int(x, 0)}
    current_id = _safe_int(current_account.id, 0)
    if current_id:
        attempted.add(current_id)

    selected_type = _normalize_account_type_name(config_type_name) or _normalize_account_type_name(
        current_account.account_type_name
    )
    next_account = await _select_logged_quote_platform_account(
        db,
        owner_user_id=owner_user_id,
        platform_code=case.platform_code or current_account.platform_code or "",
        account_type_name=selected_type,
        exclude_account_ids=attempted,
    )
    if next_account is None:
        return None

    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "account_auto_switch": True,
            "from_account_id": current_id,
            "to_account_id": next_account.id,
            "reason": reason_text,
            "account_type_name": selected_type,
        },
    )
    await db.flush()

    prefix = f"{_quote_account_label(current_account)}：{reason_text}，已自动切换到{_quote_account_label(next_account)}。\n"
    return await _continue_quote_with_platform_account(
        db,
        case=case,
        owner_user_id=owner_user_id,
        snapshot=snapshot,
        trace_id=trace_id,
        platform_account=next_account,
        merged_entities=merged_entities or {},
        normalized_data=normalized_data or _json_obj(snapshot.get("normalized_data")),
        images_by_slot=images_by_slot or _json_obj(snapshot.get("images_by_slot")),
        attached_images=attached_images or [],
        config_type_name=selected_type,
        attempted_account_ids=attempted,
        reply_prefix=prefix,
        operator_role_name=operator_role_name,
    )


async def _auto_retry_duplicate_quote_once(
    db: AsyncSession,
    *,
    case: QuoteCase,
    task: QuoteTask,
    owner_user_id: int,
    snapshot: Dict[str, Any],
    trace_id: str,
    platform_account: QuotePlatformAccountProfile,
    platform_name: str,
    config_type_name: Optional[str],
    operator_role_name: Any = "",
) -> Optional[Tuple[str, Dict[str, Any]]]:
    if _snapshot_has_duplicate_quote_confirmation(snapshot):
        return None
    warning = _to_str(_json_obj(task.request_payload).get("duplicate_quote_warning") or task.error_detail).strip()
    if warning:
        await _persist_platform_text_notice_if_recently_absent(
            db,
            case=case,
            owner_user_id=owner_user_id,
            message=warning,
            trace_id=trace_id,
            task_id=task.id,
            platform_code=case.platform_code or platform_account.platform_code,
            platform_name=platform_name,
            notice_type="duplicate_quote_notice",
        )
    confirmed_snapshot = _duplicate_quote_confirmed_snapshot(snapshot)
    task.error_detail = (warning or "平台提示重复投保，已自动确认后重试")[:1800]
    task.response_payload = {
        **_json_obj(task.response_payload),
        "duplicate_quote_auto_retry": True,
        "duplicate_quote_warning": warning,
    }
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": task.status,
            "trace_id": trace_id,
            "duplicate_quote_auto_retry": True,
            "warning": warning,
        },
    )
    await db.flush()
    return await _continue_quote_with_platform_account(
        db,
        case=case,
        owner_user_id=owner_user_id,
        snapshot=confirmed_snapshot,
        trace_id=trace_id,
        platform_account=platform_account,
        merged_entities={
            "quote_case_id": case.id,
            "order_id": case.order_id,
            "platform_code": case.platform_code or platform_account.platform_code,
            "platform_name": platform_name,
        },
        normalized_data=_json_obj(confirmed_snapshot.get("normalized_data")),
        images_by_slot=_json_obj(confirmed_snapshot.get("images_by_slot")),
        attached_images=[],
        config_type_name=config_type_name,
        attempted_account_ids=None,
        reply_prefix="",
        operator_role_name=operator_role_name,
    )


def _quote_result_from_runtime(
    result: Optional[PlatformRuntimeResult],
    *,
    platform_code: str,
    platform_name: str,
    trace_id: str,
) -> Dict[str, Any]:
    data = _json_obj(getattr(result, "data", None))
    runtime_quote_result = _json_obj(data.get("quote_result"))
    if runtime_quote_result:
        runtime_quote_result.setdefault("platform_code", platform_code)
        runtime_quote_result.setdefault("platform_name", platform_name)
        runtime_quote_result.setdefault("trace_id", trace_id)
        return runtime_quote_result
    # The caller must run _quote_runtime_result_or_failure first.  Returning
    # an empty result here is deliberately not a success fallback; an empty
    # result can never reach the result-card/image path.
    return {}


async def _finalize_quote_runtime_failure(
    db: AsyncSession,
    *,
    case: QuoteCase,
    task: QuoteTask,
    owner_user_id: int,
    platform_account: Optional[QuotePlatformAccountProfile],
    quote_runtime_result: PlatformRuntimeResult,
    response_payload: Dict[str, Any],
    trace_id: str,
    platform_code: str,
    platform_name: str,
    event_extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Persist Task/Case/account failure state shared by SMS and no-SMS paths."""

    _apply_platform_account_runtime_status(
        platform_account,
        quote_runtime_result,
        default_error="平台报价失败",
    )
    error_detail = _runtime_detail(quote_runtime_result, "平台报价失败")
    now = _now()
    task.status = TASK_STATUS_FAILED
    task.login_state = "authenticated"
    task.error_detail = error_detail
    task.response_payload = response_payload
    task.result_payload = {}
    task.finished_at = now
    task.updated_at = now
    case.status = CASE_STATUS_READY
    case.current_task_id = task.id
    case.updated_at = now
    if platform_account:
        platform_account.last_error = error_detail
        platform_account.last_check_at = now
        platform_account.updated_at = now
        if _is_runtime_quota_full_result(quote_runtime_result):
            platform_account.quota_status = ACCOUNT_QUOTA_FULL
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": TASK_STATUS_FAILED,
            "trace_id": trace_id,
            "reason": error_detail,
            **_json_obj(event_extra),
        },
    )
    await db.flush()
    await _persist_runtime_auto_notices(
        db,
        case=case,
        owner_user_id=owner_user_id,
        runtime_result=quote_runtime_result,
        trace_id=trace_id,
        task_id=task.id,
        platform_code=platform_code,
        platform_name=platform_name,
    )
    await db.flush()
    return error_detail


def _session_expired_quote_failure_response(
    *,
    case: QuoteCase,
    task: QuoteTask,
    platform_account: Optional[QuotePlatformAccountProfile],
    quote_runtime_result: PlatformRuntimeResult,
    snapshot: Dict[str, Any],
    platform_code: str,
    platform_name: str,
    trace_id: str,
    operator_role_name: Any = "",
) -> Tuple[str, Dict[str, Any]]:
    runtime_payload = _runtime_result_payload(quote_runtime_result)
    runtime_data = _json_obj(runtime_payload.get("data"))
    request_body_draft = _json_obj(
        runtime_data.get("request_body")
        or runtime_data.get("request_body_draft")
        or _json_obj(snapshot.get("request_body"))
    )
    account_label = ""
    if platform_account:
        account_label = f"（账号：{platform_account.account_username or platform_account.id}）"
    preflight = _json_obj(request_body_draft.get("preflight"))
    missing_default_config = [
        _to_str(item).strip()
        for item in (preflight.get("missingDefaultConfig") if isinstance(preflight.get("missingDefaultConfig"), list) else [])
        if _to_str(item).strip()
    ]
    draft_account_type = (
        _to_str(request_body_draft.get("accountTypeName")).strip()
        or _to_str(_json_obj(snapshot.get("platform_default_config")).get("resolved_type_name")).strip()
        or _to_str(platform_account.account_type_name if platform_account else "").strip()
        or "当前账号类型"
    )
    reply_lines = [
        f"{platform_name}{account_label}登录已过期，本次没有继续提交报价。",
        f"已完成资料解析和{draft_account_type}请求体草稿组装，重新登录该平台账号后可直接重新发起报价。",
    ]
    if _quote_account_needs_admin_contact(operator_role_name):
        reply_lines = [
            f"{platform_name}账号登录已过期，本次没有继续提交报价。",
            "请联系管理员重新登录平台账号后再发起报价。",
        ]
    if missing_default_config:
        reply_lines.append(
            "重新报价前还需要在右上角“默认参数配置”补齐："
            + "、".join(missing_default_config[:8])
            + "。"
        )
    return _quote_platform_dialog_response(
        case=case,
        task=task,
        platform_account=platform_account,
        runtime_result=quote_runtime_result,
        platform_code=platform_code,
        platform_name=platform_name,
        trace_id=trace_id,
        error_detail="\n".join(reply_lines),
        result_status=RESULT_NOT_READY,
        response_status="success",
        title=f"{platform_name}登录已过期",
        subtype="session_expired",
        severity="warning",
        actions=[
            *_quote_platform_account_manage_actions(
                operator_role_name,
                platform_code=platform_code,
                platform_name=platform_name,
            ),
            *([] if _quote_account_needs_admin_contact(operator_role_name) else [_mk_action(f"{platform_name}报价")]),
        ],
        extra_payload={"request_body": request_body_draft},
        operator_role_name=operator_role_name,
    )


async def _respond_after_quote_runtime_failure(
    db: AsyncSession,
    *,
    case: QuoteCase,
    task: QuoteTask,
    owner_user_id: int,
    platform_account: Optional[QuotePlatformAccountProfile],
    quote_runtime_result: PlatformRuntimeResult,
    snapshot: Dict[str, Any],
    trace_id: str,
    platform_code: str,
    platform_name: str,
    config_type_name: Optional[str],
    attempted_account_ids: Optional[Iterable[int]],
    operator_role_name: Any = "",
    extra_payload: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Shared post-failure branch: duplicate auto-retry, session/quota switch, dialog."""

    error_detail = _to_str(task.error_detail).strip() or _runtime_detail(quote_runtime_result, "平台报价失败")
    if platform_account and _is_runtime_duplicate_quote_result(quote_runtime_result):
        warning = _duplicate_quote_warning_from_runtime(quote_runtime_result)
        task.request_payload = {**_json_obj(task.request_payload), "duplicate_quote_warning": warning}
        retry_duplicate = await _auto_retry_duplicate_quote_once(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            trace_id=trace_id,
            platform_account=platform_account,
            platform_name=platform_name,
            config_type_name=config_type_name or platform_account.account_type_name,
            operator_role_name=operator_role_name,
        )
        if retry_duplicate is not None:
            return retry_duplicate
        if await _quote_auto_notice_already_persisted(
            db,
            owner_user_id=owner_user_id,
            session_id=case.session_id,
            dedupe_key="",
            message=warning,
            trace_id=trace_id,
        ):
            return _quote_notice_already_visible_silent_response(
                case=case,
                task=task,
                trace_id=trace_id,
                message=warning,
            )
        return _quote_platform_text_notice_response(
            case=case,
            task=task,
            runtime_result=quote_runtime_result,
            platform_code=platform_code,
            platform_name=platform_name,
            trace_id=trace_id,
            message=warning,
            result_status=RESULT_NOT_READY,
            response_status="success",
            notice_type="duplicate_quote_notice",
        )
    if _is_runtime_session_expired_result(quote_runtime_result):
        retry = await _retry_quote_with_next_platform_account(
            db,
            case=case,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            trace_id=trace_id,
            current_account=platform_account,
            config_type_name=config_type_name
            or (platform_account.account_type_name if platform_account else None),
            attempted_account_ids=attempted_account_ids,
            reason_text="登录已过期",
            operator_role_name=operator_role_name,
        )
        if retry is not None:
            return retry
        return _session_expired_quote_failure_response(
            case=case,
            task=task,
            platform_account=platform_account,
            quote_runtime_result=quote_runtime_result,
            snapshot=snapshot,
            platform_code=platform_code,
            platform_name=platform_name,
            trace_id=trace_id,
            operator_role_name=operator_role_name,
        )
    if _is_runtime_quota_full_result(quote_runtime_result):
        retry = await _retry_quote_with_next_platform_account(
            db,
            case=case,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            trace_id=trace_id,
            current_account=platform_account,
            config_type_name=config_type_name
            or (platform_account.account_type_name if platform_account else None),
            attempted_account_ids=attempted_account_ids,
            reason_text="平台提示查询额度已用完",
            operator_role_name=operator_role_name,
        )
        if retry is not None:
            return retry
    auto_notice_message = _runtime_platform_auto_notice_message_for_failure(
        quote_runtime_result,
        error_detail,
    )
    if auto_notice_message and await _quote_auto_notice_already_persisted(
        db,
        owner_user_id=owner_user_id,
        session_id=case.session_id,
        dedupe_key="",
        message=auto_notice_message,
        trace_id=trace_id,
    ):
        return _quote_notice_already_visible_silent_response(
            case=case,
            task=task,
            trace_id=trace_id,
            message=auto_notice_message,
        )
    if await _quote_auto_notice_already_persisted(
        db,
        owner_user_id=owner_user_id,
        session_id=case.session_id,
        dedupe_key="",
        message=error_detail,
        trace_id=trace_id,
    ):
        return _quote_notice_already_visible_silent_response(
            case=case,
            task=task,
            trace_id=trace_id,
            message=error_detail,
        )
    return _quote_platform_dialog_response(
        case=case,
        task=task,
        platform_account=platform_account,
        runtime_result=quote_runtime_result,
        platform_code=platform_code,
        platform_name=platform_name,
        trace_id=trace_id,
        error_detail=error_detail,
        actions=[_mk_action("查看当前材料状态"), _mk_action(f"{platform_name}报价")],
        extra_payload=extra_payload,
        operator_role_name=operator_role_name,
    )


async def _complete_waiting_task(
    db: AsyncSession,
    *,
    case: QuoteCase,
    task: QuoteTask,
    owner_user_id: int,
    sms_code: str,
    operator_role_name: Any = "",
) -> Tuple[str, Dict[str, Any]]:
    trace_id = task.trace_id or _new_trace_id()
    platform_code = task.platform_code or case.platform_code or "STUB"
    platform_name = task.platform_name or case.platform_name or platform_code
    snapshot = _json_obj(task.submitted_snapshot)
    account_payload = _json_obj(_json_obj(task.request_payload).get("platform_account"))
    account_id = _safe_int(account_payload.get("id"), 0) or None
    platform_account = (
        await _get_platform_account_profile_by_id(db, account_id=account_id)
        if account_id
        else None
    )
    snapshot_account_type_name = _normalize_account_type_name(
        _json_obj(snapshot.get("platform_default_config")).get("resolved_type_name")
        or _json_obj(snapshot.get("platform_default_config")).get("account_type_name")
        or _json_obj(snapshot.get("vehicle_type_detect")).get("config_type_name")
    )
    platform_ctx = (
        _platform_account_quote_context(platform_account, account_type_name=snapshot_account_type_name)
        if platform_account
        else _platform_context_from_public_payload(account_payload, platform_code=platform_code, platform_name=platform_name)
    )
    if not await _quote_snapshot_material_is_current(db, case=case, snapshot=snapshot):
        return await _stop_quote_for_material_change(
            db,
            case=case,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            task=task,
            platform_account=platform_account,
        )
    if not await _quote_snapshot_default_config_is_current(db, snapshot=snapshot, platform_code=platform_code):
        return await _stop_quote_for_default_config_change(
            db,
            case=case,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            task=task,
            platform_account=platform_account,
        )

    perf_started = time.perf_counter()
    perf: Dict[str, Any] = {"login_mode": "sms_verified"}
    task.status = TASK_STATUS_RUNNING
    task.login_state = "authenticated"
    task.error_detail = None
    task.response_payload = {"perf": perf}
    task.updated_at = _now()
    # Leave waiting_sms only while Task is actually waiting for a code.
    # After commit the challenge/quote window uses Task=running; Case must not
    # stay waiting_sms or concurrent wait lookups / active-case logic diverge.
    _align_case_status_with_running_quote_task(case)
    case.current_task_id = task.id
    case.updated_at = _now()
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={"task_id": task.id, "status": TASK_STATUS_RUNNING, "trace_id": trace_id, "login_mode": "sms_verified"},
    )
    await db.flush()
    await db.commit()

    challenge_started = time.perf_counter()
    async with release_chat_session_lock_for_platform_io():
        challenge_result = await quote_platform_runtime.submit_challenge(platform_ctx, sms_code, db=db)
    perf["challenge_ms"] = _elapsed_ms(challenge_started)
    if await _quote_task_was_cancelled(db, task=task):
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)
    challenge_status = _runtime_status(challenge_result)
    challenge_preserved_session = bool(
        challenge_status in RUNTIME_SESSION_DEGRADED_STATUSES
        and _json_obj(challenge_result.data).get("preserved_previous_session")
    )
    challenge_authenticated = _is_runtime_login_success(challenge_status) or challenge_preserved_session
    if _is_runtime_challenge(challenge_status):
        task.status = TASK_STATUS_WAITING_SMS
        task.login_state = "sms_required"
        task.error_detail = sanitize_quote_user_message(
            challenge_result.challenge_prompt or challenge_result.message,
            "平台要求继续验证码校验",
            platform_code=platform_code,
            platform_name=platform_name,
        )
        perf["total_ms"] = _elapsed_ms(perf_started)
        task.response_payload = {"platform_challenge": _runtime_result_payload(challenge_result), "perf": perf}
        task.updated_at = _now()
        case.status = CASE_STATUS_WAITING_SMS
        case.current_task_id = task.id
        case.updated_at = _now()
        if platform_account:
            platform_account.login_status = ACCOUNT_LOGIN_NEEDS_CODE
            platform_account.last_error = task.error_detail
            platform_account.last_check_at = _now()
            platform_account.updated_at = _now()
            await _sync_account_login_task_after_quote_challenge(
                db,
                account=platform_account,
                runtime_result=challenge_result,
                status=LOGIN_TASK_NEEDS_CODE,
                prompt=task.error_detail,
            )
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={"task_id": task.id, "status": TASK_STATUS_WAITING_SMS, "trace_id": trace_id, "reason": task.error_detail},
        )
        await db.flush()
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

    if not challenge_authenticated:
        task.status = TASK_STATUS_FAILED
        task.login_state = "failed"
        task.error_detail = _runtime_detail(challenge_result, "验证码校验失败")
        perf["total_ms"] = _elapsed_ms(perf_started)
        task.response_payload = {"platform_challenge": _runtime_result_payload(challenge_result), "perf": perf}
        task.finished_at = _now()
        task.updated_at = _now()
        case.status = CASE_STATUS_READY
        case.current_task_id = None
        case.updated_at = _now()
        if platform_account:
            platform_account.login_status = ACCOUNT_LOGIN_FAILED
            platform_account.last_error = task.error_detail
            platform_account.last_check_at = _now()
            platform_account.updated_at = _now()
            await _sync_account_login_task_after_quote_challenge(
                db,
                account=platform_account,
                runtime_result=challenge_result,
                status=LOGIN_TASK_EXPIRED if _is_runtime_session_expired_result(challenge_result) else LOGIN_TASK_FAILED,
                error_detail=task.error_detail,
            )
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={"task_id": task.id, "status": TASK_STATUS_FAILED, "trace_id": trace_id, "reason": task.error_detail},
        )
        await db.flush()
        admin_text = f"{platform_name}验证码校验失败：{task.error_detail}。请重新点击报价或账号登录后再试。"
        return (
            _quote_account_action_text(
                operator_role_name,
                admin_text,
                f"{platform_name}验证码校验失败，请联系管理员处理。",
            ),
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
        platform_account.login_status = ACCOUNT_LOGIN_DEGRADED if challenge_preserved_session else ACCOUNT_LOGIN_AUTHENTICATED
        platform_account.last_error = None
        if not challenge_preserved_session:
            platform_account.last_login_at = _now()
        platform_account.last_check_at = _now()
        platform_account.updated_at = _now()
        if platform_account.quota_status == ACCOUNT_QUOTA_UNKNOWN:
            platform_account.quota_status = ACCOUNT_QUOTA_AVAILABLE
        _clear_account_inspection_notice(platform_account)
        await _sync_account_login_task_after_quote_challenge(
            db,
            account=platform_account,
            runtime_result=challenge_result,
            status=LOGIN_TASK_SUCCESS,
        )
    if not await _quote_snapshot_material_is_current(db, case=case, snapshot=snapshot):
        return await _stop_quote_for_material_change(
            db,
            case=case,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            task=task,
            platform_account=platform_account,
            response_payload={"challenge": _runtime_result_payload(challenge_result)},
        )

    task_operation = _to_str(_json_obj(task.request_payload).get("operation")).strip() or "quote"
    if task_operation == RENEWAL_LOOKUP_OPERATION:
        if platform_account is None:
            task.status = TASK_STATUS_FAILED
            task.login_state = "failed"
            task.error_detail = "平台账号不存在，请重新发起续保查询"
            task.finished_at = _now()
            task.updated_at = _now()
            case.status = CASE_STATUS_READY
            case.current_task_id = task.id
            case.updated_at = _now()
            await db.flush()
            return (
                f"{platform_name}续保查询失败：{task.error_detail}",
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
        return await _complete_renewal_lookup_without_sms(
            db,
            case=case,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            trace_id=trace_id,
            platform_account=platform_account,
            login_mode="sms_verified",
            task=task,
            operator_role_name=operator_role_name,
        )

    task.request_payload = {
        **_json_obj(task.request_payload),
        "platform_default_config": _json_obj(snapshot.get("platform_default_config")),
        "default_config_json": _json_obj(snapshot.get("default_config_json")),
        "vehicle_type_detect": _json_obj(snapshot.get("vehicle_type_detect")),
        "request_body": _json_obj(snapshot.get("request_body")),
    }
    quota_reservation: Dict[str, Any] = {"configured": False, "available": True, "reserved": False}
    if platform_account:
        quota_started = time.perf_counter()
        quota_reservation = await _reserve_account_quota_for_quote(db, account=platform_account)
        perf["quota_reserve_ms"] = _elapsed_ms(quota_started)
        if await _quote_task_was_cancelled(db, task=task):
            await _release_account_quota_reservation(db, account=platform_account, reservation=quota_reservation)
            return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)
        if not quota_reservation.get("available", True):
            task.status = TASK_STATUS_FAILED
            task.login_state = "authenticated"
            task.error_detail = _quote_quota_exhausted_message(
                platform_name,
                platform_account,
                operator_role_name=operator_role_name,
            )
            perf["total_ms"] = _elapsed_ms(perf_started)
            task.response_payload = {"platform_challenge": _runtime_result_payload(challenge_result), "perf": perf}
            task.finished_at = _now()
            task.updated_at = _now()
            case.status = CASE_STATUS_READY
            case.current_task_id = task.id
            case.updated_at = _now()
            platform_account.quota_status = ACCOUNT_QUOTA_FULL
            platform_account.last_error = task.error_detail
            platform_account.last_check_at = _now()
            platform_account.updated_at = _now()
            await _add_event(
                db,
                case=case,
                owner_user_id=owner_user_id,
                event_type="task",
                role="system",
                payload={"task_id": task.id, "status": TASK_STATUS_FAILED, "trace_id": trace_id, "reason": task.error_detail},
            )
            await db.flush()
            retry = await _retry_quote_with_next_platform_account(
                db,
                case=case,
                owner_user_id=owner_user_id,
                snapshot=snapshot,
                trace_id=trace_id,
                current_account=platform_account,
                config_type_name=_json_obj(snapshot.get("platform_default_config")).get("resolved_type_name")
                or (platform_account.account_type_name if platform_account else None),
                attempted_account_ids={account_id} if account_id else None,
                reason_text="查询额度已用完",
                operator_role_name=operator_role_name,
            )
            if retry is not None:
                return retry
            return (
                task.error_detail,
                {
                    "status": "success",
                    "intent": "quote",
                    "trace_id": trace_id,
                    "data": _mk_data(
                        result_status=RESULT_NEED_MORE,
                        message=task.error_detail,
                        entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                        payload={
                            "quote_task": {"id": task.id, "status": task.status, "trace_id": trace_id},
                            "platform_account": _credential_public_payload(platform_account),
                            "quota": quota_reservation.get("after") or {},
                        },
                    ),
                    "actions": [
                        *_quote_platform_account_manage_actions(
                            operator_role_name,
                            platform_code=platform_code,
                            platform_name=platform_name,
                        ),
                        *([] if _quote_account_needs_admin_contact(operator_role_name) else [_mk_action(f"{platform_name}报价")]),
                    ],
                },
            )
    platform_quote_started = time.perf_counter()
    platform_ctx = _attach_quote_auto_notice_callback(
        platform_ctx,
        owner_user_id=owner_user_id,
        session_id=case.session_id,
        case_id=case.id,
        task_id=task.id,
        trace_id=trace_id,
        platform_code=platform_code,
        platform_name=platform_name,
    )
    async with release_chat_session_lock_for_platform_io():
        quote_runtime_result = await quote_platform_runtime.quote(platform_ctx, snapshot, db=db)
    perf["platform_quote_ms"] = _elapsed_ms(platform_quote_started)
    quote_runtime_result = _quote_runtime_result_or_failure(quote_runtime_result)
    if await _quote_task_was_cancelled(db, task=task):
        await _release_account_quota_reservation(db, account=platform_account, reservation=quota_reservation)
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)
    quote_status = _runtime_status(quote_runtime_result)
    if not _is_runtime_quote_success(quote_status):
        await _release_account_quota_reservation(db, account=platform_account, reservation=quota_reservation)
        await _finalize_quote_runtime_failure(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            platform_account=platform_account,
            quote_runtime_result=quote_runtime_result,
            response_payload={
                "sms_code_length": len(sms_code),
                "challenge": _runtime_result_payload(challenge_result),
                "quote": _runtime_result_payload(quote_runtime_result),
                "perf": {**perf, "total_ms": _elapsed_ms(perf_started)},
            },
            trace_id=trace_id,
            platform_code=platform_code,
            platform_name=platform_name,
        )
        return await _respond_after_quote_runtime_failure(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            platform_account=platform_account,
            quote_runtime_result=quote_runtime_result,
            snapshot=snapshot,
            trace_id=trace_id,
            platform_code=platform_code,
            platform_name=platform_name,
            config_type_name=_json_obj(snapshot.get("platform_default_config")).get("resolved_type_name")
            or (platform_account.account_type_name if platform_account else None),
            attempted_account_ids={account_id} if account_id else None,
            operator_role_name=operator_role_name,
        )
    result = _quote_result_from_runtime(
        quote_runtime_result,
        platform_code=platform_code,
        platform_name=platform_name,
        trace_id=trace_id,
    )
    task = await _lock_quote_task(db, task)
    if await _quote_task_was_cancelled(db, task=task):
        await _release_account_quota_reservation(db, account=platform_account, reservation=quota_reservation)
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)
    if not await _quote_snapshot_material_is_current(db, case=case, snapshot=snapshot):
        perf["total_ms"] = _elapsed_ms(perf_started)
        return await _stop_quote_for_material_change(
            db,
            case=case,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            task=task,
            platform_account=platform_account,
            quota_reservation=quota_reservation,
            response_payload={"quote": _runtime_result_payload(quote_runtime_result), "perf": perf},
        )

    # Create/upload a result image only after this quote is still current.
    display_started = time.perf_counter()
    try:
        result = _enrich_quote_result_for_display(
            result,
            platform_account=platform_account,
            platform_name=platform_name,
            generate_image=not _quote_result_image_async_enabled(),
        )
    except Exception as exc:
        perf["result_display_ms"] = _elapsed_ms(display_started)
        perf["total_ms"] = _elapsed_ms(perf_started)
        return await _fail_quote_after_result_materialization(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            error=exc,
            platform_account=platform_account,
            quota_reservation=quota_reservation,
            response_payload={
                "quote": _runtime_result_payload(quote_runtime_result),
                "perf": perf,
            },
        )
    perf["result_display_ms"] = _elapsed_ms(display_started)
    if result.get("result_image_ms") is not None:
        perf["result_image_ms"] = _safe_int(result.get("result_image_ms"), 0)
    perf["total_ms"] = _elapsed_ms(perf_started)
    task.status = TASK_STATUS_SUCCESS
    task.login_state = "authenticated"
    task.response_payload = {
        "sms_code_length": len(sms_code),
        "challenge": _runtime_result_payload(challenge_result),
        "quote": _runtime_result_payload(quote_runtime_result),
        "perf": perf,
    }
    task.result_payload = result
    task.finished_at = _now()
    task.updated_at = _now()

    case.status = CASE_STATUS_QUOTED
    case.quote_count = _safe_int(case.quote_count, 0) + 1
    case.current_task_id = task.id
    case.updated_at = _now()
    await _mark_platform_account_used(
        db,
        account_id=account_id,
        owner_user_id=owner_user_id,
        login_state=ACCOUNT_LOGIN_DEGRADED if challenge_preserved_session else ACCOUNT_LOGIN_AUTHENTICATED,
        consume_quota=False,
    )
    if platform_account:
        quota_update_started = time.perf_counter()
        await _record_account_quota_consumed(
            db,
            account=platform_account,
            reservation=quota_reservation,
            operator_user_id=owner_user_id,
        )
        await _reconcile_account_quota_with_platform_usage(
            db,
            account=platform_account,
            runtime_result=quote_runtime_result,
            operator_user_id=owner_user_id,
        )
        perf["quota_update_ms"] = _elapsed_ms(quota_update_started)
        perf["total_ms"] = _elapsed_ms(perf_started)
        task.response_payload = {**_json_obj(task.response_payload), "perf": perf}

    auto_date_adjustments = await _persist_quote_auto_adjusted_dates_to_case(
        db,
        case=case,
        task=task,
        result=result,
    )

    _log_quote_perf(
        stage="success",
        trace_id=trace_id,
        case_id=case.id,
        task_id=task.id,
        platform_code=platform_code,
        account_id=account_id,
        perf=perf,
    )
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": "success",
            "trace_id": trace_id,
            "result": result,
            "insurance_date_auto_adjustments": auto_date_adjustments,
        },
    )
    await db.flush()
    await _persist_unemitted_quote_auto_notices(
        db,
        case=case,
        owner_user_id=owner_user_id,
        result=result,
        trace_id=trace_id,
        task_id=task.id,
        platform_code=platform_code,
        platform_name=platform_name,
    )
    await db.flush()

    reply = _quote_result_reply_text(result, platform_name=platform_name)
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
    response_data = _mk_data(
        result_status=RESULT_SUCCESS,
        message="报价流程已完成",
        entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
        payload=payload,
    )
    response_data["quote_case"] = payload["quote_case"]
    response_data["quote_task"] = payload["quote_task"]
    response_data["quote_result"] = result
    return reply, {
        "status": "success",
        "intent": "quote",
        "trace_id": trace_id,
        "data": response_data,
        "actions": [],
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
    attempted_account_ids: Optional[Iterable[int]] = None,
    account_type_name: Optional[str] = None,
    operator_role_name: Any = "",
) -> Tuple[str, Dict[str, Any]]:
    platform_code = case.platform_code or platform_account.platform_code or "STUB"
    platform_name = case.platform_name or platform_account.platform_name or platform_code
    perf_started = time.perf_counter()
    perf: Dict[str, Any] = {"login_mode": login_mode}
    snapshot_account_type_name = _normalize_account_type_name(
        account_type_name
        or _json_obj(snapshot.get("platform_default_config")).get("resolved_type_name")
        or _json_obj(snapshot.get("platform_default_config")).get("account_type_name")
        or _json_obj(snapshot.get("vehicle_type_detect")).get("config_type_name")
    )
    platform_ctx = _platform_account_quote_context(platform_account, account_type_name=snapshot_account_type_name)
    attempted = {int(x) for x in (attempted_account_ids or []) if _safe_int(x, 0)}
    current_account_id = _safe_int(platform_account.id, 0)
    if current_account_id:
        attempted.add(current_account_id)
    if not await _quote_snapshot_material_is_current(db, case=case, snapshot=snapshot):
        return _quote_superseded_silent_response(case=case, task=None, trace_id=trace_id)

    request_payload = {
        "mode": "quote_attempt",
        "login": login_mode,
        "owner_user_id": owner_user_id,
        "platform_account": _credential_public_payload(platform_account),
        "platform_default_config": _json_obj(snapshot.get("platform_default_config")),
        "default_config_json": _json_obj(snapshot.get("default_config_json")),
        "vehicle_type_detect": _json_obj(snapshot.get("vehicle_type_detect")),
        "request_body": _json_obj(snapshot.get("request_body")),
    }
    task = QuoteTask(
        quote_case_id=case.id,
        platform_code=platform_code,
        platform_name=platform_name,
        status=TASK_STATUS_RUNNING,
        login_state="authenticated",
        sms_phone_mask=platform_account.login_phone_mask,
        trace_id=trace_id,
        request_payload=request_payload,
        response_payload={"perf": perf},
        result_payload={},
        submitted_snapshot=snapshot,
        started_at=_now(),
    )
    db.add(task)
    await db.flush()
    case.current_task_id = task.id
    case.updated_at = _now()
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={"task_id": task.id, "status": TASK_STATUS_RUNNING, "trace_id": trace_id, "login_mode": login_mode},
    )
    await db.flush()
    await db.commit()

    quota_started = time.perf_counter()
    quota_reservation = await _reserve_account_quota_for_quote(db, account=platform_account)
    perf["quota_reserve_ms"] = _elapsed_ms(quota_started)
    if await _quote_task_was_cancelled(db, task=task):
        await _release_account_quota_reservation(db, account=platform_account, reservation=quota_reservation)
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)
    if not quota_reservation.get("available", True):
        error_detail = _quote_quota_exhausted_message(
            platform_name,
            platform_account,
            operator_role_name=operator_role_name,
        )
        perf["total_ms"] = _elapsed_ms(perf_started)
        task.status = TASK_STATUS_FAILED
        task.error_detail = error_detail
        task.response_payload = {"perf": perf}
        task.finished_at = _now()
        task.updated_at = _now()
        case.status = CASE_STATUS_READY
        case.current_task_id = task.id
        case.updated_at = _now()
        platform_account.quota_status = ACCOUNT_QUOTA_FULL
        platform_account.last_error = error_detail
        platform_account.last_check_at = _now()
        platform_account.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={"task_id": task.id, "status": TASK_STATUS_FAILED, "trace_id": trace_id, "reason": error_detail, "login_mode": login_mode},
        )
        await db.flush()
        retry = await _retry_quote_with_next_platform_account(
            db,
            case=case,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            trace_id=trace_id,
            current_account=platform_account,
            config_type_name=account_type_name or platform_account.account_type_name,
            attempted_account_ids=attempted,
            reason_text="查询额度已用完",
            operator_role_name=operator_role_name,
        )
        if retry is not None:
            return retry
        return (
            error_detail,
            {
                "status": "success",
                "intent": "quote",
                "trace_id": trace_id,
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message=error_detail,
                    entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                    payload={
                        "quote_case": {
                            "id": case.id,
                            "case_no": case.case_no,
                            "status": case.status,
                            "order_id": case.order_id,
                            "source_type": case.source_type,
                        },
                        "quote_task": {"id": task.id, "status": task.status, "trace_id": trace_id},
                        "platform_account": _credential_public_payload(platform_account),
                        "quota": quota_reservation.get("after") or {},
                    },
                ),
                "actions": [
                    *_quote_platform_account_manage_actions(
                        operator_role_name,
                        platform_code=platform_code,
                        platform_name=platform_name,
                    ),
                    *([] if _quote_account_needs_admin_contact(operator_role_name) else [_mk_action(f"{platform_name}报价")]),
                ],
            },
        )
    platform_quote_started = time.perf_counter()
    platform_ctx = _attach_quote_auto_notice_callback(
        platform_ctx,
        owner_user_id=owner_user_id,
        session_id=case.session_id,
        case_id=case.id,
        task_id=task.id,
        trace_id=trace_id,
        platform_code=platform_code,
        platform_name=platform_name,
    )
    async with release_chat_session_lock_for_platform_io():
        quote_runtime_result = await quote_platform_runtime.quote(platform_ctx, snapshot, db=db)
    perf["platform_quote_ms"] = _elapsed_ms(platform_quote_started)
    quote_runtime_result = _quote_runtime_result_or_failure(quote_runtime_result)
    if await _quote_task_was_cancelled(db, task=task):
        await _release_account_quota_reservation(db, account=platform_account, reservation=quota_reservation)
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)
    if not await _quote_snapshot_material_is_current(db, case=case, snapshot=snapshot):
        perf["total_ms"] = _elapsed_ms(perf_started)
        task.response_payload = {
            "login": login_mode,
            "quote": _runtime_result_payload(quote_runtime_result),
            "material_changed": True,
            "perf": perf,
        }
        return await _stop_quote_for_material_change(
            db,
            case=case,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            task=task,
            platform_account=platform_account,
            quota_reservation=quota_reservation,
            response_payload={"quote": _runtime_result_payload(quote_runtime_result)},
        )
    quote_status = _runtime_status(quote_runtime_result)
    if not _is_runtime_quote_success(quote_status):
        await _release_account_quota_reservation(db, account=platform_account, reservation=quota_reservation)
        perf["total_ms"] = _elapsed_ms(perf_started)
        error_detail = await _finalize_quote_runtime_failure(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            platform_account=platform_account,
            quote_runtime_result=quote_runtime_result,
            response_payload={
                "login": login_mode,
                "quote": _runtime_result_payload(quote_runtime_result),
                "perf": perf,
            },
            trace_id=trace_id,
            platform_code=platform_code,
            platform_name=platform_name,
            event_extra={"login_mode": login_mode},
        )
        return await _respond_after_quote_runtime_failure(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            platform_account=platform_account,
            quote_runtime_result=quote_runtime_result,
            snapshot=snapshot,
            trace_id=trace_id,
            platform_code=platform_code,
            platform_name=platform_name,
            config_type_name=account_type_name or platform_account.account_type_name,
            attempted_account_ids=attempted,
            operator_role_name=operator_role_name,
            extra_payload={
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
            },
        )
    result = _quote_result_from_runtime(
        quote_runtime_result,
        platform_code=platform_code,
        platform_name=platform_name,
        trace_id=trace_id,
    )
    task = await _lock_quote_task(db, task)
    if await _quote_task_was_cancelled(db, task=task):
        await _release_account_quota_reservation(db, account=platform_account, reservation=quota_reservation)
        return _quote_superseded_silent_response(case=case, task=task, trace_id=trace_id)
    if not await _quote_snapshot_material_is_current(db, case=case, snapshot=snapshot):
        perf["total_ms"] = _elapsed_ms(perf_started)
        task.response_payload = {
            **_json_obj(task.response_payload),
            "quote": _runtime_result_payload(quote_runtime_result),
            "material_changed_before_finish": True,
            "perf": perf,
        }
        return await _stop_quote_for_material_change(
            db,
            case=case,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            task=task,
            platform_account=platform_account,
            quota_reservation=quota_reservation,
            response_payload={"quote": _runtime_result_payload(quote_runtime_result)},
        )
    # Create/upload a result image only after this quote is still current.
    display_started = time.perf_counter()
    try:
        result = _enrich_quote_result_for_display(
            result,
            platform_account=platform_account,
            platform_name=platform_name,
            generate_image=not _quote_result_image_async_enabled(),
        )
    except Exception as exc:
        perf["result_display_ms"] = _elapsed_ms(display_started)
        perf["total_ms"] = _elapsed_ms(perf_started)
        return await _fail_quote_after_result_materialization(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            platform_name=platform_name,
            trace_id=trace_id,
            error=exc,
            platform_account=platform_account,
            quota_reservation=quota_reservation,
            response_payload={
                "quote": _runtime_result_payload(quote_runtime_result),
                "perf": perf,
            },
        )
    perf["result_display_ms"] = _elapsed_ms(display_started)
    if result.get("result_image_ms") is not None:
        perf["result_image_ms"] = _safe_int(result.get("result_image_ms"), 0)
    perf["total_ms"] = _elapsed_ms(perf_started)
    task.status = TASK_STATUS_SUCCESS
    task.login_state = "authenticated"
    task.response_payload = {
        "login": login_mode,
        "quote": _runtime_result_payload(quote_runtime_result),
        "perf": perf,
    }
    task.result_payload = result
    task.finished_at = _now()
    task.updated_at = _now()
    await db.flush()

    case.status = CASE_STATUS_QUOTED
    case.quote_count = _safe_int(case.quote_count, 0) + 1
    case.current_task_id = task.id
    case.updated_at = _now()
    await _mark_platform_account_used(
        db,
        account_id=platform_account.id,
        owner_user_id=owner_user_id,
        login_state=ACCOUNT_LOGIN_AUTHENTICATED,
        consume_quota=False,
    )
    quota_update_started = time.perf_counter()
    await _record_account_quota_consumed(
        db,
        account=platform_account,
        reservation=quota_reservation,
        operator_user_id=owner_user_id,
    )
    await _reconcile_account_quota_with_platform_usage(
        db,
        account=platform_account,
        runtime_result=quote_runtime_result,
        operator_user_id=owner_user_id,
    )
    perf["quota_update_ms"] = _elapsed_ms(quota_update_started)
    perf["total_ms"] = _elapsed_ms(perf_started)
    task.response_payload = {**_json_obj(task.response_payload), "perf": perf}
    auto_date_adjustments = await _persist_quote_auto_adjusted_dates_to_case(
        db,
        case=case,
        task=task,
        result=result,
    )
    _log_quote_perf(
        stage="success",
        trace_id=trace_id,
        case_id=case.id,
        task_id=task.id,
        platform_code=platform_code,
        account_id=platform_account.id,
        perf=perf,
    )
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="task",
        role="system",
        payload={
            "task_id": task.id,
            "status": TASK_STATUS_SUCCESS,
            "trace_id": trace_id,
            "result": result,
            "login_mode": login_mode,
            "insurance_date_auto_adjustments": auto_date_adjustments,
        },
    )
    await db.flush()
    await _persist_unemitted_quote_auto_notices(
        db,
        case=case,
        owner_user_id=owner_user_id,
        result=result,
        trace_id=trace_id,
        task_id=task.id,
        platform_code=platform_code,
        platform_name=platform_name,
    )
    await db.flush()

    account_label = _normalize_account_type_name(platform_account.account_type_name) or platform_account.account_username or "未标记账号"
    reply = _quote_result_reply_text(result, platform_name=platform_name, account_label=account_label)
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
            message="报价流程已完成",
            entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
            payload=payload,
        ),
        "actions": [],
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
    _ensure_quote_flow_access(ctx)

    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    await _expire_stale_waiting_sms_tasks_for_owner_session(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    await _expire_stale_running_quote_tasks_for_owner_session(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    merged_entities = dict(entities or {})
    signal = detect_quote_signal(text)
    if isinstance(signal.get("entities"), dict):
        merged_entities.update(signal["entities"])
    override_signal = detect_quote_config_override_signal(text)
    quote_field_overrides = _json_obj(override_signal.get("overrides"))
    if quote_field_overrides:
        merged_entities = {
            **merged_entities,
            "quote_field_overrides": quote_field_overrides,
            "force_requote": True,
        }
    data_override_signal = detect_quote_data_override_signal(text)
    quote_data_overrides = _json_obj(data_override_signal.get("overrides"))
    if quote_data_overrides:
        merged_entities = {
            **merged_entities,
            QUOTE_DATA_OVERRIDES_KEY: quote_data_overrides,
            "force_requote": True,
        }

    platform_code = _to_str(merged_entities.get("platform_code")).strip().upper()
    platform_name = _to_str(merged_entities.get("platform_name")).strip()
    if not platform_code and platform_name:
        platform_code = _platform_code_from_display_name(platform_name) or "STUB"
    if not platform_name and platform_code:
        platform_name = _platform_display_name(platform_code)
    if signal.get("is_quote") and not platform_name:
        command_platform_name = _quote_platform_name_from_command(text)
        if command_platform_name:
            platform_name = command_platform_name
            platform_code = _platform_code_from_display_name(platform_name) or platform_code or "STUB"
            if platform_code != "STUB":
                platform_name = _platform_display_name(platform_code) or platform_name
    explicit_platform_quote = bool(
        signal.get("is_quote")
        and platform_code
        and platform_name
        and _is_explicit_platform_quote_command(text, platform_code, platform_name)
    )

    order_id = _safe_int((ctx or {}).get("order_id"), 0) or _safe_int(merged_entities.get("order_id"), 0) or None
    extracted = extract_quote_fields(text)
    text_data = _quote_text_data_from_entities(extracted, merged_entities)
    order = await _find_order(
        db,
        ctx=ctx,
        order_id=order_id,
        plate_no=_quote_lookup_value(quote_data_overrides, extracted, merged_entities, "plate_no") or None,
        owner_phone=_quote_lookup_value(quote_data_overrides, extracted, merged_entities, "owner_phone") or None,
        owner_name=_quote_lookup_value(quote_data_overrides, extracted, merged_entities, "owner_name") or None,
    )
    if order_id and order is None:
        return _quote_order_not_accessible_response(order_id=order_id, platform_name=platform_name)
    exclude_case_ids: Set[int] = set()
    duplicate_waiting_pair = await _find_waiting_duplicate_quote_confirm_task(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    if duplicate_waiting_pair:
        waiting_case, waiting_task = duplicate_waiting_pair
        exclude_case_ids.add(_safe_int(getattr(waiting_case, "id", 0), 0))
        await _cancel_waiting_duplicate_quote_task(
            db,
            case=waiting_case,
            task=waiting_task,
            owner_user_id=owner_user_id,
            reason="已上传新资料，自动中止上一笔重复投保确认",
        )
    case = await _get_or_create_case(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        order=order,
        platform_code=platform_code if explicit_platform_quote else "",
        platform_name=platform_name if explicit_platform_quote else "",
        ctx=ctx,
        # A new image after a completed quote usually means a new order. Even
        # if the caption carries field values, do not drag the previous quoted
        # case forward unless the user explicitly asks to requote.
        reuse_quoted=bool(_is_explicit_requote(text)),
        exclude_case_ids=exclude_case_ids,
    )
    case = await _lock_quote_case(db, case)
    quote_check_requested = explicit_platform_quote or _quote_check_context_is_active(case)

    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="chat",
        role="user",
        content=text,
        payload={"image_message": True, "image_count": len(images)},
    )

    order_data = _order_data(order)
    attached_images = await _attach_uploaded_images(db, case=case, owner_user_id=owner_user_id, images=images)

    cancelled_waiting_tasks = 0
    if attached_images:
        cancelled_waiting_tasks = await _cancel_active_quote_tasks_for_case(
            db,
            case=case,
            reason="cancelled_by_material_change",
        )

    images_by_slot = await _active_images_by_slot(db, case.id)
    normalized_data = _normalize_quote_case_data(
        base_data=_json_obj(case.normalized_data),
        order_data=order_data,
        text_data=text_data,
        images_by_slot=images_by_slot,
    )
    auto_platform_code = _to_str(case.platform_code or platform_code).strip().upper()
    auto_platform_name = _to_str(case.platform_name or platform_name).strip()
    if auto_platform_code and not auto_platform_name:
        auto_platform_name = _platform_display_name(auto_platform_code)
    auto_account_type_name = ""
    if explicit_platform_quote and auto_platform_code and auto_platform_name:
        auto_account_type_name = _extract_account_type_from_quote_text(text, auto_platform_name, auto_platform_code)
    if not auto_account_type_name:
        auto_account_type_name = _normalize_account_type_name(
            merged_entities.get("account_type_name")
            or detect_quote_vehicle_type(normalized_data, images_by_slot).get("config_type_name")
        )
    if quote_check_requested and auto_platform_name and not _is_quote_platform_developed(auto_platform_code):
        case.normalized_data = normalized_data
        case.draft_order_data = normalized_data
        case.status = CASE_STATUS_COLLECTING
        case.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="status",
            role="assistant",
            payload={
                "status": case.status,
                "unsupported_platform": True,
                "platform_code": auto_platform_code,
                "platform_name": auto_platform_name,
                "attached_images": attached_images,
                "quote_check_requested": quote_check_requested,
            },
        )
        await db.flush()
        return _unsupported_quote_platform_response(
            platform_code=auto_platform_code,
            platform_name=auto_platform_name,
            entities={**merged_entities, "quote_case_id": case.id, "order_id": case.order_id},
        )
    material_conflicts = _quote_material_issues(normalized_data, images_by_slot)
    missing = (
        _missing_requirements_for_quote_flow(
            normalized_data,
            images_by_slot,
            platform_code=auto_platform_code,
            account_type_name=auto_account_type_name,
        )
        if quote_check_requested
        else material_conflicts
    )
    case.normalized_data = normalized_data
    case.draft_order_data = normalized_data
    case.missing_requirements = missing
    case.status = CASE_STATUS_READY if quote_check_requested and not missing else CASE_STATUS_COLLECTING
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
            "quote_check_requested": quote_check_requested,
        },
    )

    if quote_check_requested and auto_platform_code and auto_platform_name and not missing:
        quote_ctx = dict(ctx or {})
        quote_ctx["images"] = []
        quote_ctx["uploaded_images"] = []
        quote_ctx["quote_images"] = []
        page_ctx = dict(quote_ctx.get("page_context") or {})
        page_ctx.pop("uploaded_images", None)
        quote_ctx["page_context"] = page_ctx
        quote_reply, quote_meta = await handle_quote_message(
            db,
            ctx=quote_ctx,
            entities={
                **merged_entities,
                "platform_code": auto_platform_code,
                "platform_name": auto_platform_name,
                "account_type_name": auto_account_type_name or merged_entities.get("account_type_name"),
            },
            text=f"{auto_platform_name}报价",
        )
        data = quote_meta.get("data")
        if isinstance(data, dict):
            payload = data.setdefault("payload", {})
            if isinstance(payload, dict):
                payload["auto_started_after_image_collect"] = True
                payload["image_collect"] = {
                    "attached_count": len(attached_images),
                    "ready_slots": {k: len(v or []) for k, v in (images_by_slot or {}).items() if v},
                }
        return quote_reply, quote_meta

    await db.flush()

    visible_image_quote_check = bool((explicit_platform_quote and missing) or (not quote_check_requested and material_conflicts))
    lines: List[str] = []
    if visible_image_quote_check:
        if quote_check_requested:
            lines.append(f"{auto_platform_name or platform_name or '平台'}报价资料还不完整，已中断本次报价。")
        else:
            lines.append("资料冲突，已暂停本次材料归位。请确认后再报价。")
        conflict_labels = [_missing_item_text(item) for item in missing if item.get("type") == "data_conflict"]
        ordinary_labels = [_missing_item_text(item) for item in missing if item.get("type") != "data_conflict"]
        if ordinary_labels:
            lines.append("缺少字段：" + "、".join(ordinary_labels[:8]))
        if conflict_labels:
            lines.append("资料冲突：" + "、".join(conflict_labels[:8]))

    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=missing,
        attached_images=attached_images,
        platform_account=None,
    )
    payload["quote_check_requested"] = quote_check_requested
    if not visible_image_quote_check:
        payload["silent"] = True
        payload["ui_visible"] = False
    response_data = _mk_data(
        result_status=RESULT_NEED_MORE if visible_image_quote_check else RESULT_SUCCESS,
        message="报价资料不足，已中断本次报价" if visible_image_quote_check else "图片已进入报价材料",
        entities={**merged_entities, "quote_case_id": case.id, "order_id": case.order_id},
        payload=payload,
    )
    if not visible_image_quote_check:
        response_data["silent"] = True
        response_data["ui_visible"] = False
    reply_text = "\n".join(lines)
    return reply_text, {
        "status": "success",
        "intent": "quote" if visible_image_quote_check else "quote_image_collect",
        "trace_id": _new_trace_id(),
        "silent": not visible_image_quote_check,
        "ui_visible": bool(visible_image_quote_check),
        "data": response_data,
        "actions": [],
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
        case_cancelled_tasks = await _cancel_active_quote_tasks_for_case(
            db,
            case=case,
            reason="cancelled_by_image_recall",
            now=now,
        )
        cancelled_tasks += case_cancelled_tasks

        images_by_slot = await _active_images_by_slot(db, int(case.id))
        normalized_data = dict(_json_obj(case.normalized_data))
        for item in case_changed_images:
            for field in QUOTE_IMAGE_FIELDS_BY_SLOT.get(_to_str(item.get("confirmed_slot_key")).strip(), ()):
                normalized_data.pop(field, None)
        normalized_data = _normalize_quote_case_data(
            base_data=normalized_data,
            order_data={},
            text_data={},
            images_by_slot=images_by_slot,
        )
        vehicle_type_detect = detect_quote_vehicle_type(normalized_data, images_by_slot)
        missing = _missing_requirements_for_quote_flow(
            normalized_data,
            images_by_slot,
            platform_code=case.platform_code or "",
            account_type_name=vehicle_type_detect.get("config_type_name"),
        )
        case.normalized_data = normalized_data
        case.draft_order_data = normalized_data
        case.missing_requirements = missing
        if case.status in {
            CASE_STATUS_READY,
            CASE_STATUS_WAITING_SMS,
            CASE_STATUS_WAITING_DUPLICATE_CONFIRM,
            CASE_STATUS_FAILED,
        }:
            case.status = CASE_STATUS_COLLECTING if missing else CASE_STATUS_READY
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


async def handle_quote_text_material_message(
    db: AsyncSession,
    *,
    ctx: Dict[str, Any],
    entities: Dict[str, Any],
    text: str,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    owner_user_id = _ctx_current_user_id(ctx)
    if owner_user_id <= 0:
        return None
    extracted = extract_quote_fields(text)
    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    await _expire_stale_waiting_sms_tasks_for_owner_session(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    await _expire_stale_running_quote_tasks_for_owner_session(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    form_values = _quote_material_form_values_from_context(ctx or {})
    form_session_id = _to_str((ctx or {}).get("quote_material_form_session_id")).strip()
    form_case_id = _safe_int((ctx or {}).get("quote_material_form_case_id"), 0)
    if form_values and form_session_id and session_id and form_session_id != session_id:
        return (
            "这份资料表单属于另一个会话，我没有写入当前会话。请在当前会话重新输入“手工”、“补资料”或“改资料”后再提交。",
            {
                "status": "success",
                "intent": "quote_material_form",
                "trace_id": _new_trace_id(),
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="资料表单会话不一致",
                    entities={"quote_case_id": form_case_id or None},
                    payload={"quote_material_form_stale": True},
                ),
                "actions": [],
            },
        )
    looks_like_material = _looks_like_quote_text_material(text, extracted)
    if form_values:
        looks_like_material = True
    active_case_for_followup: Optional[QuoteCase] = None
    vehicle_type_text_data = _quote_vehicle_type_text_data(text, extracted)
    if (
        not looks_like_material
        and vehicle_type_text_data
        and _looks_like_quote_vehicle_type_followup_command(text)
        and session_id
    ):
        active_case_for_followup = await _latest_active_case(db, owner_user_id=owner_user_id, session_id=session_id)
        looks_like_material = active_case_for_followup is not None
    if not looks_like_material and _quote_text_material_field_count(extracted) >= 1 and session_id:
        if active_case_for_followup is None:
            active_case_for_followup = await _latest_active_case(db, owner_user_id=owner_user_id, session_id=session_id)
        looks_like_material = _quote_case_has_pending_quote_check(active_case_for_followup)
    if not looks_like_material:
        return None
    _ensure_quote_flow_access(ctx)

    signal = detect_quote_signal(text)
    merged_entities = {**(entities or {}), **_json_obj(signal.get("entities"))}
    form_overrides = _quote_material_form_overrides_from_values(form_values)
    form_config_overrides = _quote_material_form_config_overrides_from_values(form_values)
    if form_values.get("account_type_name"):
        merged_entities["account_type_name"] = form_values.get("account_type_name")
    if form_overrides:
        merged_entities[QUOTE_DATA_OVERRIDES_KEY] = _merge_quote_data_overrides(
            merged_entities.get(QUOTE_DATA_OVERRIDES_KEY),
            form_overrides,
        )
    if form_config_overrides:
        merged_entities["quote_field_overrides"] = _merge_quote_config_overrides(
            merged_entities.get("quote_field_overrides"),
            form_config_overrides,
        )
    text_data = _quote_text_data_from_entities(extracted, merged_entities)
    type_data = vehicle_type_text_data or _quote_vehicle_type_text_data(text, text_data)
    if type_data:
        text_data = _merge_data(text_data, type_data)
        merged_entities = {**merged_entities, **type_data}

    platform_code = _to_str(merged_entities.get("platform_code")).strip().upper()
    platform_name = _to_str(merged_entities.get("platform_name")).strip()
    if not platform_code and platform_name:
        platform_code = _platform_code_from_display_name(platform_name) or ""
    if not platform_name and platform_code:
        platform_name = _platform_display_name(platform_code) or platform_code
    if platform_code and platform_name and not _is_quote_platform_developed(platform_code):
        platform_code = ""
        platform_name = ""

    order_id = (
        _safe_int((ctx or {}).get("order_id"), 0)
        or _safe_int(merged_entities.get("order_id"), 0)
        or None
    )
    order = await _find_order(
        db,
        ctx=ctx,
        order_id=order_id,
        plate_no=_to_str(text_data.get("plate_no")).strip() or None,
        owner_phone=_to_str(text_data.get("owner_phone")).strip() or None,
        owner_name=_to_str(text_data.get("owner_name")).strip() or None,
    )
    if order_id and order is None:
        order = None

    exclude_case_ids: Set[int] = set()
    duplicate_waiting_pair = await _find_waiting_duplicate_quote_confirm_task(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    if duplicate_waiting_pair:
        waiting_case, waiting_task = duplicate_waiting_pair
        exclude_case_ids.add(_safe_int(getattr(waiting_case, "id", 0), 0))
        await _cancel_waiting_duplicate_quote_task(
            db,
            case=waiting_case,
            task=waiting_task,
            owner_user_id=owner_user_id,
            reason="已提交新文本资料，自动中止上一笔重复投保确认",
        )

    case = None
    if form_case_id:
        case = await _quote_case_by_id_for_form(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            quote_case_id=form_case_id,
        )
        if case is None:
            return (
                "这份资料表单对应的会话材料已变化或不可用，我没有写入当前会话。请重新输入“手工”、“补资料”或“改资料”打开最新表单。",
                {
                    "status": "success",
                    "intent": "quote_material_form",
                    "trace_id": _new_trace_id(),
                    "data": _mk_data(
                        result_status=RESULT_NEED_MORE,
                        message="资料表单对应 case 不可用",
                        entities={"quote_case_id": form_case_id},
                        payload={"quote_material_form_stale": True},
                    ),
                    "actions": [],
                },
            )
        changed = False
        if platform_code and _to_str(case.platform_code).strip().upper() != platform_code:
            case.platform_code = platform_code
            changed = True
        if platform_name and _to_str(case.platform_name).strip() != platform_name:
            case.platform_name = platform_name
            changed = True
        if changed:
            case.updated_at = _now()
    if case is None:
        case = await _get_or_create_case(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            order=order,
            platform_code=platform_code,
            platform_name=platform_name,
            ctx=ctx,
            reuse_quoted=bool(_is_explicit_requote(text)),
            exclude_case_ids=exclude_case_ids,
        )
    case = await _lock_quote_case(db, case)
    pending_quote_check = _quote_case_has_pending_quote_check(case)
    cancelled_active_quote_tasks = await _cancel_active_quote_tasks_for_case(
        db,
        case=case,
        reason=QUOTE_MATERIAL_CHANGED_MESSAGE,
    )

    images_by_slot = await _active_images_by_slot(db, case.id)
    base_data = _json_obj(case.normalized_data) or _json_obj(case.draft_order_data)
    material_text = _norm_text(text)
    if material_text:
        previous_raw_text = _to_str(base_data.get("raw_text")).strip()
        if previous_raw_text and material_text not in previous_raw_text:
            text_data["raw_text"] = (previous_raw_text + "\n" + material_text)[-6000:]
        else:
            text_data["raw_text"] = previous_raw_text or material_text
    normalized_data = _normalize_quote_case_data(
        base_data=base_data,
        order_data=_order_data(order),
        text_data=text_data,
        images_by_slot=images_by_slot,
    )
    platform_code = _to_str(case.platform_code or platform_code).strip().upper()
    platform_name = _to_str(case.platform_name or platform_name).strip()
    if platform_code and not platform_name:
        platform_name = _platform_display_name(platform_code) or platform_code
    vehicle_type_detect = detect_quote_vehicle_type(normalized_data, images_by_slot)
    selected_account_type_name = _normalize_account_type_name(
        merged_entities.get("account_type_name")
        or normalized_data.get("account_type_name")
        or vehicle_type_detect.get("config_type_name")
    )
    missing = (
        _missing_requirements_for_quote_flow(
            normalized_data,
            images_by_slot,
            platform_code=platform_code,
            account_type_name=selected_account_type_name,
        )
        if pending_quote_check
        else []
    )

    case.draft_order_data = normalized_data
    case.normalized_data = normalized_data
    case.missing_requirements = missing
    case.status = CASE_STATUS_READY if pending_quote_check and platform_code and selected_account_type_name and not missing else CASE_STATUS_COLLECTING
    case.updated_at = _now()
    await _add_event(
        db,
        case=case,
        owner_user_id=owner_user_id,
        event_type="chat",
        role="user",
        content=text,
        payload={
            "text_material": True,
            "extracted_fields": extracted,
            "vehicle_type_detect": vehicle_type_detect,
            "cancelled_active_quote_tasks": cancelled_active_quote_tasks,
        },
    )

    if pending_quote_check and platform_code and platform_name and selected_account_type_name and not missing:
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="status",
            role="assistant",
            payload={
                "status": case.status,
                "text_material": True,
                "auto_continue_after_text_material": True,
                "cancelled_active_quote_tasks": cancelled_active_quote_tasks,
            },
        )
        await db.flush()
        quote_ctx = dict(ctx or {})
        quote_ctx["images"] = []
        quote_ctx["uploaded_images"] = []
        quote_ctx["quote_images"] = []
        page_ctx = dict(quote_ctx.get("page_context") or {})
        page_ctx.pop("uploaded_images", None)
        quote_ctx["page_context"] = page_ctx
        quote_reply, quote_meta = await handle_quote_message(
            db,
            ctx=quote_ctx,
            entities={
                **merged_entities,
                "platform_code": platform_code,
                "platform_name": platform_name,
                "account_type_name": selected_account_type_name,
            },
            text=f"{platform_name}报价",
        )
        data = quote_meta.get("data")
        if isinstance(data, dict):
            payload = data.setdefault("payload", {})
            if isinstance(payload, dict):
                payload["auto_started_after_text_collect"] = True
                payload["text_collect"] = {
                    "field_count": _quote_text_material_field_count(text_data),
                    "cancelled_active_quote_tasks": cancelled_active_quote_tasks,
                }
        return quote_reply, quote_meta

    if pending_quote_check and platform_code and platform_name and missing:
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="status",
            role="assistant",
            payload={"status": "collecting", "text_material": True, "missing": missing},
        )
        await db.flush()
        payload = _case_payload(
            case=case,
            normalized_data=normalized_data,
            images_by_slot=images_by_slot,
            missing=missing,
            attached_images=[],
            platform_account=None,
        )
        payload.update(
            {
                "text_material": True,
                "vehicle_type_detect": vehicle_type_detect,
                "pending_quote_check": True,
            }
        )
        missing_fields = [_missing_item_text(item) for item in missing if item.get("type") == "field"]
        missing_images = [_missing_item_text(item) for item in missing if item.get("type") == "image"]
        missing_conflicts = [_missing_item_text(item) for item in missing if item.get("type") == "data_conflict"]
        lines = [f"{platform_name}报价资料还不完整，已中断本次报价。"]
        if missing_fields:
            lines.append("缺少字段：" + "、".join(missing_fields))
        if missing_images:
            lines.append("缺少图片：" + "、".join(missing_images))
        if missing_conflicts:
            lines.append("资料冲突：" + "、".join(missing_conflicts))
        return (
            "\n".join(lines),
            {
                "status": "success",
                "intent": "quote",
                "trace_id": _new_trace_id(),
                "data": _mk_data(
                    result_status=RESULT_NEED_MORE,
                    message="报价资料不完整",
                    entities={"quote_case_id": case.id, "order_id": case.order_id},
                    payload=payload,
                ),
                "actions": [_mk_action(f"{platform_name}报价")],
            },
        )

    await db.flush()

    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=missing,
        attached_images=[],
        platform_account=None,
    )
    payload.update(
        {
            "text_material": True,
            "vehicle_type_detect": vehicle_type_detect,
            "silent": True,
            "ui_visible": False,
        }
    )
    data = _mk_data(
        result_status=RESULT_SUCCESS,
        message="已记录文本报价资料",
        entities={"quote_case_id": case.id, "order_id": case.order_id},
        payload=payload,
    )
    data["silent"] = True
    data["ui_visible"] = False
    return (
        "",
        {
            "status": "success",
            "intent": "quote_text_material",
            "trace_id": _new_trace_id(),
            "silent": True,
            "ui_visible": False,
            "data": data,
            "actions": [],
        },
    )


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
    _ensure_quote_flow_access(ctx)
    operator_role_name = _ctx_role_name(ctx)

    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    await _expire_stale_waiting_sms_tasks_for_owner_session(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    await _expire_stale_running_quote_tasks_for_owner_session(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    await _cancel_orphaned_waiting_duplicate_confirm_tasks(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    signal = detect_quote_signal(text)
    merged_entities = {**(entities or {}), **_json_obj(signal.get("entities"))}
    quote_flow_type = _to_str(merged_entities.get(QUOTE_FLOW_TYPE_KEY)).strip() or QUOTE_FLOW_NORMAL
    quote_product_exclusions = _extract_quote_product_exclusions(text)
    quote_command_mode = _to_str(merged_entities.get("quote_command_mode")).strip()
    if quote_command_mode == "全保":
        base_product_exclusions: List[str] = []
    elif quote_command_mode == "单商":
        base_product_exclusions = [QUOTE_COMPULSORY_LABEL]
    elif quote_command_mode == "交三":
        base_product_exclusions = [QUOTE_LOSS_LABEL]
    else:
        base_product_exclusions = _normalize_quote_product_exclusions(merged_entities.get(QUOTE_PRODUCT_EXCLUSIONS_KEY))
    if quote_product_exclusions:
        merged_entities[QUOTE_PRODUCT_EXCLUSIONS_KEY] = _normalize_quote_product_exclusions(
            [*base_product_exclusions, *quote_product_exclusions]
        )
        merged_entities["force_requote"] = True
    elif quote_command_mode in {"全保", "交三", "单商"} or QUOTE_PRODUCT_EXCLUSIONS_KEY in merged_entities:
        merged_entities[QUOTE_PRODUCT_EXCLUSIONS_KEY] = _normalize_quote_product_exclusions(base_product_exclusions)
    transfer_vehicle_command = _extract_transfer_vehicle_command(text)
    if transfer_vehicle_command:
        for key in ("is_transfer_vehicle", "transfer_date", "transfer_vehicle_override"):
            if transfer_vehicle_command.get(key) not in (None, ""):
                merged_entities[key] = transfer_vehicle_command.get(key)
    override_signal = detect_quote_config_override_signal(text)
    quote_field_overrides = _json_obj(override_signal.get("overrides"))
    repair_code_command = _extract_quote_repair_code_command(text)
    repair_code_resolution: Dict[str, Any] = {}
    extracted = extract_quote_fields(text)
    data_override_signal = detect_quote_data_override_signal(text)
    quote_data_overrides = _json_obj(data_override_signal.get("overrides"))
    if quote_data_overrides:
        merged_entities = {
            **merged_entities,
            QUOTE_DATA_OVERRIDES_KEY: quote_data_overrides,
            "force_requote": True,
        }
    quote_date_overrides = {
        key: extracted.get(key)
        for key in ("commercial_start_date", "compulsory_start_date")
        if extracted.get(key) not in (None, "")
    }
    if repair_code_command:
        try:
            repair_code_resolution = await _resolve_quote_repair_code_command(
                db,
                ctx=ctx,
                owner_user_id=owner_user_id,
                command=repair_code_command,
            )
        except Exception as exc:
            detail = sanitize_quote_user_message(exc, "送修码查询失败")
            return (
                f"送修码设置失败：{detail}",
                {
                    "status": "success",
                    "intent": "quote_config_override",
                    "trace_id": _new_trace_id(),
                    "data": _mk_data(
                        result_status=RESULT_FAILED,
                        message=detail,
                        entities={},
                        payload={"repair_code_command": repair_code_command},
                    ),
                    "actions": [],
                },
            )
        quote_field_overrides = _merge_quote_config_overrides(
            quote_field_overrides,
            repair_code_resolution.get("overrides"),
            validate_positive=False,
        )
    joint_sales_image_adjustment = _extract_joint_sales_image_adjustment(text)
    if joint_sales_image_adjustment:
        quote_field_overrides = _merge_quote_config_overrides(
            quote_field_overrides,
            {joint_sales_image_adjustment.get("field_name") or "途家安顺保费": joint_sales_image_adjustment.get("field_value")},
        )
    if quote_field_overrides:
        merged_entities = {
            **merged_entities,
            "quote_field_overrides": quote_field_overrides,
            "force_requote": True,
        }
    quote_state_changed = bool(
        quote_field_overrides
        or quote_data_overrides
        or quote_product_exclusions
        or quote_command_mode in {"全保", "交三", "单商"}
        or QUOTE_PRODUCT_EXCLUSIONS_KEY in merged_entities
        or transfer_vehicle_command
        or quote_date_overrides
    )
    override_summary = "、".join(
        item
        for item in (
            _quote_data_override_summary(quote_data_overrides),
            _to_str(repair_code_resolution.get("summary")).strip() or _quote_override_summary(quote_field_overrides),
        )
        if item
    )
    override_reply_prefix = f"已按本次调整重报：{override_summary}\n" if override_summary else ""
    sms_code = _extract_sms_code(text)

    waiting_pair = await _find_waiting_task(db, owner_user_id=owner_user_id, session_id=session_id, for_update=True)
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
        return await _complete_waiting_task(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            sms_code=sms_code,
            operator_role_name=_ctx_role_name(ctx),
        )

    if sms_code:
        expired_pair = await _find_waiting_task(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            include_expired=True,
            for_update=True,
        )
        if expired_pair and _is_sms_task_expired(expired_pair[1]):
            case, task = expired_pair
            await _expire_waiting_sms_task(
                db,
                case=case,
                task=task,
                owner_user_id=owner_user_id,
                reason=QUOTE_SMS_EXPIRED_MESSAGE,
            )
            await db.flush()
            platform_label = case.platform_name or case.platform_code or "平台"
            return _build_quote_user_failure_response(
                reply=(
                    f"{platform_label}验证码已过期，我没有继续提交旧验证码。\n"
                    f"请重新发送“{platform_label}报价”触发新的短信验证码。"
                ),
                case=case,
                task=task,
                trace_id=task.trace_id or _new_trace_id(),
                failure_code=FAILURE_CODE_SMS_EXPIRED,
                failure_reason=QUOTE_SMS_EXPIRED_MESSAGE,
                result_status=RESULT_NOT_READY,
                response_status="success",
                actions=[_mk_action(f"{platform_label}报价")],
                payload={"quote_case": {"id": case.id, "case_no": case.case_no, "status": case.status}},
            )
        invalid_pair = await _find_recent_invalid_sms_task(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
        )
        if invalid_pair:
            case, task = invalid_pair
            reason = _to_str(task.error_detail).strip()
            material_changed = reason in {"cancelled_by_material_change", "cancelled_by_image_recall", QUOTE_MATERIAL_CHANGED_MESSAGE}
            message = "材料已更新，上一条验证码已作废" if material_changed else "验证码已过期"
            return (
                f"{case.platform_name or case.platform_code or '平台'}{message}，我没有继续提交旧验证码。\n请确认材料后重新发送“{case.platform_name or case.platform_code or '平台'}报价”。",
                {
                    "status": "success",
                    "intent": "quote",
                    "trace_id": task.trace_id or _new_trace_id(),
                    "data": _mk_data(
                        result_status=RESULT_NOT_READY,
                        message=message,
                        entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                        payload={"quote_case": {"id": case.id, "case_no": case.case_no, "status": case.status}},
                    ),
                    "actions": [_mk_action(f"{case.platform_name or '太平洋'}报价"), _mk_action("查看当前材料状态")],
                },
            )

    if joint_sales_image_adjustment and waiting_pair:
        case, task = waiting_pair
        await db.flush()
        return (
            f"{case.platform_name or case.platform_code or '平台'}报价仍在等待验证码，请先输入验证码完成报价，再调整非车金额。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": task.trace_id or _new_trace_id(),
                "data": _mk_data(
                    result_status=RESULT_NOT_READY,
                    message="当前报价仍在等待验证码",
                    entities={"quote_case_id": case.id, "quote_task_id": task.id, "order_id": case.order_id},
                    payload={"joint_sales_image_adjustment": joint_sales_image_adjustment},
                ),
                "actions": [_mk_action("输入短信验证码")],
            },
        )

    if waiting_pair:
        _, waiting_task = waiting_pair
        waiting_product_state_changed = _quote_product_state_changes_current(
            quote_command_mode=quote_command_mode,
            quote_product_exclusions=quote_product_exclusions,
            current_exclusions=_snapshot_quote_product_exclusions(waiting_task.submitted_snapshot),
        )
    else:
        waiting_product_state_changed = False

    if waiting_pair and (quote_field_overrides or quote_data_overrides or transfer_vehicle_command or quote_date_overrides or waiting_product_state_changed):
        case, task = waiting_pair
        await _expire_waiting_sms_task(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            reason=QUOTE_MATERIAL_CHANGED_MESSAGE,
        )
        await db.flush()
        waiting_pair = None

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
        await db.flush()
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

    duplicate_confirm_pair = await _find_waiting_duplicate_quote_confirm_task(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    # Joint-sales image adjustment only rewrites a prior success result image.
    # Do not start a full duplicate-confirm requote and then discard that reply
    # (same cancel-then-adjust pattern as image/text material intake).
    if joint_sales_image_adjustment and duplicate_confirm_pair:
        waiting_case, waiting_task = duplicate_confirm_pair
        await _cancel_waiting_duplicate_quote_task(
            db,
            case=waiting_case,
            task=waiting_task,
            owner_user_id=owner_user_id,
            reason="已调整非车金额，自动中止上一笔重复投保确认",
        )
        duplicate_confirm_pair = None
    if joint_sales_image_adjustment:
        source_pair = await _latest_success_quote_task_for_session(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            ctx=ctx,
            for_update=False,
        )
        if source_pair:
            return await _handle_joint_sales_image_adjustment_message(
                db,
                ctx=ctx,
                owner_user_id=owner_user_id,
                session_id=session_id,
                text=text,
                adjustment=joint_sales_image_adjustment,
                source_pair=source_pair,
            )
    if duplicate_confirm_pair:
        _, duplicate_task = duplicate_confirm_pair
        duplicate_product_state_changed = _quote_product_state_changes_current(
            quote_command_mode=quote_command_mode,
            quote_product_exclusions=quote_product_exclusions,
            current_exclusions=_snapshot_quote_product_exclusions(duplicate_task.submitted_snapshot),
        )
    else:
        duplicate_product_state_changed = False

    # New quotes no longer create this state, but old data can still contain a
    # task created by the retired popup flow. Recover it on the next quote
    # command, never on unrelated chat input. A cancellation or a
    # material/product adjustment still takes precedence below.
    if duplicate_confirm_pair and not (
        _is_duplicate_quote_cancel_text(text)
        or quote_field_overrides
        or quote_data_overrides
        or transfer_vehicle_command
        or quote_date_overrides
        or duplicate_product_state_changed
    ) and (signal.get("is_quote") or looks_like_short_quote_command(text)):
        case, task = duplicate_confirm_pair
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="task",
            role="system",
            payload={
                "task_id": task.id,
                "trace_id": task.trace_id,
                "legacy_duplicate_quote_auto_recovered": True,
            },
        )
        return await _complete_waiting_duplicate_quote_task(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            operator_role_name=_ctx_role_name(ctx),
        )

    if duplicate_confirm_pair and (quote_field_overrides or quote_data_overrides or transfer_vehicle_command or quote_date_overrides or duplicate_product_state_changed):
        case, task = duplicate_confirm_pair
        if not merged_entities.get("platform_code") and (case.platform_code or task.platform_code):
            merged_entities["platform_code"] = case.platform_code or task.platform_code
        if not merged_entities.get("platform_name") and (case.platform_name or task.platform_name):
            merged_entities["platform_name"] = case.platform_name or task.platform_name
        if case.order_id and not merged_entities.get("order_id"):
            merged_entities["order_id"] = case.order_id

        cancelled = await _cancel_waiting_tasks_for_case(
            db,
            case=case,
            reason=QUOTE_DUPLICATE_CONFIRM_REPLACED_MESSAGE,
        )
        if cancelled:
            case.status = CASE_STATUS_READY
            case.current_task_id = None
            case.updated_at = _now()
            await _add_event(
                db,
                case=case,
                owner_user_id=owner_user_id,
                event_type="task",
                role="system",
                payload={
                    "duplicate_quote_confirm_cancelled": True,
                    "reason": QUOTE_DUPLICATE_CONFIRM_REPLACED_MESSAGE,
                    "quote_field_overrides": quote_field_overrides,
                    QUOTE_DATA_OVERRIDES_KEY: quote_data_overrides,
                    "quote_date_overrides": quote_date_overrides,
                    QUOTE_PRODUCT_EXCLUSIONS_KEY: quote_product_exclusions,
                    "old_task_id": task.id,
                },
            )
            await db.flush()
        duplicate_confirm_pair = None

    if duplicate_confirm_pair and _is_duplicate_quote_cancel_text(text):
        case, task = duplicate_confirm_pair
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="chat",
            role="user",
            content=text,
            payload={"duplicate_quote_cancelled": True},
        )
        return await _cancel_waiting_duplicate_quote_task(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
        )

    if duplicate_confirm_pair and _is_duplicate_quote_confirmation_text(text):
        case, task = duplicate_confirm_pair
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="chat",
            role="user",
            content=text,
            payload={"duplicate_quote_confirmed": True},
        )
        return await _complete_waiting_duplicate_quote_task(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            operator_role_name=_ctx_role_name(ctx),
        )
    if duplicate_confirm_pair and signal.get("is_quote"):
        case, task = duplicate_confirm_pair
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="chat",
            role="user",
            content=text,
            payload={"duplicate_quote_auto_confirmed": True},
        )
        return await _complete_waiting_duplicate_quote_task(
            db,
            case=case,
            task=task,
            owner_user_id=owner_user_id,
            operator_role_name=_ctx_role_name(ctx),
        )
    # Waiting for an explicit yes/no: do not guess unclear short polarity attempts.
    if (
        duplicate_confirm_pair
        and not quote_state_changed
        and not sms_code
        and _looks_like_unclear_chat_polarity_attempt(text)
    ):
        case, task = duplicate_confirm_pair
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="chat",
            role="user",
            content=text,
            payload={"duplicate_quote_confirm_unclear": True},
        )
        return _duplicate_quote_unclear_command_response(
            case=case,
            task=task,
            trace_id=task.trace_id or _new_trace_id(),
        )

    platform_code = _to_str(merged_entities.get("platform_code")).strip().upper()
    platform_name = _to_str(merged_entities.get("platform_name")).strip()
    if not platform_code and platform_name:
        platform_code = _platform_code_from_display_name(platform_name) or "STUB"
    if not platform_name and platform_code:
        platform_name = PLATFORM_ALIASES.get(platform_code, (platform_code, ()))[0]

    ctx_order_id = _safe_int((ctx or {}).get("order_id"), 0) or None
    entity_order_id = _safe_int(merged_entities.get("order_id"), 0) or None
    explicit_order_id = ctx_order_id or entity_order_id
    short_quote_command = looks_like_short_quote_command(text)
    quote_command_restarts_active_task = bool(signal.get("is_quote") or short_quote_command)
    transfer_vehicle_text_data = {
        key: merged_entities.get(key)
        for key in ("is_transfer_vehicle", "transfer_date", "transfer_vehicle_override")
        if merged_entities.get(key) not in (None, "")
    }
    cancelled_active_quote_tasks = 0
    inherited_case: Optional[QuoteCase] = None
    inherited_order_id: Optional[int] = None
    if (
        quote_state_changed
        or short_quote_command
    ) and (not platform_code or not platform_name):
        inherited_case = await _latest_reusable_session_case(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            ctx=ctx,
            reuse_quoted=True,
        )
        if inherited_case:
            platform_code = platform_code or _to_str(inherited_case.platform_code).strip().upper()
            platform_name = platform_name or _to_str(inherited_case.platform_name).strip()
            if not platform_code and platform_name:
                platform_code = _platform_code_from_display_name(platform_name)
            if not platform_name and platform_code:
                platform_name = PLATFORM_ALIASES.get(platform_code, (platform_code, ()))[0]
            if inherited_case.order_id and not merged_entities.get("order_id"):
                inherited_order_id = _safe_int(inherited_case.order_id, 0) or None
                merged_entities["order_id"] = inherited_order_id

    if not platform_name and quote_state_changed:
        case = inherited_case or await _get_or_create_case(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            order=None,
            platform_code="",
            platform_name="",
            ctx=ctx,
        )
        case = await _lock_quote_case(db, case)
        cancelled_active_quote_tasks = await _cancel_active_quote_tasks_for_case(
            db,
            case=case,
            reason=QUOTE_SUPERSEDED_MESSAGE,
        )
        old_draft = _json_obj(case.draft_order_data)
        old_case_data = _json_obj(case.normalized_data) or old_draft
        quote_flow_type = _resolve_followup_quote_flow_type(
            current_flow_type=quote_flow_type,
            merged_entities=merged_entities,
            case_data=old_case_data,
            quote_state_changed=quote_state_changed,
        )
        if quote_flow_type != QUOTE_FLOW_NORMAL or QUOTE_FLOW_TYPE_KEY in merged_entities:
            merged_entities[QUOTE_FLOW_TYPE_KEY] = quote_flow_type
        merged_overrides = _merge_quote_config_overrides(
            old_draft.get("quote_field_overrides"),
            quote_field_overrides,
        )
        merged_data_overrides = _merge_quote_data_overrides(
            old_draft.get(QUOTE_DATA_OVERRIDES_KEY),
            quote_data_overrides,
        )
        text_patch: Dict[str, Any] = {**quote_date_overrides, **transfer_vehicle_text_data}
        if quote_flow_type != QUOTE_FLOW_NORMAL or QUOTE_FLOW_TYPE_KEY in merged_entities:
            text_patch[QUOTE_FLOW_TYPE_KEY] = quote_flow_type
        if merged_overrides:
            text_patch["quote_field_overrides"] = merged_overrides
        if merged_data_overrides:
            text_patch.update(merged_data_overrides)
            text_patch[QUOTE_DATA_OVERRIDES_KEY] = merged_data_overrides
        old_product_exclusions = _normalize_quote_product_exclusions(old_case_data.get(QUOTE_PRODUCT_EXCLUSIONS_KEY))
        merged_product_exclusions, product_state_explicit = _merge_quote_product_exclusions_for_command(
            current_exclusions=old_product_exclusions,
            merged_entities=merged_entities,
            quote_command_mode=quote_command_mode,
            quote_product_exclusions=quote_product_exclusions,
        )
        if product_state_explicit or (quote_state_changed and old_product_exclusions):
            text_patch[QUOTE_PRODUCT_EXCLUSIONS_KEY] = merged_product_exclusions
        images_by_slot = await _active_images_by_slot(db, case.id)
        next_normalized = _normalize_quote_case_data(
            base_data=old_case_data,
            order_data={},
            text_data=text_patch,
            images_by_slot=images_by_slot,
        )
        case.draft_order_data = next_normalized
        case.normalized_data = next_normalized
        case.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="chat",
            role="user",
            content=text,
            payload={
                "quote_field_overrides": merged_overrides,
                QUOTE_DATA_OVERRIDES_KEY: merged_data_overrides,
                QUOTE_PRODUCT_EXCLUSIONS_KEY: text_patch.get(QUOTE_PRODUCT_EXCLUSIONS_KEY),
                "quote_date_overrides": quote_date_overrides,
                "transfer_vehicle": transfer_vehicle_text_data,
                "deferred_quote": True,
                "cancelled_active_quote_tasks": cancelled_active_quote_tasks,
            },
        )
        await db.flush()
        if cancelled_active_quote_tasks:
            await db.commit()
        summary = "、".join(
            item
            for item in (
                _quote_data_override_summary(merged_data_overrides),
                _to_str(repair_code_resolution.get("summary")).strip() or _quote_override_summary(merged_overrides),
            )
            if item
        )
        if not summary:
            summary = _quote_product_exclusion_summary(text_patch.get(QUOTE_PRODUCT_EXCLUSIONS_KEY))
        if not summary and transfer_vehicle_text_data:
            summary = "已按过户车处理" if bool(transfer_vehicle_text_data.get("is_transfer_vehicle")) else "已按非过户车处理"
        response_data = _mk_data(
            result_status=RESULT_NOT_READY,
            message="已记录报价调整",
            entities={"quote_case_id": case.id, "order_id": case.order_id},
            payload={
                "quote_field_overrides": merged_overrides,
                QUOTE_DATA_OVERRIDES_KEY: merged_data_overrides,
                QUOTE_PRODUCT_EXCLUSIONS_KEY: text_patch.get(QUOTE_PRODUCT_EXCLUSIONS_KEY),
                "quote_date_overrides": quote_date_overrides,
                "transfer_vehicle": {
                    key: next_normalized.get(key)
                    for key in ("is_transfer_vehicle", "transfer_date", "transfer_vehicle_source", "transfer_vehicle_override")
                    if next_normalized.get(key) not in (None, "")
                },
                "repair_code": {
                    "matched": repair_code_resolution.get("matched"),
                    "query": repair_code_resolution.get("query"),
                } if repair_code_resolution else {},
                "silent": False,
                "ui_visible": True,
            },
        )
        response_data["silent"] = False
        response_data["ui_visible"] = True
        return (
            f"已记录：{summary}。",
            {
                "status": "success",
                "intent": "quote_config_override",
                "trace_id": _new_trace_id(),
                "silent": False,
                "ui_visible": True,
                "data": response_data,
                "actions": [],
            },
        )

    if signal.get("is_quote") and not platform_name:
        command_platform_name = _quote_platform_name_from_command(text)
        if command_platform_name:
            platform_name = command_platform_name
            platform_code = _platform_code_from_display_name(platform_name) or platform_code or "STUB"
            if platform_code != "STUB":
                platform_name = _platform_display_name(platform_code) or platform_name

    if platform_name and not _is_quote_platform_developed(platform_code):
        return _unsupported_quote_platform_response(
            platform_code=platform_code,
            platform_name=platform_name,
            entities=merged_entities,
        )

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

    account_type_name = _normalize_account_type_name(
        merged_entities.get("account_type_name") or merged_entities.get("account_type")
    ) or _extract_account_type_from_quote_text(text, platform_name, platform_code)

    order_id = explicit_order_id or inherited_order_id or None
    text_data = _quote_text_data_from_entities(extracted, merged_entities)
    plate_no = _quote_lookup_value(quote_data_overrides, extracted, merged_entities, "plate_no") or None
    owner_phone = _quote_lookup_value(quote_data_overrides, extracted, merged_entities, "owner_phone") or None
    owner_name = _quote_lookup_value(quote_data_overrides, extracted, merged_entities, "owner_name") or None
    order = await _find_order(
        db,
        ctx=ctx,
        order_id=order_id,
        plate_no=plate_no,
        owner_phone=owner_phone,
        owner_name=owner_name,
    )
    if order_id and order is None:
        return _quote_order_not_accessible_response(
            order_id=order_id,
            platform_name=platform_name,
            inherited=bool(inherited_order_id and not explicit_order_id),
        )

    case = await _get_or_create_case(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        order=order,
        platform_code=platform_code,
        platform_name=platform_name,
        ctx=ctx,
    )
    case = await _lock_quote_case(db, case)
    await _add_event(db, case=case, owner_user_id=owner_user_id, event_type="chat", role="user", content=text, payload={})
    cancelled_active_quote_tasks = 0
    if quote_state_changed or quote_command_restarts_active_task:
        cancelled_active_quote_tasks = await _cancel_active_quote_tasks_for_case(
            db,
            case=case,
            reason=QUOTE_SUPERSEDED_MESSAGE,
        )

    order_data = _order_data(order)
    old_draft = _json_obj(case.draft_order_data)
    old_case_data = _json_obj(case.normalized_data) or old_draft
    resolved_quote_flow_type = _resolve_followup_quote_flow_type(
        current_flow_type=quote_flow_type,
        merged_entities=merged_entities,
        case_data=old_case_data,
        quote_state_changed=quote_state_changed,
    )
    if resolved_quote_flow_type != quote_flow_type:
        # Follow-up parameter changes should stay inside the active branch
        # (for example renewal quotePolicy.do) unless the command explicitly switches flow.
        quote_flow_type = resolved_quote_flow_type
    if quote_flow_type != QUOTE_FLOW_NORMAL or QUOTE_FLOW_TYPE_KEY in merged_entities:
        merged_entities[QUOTE_FLOW_TYPE_KEY] = quote_flow_type
        text_data[QUOTE_FLOW_TYPE_KEY] = quote_flow_type
    merged_quote_field_overrides = _merge_quote_config_overrides(
        old_draft.get("quote_field_overrides"),
        merged_entities.get("quote_field_overrides"),
    )
    if merged_quote_field_overrides:
        text_data["quote_field_overrides"] = merged_quote_field_overrides
    merged_quote_data_overrides = _merge_quote_data_overrides(
        old_draft.get(QUOTE_DATA_OVERRIDES_KEY),
        merged_entities.get(QUOTE_DATA_OVERRIDES_KEY),
    )
    if merged_quote_data_overrides:
        text_data.update(merged_quote_data_overrides)
        text_data[QUOTE_DATA_OVERRIDES_KEY] = merged_quote_data_overrides
    old_product_exclusions = _normalize_quote_product_exclusions(old_case_data.get(QUOTE_PRODUCT_EXCLUSIONS_KEY))
    merged_product_exclusions, product_state_explicit = _merge_quote_product_exclusions_for_command(
        current_exclusions=old_product_exclusions,
        merged_entities=merged_entities,
        quote_command_mode=quote_command_mode,
        quote_product_exclusions=quote_product_exclusions,
    )
    if product_state_explicit or (quote_state_changed and old_product_exclusions):
        text_data[QUOTE_PRODUCT_EXCLUSIONS_KEY] = merged_product_exclusions

    await _sync_order_images_to_case(db, case=case, owner_user_id=owner_user_id, order=order)
    attached_images = await _attach_uploaded_images(
        db,
        case=case,
        owner_user_id=owner_user_id,
        images=_collect_context_images(ctx),
    )

    images_by_slot = await _active_images_by_slot(db, case.id)
    normalized_data = _normalize_quote_case_data(
        base_data=old_case_data,
        order_data=order_data,
        text_data=text_data,
        images_by_slot=images_by_slot,
    )
    case.draft_order_data = normalized_data
    case.normalized_data = normalized_data
    if quote_state_changed or quote_command_restarts_active_task or cancelled_active_quote_tasks:
        await db.flush()
        await db.commit()
    vehicle_type_detect = detect_quote_vehicle_type(normalized_data, images_by_slot)
    auto_account_type_name = _normalize_account_type_name(vehicle_type_detect.get("config_type_name"))
    selected_account_type_name = account_type_name or auto_account_type_name
    platform_account = await _select_logged_quote_platform_account(
        db,
        owner_user_id=owner_user_id,
        platform_code=platform_code,
        account_type_name=selected_account_type_name,
    )
    platform_has_enabled_account = False
    if platform_account is None:
        platform_has_enabled_account = await _has_enabled_quote_platform_account(
            db,
            owner_user_id=owner_user_id,
            platform_code=platform_code,
            account_type_name=None,
        )
    missing = _missing_requirements_for_quote_flow(
        normalized_data,
        images_by_slot,
        platform_code=platform_code,
        account_type_name=selected_account_type_name,
        quote_flow_type=quote_flow_type,
    )
    case.missing_requirements = missing
    preflight_items = await _collect_quote_command_preflight_items(
        db,
        missing=missing,
        platform_account=platform_account,
        platform_has_enabled_account=platform_has_enabled_account,
        platform_code=platform_code,
        platform_name=platform_name,
        selected_account_type_name=selected_account_type_name or account_type_name,
        operator_role_name=operator_role_name,
    )
    if preflight_items:
        has_material_blocker = any(
            _to_str(item.get("failure_code")).strip() == FAILURE_CODE_MATERIAL_MISSING for item in preflight_items
        )
        case.status = CASE_STATUS_COLLECTING if has_material_blocker else CASE_STATUS_READY
        case.updated_at = _now()
        await _add_event(
            db,
            case=case,
            owner_user_id=owner_user_id,
            event_type="status",
            role="assistant",
            payload={
                "status": case.status,
                "missing": missing,
                "preflight_checklist": preflight_items,
                "need_platform_account": platform_account is None,
                "need_platform_default_config": any(
                    _to_str(item.get("failure_code")).strip() == FAILURE_CODE_DEFAULT_CONFIG_MISSING
                    for item in preflight_items
                ),
                "platform_code": platform_code,
                "account_type_name": selected_account_type_name or account_type_name,
            },
        )
        await db.flush()
        payload = _case_payload(
            case=case,
            normalized_data=normalized_data,
            images_by_slot=images_by_slot,
            missing=missing,
            attached_images=attached_images,
            platform_account=platform_account,
        )
        return _build_quote_preflight_blocked_response(
            case=case,
            platform_code=platform_code,
            platform_name=platform_name,
            selected_account_type_name=selected_account_type_name or account_type_name,
            items=preflight_items,
            merged_entities=merged_entities,
            payload=payload,
            override_summary=override_summary,
            attached_images=attached_images,
            operator_role_name=operator_role_name,
        )

    case.status = CASE_STATUS_READY
    case.updated_at = _now()

    snapshot = _snapshot_payload(case=case, normalized_data=normalized_data, images_by_slot=images_by_slot)
    trace_id = _new_trace_id()
    if quote_flow_type == QUOTE_FLOW_RENEWAL:
        if _has_reusable_renewal_quote_context(normalized_data):
            snapshot = await apply_platform_default_config_to_snapshot(
                db,
                snapshot={
                    **snapshot,
                    "vehicle_type_detect": vehicle_type_detect,
                    "quote_flow_type": QUOTE_FLOW_RENEWAL,
                    QUOTE_PRODUCT_EXCLUSIONS_KEY: _normalize_quote_product_exclusions(
                        normalized_data.get(QUOTE_PRODUCT_EXCLUSIONS_KEY)
                    ),
                },
                platform_code=platform_code,
                account_type_name=platform_account.account_type_name,
                config_type_name=selected_account_type_name,
            )
            return await _continue_quote_with_platform_account(
                db,
                case=case,
                owner_user_id=owner_user_id,
                snapshot=snapshot,
                trace_id=trace_id,
                platform_account=platform_account,
                merged_entities={
                    **merged_entities,
                    QUOTE_FLOW_TYPE_KEY: QUOTE_FLOW_RENEWAL,
                    "quote_case_id": case.id,
                },
                normalized_data=normalized_data,
                images_by_slot=images_by_slot,
                attached_images=attached_images,
                config_type_name=selected_account_type_name,
                reply_prefix=override_reply_prefix,
                operator_role_name=operator_role_name,
            )
        snapshot = {
            **snapshot,
            "vehicle_type_detect": vehicle_type_detect,
            "quote_flow_type": QUOTE_FLOW_RENEWAL,
            QUOTE_PRODUCT_EXCLUSIONS_KEY: _normalize_quote_product_exclusions(
                normalized_data.get(QUOTE_PRODUCT_EXCLUSIONS_KEY)
            ),
        }
        return await _continue_renewal_lookup_with_platform_account(
            db,
            case=case,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            trace_id=trace_id,
            platform_account=platform_account,
            operator_role_name=operator_role_name,
        )
    if _should_auto_probe_renewal_before_normal_quote(
        platform_code=platform_code,
        quote_flow_type=quote_flow_type,
        account_type_name=selected_account_type_name,
        normalized_data=normalized_data,
    ):
        auto_renewal_reply, auto_renewal_response = await _continue_renewal_lookup_with_platform_account(
            db,
            case=case,
            owner_user_id=owner_user_id,
            snapshot={
                **snapshot,
                "vehicle_type_detect": vehicle_type_detect,
                "quote_flow_type": QUOTE_FLOW_RENEWAL,
                QUOTE_PRODUCT_EXCLUSIONS_KEY: _normalize_quote_product_exclusions(
                    normalized_data.get(QUOTE_PRODUCT_EXCLUSIONS_KEY)
                ),
            },
            trace_id=trace_id,
            platform_account=platform_account,
            operator_role_name=operator_role_name,
            auto_probe=True,
        )
        if not _is_silent_auto_renewal_not_found_response(auto_renewal_response):
            return auto_renewal_reply, auto_renewal_response
        trace_id = _new_trace_id()
        case.current_task_id = None
        case.status = CASE_STATUS_READY
        case.updated_at = _now()
        await db.flush()
    snapshot = await apply_platform_default_config_to_snapshot(
        db,
        snapshot=snapshot,
        platform_code=platform_code,
        account_type_name=platform_account.account_type_name,
        config_type_name=selected_account_type_name,
    )
    # Default-config blockers are already handled by preflight above; keep continue path as safety net.
    return await _continue_quote_with_platform_account(
        db,
        case=case,
        owner_user_id=owner_user_id,
        snapshot=snapshot,
        trace_id=trace_id,
        platform_account=platform_account,
        merged_entities=merged_entities,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        attached_images=attached_images,
        config_type_name=selected_account_type_name,
        reply_prefix=override_reply_prefix,
        operator_role_name=operator_role_name,
    )


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
    _ensure_quote_flow_access(ctx)

    signal = detect_platform_credential_signal(text)
    merged_entities = {**(entities or {}), **_json_obj(signal.get("entities"))}
    platform_code = _to_str(merged_entities.get("platform_code")).strip().upper()
    platform_name = _to_str(merged_entities.get("platform_name")).strip()
    if not platform_name and platform_code:
        platform_name = _platform_display_name(platform_code)
    platform_text = f"{platform_name}平台" if platform_name else "对应平台"
    operator_role_name = _ctx_role_name(ctx)
    if _quote_account_needs_admin_contact(operator_role_name):
        reply_text = f"{platform_text}账号资料请联系管理员在“平台账号管理”中维护；聊天框不再保存账号、密码或手机号。"
        actions: List[Dict[str, Any]] = []
    else:
        reply_text = f"为了避免账号资料和报价会话混在一起，{platform_text}账号请统一在右上角“平台账号管理”中新增或编辑；聊天框不再保存账号、密码或手机号。"
        actions = [
            _mk_action(
                "平台账号管理",
                "open_account_manager",
                "quote_platform_accounts",
                platform_code=platform_code,
                platform_name=platform_name,
            )
        ]
    return (
        reply_text,
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
            "actions": actions,
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
    _ensure_quote_flow_access(ctx)
    session_id = _to_str((ctx or {}).get("session_id")).strip() or None
    await _expire_stale_waiting_sms_tasks_for_owner_session(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    await _expire_stale_running_quote_tasks_for_owner_session(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    await _cancel_orphaned_waiting_duplicate_confirm_tasks(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    case = await _latest_active_case(db, owner_user_id=owner_user_id, session_id=session_id)
    if not case:
        return None
    waiting_pair = await _find_waiting_task(
        db,
        owner_user_id=owner_user_id,
        session_id=session_id,
        for_update=True,
    )
    waiting_task: Optional[QuoteTask] = None
    if waiting_pair and _safe_int(waiting_pair[0].id, 0) == _safe_int(case.id, 0):
        case = waiting_pair[0]
        waiting_task = waiting_pair[1]
        if not await _quote_snapshot_material_is_current(db, case=case, snapshot=_json_obj(waiting_task.submitted_snapshot)):
            return await _stop_quote_for_material_change(
                db,
                case=case,
                owner_user_id=owner_user_id,
                platform_name=case.platform_name or case.platform_code or "平台",
                trace_id=waiting_task.trace_id or _new_trace_id(),
                task=waiting_task,
            )
        if not await _quote_snapshot_default_config_is_current(
            db,
            snapshot=_json_obj(waiting_task.submitted_snapshot),
            platform_code=case.platform_code or "",
        ):
            return await _stop_quote_for_default_config_change(
                db,
                case=case,
                owner_user_id=owner_user_id,
                platform_name=case.platform_name or case.platform_code or "平台",
                trace_id=waiting_task.trace_id or _new_trace_id(),
                task=waiting_task,
            )

    images_by_slot = await _active_images_by_slot(db, case.id)
    normalized_data = _normalize_quote_case_data(
        base_data=_json_obj(case.normalized_data),
        order_data={},
        text_data={},
        images_by_slot=images_by_slot,
    )
    vehicle_type_detect = detect_quote_vehicle_type(normalized_data, images_by_slot)
    selected_account_type_name = _normalize_account_type_name(
        _json_obj(entities).get("account_type_name") or vehicle_type_detect.get("config_type_name")
    )
    missing = _missing_requirements_for_quote_flow(
        normalized_data,
        images_by_slot,
        platform_code=case.platform_code or "",
        account_type_name=selected_account_type_name,
    )
    if normalized_data != _json_obj(case.normalized_data) or missing != _json_list(case.missing_requirements):
        case.normalized_data = normalized_data
        case.draft_order_data = normalized_data
        case.missing_requirements = missing
        if case.status not in {
            CASE_STATUS_QUOTED,
            CASE_STATUS_WAITING_SMS,
            CASE_STATUS_WAITING_DUPLICATE_CONFIRM,
        }:
            case.status = CASE_STATUS_READY if not missing else CASE_STATUS_COLLECTING
        case.updated_at = _now()
        await db.flush()
    platform_account = await _select_logged_quote_platform_account(
        db,
        owner_user_id=owner_user_id,
        platform_code=case.platform_code or "",
        account_type_name=selected_account_type_name,
    )
    platform_has_enabled_account = False
    if platform_account is None:
        platform_has_enabled_account = await _has_enabled_quote_platform_account(
            db,
            owner_user_id=owner_user_id,
            platform_code=case.platform_code or "",
            account_type_name=None,
        )
    account_pool_unavailable = bool(platform_account is None and platform_has_enabled_account)
    payload = _case_payload(
        case=case,
        normalized_data=normalized_data,
        images_by_slot=images_by_slot,
        missing=missing,
        task=waiting_task,
        platform_account=platform_account,
    )
    payload["quote_account_selection"] = {
        "selected_account_type_name": selected_account_type_name,
        "platform_has_enabled_account": bool(platform_has_enabled_account),
        "account_pool_unavailable": bool(account_pool_unavailable),
        "fallback_to_same_platform_account": bool(platform_account is not None and selected_account_type_name),
    }

    lines = ["当前报价材料状态："]
    platform_text = _to_str(case.platform_name or case.platform_code).strip()
    if platform_text:
        lines.append(f"- 平台：{platform_text}")
    ready_slots = payload.get("ready_slots") or {}
    if ready_slots:
        lines.append("- 已归位图片：" + "、".join(f"{slot_label(k)}{v}张" for k, v in ready_slots.items()))
    if waiting_task:
        lines.append("- 当前流程：正在等待短信验证码，请直接在聊天框输入收到的 4-8 位验证码。")
    elif missing:
        lines.append("- 仍缺少：" + "、".join(_missing_item_text(item) for item in missing))
    elif case.status == CASE_STATUS_QUOTED:
        lines.append(f"- 已完成报价；如需重新报价，请输入“{case.platform_name or case.platform_code or '平台'}重新报价”。")
    else:
        lines.append("- 必填项已齐，可以继续报价流程。")
    if platform_account:
        account_payload = _credential_public_payload(platform_account) or {}
        account_type_label = _normalize_account_type_name(platform_account.account_type_name) or "未标记"
        lines.append(f"- 平台账号资料：已匹配{account_type_label}账号，已记住 {account_payload.get('login_phone_mask') or '登录资料'}")
    elif case.platform_code:
        platform_label = case.platform_name or case.platform_code
        if account_pool_unavailable:
            lines.append(
                "- 平台账号："
                + _quote_account_action_text(
                    _ctx_role_name(ctx),
                    f"{platform_label}同平台账号暂不可用，请确认账号已登录、未等待验证码且额度未用完。",
                    f"{platform_label}平台账号暂不可用，请联系管理员处理。",
                )
            )
        else:
            type_hint = f"（类型：{selected_account_type_name}）" if selected_account_type_name else ""
            lines.append(
                "- 平台账号："
                + _quote_account_action_text(
                    _ctx_role_name(ctx),
                    f"{platform_label}{type_hint}暂无可用账号，请在右上角“平台账号管理”新增、启用或调整额度。",
                    f"{platform_label}{type_hint}暂无可用账号，请联系管理员新增、启用或调整额度。",
                )
            )

    return "\n".join(lines), {
        "status": "success",
        "intent": "quote_material_status",
        "trace_id": _new_trace_id(),
        "data": _mk_data(
            result_status=RESULT_NOT_READY if (waiting_task or missing) else RESULT_SUCCESS,
            message="正在等待短信验证码" if waiting_task else "已返回报价草稿材料状态",
            entities={**(entities or {}), "quote_case_id": case.id, "order_id": case.order_id},
            payload=payload,
        ),
        "actions": (
            [_mk_action("输入短信验证码"), _mk_action("查看当前材料状态")]
            if waiting_task
            else [_mk_action(f"{case.platform_name or '太平洋'}报价"), _mk_action("查看当前材料状态")]
        ),
    }
