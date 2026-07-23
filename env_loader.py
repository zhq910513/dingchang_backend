from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent
LOCAL_ENV_FILE = ".env.local"
SERVER_ENV_FILE = ".env.server"


def _normalize_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return path


def _runtime_profile() -> str:
    for name in ("DINGCHANG_ENV_PROFILE", "DINGCHANG_RUNTIME_ENV", "APP_ENV", "ENV"):
        value = os.getenv(name)
        if value:
            return str(value).strip().lower()
    return ""


def resolve_env_file(preferred: str | None = None) -> Path | None:
    if preferred:
        candidate = _normalize_path(preferred)
        return candidate if candidate.exists() else None

    explicit = os.getenv("DINGCHANG_ENV_FILE") or os.getenv("ENV_FILE") or os.getenv("APP_ENV_FILE")
    if explicit:
        candidate = _normalize_path(explicit)
        return candidate if candidate.exists() else None

    profile = _runtime_profile()
    if profile in {"prod", "production", "server", "release", "docker"}:
        candidates = (SERVER_ENV_FILE,)
    else:
        candidates = (LOCAL_ENV_FILE, SERVER_ENV_FILE)

    for name in candidates:
        candidate = _normalize_path(name)
        if candidate.exists():
            return candidate
    return None


@lru_cache(maxsize=1)
def load_backend_env(preferred: str | None = None) -> tuple[Path, ...]:
    env_files: tuple[Path, ...]
    if preferred:
        env_file = resolve_env_file(preferred)
        env_files = (env_file,) if env_file is not None else tuple()
    else:
        profile = _runtime_profile()
        if profile in {"prod", "production", "server", "release", "docker"}:
            candidates = (SERVER_ENV_FILE,)
        else:
            candidates = (SERVER_ENV_FILE, LOCAL_ENV_FILE)
        env_files = tuple(
            candidate
            for candidate in (_normalize_path(name) for name in candidates)
            if candidate.exists()
        )

    merged: dict[str, str] = {}
    for env_file in env_files:
        values = dotenv_values(env_file)
        for key, value in values.items():
            if not key or value is None:
                continue
            merged[key] = str(value)

    for key, value in merged.items():
        os.environ.setdefault(key, value)

    if env_files:
        os.environ.setdefault("DINGCHANG_ENV_FILE", str(env_files[-1]))
    return env_files


def env_file_for_settings(preferred: str | None = None) -> str | None:
    env_files = load_backend_env(preferred)
    if not env_files:
        return None
    return str(env_files[-1])
