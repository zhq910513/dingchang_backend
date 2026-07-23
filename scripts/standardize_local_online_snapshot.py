from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pymysql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from env_loader import load_backend_env  # noqa: E402
from app.core.db import Base, engine, ensure_schema_additive_on_startup, load_all_models  # noqa: E402
from app.services.ocr_cleaner import CLEANING_RULE_VERSION, clean_dynamic_data_for_ocr  # noqa: E402
from app.services.order_fact_service import build_order_fact_payload  # noqa: E402

load_backend_env()


CONFIRM_TEXT = "STANDARDIZE_LOCAL_ONLINE_SNAPSHOT"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
FACT_FIELDS = (
    "owner_name",
    "plate_no",
    "vin",
    "engine_no",
    "vehicle_model",
    "first_register_date",
    "id_number",
)


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
        read_timeout=600,
        write_timeout=600,
        max_allowed_packet=1024 * 1024 * 1024,
    )


def _guard_local(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _db_config()
    host = str(cfg["host"] or "").strip().lower()
    if args.confirm != CONFIRM_TEXT:
        raise SystemExit(f"refused: pass --confirm {CONFIRM_TEXT}")
    if host not in LOCAL_HOSTS and not args.allow_non_local:
        raise SystemExit(f"refused: DB_HOST={cfg['host']!r} is not local")
    return {"host": cfg["host"], "port": cfg["port"], "database": cfg["database"], "user": cfg["user"]}


def _quote_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _model_tables() -> list[str]:
    load_all_models()
    return sorted(Base.metadata.tables.keys())


def _show_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        return sorted([str(list(row.values())[0]) for row in cur.fetchall()])


def _row_counts(conn, tables: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT COUNT(*) c FROM {_quote_ident(table)}")
            out[table] = int(cur.fetchone()["c"])
    return out


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _field_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changes[key] = {"before": before.get(key), "after": after.get(key)}
    return changes


def _sync_order_fact(cur, order_id: int, dynamic_data: dict[str, Any]) -> None:
    payload = build_order_fact_payload(dynamic_data)
    sql = """
        INSERT INTO order_fact_new (
            order_id, owner_name, plate_no, vin, engine_no,
            vehicle_model, first_register_date, id_number, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP
        )
        ON DUPLICATE KEY UPDATE
            owner_name=VALUES(owner_name),
            plate_no=VALUES(plate_no),
            vin=VALUES(vin),
            engine_no=VALUES(engine_no),
            vehicle_model=VALUES(vehicle_model),
            first_register_date=VALUES(first_register_date),
            id_number=VALUES(id_number),
            updated_at=CURRENT_TIMESTAMP
    """
    cur.execute(
        sql,
        (
            int(order_id),
            payload.get("owner_name"),
            payload.get("plate_no"),
            payload.get("vin"),
            payload.get("engine_no"),
            payload.get("vehicle_model"),
            payload.get("first_register_date"),
            payload.get("id_number"),
        ),
    )


def _model_index_signatures() -> dict[str, set[tuple[bool, tuple[str, ...]]]]:
    out: dict[str, set[tuple[bool, tuple[str, ...]]]] = {}
    for table_name, table in Base.metadata.tables.items():
        signatures: set[tuple[bool, tuple[str, ...]]] = set()
        for idx in table.indexes:
            cols = tuple(col.name for col in idx.columns)
            if cols:
                signatures.add((bool(idx.unique), cols))
        for constraint in table.constraints:
            cols = tuple(
                getattr(col, "name", "") for col in getattr(constraint, "columns", []) if getattr(col, "name", "")
            )
            if not cols:
                continue
            name = constraint.__class__.__name__.lower()
            if "primarykey" in name or "unique" in name:
                signatures.add((True, cols))
        out[table_name] = signatures
    return out


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


def _missing_model_indexes(db_index_map: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    model = _model_index_signatures()
    missing: dict[str, list[dict[str, Any]]] = {}
    for table_name, signatures in model.items():
        db_signatures = {
            (bool(idx["unique"]), tuple(idx["cols"])) for idx in db_index_map.get(table_name, []) if idx.get("cols")
        }
        for unique, cols in sorted(signatures, key=lambda item: (item[1], item[0])):
            if (unique, cols) not in db_signatures:
                missing.setdefault(table_name, []).append({"unique": unique, "cols": list(cols)})
    return missing


async def _ensure_indexes() -> dict[str, Any]:
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


def standardize(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    guard = _guard_local(args)
    model_tables = _model_tables()

    conn = _db_conn(autocommit=False)
    field_stats: dict[str, Counter] = defaultdict(Counter)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    changed_rows = 0
    total_rows = 0
    fact_rows = 0

    try:
        before_tables = _show_tables(conn)
        before_counts = _row_counts(conn, before_tables)
        extra_tables = sorted(set(before_tables) - set(model_tables))

        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            cur.execute("TRUNCATE TABLE order_fact_new")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()

        last_id = 0
        while True:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, dynamic_data, updated_at
                    FROM order_new
                    WHERE id > %s
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (last_id, args.batch_size),
                )
                rows = cur.fetchall()
            if not rows:
                break

            try:
                with conn.cursor() as cur:
                    for row in rows:
                        total_rows += 1
                        order_id = int(row["id"])
                        last_id = order_id
                        before = _as_dict(row.get("dynamic_data"))
                        after = clean_dynamic_data_for_ocr(before)
                        changes = _field_changes(before, after)
                        if changes:
                            changed_rows += 1
                            for field_name, change in changes.items():
                                old = change.get("before")
                                new = change.get("after")
                                field_stats[field_name]["changed"] += 1
                                if old not in (None, "") and new in (None, ""):
                                    field_stats[field_name]["nullified"] += 1
                                elif old in (None, "") and new not in (None, ""):
                                    field_stats[field_name]["filled"] += 1
                                if field_name not in before and field_name in after:
                                    field_stats[field_name]["added"] += 1
                                if field_name in before and field_name not in after:
                                    field_stats[field_name]["removed"] += 1
                                if len(examples[field_name]) < args.example_limit:
                                    examples[field_name].append(
                                        {"order_id": order_id, "before": old, "after": new}
                                    )
                            cur.execute(
                                "UPDATE order_new SET dynamic_data=%s, updated_at=%s WHERE id=%s",
                                (_json_dumps(after), row.get("updated_at"), order_id),
                            )
                        _sync_order_fact(cur, order_id, after)
                        fact_rows += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in extra_tables:
                cur.execute(f"DROP TABLE IF EXISTS {_quote_ident(table)}")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    finally:
        conn.close()

    schema_result = asyncio.run(_ensure_indexes())

    verify_conn = _db_conn(autocommit=True)
    try:
        after_tables = _show_tables(verify_conn)
        after_counts = _row_counts(verify_conn, after_tables)
        after_indexes = _db_indexes(verify_conn, [table for table in model_tables if table in after_tables])
    finally:
        verify_conn.close()

    extra_after = sorted(set(after_tables) - set(model_tables))
    missing_tables_after = sorted(set(model_tables) - set(after_tables))
    missing_indexes_after = _missing_model_indexes(after_indexes)

    ok = (
        not extra_after
        and not missing_tables_after
        and not missing_indexes_after
        and int(after_counts.get("order_fact_new", 0)) == int(after_counts.get("order_new", -1))
    )

    report = {
        "ok": ok,
        "mode": "standardize_local_online_snapshot",
        "guard": guard,
        "rule_version": CLEANING_RULE_VERSION,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "summary": {
            "before_table_count": len(before_tables),
            "extra_table_count_before": len(extra_tables),
            "dropped_extra_table_count": len(extra_tables),
            "after_table_count": len(after_tables),
            "order_rows": total_rows,
            "changed_order_rows": changed_rows,
            "rebuilt_order_fact_rows": fact_rows,
            "order_fact_count_after": after_counts.get("order_fact_new"),
            "missing_model_table_count_after": len(missing_tables_after),
            "extra_table_count_after": len(extra_after),
            "missing_model_index_count_after": sum(len(v) for v in missing_indexes_after.values()),
        },
        "before": {
            "tables": before_tables,
            "row_counts": before_counts,
            "extra_tables": extra_tables,
        },
        "actions": {
            "dropped_extra_tables": extra_tables,
            "schema_result": schema_result,
        },
        "cleaning": {
            "field_stats": {key: dict(value) for key, value in sorted(field_stats.items())},
            "examples": {key: value for key, value in sorted(examples.items())},
        },
        "after": {
            "tables": after_tables,
            "row_counts": after_counts,
            "extra_tables": extra_after,
            "missing_model_tables": missing_tables_after,
            "missing_model_indexes": missing_indexes_after,
        },
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(_json_dumps(report, pretty=True), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean local online snapshot and convert it to the current model schema.")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--allow-non-local", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--example-limit", type=int, default=20)
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path(f"logs/local-standardized-snapshot-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.batch_size = max(1, int(args.batch_size or 500))
    args.report_path = args.report_path.resolve()
    report = standardize(args)
    print(
        _json_dumps(
            {
                "ok": report["ok"],
                "report": str(args.report_path),
                "summary": report["summary"],
            },
            pretty=True,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
