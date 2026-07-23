from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymysql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from env_loader import load_backend_env  # noqa: E402
from app.core.db import Base, load_all_models  # noqa: E402

load_backend_env()


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
FINANCE_NUMERIC_COLUMNS = (
    "commercial_amount",
    "compulsory_amount",
    "vehicle_tax_amount",
    "non_vehicle_amount",
    "premium_total",
    "channel_commercial_point",
    "channel_commercial_supplement_point",
    "channel_compulsory_point",
    "channel_vehicle_tax_point",
    "channel_non_vehicle_point",
    "channel_reward",
    "channel_total",
    "customer_commercial_point",
    "customer_commercial_supplement_point",
    "customer_compulsory_point",
    "customer_vehicle_tax_point",
    "customer_non_vehicle_point",
    "customer_reward",
    "customer_total",
    "profit",
)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _json_dumps(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=_json_default)


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


def _guard_local(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _db_config()
    host = str(cfg["host"] or "").strip().lower()
    if host not in LOCAL_HOSTS and not args.allow_non_local:
        raise SystemExit(f"refused: DB_HOST={cfg['host']!r} is not local")
    return {"host": cfg["host"], "port": cfg["port"], "database": cfg["database"], "user": cfg["user"]}


def _db_conn():
    cfg = _db_config()
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg["charset"],
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        read_timeout=600,
        write_timeout=600,
        max_allowed_packet=1024 * 1024 * 1024,
    )


def _quote_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _model_tables() -> list[str]:
    load_all_models()
    return sorted(Base.metadata.tables.keys())


def _column_meta(conn, table: str) -> list[dict[str, Any]]:
    sql = """
        SELECT COLUMN_NAME, DATA_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s
          AND TABLE_NAME=%s
        ORDER BY ORDINAL_POSITION
    """
    with conn.cursor() as cur:
        cur.execute(sql, (_db_config()["database"], table))
        return [{"name": str(row["COLUMN_NAME"]), "data_type": str(row["DATA_TYPE"]).lower()} for row in cur.fetchall()]


def _primary_key_columns(conn, table: str) -> list[str]:
    sql = """
        SELECT COLUMN_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA=%s
          AND TABLE_NAME=%s
          AND CONSTRAINT_NAME='PRIMARY'
        ORDER BY ORDINAL_POSITION
    """
    with conn.cursor() as cur:
        cur.execute(sql, (_db_config()["database"], table))
        return [str(row["COLUMN_NAME"]) for row in cur.fetchall()]


def _table_exists(conn, table: str) -> bool:
    sql = """
        SELECT COUNT(*) c
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA=%s
          AND TABLE_NAME=%s
          AND TABLE_TYPE='BASE TABLE'
    """
    with conn.cursor() as cur:
        cur.execute(sql, (_db_config()["database"], table))
        return int(cur.fetchone()["c"] or 0) > 0


def _normal_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _normal_value(value: Any, *, data_type: str) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if data_type == "json":
        return _normal_json(value)
    return value


def _hash_table(conn, table: str) -> dict[str, Any]:
    meta = _column_meta(conn, table)
    columns = [item["name"] for item in meta]
    data_types = {item["name"]: item["data_type"] for item in meta}
    pk_cols = _primary_key_columns(conn, table)
    order_cols = pk_cols or columns

    col_sql = ", ".join(_quote_ident(col) for col in columns)
    order_sql = ", ".join(_quote_ident(col) for col in order_cols)
    sql = f"SELECT {col_sql} FROM {_quote_ident(table)} ORDER BY {order_sql}"

    h = hashlib.sha256()
    count = 0
    with conn.cursor() as cur:
        cur.execute(sql)
        while True:
            rows = cur.fetchmany(500)
            if not rows:
                break
            for row in rows:
                normalized = {col: _normal_value(row.get(col), data_type=data_types.get(col, "")) for col in columns}
                h.update(_json_dumps(normalized).encode("utf-8"))
                h.update(b"\n")
                count += 1

    return {
        "row_count": count,
        "sha256": h.hexdigest(),
        "columns": columns,
        "primary_key": pk_cols,
    }


def _scalar(conn, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if not row:
        return None
    return list(row.values())[0]


def _rows(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _sum_expr(column: str) -> str:
    return f"CAST(COALESCE(SUM({_quote_ident(column)}), 0) AS CHAR) AS {_quote_ident(column)}"


def _distribution(conn, table: str, column: str) -> dict[str, int]:
    rows = _rows(
        conn,
        f"SELECT {_quote_ident(column)} v, COUNT(*) c FROM {_quote_ident(table)} GROUP BY {_quote_ident(column)} ORDER BY {_quote_ident(column)}",
    )
    return {str(row["v"]): int(row["c"] or 0) for row in rows}


def _latest_rows(conn, table: str, id_col: str = "id", limit: int = 10) -> list[dict[str, Any]]:
    columns = [x["name"] for x in _column_meta(conn, table)]
    order_parts = []
    if "updated_at" in columns:
        order_parts.append("updated_at DESC")
    if "created_at" in columns:
        order_parts.append("created_at DESC")
    order_parts.append(f"{_quote_ident(id_col)} DESC")
    selected = [id_col]
    for col in ("created_at", "updated_at"):
        if col in columns:
            selected.append(col)
    sql = (
        "SELECT "
        + ", ".join(_quote_ident(col) for col in selected)
        + f" FROM {_quote_ident(table)} ORDER BY "
        + ", ".join(order_parts)
        + f" LIMIT {int(limit)}"
    )
    return [
        {key: _normal_value(value, data_type="") for key, value in row.items()}
        for row in _rows(conn, sql)
    ]


def _business_stats(conn) -> dict[str, Any]:
    stats: dict[str, Any] = {}

    if _table_exists(conn, "order_new"):
        stats["order_status_distribution"] = {
            "is_finished": _distribution(conn, "order_new", "is_finished"),
            "is_paid": _distribution(conn, "order_new", "is_paid"),
            "is_rebate": _distribution(conn, "order_new", "is_rebate"),
            "status": _distribution(conn, "order_new", "status"),
            "audit_status": _distribution(conn, "order_new", "audit_status"),
        }
        stats["order_watermark"] = {
            "min_id": _scalar(conn, "SELECT MIN(id) FROM order_new"),
            "max_id": _scalar(conn, "SELECT MAX(id) FROM order_new"),
            "min_created_at": _scalar(conn, "SELECT MIN(created_at) FROM order_new"),
            "max_created_at": _scalar(conn, "SELECT MAX(created_at) FROM order_new"),
            "max_updated_at": _scalar(conn, "SELECT MAX(updated_at) FROM order_new"),
            "latest_rows": _latest_rows(conn, "order_new"),
        }
        stats["order_dimension_counts"] = {
            "salesperson_id": _distribution(conn, "order_new", "salesperson_id"),
            "customer_group_id": _distribution(conn, "order_new", "customer_group_id"),
            "channel_group_id": _distribution(conn, "order_new", "channel_group_id"),
        }

    if _table_exists(conn, "order_info_new"):
        sql = "SELECT " + ", ".join(_sum_expr(col) for col in FINANCE_NUMERIC_COLUMNS) + " FROM order_info_new"
        stats["finance_sums"] = _rows(conn, sql)[0]
        stats["finance_watermark"] = {
            "row_count": _scalar(conn, "SELECT COUNT(*) FROM order_info_new"),
            "min_id": _scalar(conn, "SELECT MIN(id) FROM order_info_new"),
            "max_id": _scalar(conn, "SELECT MAX(id) FROM order_info_new"),
            "max_updated_at": _scalar(conn, "SELECT MAX(updated_at) FROM order_info_new"),
            "latest_rows": _latest_rows(conn, "order_info_new"),
        }

    if _table_exists(conn, "ocr_task_new"):
        stats["ocr_task_status_distribution"] = _distribution(conn, "ocr_task_new", "status")

    if _table_exists(conn, "order_image_new") and _table_exists(conn, "image_file_new"):
        stats["image_link_stats"] = {
            "order_image_rows": _scalar(conn, "SELECT COUNT(*) FROM order_image_new"),
            "image_file_rows": _scalar(conn, "SELECT COUNT(*) FROM image_file_new"),
            "order_images_missing_file": _scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM order_image_new oi
                LEFT JOIN image_file_new f ON f.id = oi.image_file_id
                WHERE oi.image_file_id IS NOT NULL AND f.id IS NULL
                """,
            ),
            "order_images_missing_order": _scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM order_image_new oi
                LEFT JOIN order_new o ON o.id = oi.order_id
                WHERE o.id IS NULL
                """,
            ),
        }

    if _table_exists(conn, "order_fact_new") and _table_exists(conn, "order_new"):
        stats["order_fact_stats"] = {
            "row_count": _scalar(conn, "SELECT COUNT(*) FROM order_fact_new"),
            "missing_fact_orders": _scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM order_new o
                LEFT JOIN order_fact_new f ON f.order_id = o.id
                WHERE f.order_id IS NULL
                """,
            ),
            "orphan_fact_rows": _scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM order_fact_new f
                LEFT JOIN order_new o ON o.id = f.order_id
                WHERE o.id IS NULL
                """,
            ),
        }

    return stats


def build_fingerprint(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    guard = _guard_local(args)
    tables = _model_tables()
    conn = _db_conn()
    try:
        table_hashes = {}
        missing_tables = []
        for table in tables:
            if not _table_exists(conn, table):
                missing_tables.append(table)
                continue
            table_hashes[table] = _hash_table(conn, table)
        business_stats = _business_stats(conn)
    finally:
        conn.close()

    report = {
        "ok": not missing_tables,
        "mode": "db_business_fingerprint",
        "label": args.label,
        "guard": guard,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "summary": {
            "model_table_count": len(tables),
            "hashed_table_count": len(table_hashes),
            "row_total": sum(int(item["row_count"]) for item in table_hashes.values()),
            "missing_table_count": len(missing_tables),
        },
        "tables": tables,
        "missing_tables": missing_tables,
        "table_hashes": table_hashes,
        "business_stats": business_stats,
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(_json_dumps(report, pretty=True), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build deterministic local DB fingerprints for Dingchang model tables.")
    parser.add_argument("--allow-non-local", action="store_true")
    parser.add_argument("--label", default="local")
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path(f"logs/db-business-fingerprint-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.report_path = args.report_path.resolve()
    report = build_fingerprint(args)
    print(
        _json_dumps(
            {
                "ok": report["ok"],
                "label": report["label"],
                "report": str(args.report_path),
                "summary": report["summary"],
            },
            pretty=True,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
