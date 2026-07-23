# encoding: utf-8
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urljoin, urlsplit

import requests
from requests import Response, Session
from requests.cookies import RequestsCookieJar, create_cookie

from env_loader import load_backend_env
from app.core.config import settings
from app.services.quote_platforms.base import PlatformAccountContext
from app.services.quote_platforms.session_models import AccountSessionSnapshot, CookieRecord, JwtClaims, iso_now

PLATFORM_CODE = "PICC"
PROTOCOL_PLATFORM_CODE = "picc"
KEEPALIVE_PATH = "/khyx/newFront/um/umtmenu/prepareAll.do"
KEEPALIVE_PARAMS = {"isnewFront": "1"}

load_backend_env()


class PiccProtocolError(RuntimeError):
    pass


class PiccConfigError(PiccProtocolError):
    pass


class PiccRequestError(PiccProtocolError):
    pass


class PiccTransientGatewayError(PiccRequestError):
    pass


class PiccSessionExpiredError(PiccProtocolError):
    pass


def _env(name: str, default: Any = "") -> Any:
    value = os.getenv(name)
    if value is not None:
        return value
    return getattr(settings, name, default)


def _env_first(names: tuple[str, ...], default: Any = "") -> Any:
    for name in names:
        value = _env(name, None)
        if value not in (None, ""):
            return value
    return default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, default))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, default))
    except Exception:
        return default


@dataclass(frozen=True)
class PiccProtocolConfig:
    base_url: str
    province: str
    verify_ssl: bool
    connect_timeout: float
    read_timeout: float
    keepalive_seconds: int
    jwt_refresh_before_seconds: int
    user_agent: str
    captcha_api_url: str
    captcha_username: str
    captcha_password: str
    captcha_type_id: int
    captcha_timeout: float
    captcha_max_rounds: int

    @classmethod
    def from_env(cls) -> "PiccProtocolConfig":
        return cls(
            base_url=str(_env("PICC_BASE_URL", "https://jiangx.yxgl-picc.cn:41001")).rstrip("/"),
            province=str(_env("PICC_PROVINCE", "jiangx")).strip() or "jiangx",
            verify_ssl=_env_bool("PICC_VERIFY_SSL", True),
            connect_timeout=_env_float("PICC_CONNECT_TIMEOUT", 10.0),
            read_timeout=_env_float("PICC_READ_TIMEOUT", 40.0),
            keepalive_seconds=_env_int("PICC_KEEPALIVE_SECONDS", 300),
            jwt_refresh_before_seconds=_env_int("PICC_JWT_REFRESH_BEFORE_SECONDS", 25 * 60),
            user_agent=str(
                _env(
                    "PICC_USER_AGENT",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
                )
            ),
            captcha_api_url=str(_env("PICC_CAPTCHA_API_URL", "http://api.ttshitu.com/predict")).strip(),
            captcha_username=str(_env_first(("PICC_CAPTCHA_USERNAME", "API_PARSE_CAPTCHA_UNAME"), "") or "").strip(),
            captcha_password=str(_env_first(("PICC_CAPTCHA_PASSWORD", "API_PARSE_CAPTCHA_PWD"), "") or "").strip(),
            captcha_type_id=_env_int("PICC_CAPTCHA_TYPE_ID", 34),
            captcha_timeout=_env_float("PICC_CAPTCHA_TIMEOUT", 10.0),
            captcha_max_rounds=max(1, _env_int("PICC_CAPTCHA_MAX_ROUNDS", 5)),
        )

    @property
    def timeout(self) -> tuple[float, float]:
        return self.connect_timeout, self.read_timeout

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc

    @property
    def login_referer(self) -> str:
        return f"{self.base_url}/khyxui/login?province={self.province}&isZbdsfsChecked=0"


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    try:
        payload = str(token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        raise PiccRequestError("PICC 返回的 USER_TOKEN 不是有效 JWT") from exc


def jwt_claims_from_token(token: str) -> JwtClaims:
    payload = decode_jwt_payload(token)
    return JwtClaims(
        issued_at=int(payload.get("iat")) if payload.get("iat") else None,
        expires_at=int(payload.get("exp")) if payload.get("exp") else None,
        session_id=str(payload.get("sessionId") or payload.get("session_id") or "") or None,
        user_code=str(payload.get("userCode") or payload.get("user_code") or "") or None,
        company_id=str(payload.get("comId") or payload.get("company_id") or "") or None,
        raw=payload,
    )


def _valid_token_sort_key(token: str) -> tuple[int, int]:
    try:
        payload = decode_jwt_payload(token)
        return int(payload.get("iat") or 0), int(payload.get("exp") or 0)
    except Exception:
        return (0, 0)


def _best_valid_token(tokens: list[str]) -> str:
    valid = [str(token or "").strip() for token in tokens if _valid_token_sort_key(str(token or "").strip()) != (0, 0)]
    if not valid:
        return ""
    valid.sort(key=_valid_token_sort_key, reverse=True)
    return valid[0]


def get_cookie_value(jar: RequestsCookieJar, name: str, preferred_host: str = "") -> str:
    candidates: list[tuple[int, str]] = []
    host = preferred_host.split(":", 1)[0].lower()
    for cookie in jar:
        if cookie.name != name:
            continue
        domain = (cookie.domain or "").lstrip(".").lower()
        score = 1 if host and (domain == host or host.endswith("." + domain)) else 0
        candidates.append((score, cookie.value))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def set_cookie_value(jar: RequestsCookieJar, name: str, value: str, host: str) -> None:
    domain = host.split(":", 1)[0]
    for cookie in list(jar):
        if cookie.name == name:
            try:
                jar.clear(cookie.domain, cookie.path, cookie.name)
            except KeyError:
                pass
    jar.set_cookie(create_cookie(name=name, value=value, domain=domain, path="/"))


def cookie_records_from_session(session: Session) -> list[CookieRecord]:
    records: list[CookieRecord] = []
    for cookie in session.cookies:
        records.append(
            CookieRecord(
                name=cookie.name,
                value=cookie.value,
                domain=cookie.domain or "",
                path=cookie.path or "/",
                expires=float(cookie.expires) if cookie.expires else None,
                http_only=bool(cookie.has_nonstandard_attr("HttpOnly")),
                secure=bool(cookie.secure),
                same_site=cookie.get_nonstandard_attr("SameSite") or None,
            )
        )
    return records


def apply_cookie_records(session: Session, cookies: list[CookieRecord]) -> None:
    for cookie in cookies or []:
        session.cookies.set_cookie(
            create_cookie(
                name=cookie.name,
                value=cookie.value,
                domain=cookie.domain or "",
                path=cookie.path or "/",
                expires=int(cookie.expires) if cookie.expires else None,
                secure=bool(cookie.secure),
            )
        )


def protocol_account_id(ctx: PlatformAccountContext) -> str:
    import hashlib

    username = str(ctx.account_username or "").strip()
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:16] if username else str(ctx.account_id)
    return f"{PROTOCOL_PLATFORM_CODE}_{digest}"


class PiccProtocolClient:
    def __init__(
        self,
        ctx: PlatformAccountContext,
        *,
        config: Optional[PiccProtocolConfig] = None,
        snapshot: Optional[AccountSessionSnapshot] = None,
        session: Optional[Session] = None,
    ) -> None:
        self.ctx = ctx
        self.config = config or PiccProtocolConfig.from_env()
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            }
        )
        self.snapshot = snapshot
        if snapshot is not None:
            apply_cookie_records(self.session, snapshot.cookies)
            token = snapshot.user_token or snapshot.authorization
            if token:
                self.session.headers["Authorization"] = token

    def url(self, path: str) -> str:
        return urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))

    def _json(self, response: Response) -> Any:
        try:
            return response.json()
        except Exception as exc:
            if response.status_code in {502, 503, 504}:
                raise PiccTransientGatewayError(f"PICC 平台网关临时异常：HTTP={response.status_code}") from exc
            raise PiccRequestError(f"PICC 接口返回非 JSON：HTTP={response.status_code}，body={response.text[:300]}") from exc

    def install_token(self, token: str) -> bool:
        token = str(token or "").strip()
        if not token:
            return False
        new_claims = jwt_claims_from_token(token)
        old_iat = int((self.snapshot.jwt.issued_at if self.snapshot else 0) or 0)
        new_iat = int(new_claims.issued_at or 0)
        old_token = self.snapshot.user_token if self.snapshot else ""
        if old_token and old_iat and new_iat and new_iat < old_iat:
            return False
        self.session.headers["Authorization"] = token
        set_cookie_value(self.session.cookies, "USER_TOKEN", token, self.config.host)
        if self.snapshot is not None:
            self.snapshot.user_token = token
            self.snapshot.authorization = token
            self.snapshot.jwt = new_claims
        return token != old_token

    def capture_snapshot(
        self,
        *,
        status: str,
        runtime_meta: Optional[Dict[str, Any]] = None,
        clear_pending_login: bool = False,
    ) -> AccountSessionSnapshot:
        previous = self.snapshot
        user_token = get_cookie_value(self.session.cookies, "USER_TOKEN", self.config.host)
        jsession_id = get_cookie_value(self.session.cookies, "JSESSIONID", self.config.host)
        if user_token:
            try:
                self.install_token(user_token)
            except PiccProtocolError:
                pass
        meta = dict(previous.runtime_meta if previous else {})
        if clear_pending_login:
            meta.pop("picc_pending_login", None)
        meta.update(
            {
                "platform_runtime": "picc_protocol",
                "protocol_account_id": protocol_account_id(self.ctx),
                "base_url": self.config.base_url,
                "province": self.config.province,
            }
        )
        meta.update(runtime_meta or {})
        snapshot = AccountSessionSnapshot(
            platform_code=PLATFORM_CODE,
            account_id=int(self.ctx.account_id),
            owner_user_id=int(self.ctx.owner_user_id or 0),
            session_version=int(previous.session_version if previous else 0),
            session_generation=previous.session_generation if previous else "",
            status=status,
            cookies=cookie_records_from_session(self.session),
            user_token=(self.snapshot.user_token if self.snapshot else "") or user_token,
            authorization=(self.snapshot.authorization if self.snapshot else "") or user_token,
            jsession_id=jsession_id or (previous.jsession_id if previous else ""),
            team="0",
            jwt=(self.snapshot.jwt if self.snapshot else JwtClaims()),
            user_agent=self.config.user_agent,
            browser_profile_path=previous.browser_profile_path if previous else None,
            runtime_meta=meta,
            last_login_at=previous.last_login_at if previous else None,
            last_authenticated_at=previous.last_authenticated_at if previous else None,
            last_business_at=previous.last_business_at if previous else None,
            last_keepalive_at=previous.last_keepalive_at if previous else None,
            last_refresh_at=previous.last_refresh_at if previous else None,
            last_validation_at=previous.last_validation_at if previous else None,
            next_keepalive_at=previous.next_keepalive_at if previous else None,
            last_error_code=previous.last_error_code if previous else None,
            last_error_message=previous.last_error_message if previous else None,
        )
        now = iso_now()
        if status == "authenticated":
            snapshot.last_authenticated_at = now
        self.snapshot = snapshot
        return snapshot

    def request_json(
        self,
        method: str,
        path: str,
        *,
        purpose: str,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        form_body: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        token = self.snapshot.user_token if self.snapshot else ""
        jsession_id = self.snapshot.jsession_id if self.snapshot else ""
        if not token or not jsession_id:
            raise PiccRequestError("PICC 当前账号缺少完整会话，请重新登录")
        jwt_expires_at = int((self.snapshot.jwt.expires_at if self.snapshot else 0) or 0)
        if jwt_expires_at > 0 and jwt_expires_at <= int(time.time()):
            raise PiccSessionExpiredError("PICC 登录令牌已过期，请重新登录")

        request_headers = dict(headers or {})
        request_headers["Authorization"] = token
        kwargs: Dict[str, Any] = {
            "params": dict(params or {}),
            "headers": request_headers,
            "timeout": self.config.timeout,
            "verify": self.config.verify_ssl,
        }
        if json_body is not None:
            kwargs["json"] = dict(json_body)
        elif form_body is not None:
            kwargs["data"] = dict(form_body)

        method_upper = method.upper()
        attempts = 3 if method_upper == "GET" else 1
        response: Optional[Response] = None
        last_exc: Optional[PiccTransientGatewayError] = None
        for attempt in range(1, attempts + 1):
            response = self.session.request(method_upper, self.url(path), **kwargs)
            try:
                data = self._json(response)
                break
            except PiccTransientGatewayError as exc:
                last_exc = exc
                if attempt >= attempts:
                    raise
                time.sleep(0.6 * attempt)
        else:  # pragma: no cover - defensive; loop always breaks or raises.
            raise last_exc or PiccRequestError("PICC 接口响应异常")
        if response is None:  # pragma: no cover - defensive.
            raise PiccRequestError("PICC 接口未返回响应")
        if response.status_code != 200:
            raise PiccRequestError(f"PICC 接口 HTTP={response.status_code}，返回={data}")
        if isinstance(data, Mapping) and int(data.get("status", -999)) == 16:
            raise PiccSessionExpiredError(str(data.get("statusText") or "PICC 登录已过期"))

        candidates = [
            response.headers.get("new-jwt", ""),
            get_cookie_value(self.session.cookies, "USER_TOKEN", self.config.host),
        ]
        best_token = _best_valid_token([token for token in candidates if token])
        if best_token:
            self.install_token(best_token)

        now = iso_now()
        if self.snapshot is not None:
            self.snapshot.last_authenticated_at = now
            if purpose == "business":
                self.snapshot.last_business_at = now
            elif purpose == "keepalive":
                self.snapshot.last_keepalive_at = now
            elif purpose == "validate":
                self.snapshot.last_validation_at = now
        return data

    def validate(self) -> Any:
        return self.request_json(
            "GET",
            KEEPALIVE_PATH,
            purpose="validate",
            params=KEEPALIVE_PARAMS,
            headers={"Referer": f"{self.config.base_url}/khyxui/homePage"},
        )

    def keepalive(self) -> Any:
        return self.request_json(
            "GET",
            KEEPALIVE_PATH,
            purpose="keepalive",
            params=KEEPALIVE_PARAMS,
            headers={"Referer": f"{self.config.base_url}/khyxui/homePage"},
        )


def snapshot_from_context(ctx: PlatformAccountContext) -> Optional[AccountSessionSnapshot]:
    raw = ctx.payload.get("session_snapshot") if isinstance(ctx.payload, dict) else None
    if not isinstance(raw, dict):
        return None
    return AccountSessionSnapshot.from_dict(raw)


def success_data(client: PiccProtocolClient, *, status: str = "authenticated", extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    snapshot = client.capture_snapshot(status=status, runtime_meta={"last_protocol_at": int(time.time())})
    return {
        **(extra or {}),
        "session_snapshot": snapshot.to_dict(),
        "jsession_id": snapshot.jsession_id,
        "user_token": snapshot.user_token,
        "jwt": snapshot.jwt.raw or {
            "issued_at": snapshot.jwt.issued_at,
            "expires_at": snapshot.jwt.expires_at,
        },
    }
