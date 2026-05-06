# app/main.py
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import router as v1_router
from app.core.config import settings
from app.core.db import (
    Base,
    close_redis,
    engine,
    init_redis,
    ensure_schema_additive_on_startup,
)
from app.core.logging_config import setup_logging
from app.core.seed_author_role import seed_initial_data
from app.core.seed_order_fields import seed_order_fields
from app.models.ocr_task import OcrTask
from app.services.order_fact_service import backfill_missing_order_facts, count_missing_order_facts
from app.services.ocr_worker import run_ocr_task

setup_logging()
logger = logging.getLogger(__name__)

AUTO_SEED_FIELDS = os.getenv("AUTO_SEED_FIELDS", "1") == "1"
AUTO_SEED_AUTH = os.getenv("AUTO_SEED_AUTH", "1") == "1"
AUTO_CREATE_TABLES = os.getenv("AUTO_CREATE_TABLES", "0") == "1"

# 默认只检查，不执行 DDL；如需受控迁移，必须显式打开对应环境变量。
AUTO_SCHEMA_CHECK = os.getenv("AUTO_SCHEMA_CHECK", "1") == "1"
AUTO_ADD_COLUMNS = os.getenv("AUTO_ADD_COLUMNS", "0") == "1"
AUTO_ADD_COLUMNS_STRICT = os.getenv("AUTO_ADD_COLUMNS_STRICT", "1") == "1"
AUTO_ADD_INDEXES = os.getenv("AUTO_ADD_INDEXES", "0") == "1"

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

ORDER_FACT_BACKFILL_ENABLED = os.getenv("ORDER_FACT_BACKFILL_ENABLED", "1") == "1"
ORDER_FACT_BACKFILL_BATCH_SIZE = int(os.getenv("ORDER_FACT_BACKFILL_BATCH_SIZE", "1000") or "1000")
ORDER_FACT_BACKFILL_MAX_BATCHES = int(os.getenv("ORDER_FACT_BACKFILL_MAX_BATCHES", "20") or "20")

OCR_POLL_ENABLED = os.getenv("OCR_POLL_ENABLED", "1") == "1"
OCR_POLL_INTERVAL_SECONDS = int(os.getenv("OCR_POLL_INTERVAL_SECONDS", "3") or "3")
OCR_POLL_BATCH_SIZE = int(os.getenv("OCR_POLL_BATCH_SIZE", "3") or "3")

DB_TIME_ZONE = os.getenv("DB_TIME_ZONE", "+08:00")
DB_SET_TIME_ZONE_ENABLED = os.getenv("DB_SET_TIME_ZONE_ENABLED", "1") == "1"

# ========= Redis 分布式锁（启动单例守卫） =========
LOCK_SEED_KEY = os.getenv("STARTUP_LOCK_SEED_KEY", "dingchang:startup:seed")
LOCK_POLL_KEY = os.getenv("STARTUP_LOCK_POLL_KEY", "dingchang:startup:ocr_poller")
LOCK_FACT_BACKFILL_KEY = os.getenv("STARTUP_LOCK_FACT_BACKFILL_KEY", "dingchang:startup:order_fact_backfill")

# seed 锁：短一些即可
LOCK_SEED_TTL_SECONDS = int(os.getenv("STARTUP_LOCK_SEED_TTL_SECONDS", "120") or "120")
# order_fact 回填可能扫批量订单，TTL 给足一些；没有 Redis 时仍按旧行为兼容单机启动。
LOCK_FACT_BACKFILL_TTL_SECONDS = int(os.getenv("STARTUP_LOCK_FACT_BACKFILL_TTL_SECONDS", "600") or "600")
# poller 锁：需要续租
LOCK_POLL_TTL_SECONDS = int(os.getenv("STARTUP_LOCK_POLL_TTL_SECONDS", "30") or "30")

# Lua：仅当 value 匹配时删除（防误删）
_LUA_RELEASE = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
"""

# Lua：仅当 value 匹配时续租
_LUA_RENEW = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("expire", KEYS[1], ARGV[2])
else
  return 0
end
"""


async def _redis_client():
    # 延迟 import，避免循环依赖；init_redis() 会初始化 app.core.db.redis
    from app.core.db import redis  # type: ignore

    return redis


async def acquire_lock(key: str, ttl_seconds: int) -> str | None:
    r = await _redis_client()
    if not r:
        logger.warning("[startup_lock] redis not available, skip lock key=%s", key)
        return None

    token = str(uuid4())
    try:
        # redis-py asyncio：set(name, value, ex=, nx=True)
        ok = await r.set(key, token, ex=int(ttl_seconds), nx=True)
        if ok:
            logger.info("[startup_lock] acquired key=%s ttl=%ss", key, ttl_seconds)
            return token
        return None
    except Exception:
        logger.exception("[startup_lock] acquire failed key=%s", key)
        return None


async def release_lock(key: str, token: str) -> None:
    r = await _redis_client()
    if not r:
        return
    try:
        await r.eval(_LUA_RELEASE, 1, key, token)
        logger.info("[startup_lock] released key=%s", key)
    except Exception:
        logger.exception("[startup_lock] release failed key=%s", key)


async def renew_lock(key: str, token: str, ttl_seconds: int) -> bool:
    r = await _redis_client()
    if not r:
        return False
    try:
        res = await r.eval(_LUA_RENEW, 1, key, token, int(ttl_seconds))
        return int(res or 0) == 1
    except Exception:
        logger.exception("[startup_lock] renew failed key=%s", key)
        return False


async def _apply_db_time_zone(obj) -> None:
    if not DB_SET_TIME_ZONE_ENABLED:
        return
    try:
        await obj.execute(text("SET time_zone = :tz"), {"tz": DB_TIME_ZONE})
    except Exception:
        logger.exception("SET time_zone failed (tz=%s)", DB_TIME_ZONE)


async def _ocr_poller_loop(stop_event: asyncio.Event, lock_token: str | None) -> None:
    interval = max(1, int(OCR_POLL_INTERVAL_SECONDS))
    batch_size = max(1, int(OCR_POLL_BATCH_SIZE))
    logger.info("[ocr_poller] started interval=%ss batch_size=%s", interval, batch_size)

    # 续租频率：取 interval/2，最小 3 秒
    renew_every = max(3, min(15, interval // 2 or 3))
    last_renew = 0.0

    while not stop_event.is_set():
        # ✅ 续租锁：避免锁过期导致另一个 worker 接管 poller
        if lock_token:
            now = time.time()
            if now - last_renew >= renew_every:
                ok = await renew_lock(LOCK_POLL_KEY, lock_token, LOCK_POLL_TTL_SECONDS)
                if not ok:
                    logger.warning("[ocr_poller] lock lost, stop poller (another worker may take over)")
                    break
                last_renew = now

        try:
            async with SessionLocal() as db:
                await _apply_db_time_zone(db)

                stmt = (
                    select(OcrTask.id)
                    .where(
                        and_(
                            OcrTask.status == "pending",
                            OcrTask.active_scope_id.isnot(None),
                        )
                    )
                    .order_by(OcrTask.id.asc())
                    .limit(batch_size)
                )
                ids = (await db.execute(stmt)).scalars().all()

            for tid in ids:
                if stop_event.is_set():
                    break
                try:
                    await run_ocr_task(int(tid))
                except Exception:
                    logger.exception("[ocr_poller] run_ocr_task failed task_id=%s", tid)
        except Exception:
            logger.exception("[ocr_poller] loop error")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("[ocr_poller] stopped")


async def _run_order_fact_backfill_once() -> None:
    total_backfilled = 0
    batch_size = max(1, int(ORDER_FACT_BACKFILL_BATCH_SIZE or 1000))
    max_batches = max(1, int(ORDER_FACT_BACKFILL_MAX_BATCHES or 20))

    for _ in range(max_batches):
        async with SessionLocal() as db:
            await _apply_db_time_zone(db)
            batch = await backfill_missing_order_facts(db, batch_size=batch_size)
            if batch <= 0:
                break
            await db.commit()
            total_backfilled += batch

    async with SessionLocal() as db:
        await _apply_db_time_zone(db)
        remaining_missing = await count_missing_order_facts(db)

    logger.info(
        "[order_fact_backfill] enabled=1 batch_size=%s max_batches=%s backfilled=%s remaining=%s",
        batch_size,
        max_batches,
        total_backfilled,
        remaining_missing,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()

    # ✅ 启动期 schema：先校验并日志打印，再按开关执行 create_all + 仅 ADD COLUMN
    if AUTO_SCHEMA_CHECK or AUTO_CREATE_TABLES or AUTO_ADD_COLUMNS or AUTO_ADD_INDEXES:
        logger.info(
            "[schema_boot] AUTO_SCHEMA_CHECK=%s AUTO_CREATE_TABLES=%s "
            "AUTO_ADD_COLUMNS=%s AUTO_ADD_INDEXES=%s STRICT=%s",
            int(AUTO_SCHEMA_CHECK),
            int(AUTO_CREATE_TABLES),
            int(AUTO_ADD_COLUMNS),
            int(AUTO_ADD_INDEXES),
            int(AUTO_ADD_COLUMNS_STRICT),
        )
        await ensure_schema_additive_on_startup(
            add_tables=bool(AUTO_CREATE_TABLES),
            add_columns=bool(AUTO_ADD_COLUMNS),
            add_indexes=bool(AUTO_ADD_INDEXES),
            log_details=bool(AUTO_SCHEMA_CHECK),
            strict_add_columns=bool(AUTO_ADD_COLUMNS_STRICT),
        )
    else:
        from app import models as _models  # noqa: F401

        if AUTO_CREATE_TABLES:
            async with engine.begin() as conn:
                await _apply_db_time_zone(conn)
                await conn.run_sync(Base.metadata.create_all)
            logger.info("AUTO_CREATE_TABLES=1 -> Base.metadata.create_all executed")

    # ✅ 启动 seed 仅允许一个 worker 执行（避免 1062 并发冲突）
    seed_token = None
    try:
        if AUTO_SEED_AUTH or AUTO_SEED_FIELDS:
            seed_token = await acquire_lock(LOCK_SEED_KEY, LOCK_SEED_TTL_SECONDS)

        if seed_token:
            async with SessionLocal() as db:
                await _apply_db_time_zone(db)

                if AUTO_SEED_AUTH:
                    await seed_initial_data(db)
                if AUTO_SEED_FIELDS:
                    await seed_order_fields(db)
        else:
            if AUTO_SEED_AUTH or AUTO_SEED_FIELDS:
                logger.info("[startup_seed] skipped (lock not acquired) key=%s", LOCK_SEED_KEY)
    finally:
        if seed_token:
            await release_lock(LOCK_SEED_KEY, seed_token)

    # ✅ order_fact 回填：多 worker 部署时只允许一个实例扫表，避免启动期争抢列表查询资源
    if ORDER_FACT_BACKFILL_ENABLED:
        fact_token = await acquire_lock(LOCK_FACT_BACKFILL_KEY, LOCK_FACT_BACKFILL_TTL_SECONDS)
        if fact_token:
            try:
                await _run_order_fact_backfill_once()
            finally:
                await release_lock(LOCK_FACT_BACKFILL_KEY, fact_token)
        elif await _redis_client() is None:
            logger.warning("[order_fact_backfill] redis lock unavailable; running without distributed lock")
            await _run_order_fact_backfill_once()
        else:
            logger.info("[order_fact_backfill] skipped (lock not acquired) key=%s", LOCK_FACT_BACKFILL_KEY)
    else:
        logger.info("[order_fact_backfill] disabled (ORDER_FACT_BACKFILL_ENABLED=0)")

    poll_token = None
    if OCR_POLL_ENABLED:
        poll_token = await acquire_lock(LOCK_POLL_KEY, LOCK_POLL_TTL_SECONDS)
        if poll_token:
            stop_event = asyncio.Event()
            app.state.ocr_poller_stop_event = stop_event
            app.state.ocr_poller_lock_token = poll_token
            app.state.ocr_poller_task = asyncio.create_task(_ocr_poller_loop(stop_event, poll_token))
        else:
            logger.info("[ocr_poller] skipped (lock not acquired) key=%s", LOCK_POLL_KEY)
    else:
        logger.info("[ocr_poller] disabled (OCR_POLL_ENABLED=0)")

    try:
        yield
    finally:
        try:
            if hasattr(app.state, "ocr_poller_stop_event"):
                app.state.ocr_poller_stop_event.set()

            if hasattr(app.state, "ocr_poller_task"):
                task = app.state.ocr_poller_task
                try:
                    await asyncio.wait_for(task, timeout=5)
                except Exception:
                    task.cancel()
                    try:
                        await task
                    except Exception:
                        pass

            if hasattr(app.state, "ocr_poller_lock_token"):
                try:
                    await release_lock(LOCK_POLL_KEY, app.state.ocr_poller_lock_token)
                except Exception:
                    logger.exception("release ocr poller lock failed")
        except Exception:
            logger.exception("stop ocr poller failed")

        await close_redis()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

# ✅ 静态挂载（用 settings 里真实存在的字段）
try:
    local_root: Path = settings.LOCAL_STORAGE_ROOT_PATH
    local_root.mkdir(parents=True, exist_ok=True)
    app.mount(
        settings.LOCAL_PUBLIC_PREFIX,
        StaticFiles(directory=str(local_root)),
        name="static",
    )
except Exception as e:
    logger.warning("Static mount skipped: %s", e)


@app.middleware("http")
async def health(request, call_next):
    """
    ✅ 只处理 /api/health
    - 其它路径必须 call_next 放行，否则会把所有接口都“短路”成健康检查
    - middleware 签名必须是 (request, call_next)，否则会触发 TypeError
    """
    path = (request.url.path or "").rstrip("/")
    if path == "/api/health":
        db_ok = True
        redis_ok = True

        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            db_ok = False

        try:
            from app.core.db import redis  # type: ignore

            if redis:
                await redis.ping()
        except Exception:
            redis_ok = False

        ok = db_ok and redis_ok
        status_code = 200 if ok else 503
        return JSONResponse(
            status_code=status_code,
            content={"ok": ok, "db": db_ok, "redis": redis_ok, "env": getattr(settings, "ENV", None)},
        )

    return await call_next(request)


app.include_router(v1_router, prefix="/api")
