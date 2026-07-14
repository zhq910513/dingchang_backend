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


class OcrCallError(RuntimeError):
    """百度 OCR 调用失败（含百度业务错误码）"""
    pass


TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"

# 百度 OCR 接口映射（统一入口）
ENDPOINTS = {
    "idcard": "https://aip.baidubce.com/rest/2.0/ocr/v1/idcard",
    "vehicle_license": "https://aip.baidubce.com/rest/2.0/ocr/v1/vehicle_license",
    "vehicle_certificate": "https://aip.baidubce.com/rest/2.0/ocr/v1/vehicle_certificate",
    # 模板接口失败或用户放错卡槽时使用，结果由业务侧做保守正则提取。
    "accurate_basic": "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic",
}

# token 相关（进程内缓存）
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


def _reset_token_cache() -> None:
    global _cached_token, _cached_token_expires_at
    with _token_lock:
        _cached_token = None
        _cached_token_expires_at = 0.0


def _get_access_token(force_refresh: bool = False) -> str:
    global _cached_token, _cached_token_expires_at

    api_key, secret_key = _require_config()
    now = time.time()

    if (not force_refresh) and _cached_token and (now + _TOKEN_SAFETY_SECONDS) < _cached_token_expires_at:
        return _cached_token

    with _token_lock:
        now = time.time()
        if (not force_refresh) and _cached_token and (now + _TOKEN_SAFETY_SECONDS) < _cached_token_expires_at:
            return _cached_token

        params = {
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret_key,
        }

        try:
            resp = _session.post(
                TOKEN_URL,
                params=params,
                timeout=_TIMEOUT,
                verify=_verify_param(),
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise OcrCallError(f"获取百度 access_token 网络异常：{e}") from e
        except Exception as e:
            raise OcrCallError(f"获取百度 access_token 响应解析失败：{e}") from e

        token = data.get("access_token")
        if not token:
            raise OcrCallError(f"获取百度 access_token 失败：{data}")

        expires_in = int(data.get("expires_in") or (29 * 24 * 3600))
        _cached_token = str(token)
        _cached_token_expires_at = time.time() + max(60, expires_in)
        return _cached_token


def _is_http_url(s: str) -> bool:
    try:
        u = urlparse(s)
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False


def _extract_baidu_error(data: Dict[str, Any]) -> tuple[int, str]:
    """
    百度 OCR 业务错误通常是：
    {"error_code": xxx, "error_msg": "..."}
    成功时通常没有 error_code
    """
    try:
        code = int(data.get("error_code") or 0)
    except Exception:
        code = 0
    msg = str(data.get("error_msg") or "").strip()
    return code, msg


def _call_ocr_once(
        *,
        api_type: str,
        image_url: str,
        side: Optional[str],
        detect_direction: bool,
        access_token: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"url": image_url}

    if api_type == "idcard":
        # 百度 idcard 接口一般使用 front/back
        payload["id_card_side"] = (side or "front").strip()
    elif api_type == "vehicle_license":
        # 你当前业务里行驶证 main/sub，可映射 front/back
        side_norm = (side or "front").strip().lower()
        if side_norm in ("main", "front"):
            payload["vehicle_license_side"] = "front"
        elif side_norm in ("sub", "back"):
            payload["vehicle_license_side"] = "back"
        else:
            payload["vehicle_license_side"] = "front"
        payload["detect_direction"] = _bool_str(bool(detect_direction))
    elif api_type == "vehicle_certificate":
        # 百度 vehicle_certificate 无 side 参数
        pass
    elif api_type == "accurate_basic":
        payload["detect_direction"] = _bool_str(bool(detect_direction))

    url = f"{ENDPOINTS[api_type]}?access_token={access_token}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        resp = _session.post(
            url,
            data=payload,
            headers=headers,
            timeout=_TIMEOUT,
            verify=_verify_param(),
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise OcrCallError(f"百度 OCR 响应不是 JSON 对象：{type(data).__name__}")
        return data
    except requests.RequestException as e:
        raise OcrCallError(f"百度 OCR 网络异常（{api_type}）：{e}") from e
    except ValueError as e:
        raise OcrCallError(f"百度 OCR JSON 解析失败（{api_type}）：{e}") from e


def call_ocr(
        api_type: str,
        image_url: str,
        side: Optional[str] = None,
        detect_direction: bool = True,
) -> Dict[str, Any]:
    """
    ✅ 当前方案：只允许百度通过 URL 抓取图片（BOS/公网可访问链接）
    - 不支持本地路径/base64（按你当前方案统一）
    - 自动处理 token 缓存
    - token 失效类错误会自动重试 1 次
    - 百度业务错误会抛 OcrCallError（避免把失败结果当成功写缓存）
    """
    api_type = (api_type or "").strip()
    if api_type not in ENDPOINTS:
        raise ValueError(f"未知 api_type：{api_type}，可选：{list(ENDPOINTS.keys())}")

    image_url = (image_url or "").strip()
    if not image_url:
        raise ValueError("image_url 不能为空")
    if not _is_http_url(image_url):
        raise ValueError("image_url 必须是 http/https 公网可访问 URL（当前方案不支持本地路径）")

    # 第一次调用
    token = _get_access_token(force_refresh=False)
    data = _call_ocr_once(
        api_type=api_type,
        image_url=image_url,
        side=side,
        detect_direction=detect_direction,
        access_token=token,
    )

    code, msg = _extract_baidu_error(data)

    # token 失效/过期类错误：重刷 token 后重试一次
    # 常见：110/111（以百度实际返回为准，做宽松兜底）
    if code in (110, 111) or ("Access token" in msg) or ("access token" in msg):
        _reset_token_cache()
        token = _get_access_token(force_refresh=True)
        data = _call_ocr_once(
            api_type=api_type,
            image_url=image_url,
            side=side,
            detect_direction=detect_direction,
            access_token=token,
        )
        code, msg = _extract_baidu_error(data)

    # 百度业务错误：抛异常（调用方决定写 task failed / 不写缓存）
    if code:
        raise OcrCallError(f"百度 OCR 业务失败（{api_type}）error_code={code} error_msg={msg or 'unknown'}")

    return data
