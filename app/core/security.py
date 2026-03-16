# encoding: utf-8
"""
安全相关工具（生产可用版本）

- 密码哈希：PBKDF2-HMAC-SHA256（标准库实现，带随机盐与高迭代次数）
- 兼容旧版 sha256(plain) 哈希（避免升级导致存量用户无法登录）
- verify 使用恒时比较，降低时序侧信道风险
- 会话 token 使用 secrets.token_urlsafe(32)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Tuple

from .config import settings

_PBKDF2_PREFIX = "pbkdf2_sha256"
_PASSWORD_HASH_ITERATIONS = int(os.getenv("PASSWORD_HASH_ITERATIONS", "260000") or "260000")
_SALT_BYTES = int(os.getenv("PASSWORD_HASH_SALT_BYTES", "16") or "16")


def _pepper() -> bytes:
    return (settings.SECRET_KEY or "").encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def _pbkdf2_hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8") + _pepper(),
        salt,
        iterations,
        dklen=32,
    )


def hash_password(password: str) -> str:
    if password is None:
      password = ""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = _pbkdf2_hash(password, salt, _PASSWORD_HASH_ITERATIONS)
    return f"{_PBKDF2_PREFIX}${_PASSWORD_HASH_ITERATIONS}${_b64e(salt)}${_b64e(dk)}"


def _is_legacy_sha256(hashed: str) -> bool:
    if not hashed or len(hashed) != 64:
        return False
    try:
        int(hashed, 16)
        return True
    except Exception:
        return False


def _parse_pbkdf2(hashed: str) -> Tuple[int, bytes, bytes]:
    parts = (hashed or "").split("$")
    if len(parts) != 4 or parts[0] != _PBKDF2_PREFIX:
        raise ValueError("Invalid password hash format.")
    iterations = int(parts[1])
    salt = _b64d(parts[2])
    dk = _b64d(parts[3])
    return iterations, salt, dk


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False

    if hashed.startswith(_PBKDF2_PREFIX + "$"):
        try:
            iterations, salt, dk = _parse_pbkdf2(hashed)
            calc = _pbkdf2_hash(plain, salt, iterations)
            return hmac.compare_digest(calc, dk)
        except Exception:
            return False

    if _is_legacy_sha256(hashed):
        calc_hex = hashlib.sha256(plain.encode("utf-8")).hexdigest()
        return hmac.compare_digest(calc_hex, hashed)

    return False


def needs_password_rehash(hashed: str) -> bool:
    if not hashed:
        return True

    if _is_legacy_sha256(hashed):
        return True

    if not hashed.startswith(_PBKDF2_PREFIX + "$"):
        return True

    try:
        iterations, salt, dk = _parse_pbkdf2(hashed)
        if iterations < _PASSWORD_HASH_ITERATIONS:
            return True
        if len(salt) < _SALT_BYTES:
            return True
        if len(dk) != 32:
            return True
        return False
    except Exception:
        return True


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)