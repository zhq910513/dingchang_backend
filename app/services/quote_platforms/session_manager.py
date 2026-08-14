# encoding: utf-8
from __future__ import annotations

import asyncio
import hashlib
import os
import random
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Awaitable, Dict, Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import db as db_core
from app.core.config import settings
from app.models.quote_assistant import QuotePlatformAccountProfile, QuotePlatformAccountSessionState
from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.quote_platforms.browser_runtime import BrowserRuntimeError, BrowserLease, browser_runtime_manager
from app.services.quote_platforms.registry import get_quote_platform_adapter
from app.services.quote_platforms.session_models import (
    AccountSessionSnapshot,
    CookieRecord,
    iso_now,
    jwt_claims_from_mapping,
    now_db,
)
from app.services.quote_secret_box import decrypt_json, encrypt_json

SESSION_STATUS_OFFLINE = "offline"
SESSION_STATUS_LOGGING_IN = "logging_in"
SESSION_STATUS_WAITING_CHALLENGE = "waiting_challenge"
SESSION_STATUS_AUTHENTICATED = "authenticated"
SESSION_STATUS_EXPIRED = "expired"
SESSION_STATUS_DISABLED = "disabled"
SESSION_STATUS_LOGIN_FAILED = "login_failed"
SESSION_STATUS_DEGRADED = "degraded"

AUTH_EXPIRED_STATUSES = {"expired", "session_expired", "not_authenticated", "unauthorized", "status_16"}
AUTH_USABLE_SESSION_STATUSES = {SESSION_STATUS_AUTHENTICATED, SESSION_STATUS_DEGRADED}
PRESERVED_CHALLENGE_SESSION_META_KEY = "previous_usable_session_before_challenge"
QUOTE_PLATFORM_SYNTHETIC_SESSION = os.getenv("QUOTE_PLATFORM_SYNTHETIC_SESSION", "0") == "1"
QUOTE_PLATFORM_REDIS_SESSION_MIRROR = os.getenv("QUOTE_PLATFORM_REDIS_SESSION_MIRROR", "1") == "1"
QUOTE_PLATFORM_REQUIRE_REDIS_LOCK_FOR_PICC = os.getenv("QUOTE_PLATFORM_REQUIRE_REDIS_LOCK_FOR_PICC", "1") == "1"
QUOTE_PLATFORM_LOGIN_LOCK_SECONDS = int(os.getenv("QUOTE_PLATFORM_LOGIN_LOCK_SECONDS", "300") or "300")
QUOTE_PLATFORM_WORKER_LOCK_SECONDS = int(os.getenv("QUOTE_PLATFORM_WORKER_LOCK_SECONDS", "90") or "90")
QUOTE_PLATFORM_KEEPALIVE_CALL_TIMEOUT_SECONDS = int(os.getenv("QUOTE_PLATFORM_KEEPALIVE_CALL_TIMEOUT_SECONDS", "75") or "75")
QUOTE_PLATFORM_CHECK_QUOTA_CALL_TIMEOUT_SECONDS = int(os.getenv("QUOTE_PLATFORM_CHECK_QUOTA_CALL_TIMEOUT_SECONDS", "90") or "90")

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_RENEW_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


def _to_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except Exception:
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


def _json_obj(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _snapshot_aad(account: QuotePlatformAccountProfile) -> str:
    return (
        f"quote_platform_session:{int(account.owner_user_id or 0)}:"
        f"{_to_str(account.platform_code).strip().upper()}:{int(account.id or 0)}"
    )


def _platform_code(account: QuotePlatformAccountProfile | str) -> str:
    if isinstance(account, str):
        return _to_str(account).strip().upper()
    return _to_str(getattr(account, "platform_code", "")).strip().upper()


def _platform_runtime_account_id(account: QuotePlatformAccountProfile) -> str:
    """Return the platform runtime account id used by auto_business Redis keys."""
    code = _platform_code(account)
    username = _to_str(getattr(account, "account_username", "")).strip()
    if code == "PICC" and username:
        digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]
        return f"picc_{digest}"
    return str(int(getattr(account, "id", 0) or 0))


def _redis_session_key(account: QuotePlatformAccountProfile) -> str:
    return f"account_session:{_platform_code(account).lower()}:{_platform_runtime_account_id(account)}"


def _redis_snapshot_aad(account: QuotePlatformAccountProfile) -> str:
    return (
        f"quote_platform_redis_session:{int(account.owner_user_id or 0)}:"
        f"{_platform_code(account)}:{_platform_runtime_account_id(account)}"
    )


def _lease_key(account: QuotePlatformAccountProfile, *, purpose: str) -> str:
    prefix = "login_lock" if purpose in {"login", "challenge"} else "worker_owner"
    return f"{prefix}:{_platform_code(account).lower()}:{_platform_runtime_account_id(account)}"


def _lock_requires_redis(key: str) -> bool:
    return QUOTE_PLATFORM_REQUIRE_REDIS_LOCK_FOR_PICC and ":picc:" in f":{_to_str(key).lower()}:"


def _keepalive_interval_seconds(platform_code: str) -> int:
    code = _platform_code(platform_code)
    if code == "PICC":
        return max(30, int(getattr(settings, "PICC_KEEPALIVE_SECONDS", 300) or 300))
    return max(60, int(os.getenv("QUOTE_PLATFORM_KEEPALIVE_SECONDS", "300") or "300"))


def _next_keepalive_time(platform_code: str):
    interval = _keepalive_interval_seconds(platform_code)
    if _platform_code(platform_code) == "PICC" and interval >= 300:
        jitter = random.randint(0, 120)
    else:
        jitter = random.randint(0, max(1, min(60, interval // 5)))
    return now_db() + timedelta(seconds=interval + jitter)


def _schedule_next_keepalive(snapshot: AccountSessionSnapshot, platform_code: str) -> None:
    next_at = _next_keepalive_time(platform_code)
    snapshot.next_keepalive_at = next_at.strftime("%Y-%m-%d %H:%M:%S")
    snapshot.runtime_meta = {
        **_json_obj(snapshot.runtime_meta),
        "next_keepalive_at": snapshot.next_keepalive_at,
        "keepalive_interval_seconds": _keepalive_interval_seconds(platform_code),
    }


def _safe_result_data(result: PlatformRuntimeResult, extra: Dict[str, Any]) -> Dict[str, Any]:
    blocked = {
        "cookies",
        "cookie",
        "authorization",
        "Authorization",
        "user_token",
        "USER_TOKEN",
        "jsession_id",
        "JSESSIONID",
        "jwt",
        "session_snapshot",
    }
    data = {str(k): v for k, v in _json_obj(result.data).items() if str(k) not in blocked}
    data.update(extra)
    return data


def _dt_score(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        return int(value.replace(tzinfo=None).timestamp())
    text = _to_str(value).strip()
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None).timestamp())
    except Exception:
        return 0


def _snapshot_score(snapshot: Optional[AccountSessionSnapshot]) -> tuple[int, int, int, int]:
    if snapshot is None:
        return (0, 0, 0, 0)
    jwt = getattr(snapshot, "jwt", None)
    active_score = max(
        _dt_score(getattr(snapshot, "last_authenticated_at", None)),
        _dt_score(getattr(snapshot, "last_business_at", None)),
        _dt_score(getattr(snapshot, "last_keepalive_at", None)),
        _dt_score(getattr(snapshot, "last_validation_at", None)),
        _dt_score(getattr(snapshot, "last_login_at", None)),
    )
    return (
        int(getattr(snapshot, "session_version", 0) or 0),
        int(getattr(jwt, "issued_at", 0) or 0),
        int(getattr(jwt, "expires_at", 0) or 0),
        active_score,
    )


def _lock_lost_result(exc: Exception) -> PlatformRuntimeResult:
    return PlatformRuntimeResult(
        status="conflict",
        message=str(exc) or "平台账号运行锁已丢失，本次结果未落库，请重试",
        data={"error_code": "REDIS_LOCK_LOST"},
    )


def _session_conflict_result() -> PlatformRuntimeResult:
    return PlatformRuntimeResult(
        status="conflict",
        message="账号会话已被其他请求更新，请刷新后重试",
        data={"error_code": "SESSION_VERSION_CONFLICT"},
    )


def _is_usable_session_snapshot(snapshot: Optional[AccountSessionSnapshot]) -> bool:
    if snapshot is None:
        return False
    return _to_str(getattr(snapshot, "status", "")).strip().lower() in AUTH_USABLE_SESSION_STATUSES


def _snapshot_payload_without_preserved_challenge(snapshot: AccountSessionSnapshot) -> Dict[str, Any]:
    payload = snapshot.to_dict()
    runtime_meta = _json_obj(payload.get("runtime_meta"))
    runtime_meta.pop(PRESERVED_CHALLENGE_SESSION_META_KEY, None)
    payload["runtime_meta"] = runtime_meta
    return payload


def _attach_previous_usable_session_to_challenge(
    *,
    challenge_snapshot: AccountSessionSnapshot,
    previous: Optional[AccountSessionSnapshot],
) -> None:
    if not _is_usable_session_snapshot(previous):
        return
    challenge_snapshot.runtime_meta = {
        **_json_obj(challenge_snapshot.runtime_meta),
        PRESERVED_CHALLENGE_SESSION_META_KEY: _snapshot_payload_without_preserved_challenge(previous),
        "previous_usable_session_preserved_at": iso_now(),
    }


def _load_preserved_usable_session_from_challenge(
    waiting_snapshot: Optional[AccountSessionSnapshot],
) -> Optional[AccountSessionSnapshot]:
    if waiting_snapshot is None:
        return None
    raw = _json_obj(_json_obj(waiting_snapshot.runtime_meta).get(PRESERVED_CHALLENGE_SESSION_META_KEY))
    if not raw:
        return None
    try:
        preserved = AccountSessionSnapshot.from_dict(raw)
    except Exception:
        return None
    return preserved if _is_usable_session_snapshot(preserved) else None


def _restore_preserved_session_after_challenge_interruption(
    *,
    ctx: PlatformAccountContext,
    waiting_snapshot: AccountSessionSnapshot,
    result: PlatformRuntimeResult,
    reason: str,
) -> Optional[AccountSessionSnapshot]:
    preserved = _load_preserved_usable_session_from_challenge(waiting_snapshot)
    if preserved is None:
        return None
    preserved.platform_code = _to_str(ctx.platform_code).strip().upper()
    preserved.account_id = int(ctx.account_id)
    preserved.owner_user_id = int(ctx.owner_user_id or preserved.owner_user_id or 0)
    preserved.status = SESSION_STATUS_DEGRADED
    preserved.session_version = int(waiting_snapshot.session_version or preserved.session_version or 0) + 1
    if not preserved.session_generation:
        preserved.session_generation = uuid4().hex

    data = _json_obj(result.data)
    status = _to_str(result.status).strip().lower()
    detail = _to_str(result.message).strip() or "新登录流程未完成"
    preserved.last_error_code = _to_str(data.get("error_code") or status or "LOGIN_CHALLENGE_INTERRUPTED").upper()
    preserved.last_error_message = f"{reason}，已恢复原有可用会话：{detail}"
    preserved.runtime_meta = {
        **_json_obj(preserved.runtime_meta),
        "challenge_interrupted_restored": True,
        "challenge_interrupted_reason": reason,
        "challenge_interrupted_status": result.status,
        "challenge_interrupted_message": result.message,
        "challenge_interrupted_at": iso_now(),
    }
    preserved.runtime_meta.pop(PRESERVED_CHALLENGE_SESSION_META_KEY, None)
    _schedule_next_keepalive(preserved, ctx.platform_code)
    return preserved


def _preserve_previous_session_after_login_failure(
    *,
    ctx: PlatformAccountContext,
    previous: AccountSessionSnapshot,
    running: AccountSessionSnapshot,
    result: PlatformRuntimeResult,
) -> AccountSessionSnapshot:
    snapshot = AccountSessionSnapshot.from_dict(previous.to_dict())
    snapshot.status = SESSION_STATUS_DEGRADED
    snapshot.session_version = int(running.session_version or previous.session_version or 0) + 1
    if not snapshot.session_generation:
        snapshot.session_generation = uuid4().hex
    data = _json_obj(result.data)
    snapshot.last_error_code = _to_str(data.get("error_code") or result.status or "LOGIN_ATTEMPT_FAILED").upper()
    snapshot.last_error_message = f"新登录失败，已保留原有可用会话：{result.message or '平台登录失败'}"
    snapshot.runtime_meta = {
        **_json_obj(snapshot.runtime_meta),
        "login_attempt_failed_preserved": True,
        "login_attempt_status": result.status,
        "login_attempt_message": result.message,
        "login_attempt_at": iso_now(),
    }
    _schedule_next_keepalive(snapshot, ctx.platform_code)
    return snapshot


def _runtime_timeout_seconds(action: str) -> int:
    if action == "keepalive":
        return max(5, QUOTE_PLATFORM_KEEPALIVE_CALL_TIMEOUT_SECONDS)
    if action == "check_quota":
        return max(5, QUOTE_PLATFORM_CHECK_QUOTA_CALL_TIMEOUT_SECONDS)
    return 0


async def _await_runtime_result(action: str, awaitable: Awaitable[PlatformRuntimeResult]) -> PlatformRuntimeResult:
    timeout = _runtime_timeout_seconds(action)
    if timeout <= 0:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError:
        action_label = "保活" if action == "keepalive" else "额度检查"
        return PlatformRuntimeResult(
            status="timeout",
            message=f"平台账号{action_label}响应超时，已保留当前会话缓存，下次会继续复用",
            data={
                "error_code": f"{action.upper()}_TIMEOUT",
                "timeout_seconds": timeout,
                "preserve_session_cache": True,
            },
        )


class _RedisLeaseLock:
    def __init__(
        self,
        key: str,
        *,
        ttl_seconds: int = 60,
        wait_seconds: float = 30.0,
        require_redis: Optional[bool] = None,
    ):
        self.key = key
        self.ttl_seconds = ttl_seconds
        self.wait_seconds = wait_seconds
        self.require_redis = require_redis
        self.token = uuid4().hex
        self.redis = None
        self.acquired = False
        self._renew_task: Optional[asyncio.Task] = None

    def _requires_redis(self) -> bool:
        if self.require_redis is not None:
            return bool(self.require_redis)
        return _lock_requires_redis(self.key)

    async def __aenter__(self):
        self.redis = getattr(db_core, "redis", None)
        if self.redis is None:
            if self._requires_redis():
                raise TimeoutError("PICC 账号运行锁不可用：Redis 未连接，为避免账号双活已拒绝本次操作")
            return self
        deadline = asyncio.get_running_loop().time() + self.wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                ok = await self.redis.set(self.key, self.token, nx=True, ex=self.ttl_seconds)
            except Exception:
                self.redis = None
                if self._requires_redis():
                    raise TimeoutError("PICC 账号运行锁不可用：Redis 写入失败，为避免账号双活已拒绝本次操作")
                return self
            if ok:
                self.acquired = True
                self._renew_task = asyncio.create_task(self._renew_loop())
                return self
            await asyncio.sleep(0.2)
        raise TimeoutError("账号正在执行登录或报价，请稍后重试")

    async def __aexit__(self, exc_type, exc, tb):
        if self._renew_task:
            self._renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._renew_task
            self._renew_task = None
        if self.redis is not None and self.acquired:
            with suppress(Exception):
                await self.redis.eval(_RELEASE_SCRIPT, 1, self.key, self.token)
        self.acquired = False

    async def _renew_loop(self) -> None:
        interval = max(1.0, self.ttl_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if self.redis is None:
                return
            try:
                renewed = await self.redis.eval(_RENEW_SCRIPT, 1, self.key, self.token, str(self.ttl_seconds))
            except Exception:
                self.acquired = False
                return
            if not renewed:
                self.acquired = False
                return

    def assert_owned(self) -> None:
        if self.redis is not None and not self.acquired:
            raise TimeoutError("平台账号运行锁已丢失，本次平台返回结果已丢弃，请稍后重试")


class QuotePlatformSessionStore:
    async def get_account(self, db: AsyncSession, ctx: PlatformAccountContext) -> Optional[QuotePlatformAccountProfile]:
        stmt = select(QuotePlatformAccountProfile).where(
            QuotePlatformAccountProfile.id == int(ctx.account_id),
            QuotePlatformAccountProfile.platform_code == _to_str(ctx.platform_code).strip().upper(),
        )
        if int(ctx.owner_user_id or 0) > 0:
            stmt = stmt.where(QuotePlatformAccountProfile.owner_user_id == int(ctx.owner_user_id))
        return (await db.execute(stmt.limit(1))).scalars().first()

    async def load(self, account: QuotePlatformAccountProfile) -> Optional[AccountSessionSnapshot]:
        db_snapshot: Optional[AccountSessionSnapshot] = None
        token = _to_str(account.secret_payload_ciphertext).strip()
        if token:
            try:
                raw = decrypt_json(token, aad=_snapshot_aad(account))
                db_snapshot = AccountSessionSnapshot.from_dict(raw or {})
                db_snapshot.runtime_meta = {
                    **_json_obj(db_snapshot.runtime_meta),
                    "session_source": "database",
                }
            except Exception:
                pass
        redis_snapshot = await self.load_redis_mirror(account)
        if db_snapshot is None:
            return redis_snapshot
        if redis_snapshot is None:
            return db_snapshot
        return redis_snapshot if _snapshot_score(redis_snapshot) > _snapshot_score(db_snapshot) else db_snapshot

    async def load_redis_mirror(self, account: QuotePlatformAccountProfile) -> Optional[AccountSessionSnapshot]:
        if not QUOTE_PLATFORM_REDIS_SESSION_MIRROR:
            return None
        redis = getattr(db_core, "redis", None)
        if redis is None:
            return None
        try:
            token = await redis.get(_redis_session_key(account))
        except Exception:
            return None
        if not token:
            return None
        try:
            raw = decrypt_json(_to_str(token), aad=_redis_snapshot_aad(account))
            snapshot = AccountSessionSnapshot.from_dict(raw or {})
            snapshot.runtime_meta = {
                **_json_obj(snapshot.runtime_meta),
                "redis_session_restored": True,
                "redis_session_key": _redis_session_key(account),
                "session_source": "redis",
            }
            return snapshot
        except Exception:
            return None

    async def save_redis_mirror(self, account: QuotePlatformAccountProfile, snapshot: AccountSessionSnapshot) -> None:
        if not QUOTE_PLATFORM_REDIS_SESSION_MIRROR:
            return
        redis = getattr(db_core, "redis", None)
        if redis is None:
            return
        try:
            token = encrypt_json(snapshot.to_dict(), aad=_redis_snapshot_aad(account))
            if token:
                await redis.set(_redis_session_key(account), token)
        except Exception:
            return

    async def delete_redis_mirror(self, account: QuotePlatformAccountProfile) -> None:
        redis = getattr(db_core, "redis", None)
        if redis is None or not QUOTE_PLATFORM_REDIS_SESSION_MIRROR:
            return
        with suppress(Exception):
            await redis.delete(_redis_session_key(account))

    async def discard_cached_session(
        self,
        db: AsyncSession,
        account: QuotePlatformAccountProfile,
        snapshot: AccountSessionSnapshot,
    ) -> None:
        account.secret_payload_ciphertext = None
        account.credential_payload = {
            **_json_obj(account.credential_payload),
            "schema_version": 3,
            "secret_storage": "encrypted",
            "session_summary": snapshot.safe_summary(),
            "session_cache_cleared_at": iso_now(),
            "session_cache_clear_reason": snapshot.last_error_message or snapshot.status,
        }
        account.updated_at = now_db()
        await db.flush()
        await self.delete_redis_mirror(account)

    async def save(
        self,
        db: AsyncSession,
        account: QuotePlatformAccountProfile,
        snapshot: AccountSessionSnapshot,
        *,
        expected_version: Optional[int] = None,
    ) -> bool:
        state = await self.get_state(db, account_id=int(account.id))
        if expected_version is not None and state is not None:
            state_version = int(state.session_version or 0)
            expected = int(expected_version)
            if state_version > expected:
                return False
            if state_version < expected:
                redis_current = await self.load_redis_mirror(account)
                if int(getattr(redis_current, "session_version", 0) or 0) != expected:
                    return False
        elif expected_version is not None and state is None:
            redis_current = await self.load_redis_mirror(account)
            if redis_current is not None and int(redis_current.session_version or 0) != int(expected_version):
                return False

        summary = snapshot.safe_summary()
        account.secret_payload_ciphertext = encrypt_json(snapshot.to_dict(), aad=_snapshot_aad(account))
        account.credential_payload = {
            **_json_obj(account.credential_payload),
            "schema_version": 3,
            "secret_storage": "encrypted",
            "session_summary": summary,
        }
        account.updated_at = now_db()

        if state is None:
            state = QuotePlatformAccountSessionState(
                account_id=int(account.id),
                owner_user_id=int(account.owner_user_id),
                platform_code=_to_str(account.platform_code).strip().upper(),
            )
            db.add(state)
        self.apply_state(state, snapshot)
        await db.flush()
        await self.save_redis_mirror(account, snapshot)
        return True

    async def get_state(self, db: AsyncSession, *, account_id: int) -> Optional[QuotePlatformAccountSessionState]:
        return (
            await db.execute(
                select(QuotePlatformAccountSessionState)
                .where(QuotePlatformAccountSessionState.account_id == int(account_id))
                .limit(1)
            )
        ).scalars().first()

    def apply_state(self, state: QuotePlatformAccountSessionState, snapshot: AccountSessionSnapshot) -> None:
        state.owner_user_id = int(snapshot.owner_user_id or 0)
        state.platform_code = _to_str(snapshot.platform_code).strip().upper()
        state.status = snapshot.status
        state.session_version = int(snapshot.session_version or 0)
        state.session_generation = snapshot.session_generation or ""
        state.jwt_issued_at = snapshot.jwt.issued_at
        state.jwt_expires_at = snapshot.jwt.expires_at
        state.last_login_at = _parse_dt(snapshot.last_login_at)
        state.last_authenticated_at = _parse_dt(snapshot.last_authenticated_at)
        state.last_keepalive_at = _parse_dt(snapshot.last_keepalive_at)
        state.last_business_at = _parse_dt(snapshot.last_business_at)
        state.last_refresh_at = _parse_dt(snapshot.last_refresh_at)
        state.last_validation_at = _parse_dt(snapshot.last_validation_at)
        state.last_error_code = snapshot.last_error_code
        state.last_error_message = snapshot.last_error_message
        state.updated_at = now_db()

    async def clear(self, db: AsyncSession, account: QuotePlatformAccountProfile, *, status: str = SESSION_STATUS_OFFLINE) -> None:
        account.secret_payload_ciphertext = None
        account.credential_payload = {
            **_json_obj(account.credential_payload),
            "schema_version": 3,
            "secret_storage": "encrypted",
            "session_summary": {"status": status, "session_version": 0},
        }
        state = await self.get_state(db, account_id=int(account.id))
        if state is None:
            state = QuotePlatformAccountSessionState(
                account_id=int(account.id),
                owner_user_id=int(account.owner_user_id),
                platform_code=_to_str(account.platform_code).strip().upper(),
            )
            db.add(state)
        state.status = status
        state.session_version = 0
        state.session_generation = ""
        state.jwt_issued_at = None
        state.jwt_expires_at = None
        state.last_error_code = None
        state.last_error_message = None
        state.updated_at = now_db()
        await db.flush()
        await self.delete_redis_mirror(account)


def _parse_dt(value: Any):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return datetime_from_iso(value)
        except Exception:
            return None
    return value


def datetime_from_iso(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    return now_db().__class__.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


def _snapshot_from_runtime(
    *,
    ctx: PlatformAccountContext,
    account: QuotePlatformAccountProfile,
    result: PlatformRuntimeResult,
    previous: Optional[AccountSessionSnapshot],
    status: str,
    fresh_generation: bool,
) -> AccountSessionSnapshot:
    data = _json_obj(result.data)
    raw_snapshot = _json_obj(data.get("session_snapshot") or data.get("session"))
    if raw_snapshot:
        snapshot = AccountSessionSnapshot.from_dict(raw_snapshot)
        snapshot.platform_code = _to_str(ctx.platform_code).strip().upper()
        snapshot.account_id = int(ctx.account_id)
        snapshot.owner_user_id = int(ctx.owner_user_id or account.owner_user_id or 0)
    elif previous is not None:
        snapshot = AccountSessionSnapshot.from_dict(previous.to_dict())
    else:
        snapshot = AccountSessionSnapshot(
            platform_code=_to_str(ctx.platform_code).strip().upper(),
            account_id=int(ctx.account_id),
            owner_user_id=int(ctx.owner_user_id or account.owner_user_id or 0),
        )

    snapshot.status = status
    snapshot.session_version = int((previous.session_version if previous else 0) or 0) + 1
    if fresh_generation or not snapshot.session_generation:
        snapshot.session_generation = uuid4().hex

    cookies = data.get("cookies")
    if isinstance(cookies, list):
        snapshot.cookies = [CookieRecord(**dict(item or {})) for item in cookies if isinstance(item, dict)]
    snapshot.user_token = _to_str(data.get("user_token") or data.get("USER_TOKEN") or snapshot.user_token)
    snapshot.authorization = _to_str(data.get("authorization") or data.get("Authorization") or snapshot.authorization)
    snapshot.jsession_id = _to_str(data.get("jsession_id") or data.get("JSESSIONID") or snapshot.jsession_id)
    snapshot.user_agent = _to_str(data.get("user_agent") or snapshot.user_agent)
    snapshot.browser_profile_path = _to_str(data.get("browser_profile_path") or ctx.payload.get("browser_profile_path") or snapshot.browser_profile_path) or None

    jwt_raw = _json_obj(data.get("jwt"))
    if jwt_raw:
        snapshot.jwt = jwt_claims_from_mapping(jwt_raw)

    now = iso_now()
    snapshot.last_login_at = now
    if status == SESSION_STATUS_AUTHENTICATED:
        snapshot.last_authenticated_at = now
        snapshot.last_error_code = None
        snapshot.last_error_message = None
        _schedule_next_keepalive(snapshot, ctx.platform_code)
    else:
        snapshot.last_error_code = _to_str(data.get("error_code") or status).upper() or None
        snapshot.last_error_message = result.message or None

    snapshot.runtime_meta = {
        **_json_obj(snapshot.runtime_meta),
        "adapter_status": result.status,
        "adapter_message": result.message,
        "synthetic": not bool(raw_snapshot),
    }
    return snapshot


def _merge_runtime_session_fields(
    snapshot: AccountSessionSnapshot,
    *,
    ctx: PlatformAccountContext,
    result: PlatformRuntimeResult,
) -> None:
    data = _json_obj(result.data)
    raw_snapshot = _json_obj(data.get("session_snapshot") or data.get("session"))
    incoming: Optional[AccountSessionSnapshot] = None
    if raw_snapshot:
        try:
            incoming = AccountSessionSnapshot.from_dict(raw_snapshot)
        except Exception:
            incoming = None

    if incoming is not None:
        if incoming.cookies:
            snapshot.cookies = incoming.cookies
        snapshot.user_token = incoming.user_token or snapshot.user_token
        snapshot.authorization = incoming.authorization or snapshot.authorization
        snapshot.jsession_id = incoming.jsession_id or snapshot.jsession_id
        snapshot.team = incoming.team or snapshot.team
        snapshot.user_agent = incoming.user_agent or snapshot.user_agent
        snapshot.browser_profile_path = incoming.browser_profile_path or snapshot.browser_profile_path
        if incoming.jwt.issued_at or incoming.jwt.expires_at or incoming.jwt.raw:
            snapshot.jwt = incoming.jwt
        for key in (
            "last_authenticated_at",
            "last_business_at",
            "last_keepalive_at",
            "last_refresh_at",
            "last_validation_at",
            "next_keepalive_at",
        ):
            value = getattr(incoming, key, None)
            if value:
                setattr(snapshot, key, value)
        snapshot.runtime_meta = {
            **_json_obj(snapshot.runtime_meta),
            **_json_obj(incoming.runtime_meta),
        }

    cookies = data.get("cookies")
    if isinstance(cookies, list):
        snapshot.cookies = [CookieRecord(**dict(item or {})) for item in cookies if isinstance(item, dict)]
    snapshot.user_token = _to_str(data.get("user_token") or data.get("USER_TOKEN") or snapshot.user_token)
    snapshot.authorization = _to_str(data.get("authorization") or data.get("Authorization") or snapshot.authorization)
    snapshot.jsession_id = _to_str(data.get("jsession_id") or data.get("JSESSIONID") or snapshot.jsession_id)
    snapshot.user_agent = _to_str(data.get("user_agent") or snapshot.user_agent)
    snapshot.browser_profile_path = (
        _to_str(data.get("browser_profile_path") or ctx.payload.get("browser_profile_path") or snapshot.browser_profile_path)
        or None
    )

    jwt_raw = _json_obj(data.get("jwt"))
    if jwt_raw:
        snapshot.jwt = jwt_claims_from_mapping(jwt_raw)

    snapshot.runtime_meta = {
        **_json_obj(snapshot.runtime_meta),
        **_json_obj(data.get("runtime_meta")),
        "adapter_status": result.status,
        "adapter_message": result.message,
        "synthetic": not bool(raw_snapshot),
    }


def _snapshot_from_business_runtime(
    *,
    ctx: PlatformAccountContext,
    account: QuotePlatformAccountProfile,
    result: PlatformRuntimeResult,
    previous: AccountSessionSnapshot,
    status: str,
    action: str,
) -> AccountSessionSnapshot:
    snapshot = AccountSessionSnapshot.from_dict(previous.to_dict())
    _merge_runtime_session_fields(snapshot, ctx=ctx, result=result)
    snapshot.platform_code = _to_str(ctx.platform_code).strip().upper()
    snapshot.account_id = int(ctx.account_id)
    snapshot.owner_user_id = int(ctx.owner_user_id or account.owner_user_id or 0)
    snapshot.status = status
    snapshot.session_version = int(previous.session_version or 0) + 1
    if not snapshot.session_generation:
        snapshot.session_generation = uuid4().hex

    now = iso_now()
    if action in {"quote", "query_joint_sales_plan", "query_repair_codes"}:
        snapshot.last_business_at = now
    elif action == "keepalive":
        snapshot.last_keepalive_at = now
    elif action == "check_quota":
        snapshot.last_validation_at = now
    if status == SESSION_STATUS_AUTHENTICATED:
        snapshot.last_authenticated_at = now
        snapshot.last_error_code = None
        snapshot.last_error_message = None
        _schedule_next_keepalive(snapshot, ctx.platform_code)
    else:
        data = _json_obj(result.data)
        snapshot.last_error_code = _to_str(data.get("error_code") or result.status or status).upper() or None
        snapshot.last_error_message = result.message or None
    return snapshot


def _with_session(ctx: PlatformAccountContext, snapshot: AccountSessionSnapshot) -> PlatformAccountContext:
    payload = dict(ctx.payload or {})
    payload["session"] = snapshot.safe_summary()
    payload["session_snapshot"] = snapshot.to_dict()
    return replace(ctx, payload=payload)


async def _with_browser_runtime_if_needed(
    adapter: Any,
    ctx: PlatformAccountContext,
    *,
    purpose: str,
) -> tuple[PlatformAccountContext, Optional[BrowserLease]]:
    if not bool(getattr(adapter, "requires_browser_runtime", False)):
        return ctx, None
    lease = await browser_runtime_manager.ensure(ctx, purpose=purpose)
    payload = dict(ctx.payload or {})
    payload["browser_runtime"] = lease.to_safe_dict()
    payload["browser_cdp_url"] = lease.cdp_url
    payload["browser_profile_path"] = lease.browser_profile_path
    payload["login_artifact_path"] = lease.login_artifact_path
    return replace(ctx, payload=payload, profile_dir=lease.profile_dir), lease


class AccountSessionActor:
    def __init__(self, *, platform_code: str, account_id: int, store: QuotePlatformSessionStore):
        self.platform_code = _to_str(platform_code).strip().upper()
        self.account_id = int(account_id)
        self.store = store
        self.local_lock = asyncio.Lock()
        self._business_waiters = 0

    @property
    def lock_key(self) -> str:
        return f"dingchang:quote_platform:account_lock:{self.platform_code}:{self.account_id}"

    async def login(self, db: AsyncSession, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        async with self.local_lock:
            account = await self._load_account(db, ctx)
            async with _RedisLeaseLock(
                _lease_key(account, purpose="login"),
                ttl_seconds=QUOTE_PLATFORM_LOGIN_LOCK_SECONDS,
                wait_seconds=30,
            ) as runtime_lock:
                if not bool(account.enabled):
                    await self.store.clear(db, account, status=SESSION_STATUS_DISABLED)
                    return PlatformRuntimeResult(status="disabled", message="该平台账号已停用")
                previous = await self.store.load(account)
                if previous is not None and previous.status == SESSION_STATUS_WAITING_CHALLENGE:
                    restored = _restore_preserved_session_after_challenge_interruption(
                        ctx=ctx,
                        waiting_snapshot=previous,
                        result=PlatformRuntimeResult(
                            status="degraded",
                            message="上次登录仍在等待验证码，已先恢复原有会话",
                        ),
                        reason="上次登录未完成",
                    )
                    if restored is not None:
                        ok = await self.store.save(db, account, restored, expected_version=previous.session_version)
                        if not ok:
                            return _session_conflict_result()
                        previous = restored
                running = _snapshot_from_runtime(
                    ctx=ctx,
                    account=account,
                    result=PlatformRuntimeResult(status="logging_in", message="登录中"),
                    previous=previous,
                    status=SESSION_STATUS_LOGGING_IN,
                    fresh_generation=previous is None,
                )
                started = await self.store.save(db, account, running, expected_version=previous.session_version if previous else None)
                if not started:
                    return PlatformRuntimeResult(status="conflict", message="账号会话已被其他请求更新，请重新操作")

                adapter = get_quote_platform_adapter(ctx.platform_code)
                lease: Optional[BrowserLease] = None
                try:
                    runtime_ctx, lease = await _with_browser_runtime_if_needed(
                        adapter,
                        _with_session(ctx, running),
                        purpose="login",
                    )
                    result = await adapter.login(runtime_ctx)
                    if lease is not None:
                        result = replace(result, data=_safe_result_data(result, {"browser_runtime": lease.to_safe_dict()}))
                except BrowserRuntimeError as exc:
                    result = PlatformRuntimeResult(
                        status="failed",
                        message=f"浏览器容器启动失败：{str(exc) or exc.__class__.__name__}",
                        data={"error_code": exc.__class__.__name__},
                    )
                except Exception as exc:
                    result = PlatformRuntimeResult(
                        status="failed",
                        message=f"平台登录执行异常：{str(exc) or exc.__class__.__name__}",
                        data={"error_code": exc.__class__.__name__},
                    )
                if lease is not None:
                    result = replace(result, data=_safe_result_data(result, {"browser_runtime": lease.to_safe_dict()}))
                try:
                    runtime_lock.assert_owned()
                except TimeoutError as exc:
                    return _lock_lost_result(exc)

                status = _to_str(result.status).strip().lower()
                if status in {"success", "ok", "authenticated"}:
                    snapshot = _snapshot_from_runtime(
                        ctx=ctx,
                        account=account,
                        result=result,
                        previous=running,
                        status=SESSION_STATUS_AUTHENTICATED,
                        fresh_generation=True,
                    )
                    ok = await self.store.save(db, account, snapshot, expected_version=running.session_version)
                    if not ok:
                        return PlatformRuntimeResult(status="conflict", message="账号会话已被更新，请重新操作")
                    return replace(result, data=_safe_result_data(result, {"session": snapshot.safe_summary()}))
                if status in {"needs_code", "need_code", "sms_required", "requires_sms", "challenge_required", "requires_challenge"}:
                    snapshot = _snapshot_from_runtime(
                        ctx=ctx,
                        account=account,
                        result=result,
                        previous=running,
                        status=SESSION_STATUS_WAITING_CHALLENGE,
                        fresh_generation=False,
                    )
                    _attach_previous_usable_session_to_challenge(challenge_snapshot=snapshot, previous=previous)
                    ok = await self.store.save(db, account, snapshot, expected_version=running.session_version)
                    if not ok:
                        return _session_conflict_result()
                    return replace(result, data=_safe_result_data(result, {"session": snapshot.safe_summary()}))

                previous_status = _to_str(getattr(previous, "status", "")).strip().lower() if previous is not None else ""
                if previous is not None and previous_status in AUTH_USABLE_SESSION_STATUSES:
                    snapshot = _preserve_previous_session_after_login_failure(
                        ctx=ctx,
                        previous=previous,
                        running=running,
                        result=result,
                    )
                    ok = await self.store.save(db, account, snapshot, expected_version=running.session_version)
                    if not ok:
                        return _session_conflict_result()
                    message = snapshot.last_error_message or "新登录失败，已保留原有可用会话"
                    return replace(
                        result,
                        status="degraded",
                        message=message,
                        data=_safe_result_data(
                            result,
                            {
                                "session": snapshot.safe_summary(),
                                "preserved_previous_session": True,
                                "original_login_status": status,
                            },
                        ),
                    )

                snapshot = _snapshot_from_runtime(
                    ctx=ctx,
                    account=account,
                    result=result,
                    previous=running,
                    status=SESSION_STATUS_LOGIN_FAILED,
                    fresh_generation=False,
                )
                ok = await self.store.save(db, account, snapshot, expected_version=running.session_version)
                if not ok:
                    return _session_conflict_result()
                return replace(result, data=_safe_result_data(result, {"session": snapshot.safe_summary()}))

    async def submit_challenge(self, db: AsyncSession, ctx: PlatformAccountContext, challenge: str) -> PlatformRuntimeResult:
        async with self.local_lock:
            account = await self._load_account(db, ctx)
            async with _RedisLeaseLock(
                _lease_key(account, purpose="challenge"),
                ttl_seconds=QUOTE_PLATFORM_LOGIN_LOCK_SECONDS,
                wait_seconds=30,
            ) as runtime_lock:
                previous = await self.store.load(account)
                if previous is None:
                    return PlatformRuntimeResult(
                        status="expired",
                        message="登录会话已失效，请重新点击登录",
                        data={"error_code": "MISSING_LOGIN_SESSION"},
                    )
                if _to_str(previous.status).strip().lower() != SESSION_STATUS_WAITING_CHALLENGE:
                    return PlatformRuntimeResult(
                        status="expired",
                        message="登录会话已不在等待验证码状态，请重新点击登录",
                        data={
                            "error_code": "LOGIN_SESSION_NOT_WAITING_CHALLENGE",
                            "session": previous.safe_summary(),
                        },
                    )
                adapter = get_quote_platform_adapter(ctx.platform_code)
                lease: Optional[BrowserLease] = None
                try:
                    runtime_ctx, lease = await _with_browser_runtime_if_needed(
                        adapter,
                        _with_session(ctx, previous),
                        purpose="challenge",
                    )
                    result = await adapter.submit_challenge(runtime_ctx, challenge)
                    if lease is not None:
                        result = replace(result, data=_safe_result_data(result, {"browser_runtime": lease.to_safe_dict()}))
                except BrowserRuntimeError as exc:
                    result = PlatformRuntimeResult(
                        status="failed",
                        message=f"浏览器容器启动失败：{str(exc) or exc.__class__.__name__}",
                        data={"error_code": exc.__class__.__name__},
                    )
                except Exception as exc:
                    result = PlatformRuntimeResult(
                        status="failed",
                        message=f"平台验证码校验异常：{str(exc) or exc.__class__.__name__}",
                        data={"error_code": exc.__class__.__name__},
                    )
                try:
                    runtime_lock.assert_owned()
                except TimeoutError as exc:
                    return _lock_lost_result(exc)
                status = _to_str(result.status).strip().lower()
                if status in {"success", "ok", "authenticated"}:
                    snapshot = _snapshot_from_runtime(
                        ctx=ctx,
                        account=account,
                        result=result,
                        previous=previous,
                        status=SESSION_STATUS_AUTHENTICATED,
                        fresh_generation=True,
                    )
                    ok = await self.store.save(db, account, snapshot, expected_version=previous.session_version if previous else None)
                    if not ok:
                        return _session_conflict_result()
                    return replace(result, data=_safe_result_data(result, {"session": snapshot.safe_summary()}))
                if status in {"needs_code", "need_code", "sms_required", "requires_sms", "challenge_required", "requires_challenge"}:
                    snapshot = _snapshot_from_runtime(
                        ctx=ctx,
                        account=account,
                        result=result,
                        previous=previous,
                        status=SESSION_STATUS_WAITING_CHALLENGE,
                        fresh_generation=False,
                    )
                    _attach_previous_usable_session_to_challenge(challenge_snapshot=snapshot, previous=previous)
                    ok = await self.store.save(db, account, snapshot, expected_version=previous.session_version if previous else None)
                    if not ok:
                        return _session_conflict_result()
                    return replace(result, data=_safe_result_data(result, {"session": snapshot.safe_summary()}))

                restored = _restore_preserved_session_after_challenge_interruption(
                    ctx=ctx,
                    waiting_snapshot=previous,
                    result=result,
                    reason="验证码校验未完成",
                )
                if restored is not None:
                    ok = await self.store.save(db, account, restored, expected_version=previous.session_version if previous else None)
                    if not ok:
                        return _session_conflict_result()
                    message = restored.last_error_message or "验证码校验未完成，已恢复原有可用会话"
                    return replace(
                        result,
                        status="degraded",
                        message=message,
                        data=_safe_result_data(
                            result,
                            {
                                "session": restored.safe_summary(),
                                "preserved_previous_session": True,
                                "original_challenge_status": status,
                            },
                        ),
                    )

                snapshot = _snapshot_from_runtime(
                    ctx=ctx,
                    account=account,
                    result=result,
                    previous=previous,
                    status=SESSION_STATUS_LOGIN_FAILED,
                    fresh_generation=False,
                )
                ok = await self.store.save(db, account, snapshot, expected_version=previous.session_version if previous else None)
                if not ok:
                    return _session_conflict_result()
                return replace(result, data=_safe_result_data(result, {"session": snapshot.safe_summary()}))

    async def keepalive(self, db: AsyncSession, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        if self._business_waiters > 0:
            return PlatformRuntimeResult(status="skipped", message="账号存在待处理报价业务，本轮保活已跳过")
        return await self._execute_business_like(db, ctx, action="keepalive", payload={})

    async def check_quota(self, db: AsyncSession, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return await self._execute_business_like(db, ctx, action="check_quota", payload={})

    async def quote(self, db: AsyncSession, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        self._business_waiters += 1
        try:
            return await self._execute_business_like(db, ctx, action="quote", payload=quote_payload)
        finally:
            self._business_waiters -= 1

    async def query_renewal(self, db: AsyncSession, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        self._business_waiters += 1
        try:
            return await self._execute_business_like(db, ctx, action="query_renewal", payload=quote_payload)
        finally:
            self._business_waiters -= 1

    async def query_joint_sales_plan(self, db: AsyncSession, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        self._business_waiters += 1
        try:
            return await self._execute_business_like(db, ctx, action="query_joint_sales_plan", payload=quote_payload)
        finally:
            self._business_waiters -= 1

    async def query_repair_codes(self, db: AsyncSession, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        self._business_waiters += 1
        try:
            return await self._execute_business_like(db, ctx, action="query_repair_codes", payload=quote_payload)
        finally:
            self._business_waiters -= 1

    async def _execute_business_like(
        self,
        db: AsyncSession,
        ctx: PlatformAccountContext,
        *,
        action: str,
        payload: Dict[str, Any],
    ) -> PlatformRuntimeResult:
        async with self.local_lock:
            account = await self._load_account(db, ctx)
            async with _RedisLeaseLock(
                _lease_key(account, purpose=action),
                ttl_seconds=QUOTE_PLATFORM_WORKER_LOCK_SECONDS,
                wait_seconds=60,
                require_redis=action == "quote",
            ) as runtime_lock:
                snapshot = await self.store.load(account)
                allow_synthetic = QUOTE_PLATFORM_SYNTHETIC_SESSION and self.platform_code != "PICC"
                if snapshot is None and allow_synthetic:
                    snapshot = AccountSessionSnapshot(
                        platform_code=self.platform_code,
                        account_id=self.account_id,
                        owner_user_id=int(ctx.owner_user_id or account.owner_user_id or 0),
                        status=SESSION_STATUS_AUTHENTICATED,
                        runtime_meta={"synthetic": True, "reason": "missing_snapshot_migration"},
                        last_authenticated_at=iso_now(),
                    )
                    ok = await self.store.save(db, account, snapshot)
                    if not ok:
                        return _session_conflict_result()

                if snapshot is None:
                    return PlatformRuntimeResult(status="expired", message="账号没有可用登录会话，请先登录")
                if snapshot.status not in AUTH_USABLE_SESSION_STATUSES:
                    if snapshot.status == SESSION_STATUS_WAITING_CHALLENGE:
                        restored = _restore_preserved_session_after_challenge_interruption(
                            ctx=ctx,
                            waiting_snapshot=snapshot,
                            result=PlatformRuntimeResult(
                                status="degraded",
                                message="新登录仍在等待验证码，已继续复用原有会话",
                            ),
                            reason="新登录未完成",
                        )
                        if restored is not None:
                            ok = await self.store.save(db, account, restored, expected_version=snapshot.session_version)
                            if not ok:
                                return _session_conflict_result()
                            snapshot = restored
                    if snapshot.status not in AUTH_USABLE_SESSION_STATUSES:
                        return PlatformRuntimeResult(status=snapshot.status, message="账号当前没有可用登录会话，请先完成登录")

                adapter = get_quote_platform_adapter(ctx.platform_code)
                runtime_ctx = _with_session(ctx, snapshot)
                lease: Optional[BrowserLease] = None
                try:
                    runtime_ctx, lease = await _with_browser_runtime_if_needed(adapter, runtime_ctx, purpose=action)
                except BrowserRuntimeError as exc:
                    result = PlatformRuntimeResult(
                        status="failed",
                        message=f"浏览器容器启动失败：{str(exc) or exc.__class__.__name__}",
                        data={"error_code": exc.__class__.__name__},
                    )
                    if action in {"quote", "query_renewal", "query_joint_sales_plan", "query_repair_codes"}:
                        snapshot.last_business_at = iso_now()
                    elif action == "keepalive":
                        snapshot.last_keepalive_at = iso_now()
                    elif action == "check_quota":
                        snapshot.last_validation_at = iso_now()
                    snapshot.last_error_code = "BROWSER_RUNTIME_FAILED"
                    snapshot.last_error_message = result.message
                    try:
                        runtime_lock.assert_owned()
                    except TimeoutError as exc:
                        return _lock_lost_result(exc)
                    ok = await self.store.save(db, account, snapshot, expected_version=snapshot.session_version)
                    if not ok:
                        return _session_conflict_result()
                    return replace(result, data=_safe_result_data(result, {"session": snapshot.safe_summary()}))
                if action == "quote":
                    try:
                        result = await adapter.quote(runtime_ctx, payload)
                    except Exception as exc:
                        result = PlatformRuntimeResult(
                            status="failed",
                            message=f"平台报价执行异常：{str(exc) or exc.__class__.__name__}",
                            data={"error_code": exc.__class__.__name__},
                        )
                    snapshot.last_business_at = iso_now()
                elif action == "query_renewal":
                    try:
                        result = await _await_runtime_result(action, adapter.query_renewal(runtime_ctx, payload))
                    except Exception as exc:
                        result = PlatformRuntimeResult(
                            status="failed",
                            message=f"平台续保查询异常：{str(exc) or exc.__class__.__name__}",
                            data={"error_code": exc.__class__.__name__},
                        )
                    snapshot.last_business_at = iso_now()
                elif action == "query_joint_sales_plan":
                    try:
                        result = await _await_runtime_result(action, adapter.query_joint_sales_plan(runtime_ctx, payload))
                    except Exception as exc:
                        result = PlatformRuntimeResult(
                            status="failed",
                            message=f"平台途家安顺保额查询异常：{str(exc) or exc.__class__.__name__}",
                            data={"error_code": exc.__class__.__name__},
                        )
                    snapshot.last_business_at = iso_now()
                elif action == "query_repair_codes":
                    try:
                        result = await _await_runtime_result(action, adapter.query_repair_codes(runtime_ctx, payload))
                    except Exception as exc:
                        result = PlatformRuntimeResult(
                            status="failed",
                            message=f"平台送修码查询异常：{str(exc) or exc.__class__.__name__}",
                            data={"error_code": exc.__class__.__name__},
                        )
                    snapshot.last_business_at = iso_now()
                elif action == "keepalive":
                    try:
                        result = await _await_runtime_result(action, adapter.keepalive(runtime_ctx))
                    except Exception as exc:
                        result = PlatformRuntimeResult(
                            status="failed",
                            message=f"平台保活执行异常：{str(exc) or exc.__class__.__name__}",
                            data={"error_code": exc.__class__.__name__},
                        )
                    snapshot.last_keepalive_at = iso_now()
                elif action == "check_quota":
                    try:
                        result = await _await_runtime_result(action, adapter.check_quota(runtime_ctx))
                    except Exception as exc:
                        result = PlatformRuntimeResult(
                            status="failed",
                            message=f"平台额度检查异常：{str(exc) or exc.__class__.__name__}",
                            data={"error_code": exc.__class__.__name__},
                        )
                    snapshot.last_validation_at = iso_now()
                else:
                    return PlatformRuntimeResult(status="failed", message=f"未知平台账号动作：{action}")

                try:
                    runtime_lock.assert_owned()
                except TimeoutError as exc:
                    return _lock_lost_result(exc)

                status = _to_str(result.status).strip().lower()
                business_status = _to_str(_json_obj(result.data).get("business_status"))
                expired = status in AUTH_EXPIRED_STATUSES or business_status == "16"
                degraded = bool(not expired and status not in {"success", "ok", "quoted", "available", "skipped"} and action in {"keepalive", "check_quota"})
                next_snapshot = _snapshot_from_business_runtime(
                    ctx=ctx,
                    account=account,
                    result=result,
                    previous=snapshot,
                    status=SESSION_STATUS_EXPIRED if expired else (SESSION_STATUS_DEGRADED if degraded else SESSION_STATUS_AUTHENTICATED),
                    action=action,
                )
                if expired:
                    next_snapshot.last_error_code = "STATUS_16" if business_status == "16" else status.upper()
                    next_snapshot.last_error_message = result.message or "登录已过期，请重新登录"
                elif status in {"success", "ok", "quoted", "available", "skipped"}:
                    next_snapshot.last_error_code = None
                    next_snapshot.last_error_message = None
                else:
                    next_snapshot.last_error_code = status.upper() if status else "BUSINESS_FAILED"
                    next_snapshot.last_error_message = result.message or "平台业务执行失败"

                ok = await self.store.save(db, account, next_snapshot, expected_version=snapshot.session_version)
                if not ok:
                    return _session_conflict_result()
                if expired:
                    await self.store.discard_cached_session(db, account, next_snapshot)
                return replace(result, data=_safe_result_data(result, {"session": next_snapshot.safe_summary()}))

    async def _load_account(self, db: AsyncSession, ctx: PlatformAccountContext) -> QuotePlatformAccountProfile:
        account = await self.store.get_account(db, ctx)
        if account is None:
            raise ValueError("平台账号不存在或无权操作")
        return account


class QuotePlatformSessionManager:
    def __init__(self) -> None:
        self.store = QuotePlatformSessionStore()
        self._actors: Dict[tuple[str, int], AccountSessionActor] = {}
        self._actor_lock = asyncio.Lock()

    async def actor(self, platform_code: str, account_id: int) -> AccountSessionActor:
        key = (_to_str(platform_code).strip().upper(), int(account_id))
        async with self._actor_lock:
            actor = self._actors.get(key)
            if actor is None:
                actor = AccountSessionActor(platform_code=key[0], account_id=key[1], store=self.store)
                self._actors[key] = actor
            return actor

    async def login(self, db: AsyncSession, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return await (await self.actor(ctx.platform_code, ctx.account_id)).login(db, ctx)

    async def submit_challenge(self, db: AsyncSession, ctx: PlatformAccountContext, challenge: str) -> PlatformRuntimeResult:
        return await (await self.actor(ctx.platform_code, ctx.account_id)).submit_challenge(db, ctx, challenge)

    async def keepalive(self, db: AsyncSession, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return await (await self.actor(ctx.platform_code, ctx.account_id)).keepalive(db, ctx)

    async def check_quota(self, db: AsyncSession, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return await (await self.actor(ctx.platform_code, ctx.account_id)).check_quota(db, ctx)

    async def quote(self, db: AsyncSession, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return await (await self.actor(ctx.platform_code, ctx.account_id)).quote(db, ctx, quote_payload)

    async def query_renewal(self, db: AsyncSession, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return await (await self.actor(ctx.platform_code, ctx.account_id)).query_renewal(db, ctx, quote_payload)

    async def query_joint_sales_plan(self, db: AsyncSession, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return await (await self.actor(ctx.platform_code, ctx.account_id)).query_joint_sales_plan(db, ctx, quote_payload)

    async def query_repair_codes(self, db: AsyncSession, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return await (await self.actor(ctx.platform_code, ctx.account_id)).query_repair_codes(db, ctx, quote_payload)

    async def clear(self, db: AsyncSession, account: QuotePlatformAccountProfile, *, status: str = SESSION_STATUS_OFFLINE) -> None:
        await self.store.clear(db, account, status=status)


session_manager = QuotePlatformSessionManager()
