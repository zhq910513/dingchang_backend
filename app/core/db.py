# app/core/db.py
# encoding: utf-8
"""
数据库与 Redis 连接管理
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Optional

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

Base = declarative_base()

DATABASE_URL = settings.ASYNC_DATABASE_URI

DB_TIME_ZONE = os.getenv("DB_TIME_ZONE", "+08:00")
DB_SET_TIME_ZONE_ENABLED = os.getenv("DB_SET_TIME_ZONE_ENABLED", "1") == "1"

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=int(getattr(settings, "DB_POOL_SIZE", 20) or 20),
    max_overflow=int(getattr(settings, "DB_MAX_OVERFLOW", 20) or 20),
    pool_recycle=int(getattr(settings, "DB_POOL_RECYCLE", 1800) or 1800),
)


@event.listens_for(engine.sync_engine, "connect")
def _on_connect_set_time_zone(dbapi_connection, connection_record) -> None:  # noqa: ARG001
    if not DB_SET_TIME_ZONE_ENABLED:
        return
    try:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET time_zone = '{DB_TIME_ZONE}'")
        finally:
            try:
                cursor.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("SET time_zone failed (tz=%s): %s", DB_TIME_ZONE, e)


async_session_factory = sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

redis: Optional[object] = None


def load_all_models() -> None:
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


async def get_db():
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise


async def init_redis():
    global redis

    url = getattr(settings, "REDIS_URL", "") or ""
    if not url.strip():
        redis = None
        logger.info("Redis disabled: REDIS_URL is empty")
        return None

    try:
        import redis.asyncio as redis_async  # redis==5.x

        r = redis_async.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
        )

        try:
            await r.ping()
        except Exception as e:
            redis = None
            logger.warning("Redis unreachable, disabled (url=%s): %s", url, e)
            return None

        redis = r
        logger.info("Redis enabled (url=%s)", url)
        return redis
    except Exception as e:
        redis = None
        logger.warning("Redis init failed, disabled (url=%s): %s", url, e)
        return None


async def close_redis():
    global redis
    if redis:
        try:
            await redis.close()
            cp = getattr(redis, "connection_pool", None)
            if cp and hasattr(cp, "disconnect"):
                res = cp.disconnect()
                if hasattr(res, "__await__"):
                    await res
        finally:
            redis = None
