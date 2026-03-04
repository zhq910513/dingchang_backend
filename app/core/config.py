# app/core/config.py
# encoding: utf-8
"""
应用配置（统一从 .env / 环境变量读取，代码里通过 settings 使用）
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

PYDANTIC_V2 = False
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore
    from pydantic import Field  # type: ignore
    from pydantic import field_validator, model_validator  # type: ignore

    PYDANTIC_V2 = True
except Exception:  # pragma: no cover
    from pydantic import BaseSettings, Field, validator, root_validator  # type: ignore


def _is_placeholder(v: Optional[str]) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s in {"***", "change_me_to_a_random_string", "root123456"}


class Settings(BaseSettings):
    PROJECT_NAME: str = Field(default="dingchang_backend")
    ENV: str = Field(default="dev", description="dev / prod")

    # -----------------------------
    # DB
    # -----------------------------
    DB_USER: str = Field(default="root")
    DB_PASSWORD: Optional[str] = Field(default=None)
    DB_HOST: str = Field(default="127.0.0.1")
    DB_PORT: int = Field(default=3306)
    DB_NAME: str = Field(default="order_system")

    DB_POOL_SIZE: int = Field(default=20)
    DB_MAX_OVERFLOW: int = Field(default=20)
    DB_POOL_RECYCLE: int = Field(default=1800)

    ASYNC_DATABASE_URI_OVERRIDE: Optional[str] = Field(default=None)
    SYNC_DATABASE_URI_OVERRIDE: Optional[str] = Field(default=None)

    # -----------------------------
    # Redis
    # -----------------------------
    REDIS_URL: str = Field(default="redis://127.0.0.1:6379/0")

    # -----------------------------
    # Auth / Session
    # -----------------------------
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=120)
    SESSION_TIMEOUT_SECONDS: int = Field(default=7200)
    SECRET_KEY: Optional[str] = Field(default=None)

    # -----------------------------
    # Logging / Paths
    # -----------------------------
    LOG_LEVEL: str = Field(default="INFO")
    STORAGE_ROOT: str = Field(default="./storage")
    LOG_DIR: str = Field(default="./logs")

    LOCAL_PUBLIC_PREFIX: str = Field(default="/static")
    LOCAL_STORAGE_ROOT: Optional[str] = Field(default=None)

    # -----------------------------
    # BOS（方案B1：STS + 前端直传）
    # -----------------------------
    BOS_ENABLED: bool = Field(default=True)

    BOS_BUCKET: str = Field(default="dingchang")
    BOS_REGION: str = Field(default="fwh")  # 华中-武汉

    BOS_VHOST_OVERRIDE: Optional[str] = Field(default=None)
    BOS_ENDPOINT_HOST_OVERRIDE: Optional[str] = Field(default=None)
    BOS_BASE_URL_OVERRIDE: Optional[str] = Field(default=None)

    BOS_SIGNED_GET_URL: bool = Field(default=False)

    BOS_STS_ACCOUNT_ID: Optional[str] = Field(default=None)
    BOS_STS_ROLE_NAME: str = Field(default="bos_uploader")
    BOS_STS_ACCESS_KEY: Optional[str] = Field(default=None)
    BOS_STS_SECRET_KEY: Optional[str] = Field(default=None)
    BOS_STS_HOST: str = Field(default="sts.bj.baidubce.com")

    # -----------------------------
    # OCR（百度 OCR，可选）
    # -----------------------------
    BAIDU_OCR_ENABLED: bool = Field(default=True)
    BAIDU_API_KEY: Optional[str] = Field(default=None)
    BAIDU_SECRET_KEY: Optional[str] = Field(default=None)

    # -----------------------------
    # ✅ 报价助手：平台模块配置（公共入口 + 开关 + 预留缓存）
    # -----------------------------
    # 平台请求超时/重试/缓存 TTL
    AI_PLATFORM_TIMEOUT_SECONDS: int = Field(default=20)
    AI_PLATFORM_RETRY_COUNT: int = Field(default=1)
    AI_PLATFORM_CACHE_TTL_SECONDS: int = Field(default=300)

    # ✅ 平台开关（按平台 code 大写命名）
    # 例如：AI_PLATFORM_ENABLE_TP=true
    # 当前先内置 STUB（用于公共入口跑通）
    AI_PLATFORM_ENABLE_STUB: bool = Field(default=True)

    # ---------- 规范化 ----------
    if PYDANTIC_V2:
        @field_validator("ENV", mode="before")
        @classmethod
        def _normalize_env(cls, v):
            return str(v or "dev").strip().lower()

        @model_validator(mode="after")
        def _validate_prod(self):
            self._validate_required_for_env()
            return self

    else:
        @validator("ENV", pre=True)
        def _normalize_env(cls, v):
            return str(v or "dev").strip().lower()

        @root_validator
        def _validate_prod(cls, values):
            env = str(values.get("ENV", "dev")).strip().lower()
            if env in {"prod", "production"}:
                if _is_placeholder(values.get("SECRET_KEY")):
                    raise ValueError("SECRET_KEY must be set via environment variables in prod.")
                if _is_placeholder(values.get("DB_PASSWORD")):
                    raise ValueError("DB_PASSWORD must be set via environment variables in prod.")
                if bool(values.get("BOS_ENABLED", True)):
                    for k in ("BOS_STS_ACCOUNT_ID", "BOS_STS_ACCESS_KEY", "BOS_STS_SECRET_KEY"):
                        if _is_placeholder(values.get(k)):
                            raise ValueError(
                                f"{k} must be set via environment variables in prod when BOS_ENABLED=true.")
                if bool(values.get("BAIDU_OCR_ENABLED", True)):
                    for k in ("BAIDU_API_KEY", "BAIDU_SECRET_KEY"):
                        if _is_placeholder(values.get(k)):
                            raise ValueError(
                                f"{k} must be set via environment variables in prod when BAIDU_OCR_ENABLED=true.")
            return values

    def _validate_required_for_env(self) -> None:
        env = (self.ENV or "dev").strip().lower()
        if env not in {"prod", "production"}:
            return

        if _is_placeholder(self.SECRET_KEY):
            raise ValueError("SECRET_KEY must be set via environment variables in prod.")
        if _is_placeholder(self.DB_PASSWORD):
            raise ValueError("DB_PASSWORD must be set via environment variables in prod.")
        if self.BOS_ENABLED:
            for k in ("BOS_STS_ACCOUNT_ID", "BOS_STS_ACCESS_KEY", "BOS_STS_SECRET_KEY"):
                if _is_placeholder(getattr(self, k, None)):
                    raise ValueError(f"{k} must be set via environment variables in prod when BOS_ENABLED=true.")
        if self.BAIDU_OCR_ENABLED:
            for k in ("BAIDU_API_KEY", "BAIDU_SECRET_KEY"):
                if _is_placeholder(getattr(self, k, None)):
                    raise ValueError(f"{k} must be set via environment variables in prod when BAIDU_OCR_ENABLED=true.")

    @property
    def IS_PROD(self) -> bool:
        return self.ENV.strip().lower() in {"prod", "production"}

    @property
    def BOS_VHOST(self) -> str:
        if self.BOS_VHOST_OVERRIDE and self.BOS_VHOST_OVERRIDE.strip():
            return self.BOS_VHOST_OVERRIDE.strip()
        return f"{self.BOS_BUCKET}.{self.BOS_REGION}.bcebos.com"

    @property
    def BOS_ENDPOINT_HOST(self) -> str:
        if self.BOS_ENDPOINT_HOST_OVERRIDE and self.BOS_ENDPOINT_HOST_OVERRIDE.strip():
            return self.BOS_ENDPOINT_HOST_OVERRIDE.strip()
        return f"{self.BOS_REGION}.bcebos.com"

    @property
    def BOS_BASE_URL(self) -> str:
        if self.BOS_BASE_URL_OVERRIDE and self.BOS_BASE_URL_OVERRIDE.strip():
            return self.BOS_BASE_URL_OVERRIDE.strip()
        return f"https://{self.BOS_VHOST}"

    def _mysql_password_for_dsn(self) -> str:
        raw = self.DB_PASSWORD or ""
        return quote_plus(raw)

    @property
    def ASYNC_DATABASE_URI(self) -> str:
        if self.ASYNC_DATABASE_URI_OVERRIDE and self.ASYNC_DATABASE_URI_OVERRIDE.strip():
            return self.ASYNC_DATABASE_URI_OVERRIDE.strip()
        return (
            f"mysql+aiomysql://{quote_plus(self.DB_USER)}:{self._mysql_password_for_dsn()}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            "?charset=utf8mb4"
        )

    @property
    def SYNC_DATABASE_URI(self) -> str:
        if self.SYNC_DATABASE_URI_OVERRIDE and self.SYNC_DATABASE_URI_OVERRIDE.strip():
            return self.SYNC_DATABASE_URI_OVERRIDE.strip()
        return (
            f"mysql+pymysql://{quote_plus(self.DB_USER)}:{self._mysql_password_for_dsn()}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            "?charset=utf8mb4"
        )

    @property
    def LOCAL_STORAGE_ROOT_PATH(self) -> Path:
        base = (self.LOCAL_STORAGE_ROOT or "").strip() or self.STORAGE_ROOT
        return Path(base).expanduser().resolve()

    @property
    def LOG_DIR_PATH(self) -> Path:
        return Path(self.LOG_DIR).expanduser().resolve()

    if PYDANTIC_V2:
        model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)
    else:

        class Config:
            env_file = ".env"
            case_sensitive = True


settings = Settings()
