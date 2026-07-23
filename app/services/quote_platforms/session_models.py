# encoding: utf-8
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

TZ_BJ = timezone(timedelta(hours=8))


def now_db() -> datetime:
    return datetime.now(TZ_BJ).replace(tzinfo=None)


def now_ts() -> int:
    return int(datetime.now(TZ_BJ).timestamp())


@dataclass
class CookieRecord:
    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expires: Optional[float] = None
    http_only: bool = False
    secure: bool = True
    same_site: Optional[str] = None


@dataclass
class JwtClaims:
    issued_at: Optional[int] = None
    expires_at: Optional[int] = None
    session_id: Optional[str] = None
    user_code: Optional[str] = None
    company_id: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountSessionSnapshot:
    platform_code: str
    account_id: int
    owner_user_id: int = 0
    session_version: int = 1
    session_generation: str = field(default_factory=lambda: uuid4().hex)
    status: str = "offline"

    cookies: List[CookieRecord] = field(default_factory=list)
    user_token: str = ""
    authorization: str = ""
    jsession_id: str = ""
    team: str = "0"
    jwt: JwtClaims = field(default_factory=JwtClaims)

    user_agent: str = ""
    browser_profile_path: Optional[str] = None
    runtime_meta: Dict[str, Any] = field(default_factory=dict)

    last_login_at: Optional[str] = None
    last_authenticated_at: Optional[str] = None
    last_business_at: Optional[str] = None
    last_keepalive_at: Optional[str] = None
    last_refresh_at: Optional[str] = None
    last_validation_at: Optional[str] = None
    next_keepalive_at: Optional[str] = None

    last_error_code: Optional[str] = None
    last_error_message: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AccountSessionSnapshot":
        data = dict(raw or {})
        if not data.get("platform_code"):
            account_id_text = str(data.get("account_id") or "").strip().lower()
            if account_id_text.startswith("picc_"):
                data["platform_code"] = "PICC"
            else:
                data["platform_code"] = "STUB"
        status = str(data.get("status") or "").strip()
        status_map = {
            "DISABLED": "disabled",
            "OFFLINE": "offline",
            "LOGGING_IN": "logging_in",
            "LOGIN_STARTING": "logging_in",
            "CAPTCHA_VERIFYING": "logging_in",
            "WAITING_SECURITY_CODE": "waiting_challenge",
            "WAITING_CHALLENGE": "waiting_challenge",
            "ONLINE": "authenticated",
            "AUTHENTICATED": "authenticated",
            "DEGRADED": "degraded",
            "EXPIRED": "expired",
            "ERROR": "degraded",
            "LOGIN_FAILED": "login_failed",
        }
        if status:
            data["status"] = status_map.get(status.upper(), status.lower())
        cookies_raw = data.get("cookies") or []
        if isinstance(cookies_raw, dict):
            cookies_raw = [
                {"name": str(name), "value": str(value), "path": "/"}
                for name, value in cookies_raw.items()
                if str(name)
            ]
        data["cookies"] = [
            item if isinstance(item, CookieRecord) else CookieRecord(**dict(item or {}))
            for item in cookies_raw
            if isinstance(item, (dict, CookieRecord))
        ]
        jwt_raw = data.get("jwt")
        if not jwt_raw and (data.get("jwt_issued_at") or data.get("jwt_expires_at")):
            jwt_raw = {
                "issued_at": data.get("jwt_issued_at"),
                "expires_at": data.get("jwt_expires_at"),
            }
        data["jwt"] = jwt_claims_from_mapping(jwt_raw)
        for key in (
            "last_login_at",
            "last_authenticated_at",
            "last_business_at",
            "last_keepalive_at",
            "last_refresh_at",
            "last_validation_at",
        ):
            value = data.get(key)
            if isinstance(value, (int, float)):
                data[key] = datetime.fromtimestamp(float(value), TZ_BJ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        if data.get("last_error") and not data.get("last_error_message"):
            data["last_error_message"] = str(data.get("last_error"))
        allowed = {item.name for item in dataclass_fields(cls)}
        data = {key: value for key, value in data.items() if key in allowed}
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def safe_summary(self) -> Dict[str, Any]:
        return {
            "platform_code": self.platform_code,
            "account_id": self.account_id,
            "owner_user_id": self.owner_user_id,
            "status": self.status,
            "session_version": self.session_version,
            "session_generation": self.session_generation,
            "jwt_issued_at": self.jwt.issued_at,
            "jwt_expires_at": self.jwt.expires_at,
            "last_login_at": self.last_login_at,
            "last_authenticated_at": self.last_authenticated_at,
            "last_business_at": self.last_business_at,
            "last_keepalive_at": self.last_keepalive_at,
            "last_refresh_at": self.last_refresh_at,
            "last_validation_at": self.last_validation_at,
            "next_keepalive_at": self.next_keepalive_at,
            "last_error_code": self.last_error_code,
            "last_error_message": self.last_error_message,
            "has_cookie": bool(self.cookies),
            "has_token": bool(self.user_token or self.authorization or self.jsession_id),
        }


def iso_now() -> str:
    return now_db().strftime("%Y-%m-%d %H:%M:%S")


def jwt_claims_from_mapping(raw: Any) -> JwtClaims:
    """Normalize platform JWT payloads while preserving unknown vendor fields."""
    if isinstance(raw, JwtClaims):
        return raw
    data = dict(raw or {}) if isinstance(raw, dict) else {}
    raw_payload = data.get("raw") if isinstance(data.get("raw"), dict) else data

    def int_or_none(value: Any) -> Optional[int]:
        try:
            return int(value) if value not in (None, "") else None
        except Exception:
            return None

    return JwtClaims(
        issued_at=int_or_none(data.get("issued_at") or data.get("iat")),
        expires_at=int_or_none(data.get("expires_at") or data.get("exp")),
        session_id=str(data.get("session_id") or data.get("sessionId") or raw_payload.get("sessionId") or "") or None,
        user_code=str(data.get("user_code") or data.get("userCode") or raw_payload.get("userCode") or "") or None,
        company_id=str(data.get("company_id") or data.get("comId") or raw_payload.get("comId") or "") or None,
        raw=raw_payload,
    )
