# app/core/db.py
# encoding: utf-8
"""
数据库与 Redis 连接管理（生产可用版本）

- Async SQLAlchemy + MySQL(aiomysql)
- 连接建立时设置 MySQL session time_zone = +08:00（北京时间）
- Redis 兼容 redis>=4 的 redis.asyncio，以及旧版 aioredis（如存在）
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import AsyncGenerator, Optional, Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

Base = declarative_base()

DATABASE_URL = settings.ASYNC_DATABASE_URI

# ✅ DB 会话时区：统一北京时间（MySQL session time_zone）
DB_TIME_ZONE = os.getenv("DB_TIME_ZONE", "+08:00")
DB_SET_TIME_ZONE_ENABLED = os.getenv("DB_SET_TIME_ZONE_ENABLED", "1") == "1"

# ✅ 连接池参数：统一从环境变量读取，避免“配置了但不生效”
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "20") or "20")
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "20") or "20")
# 常见建议：小于云厂商 idle timeout（比如 30min），这里默认 1800s
DB_POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800") or "1800")

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_recycle=DB_POOL_RECYCLE,
)

# ✅ 连接建立时设置 session time_zone（对连接池内每条新连接生效）
@event.listens_for(engine.sync_engine, "connect")
def _on_connect_set_time_zone(dbapi_connection, connection_record) -> None:  # noqa: ARG001
    if not DB_SET_TIME_ZONE_ENABLED:
        return
    try:
        cursor = dbapi_connection.cursor()
        try:
            # tz 来自环境变量；通常由运维控制。此处保持简单直接。
            cursor.execute(f"SET time_zone = '{DB_TIME_ZONE}'")
        finally:
            try:
                cursor.close()
            except Exception:
                pass
    except Exception as e:
        # 不阻断启动：避免因权限/云厂商限制导致服务无法启动
        logger.warning("SET time_zone failed (tz=%s): %s", DB_TIME_ZONE, e)


async_session_factory = sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

# -----------------------------
# Redis（兼容 redis.asyncio 与旧 aioredis）
# -----------------------------
redis: Optional[Any] = None


async def init_redis():
    """
    初始化 Redis 客户端：
    - 优先使用 redis>=4 的 redis.asyncio（推荐）
    - 如环境里只有旧 aioredis，则尽力兼容其连接方式
    """
    global redis

    # 推荐：redis>=4
    try:
        import redis.asyncio as redis_async  # type: ignore

        redis = redis_async.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        # 做一个轻量 ping，尽早暴露配置错误（不抛也行，但抛出更利于排障）
        await redis.ping()
        return redis
    except Exception as e:
        logger.info("redis.asyncio unavailable or init failed, fallback to aioredis: %s", e)

    # 兼容：旧版 aioredis（接口差异很大）
    try:
        import aioredis  # type: ignore

        # aioredis v2（接近 redis-py）可能也有 from_url
        if hasattr(aioredis, "from_url"):
            redis = await aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )
            # 某些版本 from_url 返回的是 awaitable，某些不是；这里兼容一下
            if hasattr(redis, "ping"):
                await redis.ping()
            return redis

        # aioredis v1 常用 create_redis_pool
        if hasattr(aioredis, "create_redis_pool"):
            redis = await aioredis.create_redis_pool(settings.REDIS_URL)
            return redis

        raise RuntimeError("Unsupported aioredis version: missing from_url/create_redis_pool")
    except Exception as e:
        logger.error("Redis init failed: %s", e)
        raise


async def close_redis():
    global redis
    if not redis:
        return
    try:
        # redis.asyncio.Redis.close() 是协程
        close_fn = getattr(redis, "close", None)
        if callable(close_fn):
            res = close_fn()
            if hasattr(res, "__await__"):
                await res

        # 部分版本需要显式断开连接池（有就做，没有就跳过）
        cp = getattr(redis, "connection_pool", None)
        if cp and hasattr(cp, "disconnect"):
            maybe = cp.disconnect
            if callable(maybe):
                res = maybe()
                if hasattr(res, "__await__"):
                    await res
    finally:
        redis = None


# -----------------------------
# Models / DB Session
# -----------------------------
def load_all_models() -> None:
    """
    ✅ 关键：create_all 只会创建“已被 import 过并注册到 Base.metadata 的模型”
    所以首次建表前必须显式 import 全部 models。

    注意：这里只 import “模型模块”，不要把 API 路由模块塞进来。
    """
    modules = [
        "app.models.user",
        "app.models.role",
        "app.models.user_role",
        "app.models.customer_group",
        "app.models.channel_group",
        "app.models.field_config",
        "app.models.image_file",
        "app.models.image_ocr_result",
        "app.models.ocr_task",
        "app.models.ocr_image_cache",
        "app.models.order_info",
        "app.models.order",
        "app.models.finance",
        "app.models.session",
    ]
    for m in modules:
        try:
            importlib.import_module(m)
        except Exception as e:
            logger.warning("Model module import skipped: %s (%s)", m, e)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    统一的 AsyncSession 依赖：
    - 异常自动 rollback，避免 session 卡在 failed transaction 状态
    - 正常流程由上层决定 commit/flush 时机（这里不做自动 commit）
    """
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise


async def close_db() -> None:
    """
    优雅关闭 DB 连接池（容器停止/应用 shutdown 时调用）
    """
    try:
        await engine.dispose()
    except Exception as e:
        logger.warning("engine.dispose() failed: %s", e)
