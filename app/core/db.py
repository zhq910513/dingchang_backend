# app/core/db.py
# encoding: utf-8
"""数据库与 Redis 连接管理

能力：
- Schema 校验日志：打印 DB 现有表 / Model 表 / 缺失表 / 多余表 / 缺失列
- 只增不删：仅允许新增缺失列（ALTER TABLE ADD COLUMN），不允许删列/改列/改类型

注意：
- 本模块的 schema 自愈逻辑是“增量补齐”（additive），不会修改已有列定义（例如 updated_at 的 ON UPDATE 等），
  列定义差异需要通过 DBA/迁移脚本或显式变更来完成。
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import event, inspect as sa_inspect, text
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

# ✅ AsyncSession factory（统一入口）
async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ✅ models 导入仅执行一次（connect 事件会随连接触发，必须防重入）
_models_loaded = False

# ✅ Redis client（可选启用；init_redis() 赋值）
redis: Optional[Any] = None


@event.listens_for(engine.sync_engine, "connect")
def load_all_models(dbapi_connection=None, connection_record=None) -> None:
    """导入 ORM 模型，确保 Base.metadata 完整。

    重要：
    - 该函数注册为 SQLAlchemy connect 事件回调，事件会传入 (dbapi_connection, connection_record)；
      这里用可选参数兼容事件签名，同时也兼容模块内“无参显式调用”。
    - 强制收口：只导入一次 app.models 聚合模块（由 app/models/__init__.py 统一 import 全部模型）。
    """
    global _models_loaded

    # 显式消费回调参数，避免静态检查误报（不影响逻辑）
    _ = dbapi_connection
    _ = connection_record

    if _models_loaded:
        return

    try:
        importlib.import_module("app.models")
        _models_loaded = True
    except Exception as exc:
        # 软失败：避免某个模型导入异常导致服务直接起不来；schema_check 可能因此不完整，日志会体现。
        logger.warning("Model module import skipped: %s (%s)", "app.models", exc)


async def get_db():
    """FastAPI DB 依赖：提供 AsyncSession。"""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise


def _is_duplicate_column_error(exc: Exception) -> bool:
    msg = (str(exc) or "").lower()
    # MySQL: "Duplicate column name"
    # PostgreSQL: "duplicate column" / "already exists"
    return (
            "duplicate column name" in msg
            or "duplicate column" in msg
            or ("already exists" in msg and "column" in msg)
    )


def _quote_ident(conn, name: str) -> str:
    try:
        prep = conn.dialect.identifier_preparer
        return prep.quote(name)
    except Exception:
        return f'"{name}"'


def _compile_add_column_ddl(conn, table_name: str, col) -> str:
    """生成 ALTER TABLE ADD COLUMN 的 DDL。"""
    col_ddl = str(CreateColumn(col).compile(dialect=conn.dialect)).strip()
    tname = _quote_ident(conn, table_name)
    return f"ALTER TABLE {tname} ADD COLUMN {col_ddl}"


def _model_table_names() -> List[str]:
    return sorted([t.name for t in Base.metadata.sorted_tables])


def _diff_tables(conn) -> Tuple[List[str], List[str], List[str]]:
    insp = sa_inspect(conn)
    db_tables = sorted(insp.get_table_names())
    model_tables = _model_table_names()

    db_set = set(db_tables)
    model_set = set(model_tables)

    missing_in_db = sorted(list(model_set - db_set))
    extra_in_db = sorted(list(db_set - model_set))

    return db_tables, missing_in_db, extra_in_db


def _diff_missing_columns(conn) -> Dict[str, List[str]]:
    """返回：table -> missing column names（按 model metadata 视角）。"""
    insp = sa_inspect(conn)
    db_tables = set(insp.get_table_names())

    out: Dict[str, List[str]] = {}
    for table in Base.metadata.sorted_tables:
        tn = table.name
        if tn not in db_tables:
            continue

        try:
            db_cols = insp.get_columns(tn) or []
            db_col_names = {
                str(c.get("name") or "") for c in db_cols if (c.get("name") or "")
            }
        except Exception:
            db_col_names = set()

        model_col_names = {c.name for c in table.columns}
        missing = sorted(list(model_col_names - db_col_names))
        if missing:
            out[tn] = missing

    return out


def _get_model_column(table_name: str, col_name: str):
    """从 Base.metadata 取 Column；不存在返回 None。"""
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return None
    return table.columns.get(col_name)


def _is_unsafe_notnull_without_default(col) -> bool:
    """严格模式拦截：新增 NOT NULL 且无默认值的列。"""
    try:
        if getattr(col, "nullable", True):
            return False
        has_default = (
                getattr(col, "default", None) is not None
                or getattr(col, "server_default", None) is not None
        )
        return not has_default
    except Exception:
        return True


async def ensure_schema_additive_on_startup(
        *,
        add_tables: bool,
        add_columns: bool,
        log_details: bool = True,
        strict_add_columns: bool = True,
) -> Dict[str, Any]:
    """启动期 schema 处理（只增不删）。"""
    # 确保 Base.metadata 完整（即使 connect 事件未触发，这里也强制导入一次）
    load_all_models()

    result: Dict[str, Any] = {
        "db_tables": [],
        "model_tables": [],
        "missing_tables": [],
        "extra_tables": [],
        "missing_columns": {},  # table -> [col...]
        "added_columns": [],  # ["table.col", ...]
        "created_tables": [],  # ["table", ...]
    }

    async with engine.begin() as conn:

        def _sync_work(sync_conn):
            # phase 1: inspect before any change
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
                logger.info(
                    "[schema_check] missing_tables(in_db)=%s",
                    ",".join(missing_tables) if missing_tables else "-",
                )
                logger.info(
                    "[schema_check] extra_tables(in_db_not_in_model)=%s",
                    ",".join(extra_tables) if extra_tables else "-",
                )
                if missing_cols:
                    for tn, cols in missing_cols.items():
                        logger.info("[schema_check] missing_columns table=%s cols=%s", tn, ",".join(cols))
                else:
                    logger.info("[schema_check] missing_columns none")

            # phase 2: create missing tables (optional)
            if add_tables:
                before_set = set(db_tables)
                Base.metadata.create_all(bind=sync_conn)
                insp2 = sa_inspect(sync_conn)
                after_tables = sorted(insp2.get_table_names())
                created = sorted(list(set(after_tables) - before_set))
                if created:
                    result["created_tables"] = created
                    logger.info("[schema_apply] created_tables=%s", ",".join(created))

            # phase 3: add missing columns (optional)
            if add_columns:
                missing_cols2 = _diff_missing_columns(sync_conn)
                if missing_cols2:
                    added_columns: List[str] = result["added_columns"]
                    for tn, cols in missing_cols2.items():
                        for cn in cols:
                            col = _get_model_column(tn, cn)
                            if col is None:
                                continue

                            if strict_add_columns and _is_unsafe_notnull_without_default(col):
                                raise RuntimeError(
                                    f"[schema_apply] refuse ADD COLUMN {tn}.{cn}: NOT NULL without default (strict mode)"
                                )

                            ddl = _compile_add_column_ddl(sync_conn, tn, col)
                            try:
                                sync_conn.execute(text(ddl))
                                added_columns.append(f"{tn}.{cn}")
                                logger.info("[schema_apply] added_column=%s.%s", tn, cn)
                            except Exception as error:
                                if _is_duplicate_column_error(error):
                                    logger.warning(
                                        "[schema_apply] duplicate_column_ignored=%s.%s (%s)", tn, cn, exc
                                    )
                                    continue
                                raise
                else:
                    logger.info("[schema_apply] add_columns enabled but no missing columns")

            # phase 4: final summary
            if log_details:
                try:
                    insp3 = sa_inspect(sync_conn)
                    final_tables = sorted(insp3.get_table_names())
                    logger.info("[schema_final] db_tables=%s", ",".join(final_tables) if final_tables else "-")
                except Exception:
                    pass

            return True

        try:
            await conn.run_sync(_sync_work)
        except SQLAlchemyError as exc:
            logger.exception("[schema_apply] SQLAlchemyError: %s", exc)
            raise
        except Exception as exc:
            logger.exception("[schema_apply] failed: %s", exc)
            raise

    return result


async def init_redis():
    """初始化 Redis（可选）；成功返回 redis client，否则返回 None。"""
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
        except Exception as exc:
            redis = None
            logger.warning("Redis unreachable, disabled (url=%s): %s", url, exc)
            return None

        redis = r
        logger.info("Redis enabled (url=%s)", url)
        return redis
    except Exception as exc:
        redis = None
        logger.warning("Redis init failed, disabled (url=%s): %s", url, exc)
        return None


async def close_redis():
    """关闭 Redis 连接（若启用）。"""
    global redis
    if redis is None:
        return

    try:
        close_fn = getattr(redis, "close", None)
        if close_fn is not None:
            res = close_fn()
            if inspect.isawaitable(res):
                await res

        cp = getattr(redis, "connection_pool", None)
        disconnect = getattr(cp, "disconnect", None) if cp is not None else None
        if disconnect is not None:
            res2 = disconnect()
            if inspect.isawaitable(res2):
                await res2
    finally:
        redis = None
