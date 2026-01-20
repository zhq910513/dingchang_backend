# app/services/storage.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, Union, IO
from urllib.parse import quote, urlencode

import requests

from app.core.config import settings


@dataclass
class StsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: str  # ISO string from STS


class StorageService:
    """
    方案B1：STS + 前端直传 + MD5 key
    - 后端：发放 STS、生成短期 GET 签名 URL（供 OCR/展示）
    - 前端：用 STS 做 HEAD/PUT（去重 + 上传）

    ✅ 重要：slot_key（业务语义）与 BOS 目录前缀（物理存储）解耦
    """

    # ✅ slot_key -> bos prefix（你确认的最终规则：不做兼容）
    SLOT_PREFIX_MAP: Dict[str, str] = {
        "vehicle_cert": "cert",
        "idcard_front": "idcard",
        "idcard_back": "idcard",
        "driving_license_main": "dl",
        "driving_license_sub": "dl",
        "related": "backup",
    }

    def __init__(self) -> None:
        self.enabled: bool = bool(getattr(settings, "BOS_ENABLED", False))
        self.bucket: str = (getattr(settings, "BOS_BUCKET", "") or "").strip()
        self.vhost: str = (getattr(settings, "BOS_VHOST", "") or "").strip()
        self.endpoint_host: str = (getattr(settings, "BOS_ENDPOINT_HOST", "") or "").strip()
        self.base_url: str = (getattr(settings, "BOS_BASE_URL", "") or "").strip()

        self.sts_account_id: str = (getattr(settings, "BOS_STS_ACCOUNT_ID", "") or "").strip()
        self.sts_role_name: str = (getattr(settings, "BOS_STS_ROLE_NAME", "") or "").strip()
        self.sts_ak: str = (getattr(settings, "BOS_STS_ACCESS_KEY", "") or "").strip()
        self.sts_sk: str = (getattr(settings, "BOS_STS_SECRET_KEY", "") or "").strip()
        self.sts_host: str = (getattr(settings, "BOS_STS_HOST", "") or "sts.bj.baidubce.com").strip()

        # ✅ 展示用 URL 默认是否签名（从 Settings 读取，支持 .env）
        self.signed_get_url_enabled: bool = bool(getattr(settings, "BOS_SIGNED_GET_URL", True))

        self._sts_lock = threading.Lock()
        self._cached_sts: Optional[StsCredentials] = None
        self._cached_sts_expire_at_ts: float = 0.0  # unix seconds

        self._http = requests.Session()
        self._http.trust_env = False

    # -----------------------------
    # slot_key -> prefix
    # -----------------------------
    def _prefix_from_slot(self, slot_key: str) -> str:
        k = (slot_key or "").strip()
        p = self.SLOT_PREFIX_MAP.get(k, "")
        if not p:
            raise ValueError(f"未知 slot_key: {slot_key!r}")
        return p

    # -----------------------------
    # B1 key 规则（✅ 使用 prefix，而不是 slot_key）
    # -----------------------------
    def build_key_by_md5(self, *, scene: str, md5_hex: str, ext: str) -> str:
        """
        key = {prefix}/{md5[0:2]}/{md5[2:4]}/{md5}{ext}
        ext 形如 ".jpg" ".png"
        scene 传 slot_key（业务语义）
        """
        slot_key = (scene or "").strip()
        md5_hex = (md5_hex or "").strip().lower()
        ext = (ext or "").strip().lower()

        prefix = self._prefix_from_slot(slot_key)

        if len(md5_hex) != 32 or any(c not in "0123456789abcdef" for c in md5_hex):
            raise ValueError("md5_hex 必须是 32 位十六进制")
        if not ext.startswith("."):
            ext = "." + ext if ext else ""
        if not ext:
            ext = ".bin"

        return f"{prefix}/{md5_hex[:2]}/{md5_hex[2:4]}/{md5_hex}{ext}"

    def validate_b1_key(self, *, scene: str, storage_key: str, md5_hex: str) -> bool:
        """
        校验 storage_key 是否符合 B1 规则且属于对应 slot_key（通过映射前缀判定）。
        """
        slot_key = (scene or "").strip()
        k = (storage_key or "").lstrip("/").strip()
        md5_hex = (md5_hex or "").strip().lower()

        if not slot_key or not k or len(md5_hex) != 32:
            return False

        try:
            prefix = self._prefix_from_slot(slot_key)
        except Exception:
            return False

        if not k.startswith(prefix + "/"):
            return False

        parts = k.split("/")
        # prefix/ab/cd/md5.ext => 至少 4 段
        if len(parts) < 4:
            return False
        if parts[0] != prefix:
            return False
        if parts[1] != md5_hex[:2] or parts[2] != md5_hex[2:4]:
            return False

        name = parts[3]
        return name.startswith(md5_hex)

    # -----------------------------
    # STS AssumeRole（缓存）
    # -----------------------------
    @staticmethod
    def _utc_timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _uri_encode(s: str, *, encode_slash: bool = True) -> str:
        safe = "-_.~"
        if not encode_slash:
            safe += "/"
        return quote(s, safe=safe)

    @classmethod
    def _canonical_query(cls, params: Dict[str, Any]) -> str:
        if not params:
            return ""
        items = []
        for k, v in params.items():
            if str(k).lower() == "authorization":
                continue
            k_enc = cls._uri_encode(str(k))
            v_enc = cls._uri_encode("" if v is None else str(v))
            items.append(f"{k_enc}={v_enc}")
        items.sort()
        return "&".join(items)

    @classmethod
    def _canonical_headers(cls, headers: Dict[str, str], signed_headers: list[str]) -> str:
        lines = []
        for h in signed_headers:
            key = h.strip().lower()
            val = (headers.get(key) or "").strip()
            if not val:
                continue
            lines.append(f"{cls._uri_encode(key)}:{cls._uri_encode(val)}")
        lines.sort()
        return "\n".join(lines)

    @classmethod
    def _sign_bce_auth_v1(
        cls,
        *,
        access_key_id: str,
        secret_access_key: str,
        method: str,
        path: str,
        query_params: Dict[str, Any],
        headers: Dict[str, str],
        signed_headers: list[str],
        timestamp: str,
        auth_expire_seconds: int = 1800,
    ) -> str:
        auth_prefix = f"bce-auth-v1/{access_key_id}/{timestamp}/{int(auth_expire_seconds)}"
        signing_key_hex = hmac.new(
            secret_access_key.encode("utf-8"),
            auth_prefix.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        canonical_uri = cls._uri_encode(path, encode_slash=False)
        canonical_qs = cls._canonical_query(query_params)
        canonical_hdrs = cls._canonical_headers(headers, signed_headers)

        canonical_request = "\n".join([method.upper(), canonical_uri, canonical_qs, canonical_hdrs])
        signature = hmac.new(
            signing_key_hex.encode("utf-8"),
            canonical_request.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        signed_headers_str = ";".join([h.lower() for h in sorted(set(signed_headers))])
        return f"{auth_prefix}/{signed_headers_str}/{signature}"

    def _require_bos(self) -> None:
        if not self.enabled:
            raise RuntimeError("BOS 未启用（BOS_ENABLED=false）")
        if not self.bucket:
            raise RuntimeError("BOS_BUCKET 未配置")
        if not self.vhost:
            raise RuntimeError("BOS_VHOST 未配置（例如 dingchang.fwh.bcebos.com）")
        if not (self.sts_account_id and self.sts_role_name and self.sts_ak and self.sts_sk):
            raise RuntimeError(
                "STS 配置不完整：BOS_STS_ACCOUNT_ID / BOS_STS_ROLE_NAME / BOS_STS_ACCESS_KEY / BOS_STS_SECRET_KEY"
            )

    # -----------------------------
    # ✅ 推断 region + 构建 STS ACL
    # -----------------------------
    def _infer_region_from_vhost(self) -> str:
        """
        vhost 通常形如: {bucket}.{region}.bcebos.com
        例如: dingchang.fwh.bcebos.com -> region = fwh
        """
        v = (self.vhost or "").strip().lower()
        parts = [p for p in v.split(".") if p]
        if len(parts) >= 4 and parts[0] == (self.bucket or "").strip().lower():
            return parts[1]
        raise RuntimeError(
            f"无法从 BOS_VHOST 推断 region：vhost={self.vhost!r} bucket={self.bucket!r}。"
            f"请确保 vhost 形如 {self.bucket}.<region>.bcebos.com"
        )

    def _build_sts_access_control_list(self) -> list[dict]:
        """
        给临时凭证绑定 BOS 权限（session policy）。
        """
        region = self._infer_region_from_vhost()
        b = self.bucket
        return [
            {
                "service": "bce:bos",
                "region": region,
                "effect": "Allow",
                "resource": [
                    b,
                    f"{b}/*",
                ],
                "permission": ["READ", "LIST", "WRITE"],
            }
        ]

    def assume_role(self, *, duration_seconds: int = 900, force_refresh: bool = False) -> StsCredentials:
        self._require_bos()

        now = time.time()
        if (not force_refresh) and self._cached_sts and (now + 120) < self._cached_sts_expire_at_ts:
            return self._cached_sts

        with self._sts_lock:
            now = time.time()
            if (not force_refresh) and self._cached_sts and (now + 120) < self._cached_sts_expire_at_ts:
                return self._cached_sts

            path = "/v1/credential"
            url = f"https://{self.sts_host}{path}"
            params = {
                "assumeRole": "",
                "accountId": self.sts_account_id,
                "roleName": self.sts_role_name,
                "durationSeconds": str(int(duration_seconds)),
            }

            body_obj = {"accessControlList": self._build_sts_access_control_list()}
            body_bytes = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            content_length = str(len(body_bytes))

            x_bce_date = self._utc_timestamp()
            headers_for_sign = {
                "host": self.sts_host,
                "content-type": "application/json",
                "content-length": content_length,
                "x-bce-date": x_bce_date,
            }
            signed_headers = ["host", "content-type", "content-length", "x-bce-date"]

            authorization = self._sign_bce_auth_v1(
                access_key_id=self.sts_ak,
                secret_access_key=self.sts_sk,
                method="POST",
                path=path,
                query_params=params,
                headers=headers_for_sign,
                signed_headers=signed_headers,
                timestamp=x_bce_date,
                auth_expire_seconds=1800,
            )

            headers = {
                "Content-Type": "application/json",
                "Content-Length": content_length,
                "x-bce-date": x_bce_date,
                "Authorization": authorization,
            }

            resp = self._http.post(url, params=params, headers=headers, data=body_bytes, timeout=20)
            if resp.status_code >= 400:
                raise RuntimeError(f"STS AssumeRole 失败: HTTP {resp.status_code} {resp.text}")

            data = resp.json()
            cred = StsCredentials(
                access_key_id=data["accessKeyId"],
                secret_access_key=data["secretAccessKey"],
                session_token=data["sessionToken"],
                expiration=data.get("expiration", ""),
            )

            expire_ts = now + duration_seconds
            try:
                dt = datetime.strptime(cred.expiration, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                expire_ts = dt.timestamp()
            except Exception:
                pass

            self._cached_sts = cred
            self._cached_sts_expire_at_ts = expire_ts
            return cred

    # -----------------------------
    # ✅ BOS 服务端签名请求（HEAD/PUT）——供后端代传/去重使用
    # -----------------------------
    def _bos_url(self, storage_key: str) -> Tuple[str, str]:
        """
        返回 (url, path)
        """
        self._require_bos()
        k = (storage_key or "").lstrip("/").strip()
        if not k:
            raise ValueError("storage_key 不能为空")
        path = "/" + k
        url = f"https://{self.vhost}{path}"
        return url, path

    def _bos_signed_headers(
        self,
        *,
        cred: StsCredentials,
        method: str,
        path: str,
        content_type: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        auth_expire_seconds: int = 1800,
    ) -> Dict[str, str]:
        """
        构造 BOS 请求头（Authorization + x-bce-date + x-bce-security-token + 可选 Content-Type）
        - token 走 header（用于服务端请求）
        - query 为空（canonical_query = ""）
        """
        host = self.vhost
        ts = self._utc_timestamp()

        # 注意：签名参与的 headers 用“全小写 key”
        headers_for_sign: Dict[str, str] = {
            "host": host,
            "x-bce-date": ts,
            "x-bce-security-token": cred.session_token,
        }
        signed_headers = ["host", "x-bce-date", "x-bce-security-token"]

        ct = (content_type or "").strip()
        if ct:
            headers_for_sign["content-type"] = ct
            signed_headers.append("content-type")

        if extra_headers:
            for k, v in extra_headers.items():
                kk = (k or "").strip().lower()
                if not kk:
                    continue
                vv = (v or "").strip()
                if vv:
                    headers_for_sign[kk] = vv
                    signed_headers.append(kk)

        auth = self._sign_bce_auth_v1(
            access_key_id=cred.access_key_id,
            secret_access_key=cred.secret_access_key,
            method=method.upper(),
            path=path,
            query_params={},  # ✅ 服务端请求不走 query
            headers=headers_for_sign,
            signed_headers=signed_headers,
            timestamp=ts,
            auth_expire_seconds=int(auth_expire_seconds),
        )

        # 发送给 BOS 的 headers（大小写无所谓，requests 会处理）
        out = {
            "Authorization": auth,
            "x-bce-date": ts,
            "x-bce-security-token": cred.session_token,
        }
        if ct:
            out["Content-Type"] = ct
        if extra_headers:
            for k, v in extra_headers.items():
                kk = (k or "").strip()
                vv = (v or "").strip()
                if kk and vv:
                    out[kk] = vv
        return out

    def head_object(
        self,
        storage_key: str,
        *,
        timeout: Tuple[int, int] = (10, 60),
        auth_expire_seconds: int = 1800,
    ) -> Tuple[bool, str]:
        """
        服务端 HEAD：用于判断对象是否存在 + 读取 ETag
        返回 (exists, etag)
        """
        url, path = self._bos_url(storage_key)
        cred = self.assume_role(duration_seconds=900)

        headers = self._bos_signed_headers(
            cred=cred,
            method="HEAD",
            path=path,
            content_type=None,
            extra_headers=None,
            auth_expire_seconds=int(auth_expire_seconds),
        )

        r = self._http.request("HEAD", url, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return True, (r.headers.get("ETag") or "")
        if r.status_code == 404:
            return False, ""
        rid = r.headers.get("x-bce-request-id") or ""
        dbg = r.headers.get("x-bce-debug-id") or ""
        raise RuntimeError(f"BOS HEAD failed: status={r.status_code} request_id={rid} debug_id={dbg}")

    def put_object(
        self,
        storage_key: str,
        *,
        data: Union[bytes, IO[bytes], Any],
        content_type: str,
        timeout: Tuple[int, int] = (10, 180),
        auth_expire_seconds: int = 1800,
    ) -> str:
        """
        服务端 PUT：上传对象
        返回 etag
        """
        url, path = self._bos_url(storage_key)
        cred = self.assume_role(duration_seconds=900)

        ct = (content_type or "application/octet-stream").strip()
        headers = self._bos_signed_headers(
            cred=cred,
            method="PUT",
            path=path,
            content_type=ct,
            extra_headers=None,
            auth_expire_seconds=int(auth_expire_seconds),
        )

        r = self._http.request("PUT", url, headers=headers, data=data, timeout=timeout)
        if 200 <= r.status_code < 300:
            return (r.headers.get("ETag") or "")
        rid = r.headers.get("x-bce-request-id") or ""
        dbg = r.headers.get("x-bce-debug-id") or ""
        body = ""
        try:
            body = r.text or ""
        except Exception:
            body = ""
        raise RuntimeError(
            f"BOS PUT failed: status={r.status_code} request_id={rid} debug_id={dbg} body={body[:300]}"
        )

    # -----------------------------
    # GET URL（直链 / 签名链）
    # -----------------------------
    def object_public_url(self, storage_key: str) -> str:
        k = (storage_key or "").lstrip("/")
        if self.base_url:
            return self.base_url.rstrip("/") + "/" + k
        return f"https://{self.vhost}/{k}"

    def generate_signed_get_url(self, *, storage_key: str, expires_in: int = 900) -> str:
        """
        生成 querystring authorization 的 GET URL（可被浏览器/第三方直接 GET）。

        ✅ 关键点：
        - STS 的 session_token 以 query 参数 x-bce-security-token 形式传递
        - x-bce-security-token 必须参与 canonical query 的签名计算
        - signed headers 只签 host，避免依赖请求方必须带额外 header
        """
        self._require_bos()
        k = (storage_key or "").lstrip("/").strip()
        if not k:
            raise ValueError("storage_key 不能为空")

        cred = self.assume_role(duration_seconds=900)

        host = self.vhost
        path = "/" + k

        timestamp = self._utc_timestamp()

        query_params_for_sign = {
            "x-bce-security-token": cred.session_token,
        }

        headers_for_sign = {
            "host": host,
        }
        signed_headers = ["host"]

        auth = self._sign_bce_auth_v1(
            access_key_id=cred.access_key_id,
            secret_access_key=cred.secret_access_key,
            method="GET",
            path=path,
            query_params=query_params_for_sign,
            headers=headers_for_sign,
            signed_headers=signed_headers,
            timestamp=timestamp,
            auth_expire_seconds=int(expires_in),
        )

        q = {
            "authorization": auth,
            "x-bce-security-token": cred.session_token,
        }
        return f"https://{host}{path}?{urlencode(q)}"

    # ✅ 统一出口（展示用/返回给前端用）
    def object_url_for_display(self, storage_key: str, *, signed: Optional[bool] = None, expires_in: int = 900) -> str:
        """
        返回“用于展示/接口返回”的 URL：
        - signed=None：跟随 settings.BOS_SIGNED_GET_URL（支持 .env）
        - signed=True：返回签名 URL（失败则降级直链）
        - signed=False：返回直链
        """
        if signed is None:
            signed = bool(self.signed_get_url_enabled)

        if not signed:
            return self.object_public_url(storage_key)

        try:
            return self.generate_signed_get_url(storage_key=storage_key, expires_in=int(expires_in))
        except Exception:
            return self.object_public_url(storage_key)
