from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pymysql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from env_loader import load_backend_env  # noqa: E402
from app.core.db import Base, engine, ensure_schema_additive_on_startup, load_all_models  # noqa: E402

load_backend_env()


CONFIRM_TEXT = "RESET_LOCAL_DB_FOR_ONLINE_SNAPSHOT"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _json_dumps(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2, default=_json_default)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _env_value(key: str, default: str = "") -> str:
    value = os.getenv(key, default)
    return str(value or "").strip().strip('"').strip("'")


def _db_config() -> dict[str, Any]:
    return {
        "host": _env_value("DB_HOST", "127.0.0.1"),
        "port": int(_env_value("DB_PORT", "3306")),
        "user": _env_value("DB_USER"),
        "password": _env_value("DB_PASSWORD"),
        "database": _env_value("DB_NAME"),
        "charset": "utf8mb4",
    }


def _db_conn(*, autocommit: bool = False):
    cfg = _db_config()
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg["charset"],
        autocommit=autocommit,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _quote_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _guard_local(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _db_config()
    host = str(cfg["host"] or "").strip().lower()
    db_name = str(cfg["database"] or "").strip()
    if args.confirm != CONFIRM_TEXT:
        raise SystemExit(f"refused: pass --confirm {CONFIRM_TEXT}")
    if host not in LOCAL_HOSTS and not args.allow_non_local:
        raise SystemExit(
            f"refused: DB_HOST={cfg['host']!r} is not local. "
            "Use --allow-non-local only if you intentionally want to reset that database."
        )
    if not db_name:
        raise SystemExit("refused: DB_NAME is empty")
    return {"host": cfg["host"], "port": cfg["port"], "database": db_name, "user": cfg["user"]}


def _model_tables() -> list[str]:
    load_all_models()
    return sorted(Base.metadata.tables.keys())


def _show_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        rows = cur.fetchall()
    return sorted([str(list(row.values())[0]) for row in rows])


def _row_counts(conn, tables: list[str]) -> dict[str, int | str]:
    out: dict[str, int | str] = {}
    with conn.cursor() as cur:
        for table in tables:
            try:
                cur.execute(f"SELECT COUNT(*) c FROM {_quote_ident(table)}")
                out[table] = int(cur.fetchone()["c"])
            except Exception as exc:
                out[table] = str(exc)
    return out


def _table_auto_increment(conn, tables: list[str]) -> dict[str, int | None]:
    if not tables:
        return {}
    placeholders = ",".join(["%s"] * len(tables))
    sql = (
        "SELECT TABLE_NAME, AUTO_INCREMENT "
        "FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN (" + placeholders + ")"
    )
    params = [_db_config()["database"], *tables]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return {str(row["TABLE_NAME"]): row.get("AUTO_INCREMENT") for row in rows}


def _db_indexes(conn, tables: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not tables:
        return {}
    placeholders = ",".join(["%s"] * len(tables))
    sql = (
        "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, "
        "GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) cols "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN (" + placeholders + ") "
        "GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE "
        "ORDER BY TABLE_NAME, INDEX_NAME"
    )
    params = [_db_config()["database"], *tables]
    grouped: dict[str, list[dict[str, Any]]] = {table: [] for table in tables}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            grouped.setdefault(str(row["TABLE_NAME"]), []).append(
                {
                    "name": row["INDEX_NAME"],
                    "unique": int(row["NON_UNIQUE"] or 0) == 0,
                    "cols": str(row["cols"] or "").split(",") if row.get("cols") else [],
                }
            )
    return grouped


def _model_index_signatures() -> dict[str, set[tuple[bool, tuple[str, ...]]]]:
    out: dict[str, set[tuple[bool, tuple[str, ...]]]] = {}
    for table_name, table in Base.metadata.tables.items():
        signatures: set[tuple[bool, tuple[str, ...]]] = set()
        for idx in table.indexes:
            cols = tuple(col.name for col in idx.columns)
            if cols:
                signatures.add((bool(idx.unique), cols))
        for constraint in table.constraints:
            cols = tuple(getattr(col, "name", "") for col in getattr(constraint, "columns", []) if getattr(col, "name", ""))
            if not cols:
                continue
            name = constraint.__class__.__name__.lower()
            if "primarykey" in name or "unique" in name:
                signatures.add((True, cols))
        out[table_name] = signatures
    return out


def _missing_model_index_signatures(db_index_map: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    model = _model_index_signatures()
    missing: dict[str, list[dict[str, Any]]] = {}
    for table_name, signatures in model.items():
        db_signatures = {
            (bool(idx["unique"]), tuple(idx["cols"]))
            for idx in db_index_map.get(table_name, [])
            if idx.get("cols")
        }
        for unique, cols in sorted(signatures, key=lambda item: (item[1], item[0])):
            if (unique, cols) not in db_signatures:
                missing.setdefault(table_name, []).append({"unique": unique, "cols": list(cols)})
    return missing


def _drop_extra_tables(conn, extra_tables: list[str]) -> list[str]:
    dropped: list[str] = []
    with conn.cursor() as cur:
        for table in extra_tables:
            cur.execute(f"DROP TABLE IF EXISTS {_quote_ident(table)}")
            dropped.append(table)
    return dropped


def _truncate_used_tables(conn, model_tables: list[str]) -> list[str]:
    truncated: list[str] = []
    with conn.cursor() as cur:
        for table in model_tables:
            cur.execute(f"TRUNCATE TABLE {_quote_ident(table)}")
            truncated.append(table)
    return truncated


async def _restore_schema_indexes() -> dict[str, Any]:
    try:
        return await ensure_schema_additive_on_startup(
            add_tables=True,
            add_columns=False,
            add_indexes=True,
            log_details=False,
            strict_add_columns=True,
        )
    finally:
        await engine.dispose()


def reset_local_db(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    guard = _guard_local(args)
    model_tables = _model_tables()

    conn = _db_conn(autocommit=False)
    try:
        before_tables = _show_tables(conn)
        before_counts = _row_counts(conn, before_tables)
        extra_tables = sorted(set(before_tables) - set(model_tables))
        missing_model_tables_before = sorted(set(model_tables) - set(before_tables))
        before_indexes = _db_indexes(conn, [table for table in model_tables if table in before_tables])
        missing_indexes_before = _missing_model_index_signatures(before_indexes)

        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
        dropped = _drop_extra_tables(conn, extra_tables)
        existing_model_tables = [table for table in model_tables if table in before_tables]
        truncated = _truncate_used_tables(conn, existing_model_tables)
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    except Exception:
        try:
            with conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS=1")
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    schema_result = asyncio.run(_restore_schema_indexes())

    verify_conn = _db_conn(autocommit=True)
    try:
        after_tables = _show_tables(verify_conn)
        after_counts = _row_counts(verify_conn, [table for table in model_tables if table in after_tables])
        after_indexes = _db_indexes(verify_conn, [table for table in model_tables if table in after_tables])
        auto_increment = _table_auto_increment(verify_conn, [table for table in model_tables if table in after_tables])
    finally:
        verify_conn.close()

    extra_after = sorted(set(after_tables) - set(model_tables))
    missing_model_tables_after = sorted(set(model_tables) - set(after_tables))
    missing_indexes_after = _missing_model_index_signatures(after_indexes)
    non_empty_after = {table: count for table, count in after_counts.items() if count != 0}

    ok = not extra_after and not missing_model_tables_after and not non_empty_after and not missing_indexes_after
    report = {
        "ok": ok,
        "mode": "local_db_reset_for_online_snapshot",
        "guard": guard,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "summary": {
            "model_table_count": len(model_tables),
            "before_table_count": len(before_tables),
            "before_extra_table_count": len(extra_tables),
            "dropped_table_count": len(dropped),
            "truncated_table_count": len(truncated),
            "after_table_count": len(after_tables),
            "non_empty_after_count": len(non_empty_after),
            "missing_model_table_count_after": len(missing_model_tables_after),
            "extra_table_count_after": len(extra_after),
            "missing_index_signature_count_after": sum(len(v) for v in missing_indexes_after.values()),
        },
        "model_tables": model_tables,
        "before": {
            "tables": before_tables,
            "row_counts": before_counts,
            "extra_tables": extra_tables,
            "missing_model_tables": missing_model_tables_before,
            "missing_index_signatures": missing_indexes_before,
        },
        "actions": {
            "dropped_tables": dropped,
            "truncated_tables": truncated,
            "schema_result": schema_result,
        },
        "after": {
            "tables": after_tables,
            "row_counts": after_counts,
            "non_empty_tables": non_empty_after,
            "extra_tables": extra_after,
            "missing_model_tables": missing_model_tables_after,
            "missing_index_signatures": missing_indexes_after,
            "auto_increment": auto_increment,
        },
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(_json_dumps(report, pretty=True), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reset local DB for importing an online snapshot.")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--allow-non-local", action="store_true")
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path(f"logs/local-db-reset-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.report_path = args.report_path.resolve()
    report = reset_local_db(args)
    print(
        _json_dumps(
            {
                "ok": report["ok"],
                "report": str(args.report_path),
                "summary": report["summary"],
                "dropped_tables": report["actions"]["dropped_tables"],
            },
            pretty=True,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
