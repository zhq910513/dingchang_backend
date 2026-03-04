# app/services/ai_platforms/_base.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import abc
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from app.core.config import settings


def _to_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        return str(v)
    except Exception:
        return default


def _stable_json_dumps(obj: Any) -> str:
    """
    用于生成稳定 hash 的 JSON（排序 + 紧凑）
    """
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        # 最后兜底：字符串化
        return _to_str(obj)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


@dataclass
class QuoteContext:
    """
    报价调用上下文（service 层负责填充）
    """
    owner_user_id: Optional[int] = None
    session_id: Optional[str] = None
    order_id: Optional[int] = None
    draft_id: Optional[str] = None
    trace_id: Optional[str] = None

    # 预留：账号体系（后续平台登录态缓存按 account_id 区分）
    account_id: Optional[str] = None

    # 预留：可放 headers/cookies/token 等（适配器内部使用）
    extra: Optional[Dict[str, Any]] = None


@dataclass
class QuoteResult:
    """
    统一返回结构（前端直接消费）
    """
    ok: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    # 标准化摘要（用于前端展示）
    quote_result: Optional[Dict[str, Any]] = None

    # 审计用原始请求/响应（可选）
    raw_request: Optional[Dict[str, Any]] = None
    raw_response: Optional[Dict[str, Any]] = None

    # 缓存命中标记
    cached: bool = False

    # 追踪
    trace_id: Optional[str] = None


class CacheBackend(abc.ABC):
    @abc.abstractmethod
    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @abc.abstractmethod
    async def set(self, key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
        raise NotImplementedError


class RedisCacheBackend(CacheBackend):
    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            from app.core.db import redis  # type: ignore
            if not redis:
                return None
            s = await redis.get(key)
            if not s:
                return None
            if isinstance(s, (bytes, bytearray)):
                s = s.decode("utf-8", errors="ignore")
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    async def set(self, key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
        try:
            from app.core.db import redis  # type: ignore
            if not redis:
                return
            await redis.set(key, json.dumps(value, ensure_ascii=False), ex=int(ttl_seconds))
        except Exception:
            return


class MemoryCacheBackend(CacheBackend):
    """
    兜底：进程内缓存（开发/无 Redis）
    """

    def __init__(self) -> None:
        self._store: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        it = self._store.get(key)
        if not it:
            return None
        exp, val = it
        if exp <= now:
            try:
                del self._store[key]
            except Exception:
                pass
            return None
        return val

    async def set(self, key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
        exp = time.time() + max(1, int(ttl_seconds))
        self._store[key] = (exp, value)


def get_cache_backend() -> CacheBackend:
    # 有 redis 就用 redis，没有就用内存兜底（不影响后续接入）
    try:
        from app.core.db import redis  # type: ignore
        if redis:
            return RedisCacheBackend()
    except Exception:
        pass
    return MemoryCacheBackend()


def build_quote_cache_key(platform_code: str, material_hash: str) -> str:
    return f"ai:quote:{platform_code}:{material_hash}"


def material_hash_of(platform_code: str, material_payload: Dict[str, Any]) -> str:
    """
    报价缓存键：同“识别结果/卡槽数据 + 平台 + 关键参数”命中缓存
    """
    base = {
        "platform": platform_code,
        "material_payload": material_payload,
    }
    return _sha256_hex(_stable_json_dumps(base))


class AiPlatformAdapter(abc.ABC):
    """
    平台适配器基类（统一入口）

    你后续每个平台一个文件继承它：
    - ensure_auth: 负责保证登录态（cookie/header/token），可用 redis 缓存
    - build_payload: 把 material_payload 组装成平台 json_data
    - do_quote: 发请求拿原始响应
    - normalize_quote_result: 把原始响应转成统一摘要
    """
    platform_code: str = "unknown"

    def enabled(self) -> bool:
        """
        ✅ 配置开关（统一）
        settings 里会有 AI_PLATFORM_ENABLE_<PLATFORM_CODE_UPPER>
        例如：AI_PLATFORM_ENABLE_TP=True
        """
        code = (self.platform_code or "").strip().upper()
        if not code:
            return False
        key = f"AI_PLATFORM_ENABLE_{code}"
        try:
            return bool(getattr(settings, key, False))
        except Exception:
            return False

    def timeout_seconds(self) -> int:
        return int(getattr(settings, "AI_PLATFORM_TIMEOUT_SECONDS", 20) or 20)

    def retry_count(self) -> int:
        return int(getattr(settings, "AI_PLATFORM_RETRY_COUNT", 1) or 1)

    def cache_ttl_seconds(self) -> int:
        return int(getattr(settings, "AI_PLATFORM_CACHE_TTL_SECONDS", 300) or 300)

    async def quote(
            self,
            *,
            ctx: QuoteContext,
            material_payload: Dict[str, Any],
            use_cache: bool = True,
    ) -> QuoteResult:
        """
        ✅ 统一报价入口（带缓存 + 统一错误结构）
        """
        trace_id = ctx.trace_id or None
        if not self.enabled():
            return QuoteResult(
                ok=False,
                error_code="platform_disabled",
                error_message=f"平台未启用：{self.platform_code}",
                quote_result=None,
                raw_request=None,
                raw_response=None,
                cached=False,
                trace_id=trace_id,
            )

        # 1) cache
        mh = material_hash_of(self.platform_code, material_payload)
        cache_key = build_quote_cache_key(self.platform_code, mh)

        if use_cache:
            cached = await get_cache_backend().get(cache_key)
            if isinstance(cached, dict) and cached.get("ok") is True:
                return QuoteResult(
                    ok=True,
                    quote_result=cached.get("quote_result"),
                    raw_request=cached.get("raw_request"),
                    raw_response=cached.get("raw_response"),
                    cached=True,
                    trace_id=trace_id,
                )

        # 2) auth
        try:
            await self.ensure_auth(ctx=ctx)
        except Exception as e:
            return QuoteResult(
                ok=False,
                error_code="auth_failed",
                error_message=f"平台登录态异常：{_to_str(e)}",
                cached=False,
                trace_id=trace_id,
            )

        # 3) build payload
        try:
            req_payload = await self.build_payload(ctx=ctx, material_payload=material_payload)
        except Exception as e:
            return QuoteResult(
                ok=False,
                error_code="build_payload_failed",
                error_message=f"组装平台请求失败：{_to_str(e)}",
                cached=False,
                trace_id=trace_id,
            )

        # 4) do quote (with retry)
        last_err = None
        raw_resp: Optional[Dict[str, Any]] = None
        for _ in range(max(1, self.retry_count())):
            try:
                raw_resp = await self.do_quote(ctx=ctx, payload=req_payload)
                last_err = None
                break
            except Exception as e:
                last_err = e

        if last_err is not None:
            return QuoteResult(
                ok=False,
                error_code="platform_request_failed",
                error_message=f"平台请求失败：{_to_str(last_err)}",
                raw_request=req_payload if isinstance(req_payload, dict) else None,
                raw_response=raw_resp,
                cached=False,
                trace_id=trace_id,
            )

        # 5) normalize
        try:
            norm = await self.normalize_quote_result(ctx=ctx, payload=req_payload, raw_response=raw_resp or {})
        except Exception as e:
            return QuoteResult(
                ok=False,
                error_code="normalize_failed",
                error_message=f"平台返回解析失败：{_to_str(e)}",
                raw_request=req_payload if isinstance(req_payload, dict) else None,
                raw_response=raw_resp,
                cached=False,
                trace_id=trace_id,
            )

        res = QuoteResult(
            ok=True,
            quote_result=norm,
            raw_request=req_payload if isinstance(req_payload, dict) else None,
            raw_response=raw_resp,
            cached=False,
            trace_id=trace_id,
        )

        # 6) store cache
        if use_cache:
            await get_cache_backend().set(
                cache_key,
                {
                    "ok": True,
                    "quote_result": res.quote_result,
                    "raw_request": res.raw_request,
                    "raw_response": res.raw_response,
                },
                ttl_seconds=self.cache_ttl_seconds(),
            )

        return res

    # -------------------------
    # 子类必须实现
    # -------------------------
    @abc.abstractmethod
    async def ensure_auth(self, *, ctx: QuoteContext) -> None:
        """
        确保平台登录态（cookie/token/header）
        - 后续你补 cookie/header_authorization：就在这里读/写 redis
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def build_payload(self, *, ctx: QuoteContext, material_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        将 material_payload 转换为平台请求体（json_data 等）
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def do_quote(self, *, ctx: QuoteContext, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        发起平台报价请求，返回平台原始响应（dict）
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def normalize_quote_result(self, *, ctx: QuoteContext, payload: Dict[str, Any],
                                     raw_response: Dict[str, Any]) -> Dict[str, Any]:
        """
        将 raw_response 转换为标准化摘要 quote_result（便于前端展示）
        """
        raise NotImplementedError


class StubPlatformAdapter(AiPlatformAdapter):
    """
    ✅ 默认占位适配器：平台未接入时返回 stub
    """
    platform_code = "STUB"

    async def ensure_auth(self, *, ctx: QuoteContext) -> None:
        return

    async def build_payload(self, *, ctx: QuoteContext, material_payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "platform_code": self.platform_code,
            "material_payload": material_payload,
            "session_id": ctx.session_id,
            "order_id": ctx.order_id,
            "draft_id": ctx.draft_id,
            "trace_id": ctx.trace_id,
        }

    async def do_quote(self, *, ctx: QuoteContext, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"stub": True, "message": "platform adapter not implemented"}

    async def normalize_quote_result(self, *, ctx: QuoteContext, payload: Dict[str, Any],
                                     raw_response: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "stub",
            "message": "平台报价接口未接入",
            "price_items": [],
            "raw": raw_response,
        }
