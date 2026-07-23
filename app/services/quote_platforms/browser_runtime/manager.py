# encoding: utf-8
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.services.quote_platforms.base import PlatformAccountContext
from app.services.quote_platforms.browser_runtime.lease import BrowserLease


class BrowserRuntimeError(RuntimeError):
    pass


def _env_bool(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except Exception:
        return default


def _safe_part(value: object, default: str = "x") -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip().lower())
    text = text.strip("._-")
    return text[:48] or default


class BrowserRuntimeManager:
    """Owns one Dockerized browser runtime per platform account."""

    def __init__(self) -> None:
        self.enabled = _env_bool("QUOTE_BROWSER_RUNTIME_ENABLED", "0")
        self.image = os.getenv("QUOTE_BROWSER_RUNTIME_IMAGE", "dingchang/quote-browser-runtime:latest")
        self.container_prefix = _safe_part(os.getenv("QUOTE_BROWSER_CONTAINER_PREFIX", "dc_quote_browser"))
        self.internal_port = _env_int("QUOTE_BROWSER_INTERNAL_PORT", 9222)
        self.ready_timeout_seconds = max(5, _env_int("QUOTE_BROWSER_READY_TIMEOUT_SECONDS", 45))
        self.storage_root = Path(getattr(settings, "STORAGE_ROOT", "./storage")).expanduser().resolve()
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def ensure(self, ctx: PlatformAccountContext, *, purpose: str) -> BrowserLease:
        if not self.enabled:
            raise BrowserRuntimeError("浏览器容器运行时未启用，请设置 QUOTE_BROWSER_RUNTIME_ENABLED=1 并准备浏览器镜像")
        platform_code = _safe_part(ctx.platform_code, "stub")
        account_id = int(ctx.account_id or 0)
        if account_id <= 0:
            raise BrowserRuntimeError("浏览器容器运行时缺少有效账号ID")
        lock = await self._account_lock(platform_code, account_id)
        async with lock:
            container_name = self.container_name(platform_code=platform_code, account_id=account_id)
            profile_dir = self.profile_dir(platform_code=platform_code, account_id=account_id)
            artifact_dir = self.artifact_dir(platform_code=platform_code, account_id=account_id, purpose=purpose)
            profile_dir.mkdir(parents=True, exist_ok=True)
            artifact_dir.mkdir(parents=True, exist_ok=True)

            existed = await self._container_exists(container_name)
            reused = False
            if existed:
                if not await self._container_running(container_name):
                    await self._docker("start", container_name)
                reused = True
            else:
                await self._run_container(
                    container_name=container_name,
                    profile_dir=profile_dir,
                    artifact_dir=artifact_dir,
                    platform_code=platform_code,
                    account_id=account_id,
                )

            host_port = await self._inspect_host_port(container_name)
            if host_port <= 0:
                raise BrowserRuntimeError(f"浏览器容器端口映射异常：{container_name}")
            http_url = f"http://127.0.0.1:{host_port}"
            await self._wait_ready(http_url)
            return BrowserLease(
                platform_code=platform_code.upper(),
                account_id=account_id,
                container_name=container_name,
                image=self.image,
                cdp_url=http_url,
                http_url=http_url,
                profile_dir=profile_dir,
                artifact_dir=artifact_dir,
                internal_port=self.internal_port,
                host_port=host_port,
                reused=reused,
                meta={"purpose": purpose},
            )

    def container_name(self, *, platform_code: str, account_id: int) -> str:
        return f"{self.container_prefix}_{_safe_part(platform_code)}_{int(account_id)}"

    def profile_dir(self, *, platform_code: str, account_id: int) -> Path:
        return self.storage_root / "quote_browser_profiles" / _safe_part(platform_code) / str(int(account_id))

    def artifact_dir(self, *, platform_code: str, account_id: int, purpose: str) -> Path:
        return (
            self.storage_root
            / "quote_browser_artifacts"
            / _safe_part(platform_code)
            / str(int(account_id))
            / _safe_part(purpose, "runtime")
        )

    async def stop(self, *, platform_code: str, account_id: int) -> None:
        container_name = self.container_name(platform_code=platform_code, account_id=account_id)
        if await self._container_exists(container_name):
            await self._docker("stop", container_name, check=False)

    async def remove(self, *, platform_code: str, account_id: int) -> None:
        container_name = self.container_name(platform_code=platform_code, account_id=account_id)
        if await self._container_exists(container_name):
            await self._docker("rm", "-f", container_name, check=False)

    async def _account_lock(self, platform_code: str, account_id: int) -> asyncio.Lock:
        key = (platform_code, int(account_id))
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def _run_container(
        self,
        *,
        container_name: str,
        profile_dir: Path,
        artifact_dir: Path,
        platform_code: str,
        account_id: int,
    ) -> None:
        args = [
            "run",
            "-d",
            "--name",
            container_name,
            "--restart",
            "unless-stopped",
            "-p",
            f"127.0.0.1::{self.internal_port}",
            "-v",
            f"{profile_dir}:/data/profile",
            "-v",
            f"{artifact_dir}:/data/artifacts",
            "-e",
            f"REMOTE_DEBUGGING_PORT={self.internal_port}",
            "-e",
            f"QUOTE_PLATFORM_CODE={platform_code.upper()}",
            "-e",
            f"QUOTE_ACCOUNT_ID={int(account_id)}",
            self.image,
        ]
        await self._docker(*args)

    async def _container_exists(self, name: str) -> bool:
        result = await self._docker("inspect", name, check=False)
        return result.returncode == 0

    async def _container_running(self, name: str) -> bool:
        result = await self._docker("inspect", "-f", "{{.State.Running}}", name, check=False)
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    async def _inspect_host_port(self, name: str) -> int:
        result = await self._docker("inspect", name)
        try:
            info = json.loads(result.stdout)[0]
            ports = info.get("NetworkSettings", {}).get("Ports", {}) or {}
            mapping = ports.get(f"{self.internal_port}/tcp") or []
            first = mapping[0] if mapping else {}
            return int(first.get("HostPort") or 0)
        except Exception as exc:
            raise BrowserRuntimeError(f"读取浏览器容器端口失败：{exc}") from exc

    async def _wait_ready(self, http_url: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.ready_timeout_seconds
        last_error: Optional[str] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.to_thread(self._read_json_version, http_url)
                return
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                await asyncio.sleep(0.5)
        raise BrowserRuntimeError(f"浏览器容器启动超时：{last_error or http_url}")

    def _read_json_version(self, http_url: str) -> dict:
        with urllib.request.urlopen(f"{http_url.rstrip('/')}/json/version", timeout=2) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise BrowserRuntimeError("浏览器调试端口返回格式异常")
        return data

    async def _docker(self, *args: str, check: bool = True):
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        result = _DockerResult(
            returncode=int(proc.returncode or 0),
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "docker command failed").strip()
            raise BrowserRuntimeError(detail[:1000])
        return result


class _DockerResult:
    def __init__(self, *, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


browser_runtime_manager = BrowserRuntimeManager()

