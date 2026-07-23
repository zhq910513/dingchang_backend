# encoding: utf-8
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Any, Mapping, Optional
from urllib.parse import unquote, urljoin, urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.quote_platforms.platforms.picc.base import (
    KEEPALIVE_PATH,
    KEEPALIVE_PARAMS,
    PiccConfigError,
    PiccProtocolClient,
    PiccProtocolConfig,
    PiccProtocolError,
    get_cookie_value,
    set_cookie_value,
    snapshot_from_context,
)
from app.services.quote_platforms.platforms.picc.business import PiccBusinessAdapter
from app.services.quote_platforms.session_models import AccountSessionSnapshot, iso_now


class PiccLoginError(PiccProtocolError):
    pass


class PiccCaptchaError(PiccLoginError):
    pass


def _is_slide_verification_error(value: Any) -> bool:
    text = str(value or "")
    return "滑动图片验证码校验失败" in text or "滑块" in text and "校验失败" in text


def _is_restartable_login_error(value: Any) -> bool:
    text = str(value or "")
    if not text:
        return False
    if "滑块验证码识别未配置" in text or "平台账号不能为空" in text or "平台密码不能为空" in text:
        return False
    return bool(
        _is_slide_verification_error(text)
        or re.search(r"滑块.{0,20}(失败|失效|重新|过期)", text)
        or re.search(r"登录(上下文|会话).{0,20}(过期|失效|不存在|缺少)", text)
        or re.search(r"正式登录缺少\s*(USER_TOKEN|JSESSIONID)", text, flags=re.IGNORECASE)
    )


def _is_retryable_security_code_error(value: Any) -> bool:
    text = str(value or "")
    if not text or _is_restartable_login_error(text):
        return False
    if "滑块验证码识别未配置" in text:
        return False
    return bool(
        re.search(r"(安全码|验证码|动态码|短信).{0,30}(错误|不正确|失败|过期|失效|重新|必须|为空|缺失)", text)
        or re.search(r"(错误|失败|过期|失效|重新).{0,30}(安全码|验证码|动态码|短信)", text)
    )


@dataclass(frozen=True)
class LoginContext:
    outer_url: str
    outer_url_config: str
    domain_verify: str
    slide_captcha_version: str
    encode_key: str
    is_check_zbdsuser_flag: str
    security_psw_flag: str
    no_check_flag: str
    open_wx_login_config: str
    is_check_weak_pwd_flag: str
    is_use_yhzx_login_flag: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LoginContext":
        data = dict(raw or {})
        return cls(
            outer_url=str(data.get("outer_url", "0")),
            outer_url_config=str(data.get("outer_url_config", "1")),
            domain_verify=str(data.get("domain_verify", "1")),
            slide_captcha_version=str(data.get("slide_captcha_version", "2")),
            encode_key=str(data.get("encode_key", "")),
            is_check_zbdsuser_flag=str(data.get("is_check_zbdsuser_flag", "1")),
            security_psw_flag=str(data.get("security_psw_flag", "1")),
            no_check_flag=str(data.get("no_check_flag", "0")),
            open_wx_login_config=str(data.get("open_wx_login_config", "0")),
            is_check_weak_pwd_flag=str(data.get("is_check_weak_pwd_flag", "1")),
            is_use_yhzx_login_flag=str(data.get("is_use_yhzx_login_flag", "1")),
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def build_old_password(password: str, username: str) -> str:
    password_sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().lower()
    username_sha1 = hashlib.sha1(username.encode("utf-8")).hexdigest().lower()
    return password_sha1 + username_sha1[:10]


def rsa_encrypt_password(password: str, public_key_b64: str) -> str:
    try:
        key_bytes = base64.b64decode("".join(str(public_key_b64 or "").split()))
        public_key = serialization.load_der_public_key(key_bytes)
        encrypted = public_key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
        return base64.b64encode(encrypted).decode("ascii")
    except Exception as exc:
        raise PiccLoginError("PICC 密码 RSA 加密失败") from exc


def api_parse_captcha(config: PiccProtocolConfig, image_base64: str) -> str:
    if not config.captcha_username or not config.captcha_password:
        raise PiccConfigError("PICC 滑块验证码识别未配置，请设置打码平台账号和打码平台密码")
    image = str(image_base64 or "")
    if "," in image:
        image = image.split(",", 1)[1]
    try:
        response = requests.post(
            config.captcha_api_url,
            json={
                "username": config.captcha_username,
                "password": config.captcha_password,
                "typeid": int(config.captcha_type_id),
                "image": image,
            },
            timeout=config.captcha_timeout,
        )
        response.raise_for_status()
        result = response.json()
    except Exception as exc:
        raise PiccCaptchaError(f"PICC 滑块打码平台请求失败：{exc}") from exc
    if result.get("success"):
        data = result.get("data") or {}
        recognized = data.get("result")
        if recognized is not None and str(recognized).strip():
            return str(recognized).strip()
    raise PiccCaptchaError(f"PICC 滑块识别失败：{result.get('message') or result}")


class SlideCaptchaSolver:
    INIT_PATH = "/khyx/wf/wftmobileservice/querySystemConfigWhiteList.do"
    GET_PATH = "/khyx/slidecaptcha/getImgSwipe.do"
    VERIFY_PATH = "/khyx/slidecaptcha/rstImgSwipe.do"

    def __init__(self, session: requests.Session, config: PiccProtocolConfig) -> None:
        self.session = session
        self.config = config
        self.initialized = False

    def headers(self, origin: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.config.login_referer,
            "User-Agent": self.config.user_agent,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if origin:
            headers["Origin"] = self.config.base_url
        return headers

    @staticmethod
    def clean_base64(value: str) -> str:
        text = unquote(str(value or "").strip())
        if text.startswith("data:image"):
            text = re.sub(r"^data:image/[^;]+;base64,", "", text)
        text = "".join(text.split())
        if not text:
            raise PiccCaptchaError("PICC 滑块图片为空")
        return text + "=" * (-len(text) % 4)

    @staticmethod
    def parse_x(value: Any) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, Mapping):
            for key in ("x", "X", "data", "result", "Result", "pic_str"):
                if key in value:
                    return SlideCaptchaSolver.parse_x(value[key])
        match = re.search(r"-?\d+", str(value))
        if not match:
            raise PiccCaptchaError(f"PICC 滑块识别结果无法解析：{value}")
        return int(match.group())

    @classmethod
    def left_padding(cls, cut_image: str) -> int:
        try:
            from PIL import Image
        except Exception as exc:
            raise PiccConfigError("PICC 滑块图片处理缺少 Pillow 依赖，请安装 Pillow") from exc
        raw = base64.b64decode(cls.clean_base64(cut_image))
        with Image.open(BytesIO(raw)) as source:
            bbox = source.convert("RGBA").getchannel("A").getbbox()
        return int(bbox[0]) if bbox else 0

    def init_session(self, force: bool = False) -> None:
        if self.initialized and not force:
            return
        response = self.session.get(
            self.config.base_url + self.INIT_PATH,
            params={"configCode": "yunmaijsconfig", "comId": "00000000"},
            headers=self.headers(),
            timeout=self.config.timeout,
            verify=self.config.verify_ssl,
        )
        response.raise_for_status()
        self.initialized = True

    def get_images(self) -> dict[str, Any]:
        response = self.session.get(
            self.config.base_url + self.GET_PATH,
            headers=self.headers(),
            timeout=self.config.timeout,
            verify=self.config.verify_ssl,
        )
        response.raise_for_status()
        data = response.json()
        payload = data.get("data")
        if int(data.get("status", -1)) != 0 or not isinstance(payload, Mapping):
            raise PiccCaptchaError(f"PICC 获取滑块图片失败：{data}")
        return dict(payload)

    def submit(self, distance: int) -> None:
        b64 = lambda value: base64.b64encode(str(value).encode()).decode()
        response = self.session.post(
            self.config.base_url + self.VERIFY_PATH,
            headers=self.headers(origin=True),
            files={
                "moveEnd_X": (None, b64(distance)),
                "wbili": (None, b64(1)),
            },
            timeout=self.config.timeout,
            verify=self.config.verify_ssl,
        )
        result = response.json()
        payload = result.get("data")
        success = (
            response.status_code == 200
            and int(result.get("status", -1)) == 0
            and result.get("success") in {True, 1, "1", "true"}
            and isinstance(payload, Mapping)
            and payload.get("success") in {True, 1, "1", "true"}
        )
        if not success:
            raise PiccCaptchaError(f"PICC 滑块提交失败：distance={distance}，response={result}")

    def solve(self) -> None:
        self.init_session(force=False)
        last_error: Optional[Exception] = None
        for _ in range(max(1, int(self.config.captcha_max_rounds))):
            try:
                data = self.get_images()
                raw_x = self.parse_x(api_parse_captcha(self.config, self.clean_base64(str(data["SrcImage"]))))
                distance = raw_x - self.left_padding(str(data["CutImage"]))
                self.submit(distance)
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        raise PiccCaptchaError(f"PICC 滑块验证失败：{last_error}")


class PiccLoginFlow:
    def __init__(self, ctx: PlatformAccountContext, *, config: Optional[PiccProtocolConfig] = None) -> None:
        self.ctx = ctx
        self.config = config or PiccProtocolConfig.from_env()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.config.user_agent})
        self.captcha = SlideCaptchaSolver(self.session, self.config)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.config.login_referer,
            "User-Agent": self.config.user_agent,
        }

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = kwargs.pop("headers", None) or self._headers()
        response = self.session.request(
            method.upper(),
            urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/")),
            headers=headers,
            timeout=self.config.timeout,
            verify=self.config.verify_ssl,
            **kwargs,
        )
        try:
            result = response.json()
        except Exception as exc:
            raise PiccLoginError(f"PICC 登录接口返回非 JSON：HTTP={response.status_code}，body={response.text[:300]}") from exc
        if response.status_code != 200:
            raise PiccLoginError(f"PICC 登录接口 HTTP={response.status_code}，返回={result}")
        return result

    @staticmethod
    def _payload(data: Mapping[str, Any]) -> Mapping[str, Any]:
        nested = data.get("data")
        if isinstance(nested, Mapping):
            result = dict(nested)
            result.update({key: value for key, value in data.items() if key != "data"})
            return result
        return data

    def _load_context(self) -> LoginContext:
        init = self._request_json("GET", "/khyx/um/umtuser/initOuterUrlConfig.do")
        init_data = self._payload(init)
        outer_url = str(init_data.get("outerUrl", "0"))
        inout = self._request_json(
            "GET",
            "/khyx/um/umtuser/queryInOutAddress.do",
            params={"comIdEName": self.config.province, "outerUrl": outer_url},
        )
        if int(inout.get("status", -1)) != 0:
            raise PiccLoginError(f"PICC 读取内外网配置失败：{inout}")
        security = self._request_json(
            "GET",
            "/khyx/um/umtuser/querySecurityPswFlag.do",
            params={"comIdEName": self.config.province, "isZbdsfsChecked": "0"},
        )
        if int(security.get("status", -1)) != 0:
            raise PiccLoginError(f"PICC 读取安全配置失败：{security}")
        security_data = self._payload(security)
        return LoginContext(
            outer_url=outer_url,
            outer_url_config=str(init_data.get("outerUrlConfig", "1")),
            domain_verify=str(init_data.get("domainVerify", "1")),
            slide_captcha_version=str(init_data.get("slideCaptchaVersion", "2")),
            encode_key=str(init_data.get("encodeKey", "")),
            is_check_zbdsuser_flag=str(init_data.get("isCheckZbdsuserFlag", "1")),
            security_psw_flag=str(security_data.get("flag", "1")),
            no_check_flag=str(security_data.get("nocheckflag", "0")),
            open_wx_login_config=str(security_data.get("qyWxLoginConfig", "0")),
            is_check_weak_pwd_flag=str(security_data.get("checkWeakPwdFlag", "1")),
            is_use_yhzx_login_flag=str(security_data.get("isUseYhzxLoginFlag", "1")),
        )

    def _precheck(self, context: LoginContext, old_password: str, rsa_password: str, security_code: str) -> None:
        result = self._request_json(
            "GET",
            "/khyx/yhzx/um/umtuser/checkLoginByYhzx.do",
            params={
                "userCode": self.ctx.account_username,
                "oldPassWord": old_password,
                "rasEncryptPwd": rsa_password,
                "outerUrlConfig": context.outer_url_config,
                "outerUrl": context.outer_url,
                "verification": security_code,
                "captcha": "FIRST_LOGIN_FROM_INNER_URL" if context.outer_url == "0" else "",
                "jphoneno": "",
                "teamSelect": "0",
                "chooseVeriType": "2",
                "comIdEName": self.config.province,
                "innerVeriChecked": "1",
                "innerVerification": "",
                "isZbdsfsChecked": "0",
                "isFromKhyxui": "1",
            },
        )
        if int(result.get("status", -1)) != 0:
            raise PiccLoginError(str(result.get("data") or result.get("statusText") or result))

    def _formal_login(self, context: LoginContext, old_password: str, security_code: str) -> str:
        response = self.session.get(
            self.config.base_url + "/khyx/j_spring_security_check",
            params={
                "outerUrlConfig": context.outer_url_config,
                "outerUrl": context.outer_url,
                "currentType": "0",
                "domainVerify": context.domain_verify,
                "slideCaptchaVersion": context.slide_captcha_version,
                "openWxLoginConfig": context.open_wx_login_config,
                "sercurityPswFlag": context.security_psw_flag,
                "noCheckFlag": context.no_check_flag,
                "encodeKey": context.encode_key,
                "isCheckZbdsuserFlag": context.is_check_zbdsuser_flag,
                "isCheckWeakPwdFlag": context.is_check_weak_pwd_flag,
                "captcha": "",
                "chooseTeam": "0",
                "loginType": "0",
                "j_username": self.ctx.account_username,
                "j_password": old_password,
                "j_phoneno": "",
                "verification": security_code,
                "innerVerification": "",
                "j_host": urlsplit(self.config.base_url).netloc,
                "_eventId": "submit",
                "isYhzxLogin": context.is_use_yhzx_login_flag,
            },
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": self.config.login_referer,
                "User-Agent": self.config.user_agent,
            },
            allow_redirects=False,
            timeout=self.config.timeout,
            verify=self.config.verify_ssl,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            raise PiccLoginError(f"PICC 正式登录未跳转：HTTP={response.status_code}，body={response.text[:300]}")
        location = response.headers.get("Location", "")
        if not location:
            raise PiccLoginError("PICC 正式登录缺少 Location")
        if not get_cookie_value(self.session.cookies, "JSESSIONID", self.config.host):
            raise PiccLoginError("PICC 正式登录缺少 JSESSIONID")
        if not get_cookie_value(self.session.cookies, "USER_TOKEN", self.config.host):
            raise PiccLoginError("PICC 正式登录缺少 USER_TOKEN")

        for name, value in (("SDLComId", self.config.province), ("Team", "0"), ("TeamSelect", "0")):
            set_cookie_value(self.session.cookies, name, value, self.config.host)

        redirect_url = urljoin(self.config.base_url.rstrip("/") + "/", location.lstrip("/"))
        follow = self.session.get(
            redirect_url,
            headers={"Referer": self.config.login_referer, "User-Agent": self.config.user_agent},
            timeout=self.config.timeout,
            verify=self.config.verify_ssl,
        )
        if follow.status_code != 200:
            raise PiccLoginError(f"PICC 登录跳转页异常：HTTP={follow.status_code}")
        return redirect_url

    def start_waiting_security_code(self) -> AccountSessionSnapshot:
        if self.ctx.payload.get("credential_error"):
            raise PiccConfigError(str(self.ctx.payload.get("credential_error")))
        if not str(self.ctx.account_username or "").strip():
            raise PiccConfigError("PICC 平台账号不能为空")
        if not str(self.ctx.account_password or ""):
            raise PiccConfigError("PICC 平台密码不能为空")
        if not self.config.captcha_username or not self.config.captcha_password:
            raise PiccConfigError("PICC 滑块验证码识别未配置，请设置打码平台账号和打码平台密码")

        self.captcha.init_session(force=True)
        context = self._load_context()
        if not context.encode_key:
            raise PiccLoginError("PICC 登录初始化未返回 encodeKey")
        self.captcha.solve()

        client = PiccProtocolClient(self.ctx, config=self.config, session=self.session)
        snapshot = client.capture_snapshot(
            status="waiting_challenge",
            runtime_meta={
                "picc_pending_login": {
                    "context": context.to_dict(),
                    "created_at": iso_now(),
                },
                "login_phase": "waiting_security_code",
            },
        )
        return snapshot

    def complete_security_code(self, previous: AccountSessionSnapshot, security_code: str) -> AccountSessionSnapshot:
        clean_code = re.sub(r"\s+", "", str(security_code or ""))
        if not re.fullmatch(r"\d{6}", clean_code):
            raise PiccLoginError("PICC 安全码必须是 6 位数字")
        pending = (previous.runtime_meta or {}).get("picc_pending_login") or {}
        context_raw = pending.get("context") if isinstance(pending, dict) else None
        if not isinstance(context_raw, Mapping):
            raise PiccLoginError("PICC 登录上下文已过期，请重新点击登录")
        context = LoginContext.from_dict(context_raw)
        if not context.encode_key:
            raise PiccLoginError("PICC 登录上下文缺少 encodeKey，请重新点击登录")

        client = PiccProtocolClient(self.ctx, config=self.config, snapshot=previous)
        self.session = client.session
        self.captcha = SlideCaptchaSolver(self.session, self.config)
        old_password = build_old_password(self.ctx.account_password, self.ctx.account_username)
        rsa_password = rsa_encrypt_password(self.ctx.account_password, context.encode_key)
        try:
            # PICC 的滑块态可能在等待安全码期间失效，提交前刷新一次更接近人工操作。
            self.captcha.solve()
            self._precheck(context, old_password, rsa_password, clean_code)
        except PiccLoginError as exc:
            if not _is_slide_verification_error(exc):
                raise
            try:
                self.captcha.solve()
                self._precheck(context, old_password, rsa_password, clean_code)
            except Exception as retry_exc:
                raise PiccLoginError("PICC 滑块校验仍失败，请重新点击登录后再输入新的安全码") from retry_exc
        redirect_url = self._formal_login(context, old_password, clean_code)

        client = PiccProtocolClient(self.ctx, config=self.config, session=self.session)
        client.snapshot = previous
        snapshot = client.capture_snapshot(
            status="authenticated",
            runtime_meta={"login_redirect": redirect_url, "login_phase": "validating"},
            clear_pending_login=True,
        )
        snapshot.last_login_at = iso_now()
        client.snapshot = snapshot
        data = client.request_json(
            "GET",
            KEEPALIVE_PATH,
            purpose="validate",
            params=KEEPALIVE_PARAMS,
            headers={"Referer": redirect_url},
        )
        if not isinstance(data, Mapping) or int(data.get("status", -1)) != 0:
            raise PiccLoginError(f"PICC 登录后验证失败：{data}")
        snapshot = client.capture_snapshot(
            status="authenticated",
            runtime_meta={"login_phase": "authenticated"},
            clear_pending_login=True,
        )
        snapshot.last_login_at = iso_now()
        return snapshot


class PiccPlatformAdapter(PiccBusinessAdapter):
    platform_code = "PICC"
    platform_name = "人保"
    requires_browser_runtime = False
    keep_browser_alive = False

    async def login(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        return await asyncio.to_thread(self._login_sync, ctx)

    async def submit_challenge(self, ctx: PlatformAccountContext, challenge: str) -> PlatformRuntimeResult:
        return await asyncio.to_thread(self._submit_challenge_sync, ctx, challenge)

    def _needs_security_code_result(
        self,
        snapshot: AccountSessionSnapshot,
        *,
        message: str,
        reason: str = "",
    ) -> PlatformRuntimeResult:
        prompt = str(message or "").strip() or "请输入 PICC 平台 6 位安全码"
        return PlatformRuntimeResult(
            status="needs_code",
            message=prompt,
            data={
                "session_snapshot": snapshot.to_dict(),
                "challenge_payload": {
                    "code_length": 6,
                    "challenge_kind": "security_code",
                    "recoverable": True,
                    "reason": str(reason or "").strip()[:300],
                },
            },
            challenge_type="security_code",
            challenge_prompt=prompt,
        )

    def _restart_security_code_challenge(self, ctx: PlatformAccountContext, reason: Any) -> PlatformRuntimeResult:
        try:
            snapshot = PiccLoginFlow(ctx).start_waiting_security_code()
            reason_text = str(reason or "").strip()
            prompt = "PICC 登录校验状态已失效，我已重新初始化登录流程，请输入新的 6 位安全码"
            if reason_text:
                prompt = f"{prompt}。原因：{reason_text[:120]}"
            return self._needs_security_code_result(snapshot, message=prompt, reason=reason_text)
        except Exception as restart_exc:
            return PlatformRuntimeResult(
                status="failed",
                message=f"PICC 登录校验状态已失效，重新初始化登录失败：{str(restart_exc) or restart_exc.__class__.__name__}",
                data={
                    "error_code": restart_exc.__class__.__name__,
                    "recoverable_restart_failed": True,
                },
            )

    def _login_sync(self, ctx: PlatformAccountContext) -> PlatformRuntimeResult:
        try:
            snapshot = PiccLoginFlow(ctx).start_waiting_security_code()
            return PlatformRuntimeResult(
                status="needs_code",
                message="PICC 已完成滑块验证，等待平台 6 位安全码",
                data={
                    "session_snapshot": snapshot.to_dict(),
                    "challenge_payload": {"code_length": 6, "challenge_kind": "security_code"},
                },
                challenge_type="security_code",
                challenge_prompt="请输入 PICC 平台 6 位安全码",
            )
        except PiccConfigError as exc:
            return PlatformRuntimeResult(
                status="failed",
                message=str(exc) or exc.__class__.__name__,
                data={"error_code": exc.__class__.__name__},
            )
        except Exception as exc:
            if _is_restartable_login_error(exc):
                return self._restart_security_code_challenge(ctx, exc)
            return PlatformRuntimeResult(
                status="failed",
                message=str(exc) or exc.__class__.__name__,
                data={"error_code": exc.__class__.__name__},
            )

    def _submit_challenge_sync(self, ctx: PlatformAccountContext, challenge: str) -> PlatformRuntimeResult:
        previous: Optional[AccountSessionSnapshot] = None
        try:
            previous = snapshot_from_context(ctx)
            if previous is None:
                raise PiccLoginError("PICC 登录上下文不存在，请重新点击登录")
            snapshot = PiccLoginFlow(ctx).complete_security_code(previous, challenge)
            return PlatformRuntimeResult(
                status="success",
                message="PICC 登录成功，会话已托管并可自动续期",
                data=success_data_from_snapshot(snapshot),
            )
        except PiccConfigError as exc:
            return PlatformRuntimeResult(
                status="failed",
                message=str(exc) or exc.__class__.__name__,
                data={"error_code": exc.__class__.__name__},
            )
        except Exception as exc:
            if previous is not None and _is_retryable_security_code_error(exc):
                return self._needs_security_code_result(
                    previous,
                    message=f"PICC 安全码校验未通过，请输入新的 6 位安全码。原因：{str(exc)[:120]}",
                    reason=str(exc),
                )
            if _is_restartable_login_error(exc):
                return self._restart_security_code_challenge(ctx, exc)
            return PlatformRuntimeResult(
                status="failed",
                message=str(exc) or exc.__class__.__name__,
                data={"error_code": exc.__class__.__name__},
            )


def success_data_from_snapshot(snapshot: AccountSessionSnapshot) -> dict[str, Any]:
    return {
        "session_snapshot": snapshot.to_dict(),
        "jsession_id": snapshot.jsession_id,
        "user_token": snapshot.user_token,
        "jwt": snapshot.jwt.raw or {
            "issued_at": snapshot.jwt.issued_at,
            "expires_at": snapshot.jwt.expires_at,
        },
    }
