# app/services/bce_sts.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import settings
from app.services.bce_auth import sign_bce_auth_v1


class TLS12HttpAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "TLSVersion"):
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        pool_kwargs["ssl_context"] = ctx
        return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


def _build_session() -> requests.Session:
    s = requests.Session()

    # HTTPS 连接池 + TLS1.2+
    https_adapter = TLS12HttpAdapter(
        max_retries=Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["POST", "GET"]),
            raise_on_status=False,
        ),
        pool_connections=20,
        pool_maxsize=20,
    )
    s.mount("https://", https_adapter)

    # HTTP（一般不用，但保留）
    http_adapter = HTTPAdapter(
        max_retries=Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["POST", "GET"]),
            raise_on_status=False,
        ),
        pool_connections=20,
        pool_maxsize=20,
    )
    s.mount("http://", http_adapter)

    # ✅ 避免系统代理干扰签名请求
    s.trust_env = False
    return s


_session = _build_session()
_lock = threading.Lock()


@dataclass
class StsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: str
    expires_at_epoch: float


class BceStsService:
    """
    用长期 AK/SK 去 STS AssumeRole，缓存临时凭证。
    - 前端直传 BOS：使用临时凭证签名
    """

    def __init__(self) -> None:
        self._cached: Optional[StsCredentials] = None
        self._safety_seconds = 120

    def _require_config(self) -> tuple[str, str, str, str, str]:
        ak = (getattr(settings, "BOS_STS_ACCESS_KEY", "") or "").strip()
        sk = (getattr(settings, "BOS_STS_SECRET_KEY", "") or "").strip()
        account_id = (getattr(settings, "BOS_STS_ACCOUNT_ID", "") or "").strip()
        role_name = (getattr(settings, "BOS_STS_ROLE_NAME", "") or "").strip()
        sts_host = (getattr(settings, "BOS_STS_HOST", "") or "sts.bj.baidubce.com").strip()

        if not (ak and sk and account_id and role_name):
            raise RuntimeError(
                "STS 未配置：请设置 BOS_STS_ACCESS_KEY / BOS_STS_SECRET_KEY / BOS_STS_ACCOUNT_ID / BOS_STS_ROLE_NAME"
            )
        return ak, sk, account_id, role_name, sts_host

    def _infer_region(self) -> str:
        """
        优先 BOS_REGION；如果没配，则从 BOS_VHOST 推断：
        例如: dingchang.fwh.bcebos.com -> fwh
        """
        region = (getattr(settings, "BOS_REGION", "") or "").strip()
        if region:
            return region

        bucket = (getattr(settings, "BOS_BUCKET", "") or "").strip().lower()
        vhost = (getattr(settings, "BOS_VHOST", "") or "").strip().lower()
        parts = [p for p in vhost.split(".") if p]
        if bucket and len(parts) >= 4 and parts[0] == bucket:
            return parts[1]

        raise RuntimeError(
            f"无法确定 BOS region：请配置 BOS_REGION，或确保 BOS_VHOST 形如 {bucket}.<region>.bcebos.com"
        )

    def _build_access_control_list(self) -> Optional[list[dict]]:
        """
        给临时凭证绑定 BOS 权限（session policy）
        """
        bucket = (getattr(settings, "BOS_BUCKET", "") or "").strip()
        if not bucket:
            return None

        region = self._infer_region()

        allowed_prefixes = ("cert", "idcard", "dl", "backup")
        return [
            {
                "service": "bce:bos",
                "region": region,
                "effect": "Allow",
                "resource": [
                    f"{bucket}/{prefix}/*"
                    for prefix in allowed_prefixes
                ],
                "permission": ["READ", "WRITE"],
            }
        ]

    def get_credentials(self, duration_seconds: Optional[int] = None, force_refresh: bool = False) -> StsCredentials:
        now = time.time()
        if (not force_refresh) and self._cached and (now + self._safety_seconds) < self._cached.expires_at_epoch:
            return self._cached

        with _lock:
            now = time.time()
            if (not force_refresh) and self._cached and (now + self._safety_seconds) < self._cached.expires_at_epoch:
                return self._cached

            ak, sk, account_id, role_name, sts_host = self._require_config()
            dur = int(duration_seconds or 900)

            path = "/v1/credential"
            endpoint = f"https://{sts_host}{path}"
            params = {
                "assumeRole": "",  # canonical_query 会签成 "assumeRole"
                "accountId": account_id,
                "roleName": role_name,
                "durationSeconds": str(dur),
            }

            x_bce_date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            acl = self._build_access_control_list()
            body_obj: dict[str, Any] = {}
            if acl:
                body_obj["accessControlList"] = acl

            body_bytes = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

            headers_to_sign = {
                "host": sts_host,
                "content-type": "application/json",
                "content-length": str(len(body_bytes)),
                "x-bce-date": x_bce_date,
            }
            signed_headers = {"host", "content-type", "content-length", "x-bce-date"}

            authorization = sign_bce_auth_v1(
                method="POST",
                path=path,
                query_params=params,
                headers_to_sign=headers_to_sign,
                signed_headers=signed_headers,
                access_key_id=ak,
                secret_access_key=sk,
                timestamp=x_bce_date,
                auth_expire_seconds=1800,
            )

            headers = {
                "Host": sts_host,
                "Content-Type": "application/json",
                "Content-Length": str(len(body_bytes)),
                "x-bce-date": x_bce_date,
                "Authorization": authorization,
            }

            resp = _session.post(endpoint, params=params, headers=headers, data=body_bytes, timeout=20)
            if resp.status_code >= 400:
                raise RuntimeError(f"STS AssumeRole 失败: HTTP {resp.status_code} {resp.text}")

            data: Dict[str, Any] = resp.json()
            access_key_id = data.get("accessKeyId") or ""
            secret_access_key = data.get("secretAccessKey") or ""
            session_token = data.get("sessionToken") or ""
            expiration = data.get("expiration") or ""

            if not (access_key_id and secret_access_key and session_token and expiration):
                raise RuntimeError(f"STS 返回字段不完整：{data}")

            # 优先用 STS 返回 expiration
            expires_at_epoch = time.time() + dur
            try:
                # 格式如 2026-02-22T10:20:30Z
                import datetime as _dt
                dt = _dt.datetime.strptime(expiration, "%Y-%m-%dT%H:%M:%SZ")
                expires_at_epoch = dt.replace(tzinfo=_dt.timezone.utc).timestamp()
            except Exception:
                pass

            cred = StsCredentials(
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                session_token=session_token,
                expiration=expiration,
                expires_at_epoch=expires_at_epoch,
            )
            self._cached = cred
            return cred


# 单例（给 storage.py 或其他模块复用）
bce_sts_service = BceStsService()
