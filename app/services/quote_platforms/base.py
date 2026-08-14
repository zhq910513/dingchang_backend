# encoding: utf-8
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class PlatformAccountContext:
    platform_code: str
    platform_name: str
    account_id: int
    account_username: str
    owner_user_id: int = 0
    account_password: str = ""
    account_type_name: str = ""
    browser_env_key: str = ""
    profile_dir: Optional[Path] = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlatformRuntimeResult:
    status: str
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    challenge_type: Optional[str] = None
    challenge_prompt: Optional[str] = None


class QuotePlatformAdapter:
    platform_code = "STUB"
    platform_name = "Stub"
    requires_browser_runtime = False
    keep_browser_alive = False

    async def validate_account(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="ok")

    async def login(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="success")

    async def submit_challenge(self, ctx: PlatformAccountContext, challenge: str) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="success", data={"challenge_length": len(challenge or "")})

    async def keepalive(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="success")

    async def check_quota(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="available")

    async def detect_account_type(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="unknown")

    async def quote(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="success", data={"mode": "stub"})

    async def query_renewal(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="failed", message="当前平台暂不支持续保查询")

    async def query_joint_sales_plan(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="failed", message="当前平台暂不支持途家安顺保额查询")

    async def query_repair_codes(self, ctx: PlatformAccountContext, quote_payload: Dict[str, Any]) -> PlatformRuntimeResult:
        return PlatformRuntimeResult(status="failed", message="当前平台暂不支持送修码查询")
