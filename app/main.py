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
from app.core.db import Base, close_redis, engine, init_redis
from app.core.logging_config import setup_logging
from app.core.seed_author_role import seed_initial_data
from app.core.seed_order_fields import seed_order_fields
from app.models.ocr_task import OcrTask
from app.services.ocr_worker import run_ocr_task

setup_logging()
logger = logging.getLogger(__name__)

# ✅ 环境开关：开发期建议 1，生产环境建议 0
AUTO_SEED_FIELDS = os.getenv("AUTO_SEED_FIELDS", "1") == "1"
AUTO_SEED_AUTH = os.getenv("AUTO_SEED_AUTH", "1") == "1"

# ✅ 是否自动建表：开发期 1；生产建议用迁移工具时设为 0
AUTO_CREATE_TABLES = os.getenv("AUTO_CREATE_TABLES", "1") == "1"

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# ✅ OCR 轮询兜底（方案 B）
OCR_POLL_ENABLED = os.getenv("OCR_POLL_ENABLED", "1") == "1"
OCR_POLL_INTERVAL_SECONDS = int(os.getenv("OCR_POLL_INTERVAL_SECONDS", "3") or "3")
OCR_POLL_BATCH_SIZE = int(os.getenv("OCR_POLL_BATCH_SIZE", "3") or "3")

# ✅ DB 会话时区：统一用北京时间（MySQL session time_zone）
DB_TIME_ZONE = os.getenv("DB_TIME_ZONE", "+08:00")
DB_SET_TIME_ZONE_ENABLED = os.getenv("DB_SET_TIME_ZONE_ENABLED", "1") == "1"

# ✅ 本地存储静态挂载（BOS 未启用时前端也能直接访问图片）
# 例如：LOCAL_STORAGE_ROOT=./storage，LOCAL_PUBLIC_PREFIX=/static
try:
    local_root = Path(getattr(settings, "LOCAL_STORAGE_ROOT", "./storage")).resolve()
    local_root.mkdir(parents=True, exist_ok=True)
except Exception as e:
    logger.warning("Local storage root init skipped: %s", e)
else:
    try:
        app_static_prefix = getattr(settings, "LOCAL_PUBLIC_PREFIX", "/static")
        # 注：mount 需要在 app 创建后执行（见下方 lifespan 后的 app.mount）
    except Exception as e:
        logger.warning("Static mount config skipped: %s", e)


async def _apply_db_time_zone(obj) -> None:
    """
    ✅ 确保 MySQL session time_zone 固定（避免 CURRENT_TIMESTAMP/NOW 等时间漂移）
    obj: AsyncConnection 或 AsyncSession
    """
    if not DB_SET_TIME_ZONE_ENABLED:
        return
    try:
        await obj.execute(text("SET time_zone = :tz"), {"tz": DB_TIME_ZONE})
    except Exception:
        logger.exception("SET time_zone failed (tz=%s)", DB_TIME_ZONE)
        # 不强行 raise：避免因权限/配置差异导致服务无法启动


async def _ocr_poller_loop(stop_event: asyncio.Event) -> None:
    """
    ✅ OCR 扫描兜底：
    - 定时扫描 pending 且 active_scope_id 非空的任务
    - 调用 run_ocr_task(task_id) 执行（内部会 claim，保证幂等/抢占）
    """
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
    # startup
    await init_redis()

    # ✅ 强制 import 所有模型，确保 metadata 完整
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

    # ✅ 只有启用时才创建 poller task（避免无意义后台协程）
    if OCR_POLL_ENABLED:
        stop_event = asyncio.Event()
        app.state.ocr_poller_stop_event = stop_event
        app.state.ocr_poller_task = asyncio.create_task(_ocr_poller_loop(stop_event))
    else:
        logger.info("[ocr_poller] disabled (OCR_POLL_ENABLED=0)")

    try:
        yield
    finally:
        # shutdown
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

# ✅ 静态挂载（app 创建后）
try:
    local_root = Path(getattr(settings, "LOCAL_STORAGE_ROOT", "./storage")).resolve()
    local_root.mkdir(parents=True, exist_ok=True)
    app.mount(
        getattr(settings, "LOCAL_PUBLIC_PREFIX", "/static"),
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


# ✅ 新增：健康检查（极简）
@app.get("/api/health")
async def health():
    db_ok = True
    redis_ok = True

    # DB ping
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    # Redis ping（有就测，没有就当作不影响）
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


# ✅ 注册路由（统一走 /api 前缀，配合前端 Vite proxy）
app.include_router(v1_router, prefix="/api")
