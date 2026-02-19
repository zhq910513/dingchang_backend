# app/main.py
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

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
from app.services.ocr_worker import run_ocr_task

setup_logging()
logger = logging.getLogger(__name__)

AUTO_SEED_FIELDS = os.getenv("AUTO_SEED_FIELDS", "1") == "1"
AUTO_SEED_AUTH = os.getenv("AUTO_SEED_AUTH", "1") == "1"
AUTO_CREATE_TABLES = os.getenv("AUTO_CREATE_TABLES", "1") == "1"

# ✅ 新增：启动期 schema 校验/只增补列（默认开启）
AUTO_SCHEMA_CHECK = os.getenv("AUTO_SCHEMA_CHECK", "1") == "1"
AUTO_ADD_COLUMNS = os.getenv("AUTO_ADD_COLUMNS", "1") == "1"
AUTO_ADD_COLUMNS_STRICT = os.getenv("AUTO_ADD_COLUMNS_STRICT", "1") == "1"

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

OCR_POLL_ENABLED = os.getenv("OCR_POLL_ENABLED", "1") == "1"
OCR_POLL_INTERVAL_SECONDS = int(os.getenv("OCR_POLL_INTERVAL_SECONDS", "3") or "3")
OCR_POLL_BATCH_SIZE = int(os.getenv("OCR_POLL_BATCH_SIZE", "3") or "3")

DB_TIME_ZONE = os.getenv("DB_TIME_ZONE", "+08:00")
DB_SET_TIME_ZONE_ENABLED = os.getenv("DB_SET_TIME_ZONE_ENABLED", "1") == "1"


async def _apply_db_time_zone(obj) -> None:
    if not DB_SET_TIME_ZONE_ENABLED:
        return
    try:
        await obj.execute(text("SET time_zone = :tz"), {"tz": DB_TIME_ZONE})
    except Exception:
        logger.exception("SET time_zone failed (tz=%s)", DB_TIME_ZONE)


async def _ocr_poller_loop(stop_event: asyncio.Event) -> None:
    interval = max(1, int(OCR_POLL_INTERVAL_SECONDS))
    batch_size = max(1, int(OCR_POLL_BATCH_SIZE))
    logger.info("[ocr_poller] started interval=%ss batch_size=%s", interval, batch_size)

    while not stop_event.is_set():
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()

    # ✅ 启动期 schema：先校验并日志打印，再按开关执行 create_all + 仅 ADD COLUMN
    if AUTO_SCHEMA_CHECK or AUTO_CREATE_TABLES or AUTO_ADD_COLUMNS:
        logger.info(
            "[schema_boot] AUTO_SCHEMA_CHECK=%s AUTO_CREATE_TABLES=%s AUTO_ADD_COLUMNS=%s STRICT=%s",
            int(AUTO_SCHEMA_CHECK),
            int(AUTO_CREATE_TABLES),
            int(AUTO_ADD_COLUMNS),
            int(AUTO_ADD_COLUMNS_STRICT),
        )
        # 注意：ensure_schema_additive_on_startup 内部会 load_all_models()
        await ensure_schema_additive_on_startup(
            add_tables=bool(AUTO_CREATE_TABLES),
            add_columns=bool(AUTO_ADD_COLUMNS),
            log_details=bool(AUTO_SCHEMA_CHECK),
            strict_add_columns=bool(AUTO_ADD_COLUMNS_STRICT),
        )
    else:
        # 保留你原有逻辑（理论上走不到，因为默认 AUTO_SCHEMA_CHECK=1）
        from app import models as _models  # noqa: F401
        if AUTO_CREATE_TABLES:
            async with engine.begin() as conn:
                await _apply_db_time_zone(conn)
                await conn.run_sync(Base.metadata.create_all)
            logger.info("AUTO_CREATE_TABLES=1 -> Base.metadata.create_all executed")

    async with SessionLocal() as db:
        await _apply_db_time_zone(db)

        if AUTO_SEED_AUTH:
            await seed_initial_data(db)
        if AUTO_SEED_FIELDS:
            await seed_order_fields(db)

    if OCR_POLL_ENABLED:
        stop_event = asyncio.Event()
        app.state.ocr_poller_stop_event = stop_event
        app.state.ocr_poller_task = asyncio.create_task(_ocr_poller_loop(stop_event))
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
async def add_process_time_header(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time"] = f"{time.perf_counter() - start:.6f}"
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


@app.get("/api/health")
async def health():
    db_ok = True
    redis_ok = True

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    try:
        from app.core.db import redis

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


app.include_router(v1_router, prefix="/api")
