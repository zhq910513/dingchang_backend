# app/services/bce_auth.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote


def uri_encode(s: str, *, encode_slash: bool = True) -> str:
    safe = "-_.~"
    if not encode_slash:
        safe += "/"
    return quote(s, safe=safe)


def canonical_query(params: Dict[str, Any]) -> str:
    """
    BCE canonical query:
    - key/value 都要 RFC3986 编码
    - 排序
    - 跳过 authorization
    - 关键修复：空值参数（如 assumeRole）必须编码为 `assumeRole`，不能写成 `assumeRole=`
    """
    if not params:
        return ""

    items = []
    for k, v in params.items():
        if str(k).lower() == "authorization":
            continue

        k_enc = uri_encode(str(k))
        if v is None or str(v) == "":
            items.append(k_enc)
        else:
            v_enc = uri_encode(str(v))
            items.append(f"{k_enc}={v_enc}")

    items.sort()
    return "&".join(items)


def canonical_headers(headers: Dict[str, Any], headers_to_sign: Optional[set[str]] = None) -> Tuple[str, str]:
    """
    返回 (canonical_headers, signed_headers_str)
    """
    normalized: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        nk = str(k).strip().lower()
        nv = str(v).strip()
        if nv:
            normalized[nk] = nv

    if not headers_to_sign:
        headers_to_sign = {"host"}
        for k in normalized.keys():
            if k.startswith("x-bce-"):
                headers_to_sign.add(k)

    signed = sorted({k.strip().lower() for k in headers_to_sign if k})
    lines = []
    for k in signed:
        if k in normalized:
            lines.append(f"{uri_encode(k)}:{uri_encode(normalized[k])}")

    return "\n".join(lines), ";".join(signed)


def sign_bce_auth_v1(
    *,
    method: str,
    path: str,
    query_params: Dict[str, Any],
    headers_to_sign: Dict[str, Any],
    signed_headers: Optional[set[str]],
    access_key_id: str,
    secret_access_key: str,
    timestamp: str,
    auth_expire_seconds: int = 1800,
) -> str:
    """
    生成 bce-auth-v1 签名字符串（可用于 Header Authorization 或 QueryString authorization）

    ✅ 关键修复：
    signing_key 必须使用 HMAC(...).digest() 的原始字节，不能用 hexdigest 字符串。
    """
    auth_string_prefix = f"bce-auth-v1/{access_key_id}/{timestamp}/{int(auth_expire_seconds)}"

    # signing_key = HMAC-SHA256(SK, auth_string_prefix) -> raw bytes
    signing_key = hmac.new(
        secret_access_key.encode("utf-8"),
        auth_string_prefix.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    canonical_uri = uri_encode(path, encode_slash=False)
    cq = canonical_query(query_params)
    ch, signed_headers_str = canonical_headers(headers_to_sign, signed_headers)

    canonical_request = f"{method.upper()}\n{canonical_uri}\n{cq}\n{ch}"

    signature = hmac.new(
        signing_key,
        canonical_request.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if signed_headers_str:
        return f"{auth_string_prefix}/{signed_headers_str}/{signature}"
    return f"{auth_string_prefix}//{signature}"
