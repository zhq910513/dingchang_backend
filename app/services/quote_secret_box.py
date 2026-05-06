# encoding: utf-8
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Any, Dict, Optional

from app.core.config import settings

_PREFIX = "qsbox1"
_SALT = b"dingchang.quote.platform.account.v1"
_ITERATIONS = int(os.getenv("QUOTE_SECRET_BOX_ITERATIONS", "200000") or "200000")
_NONCE_BYTES = 16


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii"))


def _master_key() -> bytes:
    secret = (settings.SECRET_KEY or "dingchang-local-dev-secret").encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", secret, _SALT, _ITERATIONS, dklen=32)


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        counter += 1
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
    return bytes(out[:length])


def _xor(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def encrypt_text(value: Optional[str], *, aad: str = "") -> Optional[str]:
    if value is None:
        return None
    raw = str(value).encode("utf-8")
    key = _master_key()
    nonce = secrets.token_bytes(_NONCE_BYTES)
    cipher = _xor(raw, _stream(key, nonce, len(raw)))
    aad_raw = (aad or "").encode("utf-8")
    tag = hmac.new(key, b"tag\0" + aad_raw + nonce + cipher, hashlib.sha256).digest()
    return f"{_PREFIX}${_b64e(nonce)}${_b64e(cipher)}${_b64e(tag)}"


def decrypt_text(token: Optional[str], *, aad: str = "") -> Optional[str]:
    if not token:
        return None
    parts = str(token).split("$")
    if len(parts) != 4 or parts[0] != _PREFIX:
        raise ValueError("invalid encrypted secret format")
    key = _master_key()
    nonce = _b64d(parts[1])
    cipher = _b64d(parts[2])
    tag = _b64d(parts[3])
    aad_raw = (aad or "").encode("utf-8")
    expected = hmac.new(key, b"tag\0" + aad_raw + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("encrypted secret authentication failed")
    return _xor(cipher, _stream(key, nonce, len(cipher))).decode("utf-8")


def encrypt_json(value: Optional[Dict[str, Any]], *, aad: str = "") -> Optional[str]:
    if value is None:
        return None
    return encrypt_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), aad=aad)


def decrypt_json(token: Optional[str], *, aad: str = "") -> Optional[Dict[str, Any]]:
    text = decrypt_text(token, aad=aad)
    if not text:
        return None
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("encrypted secret payload must be an object")
    return obj
