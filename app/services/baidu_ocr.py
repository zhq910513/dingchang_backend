# app/services/baidu_ocr.py
# encoding: utf-8
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import settings


class OcrNotConfigured(RuntimeError):
    pass


TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
ENDPOINTS = {
    "idcard": "https://aip.baidubce.com/rest/2.0/ocr/v1/idcard",
    "vehicle_license": "https://aip.baidubce.com/rest/2.0/ocr/v1/vehicle_license",
    "vehicle_certificate": "https://aip.baidubce.com/rest/2.0/ocr/v1/vehicle_certificate",
}

_token_lock = threading.Lock()
_cached_token: Optional[str] = None
_cached_token_expires_at: float = 0.0

_TOKEN_SAFETY_SECONDS = 120
_TIMEOUT = (10, 60)


def _build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST", "GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_session = _build_session()


def _bool_str(v: bool) -> str:
    return "true" if v else "false"


def _require_config() -> tuple[str, str]:
    enabled = bool(getattr(settings, "BAIDU_OCR_ENABLED", False))
    api_key = (getattr(settings, "BAIDU_API_KEY", "") or "").strip()
    secret_key = (getattr(settings, "BAIDU_SECRET_KEY", "") or "").strip()
    if not enabled:
        raise OcrNotConfigured("百度 OCR 未启用：请设置 BAIDU_OCR_ENABLED=true")
    if not api_key or not secret_key:
        raise OcrNotConfigured("百度 OCR 未配置：请设置 BAIDU_API_KEY / BAIDU_SECRET_KEY")
    return api_key, secret_key


def _verify_param():
    try:
        import certifi  # type: ignore
        return certifi.where()
    except Exception:
        return True


def _get_access_token() -> str:
    global _cached_token, _cached_token_expires_at

    api_key, secret_key = _require_config()
    now = time.time()
    if _cached_token and (now + _TOKEN_SAFETY_SECONDS) < _cached_token_expires_at:
        return _cached_token

    with _token_lock:
        now = time.time()
        if _cached_token and (now + _TOKEN_SAFETY_SECONDS) < _cached_token_expires_at:
            return _cached_token

        params = {
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret_key,
        }

        resp = _session.post(TOKEN_URL, params=params, timeout=_TIMEOUT, verify=_verify_param())
        resp.raise_for_status()
        data = resp.json()

        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"获取百度 access_token 失败：{data}")

        expires_in = int(data.get("expires_in") or (29 * 24 * 3600))
        _cached_token = token
        _cached_token_expires_at = time.time() + expires_in
        return token


def _is_http_url(s: str) -> bool:
    try:
        u = urlparse(s)
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False


def call_ocr(
    api_type: str,
    image_url: str,
    side: Optional[str] = None,
    detect_direction: bool = True,
) -> Dict[str, Any]:
    """
    ✅ 当前方案：只允许百度通过 URL 抓取图片（BOS/公网可访问链接）。
    不再支持本地静态路径/base64 fallback（不兼容旧方案）。
    """
    if api_type not in ENDPOINTS:
        raise ValueError(f"未知 api_type：{api_type}，可选：{list(ENDPOINTS.keys())}")
    if not image_url:
        raise ValueError("image_url 不能为空")
    if not _is_http_url(image_url):
        raise ValueError("image_url 必须是 http/https 公网可访问 URL（当前方案不支持本地路径）")

    token = _get_access_token()
    payload: Dict[str, Any] = {"url": image_url}

    if api_type == "idcard":
        payload["id_card_side"] = side or "front"
    elif api_type == "vehicle_license":
        payload["vehicle_license_side"] = side or "front"
        payload["detect_direction"] = _bool_str(bool(detect_direction))

    url = f"{ENDPOINTS[api_type]}?access_token={token}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = _session.post(url, data=payload, headers=headers, timeout=_TIMEOUT, verify=_verify_param())
    resp.raise_for_status()
    return resp.json()
