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
from datetime import datetime, timedelta
from typing import Tuple

from .config import settings

# 格式：pbkdf2_sha256$<iterations>$<salt_b64>$<dk_b64>
_PBKDF2_PREFIX = "pbkdf2_sha256"

# 迭代次数：建议线上 >= 260000（可按机器性能调）
_PASSWORD_HASH_ITERATIONS = int(os.getenv("PASSWORD_HASH_ITERATIONS", "260000") or "260000")
_SALT_BYTES = int(os.getenv("PASSWORD_HASH_SALT_BYTES", "16") or "16")

# 可选 pepper：用 SECRET_KEY 作为额外“全局密钥”（不落库）
# 这样就算攻击者拿到数据库，也缺少 pepper 会更难爆破
# 注意：SECRET_KEY 必须在 prod 配好（你上一轮 config 已强制校验）
def _pepper() -> bytes:
    return (settings.SECRET_KEY or "").encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def _pbkdf2_hash(password: str, salt: bytes, iterations: int) -> bytes:
    # PBKDF2-HMAC-SHA256，dklen=32 足够
    return hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8") + _pepper(),
        salt,
        iterations,
        dklen=32,
    )


def hash_password(password: str) -> str:
    """
    生产可用密码哈希：
    pbkdf2_sha256$iterations$salt$hash
    """
    if password is None:
        password = ""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = _pbkdf2_hash(password, salt, _PASSWORD_HASH_ITERATIONS)
    return f"{_PBKDF2_PREFIX}${_PASSWORD_HASH_ITERATIONS}${_b64e(salt)}${_b64e(dk)}"


def _is_legacy_sha256(hashed: str) -> bool:
    # 旧版：纯 64 位 hex
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
    """
    校验密码：
    - 对空值做保护
    - 支持 pbkdf2 新格式
    - 兼容旧 sha256 hex（存量数据）
    - 恒时比较
    """
    if not plain or not hashed:
        return False

    # 新格式：pbkdf2
    if hashed.startswith(_PBKDF2_PREFIX + "$"):
        try:
            iterations, salt, dk = _parse_pbkdf2(hashed)
            calc = _pbkdf2_hash(plain, salt, iterations)
            return hmac.compare_digest(calc, dk)
        except Exception:
            return False

    # 旧格式：sha256(plain) hex
    if _is_legacy_sha256(hashed):
        calc_hex = hashlib.sha256(plain.encode("utf-8")).hexdigest()
        return hmac.compare_digest(calc_hex, hashed)

    return False


def generate_session_token() -> str:
    """
    生成会话 token
    """
    return secrets.token_urlsafe(32)


def get_expire_time() -> datetime:
    """
    计算会话过期时间（与现有逻辑保持一致：UTC naive）
    """
    return datetime.utcnow() + timedelta(seconds=settings.SESSION_TIMEOUT_SECONDS)
