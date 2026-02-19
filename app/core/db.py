# app/core/db.py
# encoding: utf-8
"""
数据库与 Redis 连接管理

新增能力（启动期可用）：
- Schema 校验日志：打印 DB 现有表 / Model 表 / 缺失表 / 多余表 / 缺失列
- 只增不删：仅允许新增缺失列（ALTER TABLE ADD COLUMN），不允许删列/改列/改类型
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Optional, Dict, List, Tuple

from sqlalchemy import event, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.schema import CreateColumn

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


def _is_duplicate_column_error(e: Exception) -> bool:
    msg = (str(e) or "").lower()
    # MySQL: "Duplicate column name"
    # PostgreSQL: "duplicate column" / "already exists"
    return ("duplicate column" in msg) or ("already exists" in msg and "column" in msg) or ("duplicate column name" in msg)


def _quote_ident(conn, name: str) -> str:
    try:
        prep = conn.dialect.identifier_preparer
        return prep.quote(name)
    except Exception:
        return f'"{name}"'


def _compile_add_column_ddl(conn, table_name: str, col) -> str:
    # CreateColumn 会生成：`remark VARCHAR(1024)` 之类（带列名）
    col_ddl = str(CreateColumn(col).compile(dialect=conn.dialect)).strip()
    t = _quote_ident(conn, table_name)
    return f"ALTER TABLE {t} ADD COLUMN {col_ddl}"


def _model_table_names() -> List[str]:
    return sorted([t.name for t in Base.metadata.sorted_tables])


def _diff_tables(conn) -> Tuple[List[str], List[str], List[str]]:
    insp = inspect(conn)
    db_tables = sorted(insp.get_table_names())
    model_tables = _model_table_names()

    db_set = set(db_tables)
    model_set = set(model_tables)

    missing_in_db = sorted(list(model_set - db_set))
    extra_in_db = sorted(list(db_set - model_set))

    return db_tables, missing_in_db, extra_in_db


def _diff_missing_columns(conn) -> Dict[str, List[str]]:
    """
    返回：table -> missing column names（按 model metadata 视角）
    仅对 DB 已存在的表做列对比。
    """
    insp = inspect(conn)
    db_tables = set(insp.get_table_names())

    out: Dict[str, List[str]] = {}
    for t in Base.metadata.sorted_tables:
        tn = t.name
        if tn not in db_tables:
            continue
        try:
            db_cols = insp.get_columns(tn) or []
            db_col_names = set([str(c.get("name") or "") for c in db_cols if (c.get("name") or "")])
        except Exception:
            db_col_names = set()

        model_col_names = set([c.name for c in t.columns])
        missing = sorted(list(model_col_names - db_col_names))
        if missing:
            out[tn] = missing
    return out


def _get_model_column(conn, table_name: str, col_name: str):
    # Base.metadata.tables.get() 返回 Table 或 None
    # SQLAlchemy 的 Table/ClauseElement 不能用于 bool 判断（会抛 TypeError），必须用 is None
    t = Base.metadata.tables.get(table_name)
    if t is None:
        return None
    return t.columns.get(col_name)


def _is_unsafe_notnull_without_default(col) -> bool:
    """
    对已有历史行的表，新增 NOT NULL 且无默认值 的列，常见会失败或引入风险。
    这里用于严格模式拦截。
    """
    try:
        if getattr(col, "nullable", True):
            return False
        # SQLAlchemy Column 的 default / server_default
        has_default = (getattr(col, "default", None) is not None) or (getattr(col, "server_default", None) is not None)
        return not has_default
    except Exception:
        return True


async def ensure_schema_additive_on_startup(
    *,
    add_tables: bool,
    add_columns: bool,
    log_details: bool = True,
    strict_add_columns: bool = True,
) -> Dict[str, object]:
    """
    启动期 schema 处理：
    - 先校验并打印：DB 表 / 缺表 / 多表 / 缺列
    - add_tables=True 时：create_all(checkfirst=True)
    - add_columns=True 时：仅对缺失列执行 ALTER TABLE ADD COLUMN
    - 不做删表/删列/改列/改类型
    """
    load_all_models()

    result: Dict[str, object] = {
        "db_tables": [],
        "model_tables": [],
        "missing_tables": [],
        "extra_tables": [],
        "missing_columns": {},   # table -> [col...]
        "added_columns": [],     # ["table.col", ...]
        "created_tables": [],    # ["table", ...]
    }

    async with engine.begin() as conn:
        def _sync_work(sync_conn):
            # --- phase 1: inspect before any change ---
            db_tables, missing_tables, extra_tables = _diff_tables(sync_conn)
            model_tables = _model_table_names()
            missing_cols = _diff_missing_columns(sync_conn)

            result["db_tables"] = db_tables
            result["model_tables"] = model_tables
            result["missing_tables"] = missing_tables
            result["extra_tables"] = extra_tables
            result["missing_columns"] = missing_cols

            if log_details:
                logger.info("[schema_check] db_tables=%s", ",".join(db_tables) if db_tables else "-")
                logger.info("[schema_check] model_tables=%s", ",".join(model_tables) if model_tables else "-")
                logger.info("[schema_check] missing_tables(in_db)=%s", ",".join(missing_tables) if missing_tables else "-")
                logger.info("[schema_check] extra_tables(in_db_not_in_model)=%s", ",".join(extra_tables) if extra_tables else "-")
                if missing_cols:
                    for tn, cols in missing_cols.items():
                        logger.info("[schema_check] missing_columns table=%s cols=%s", tn, ",".join(cols))
                else:
                    logger.info("[schema_check] missing_columns none")

            # --- phase 2: create missing tables (optional) ---
            if add_tables:
                # create_all 自带 checkfirst=True 逻辑
                before_set = set(db_tables)
                Base.metadata.create_all(bind=sync_conn)
                # refresh inspector
                insp2 = inspect(sync_conn)
                after_tables = sorted(insp2.get_table_names())
                created = sorted(list(set(after_tables) - before_set))
                if created:
                    result["created_tables"] = created
                    logger.info("[schema_apply] created_tables=%s", ",".join(created))

            # --- phase 3: add missing columns (optional) ---
            if add_columns:
                # refresh missing columns after create_all
                missing_cols2 = _diff_missing_columns(sync_conn)
                if missing_cols2:
                    for tn, cols in missing_cols2.items():
                        for cn in cols:
                            col = _get_model_column(sync_conn, tn, cn)
                            if col is None:
                                continue

                            if strict_add_columns and _is_unsafe_notnull_without_default(col):
                                raise RuntimeError(
                                    f"[schema_apply] refuse ADD COLUMN {tn}.{cn}: NOT NULL without default (strict mode)"
                                )

                            ddl = _compile_add_column_ddl(sync_conn, tn, col)
                            try:
                                sync_conn.execute(text(ddl))
                                result["added_columns"].append(f"{tn}.{cn}")
                                logger.info("[schema_apply] added_column=%s.%s", tn, cn)
                            except Exception as e:
                                if _is_duplicate_column_error(e):
                                    # 幂等：并发启动或重复执行，视为成功
                                    logger.warning("[schema_apply] duplicate_column_ignored=%s.%s (%s)", tn, cn, e)
                                    continue
                                raise

                else:
                    logger.info("[schema_apply] add_columns enabled but no missing columns")

            # --- phase 4: final summary ---
            if log_details:
                try:
                    insp3 = inspect(sync_conn)
                    final_tables = sorted(insp3.get_table_names())
                    logger.info("[schema_final] db_tables=%s", ",".join(final_tables) if final_tables else "-")
                except Exception:
                    pass

            return True

        try:
            await conn.run_sync(_sync_work)
        except SQLAlchemyError as e:
            logger.exception("[schema_apply] SQLAlchemyError: %s", e)
            raise
        except Exception as e:
            logger.exception("[schema_apply] failed: %s", e)
            raise

    return result


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
