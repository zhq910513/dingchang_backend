from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, TextIO

import pymysql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db import Base, load_all_models  # noqa: E402


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
    env_path = Path(".env")
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    match = re.search(rf"(?m)^{re.escape(key)}=(.*)$", text)
    value = match.group(1).strip() if match else os.getenv(key, default)
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


def _guard_local(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _db_config()
    host = str(cfg["host"] or "").strip().lower()
    if host not in LOCAL_HOSTS and not args.allow_non_local:
        raise SystemExit(f"refused: DB_HOST={cfg['host']!r} is not local")
    return {"host": cfg["host"], "port": cfg["port"], "database": cfg["database"], "user": cfg["user"]}


def _quote_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _model_tables() -> list[str]:
    load_all_models()
    return [table.name for table in Base.metadata.sorted_tables]


def _open_output(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "wt", encoding="utf-8", newline="\n")
    return path.open("w", encoding="utf-8", newline="\n")


def _show_create_table(conn, table: str) -> str:
    with conn.cursor() as cur:
        cur.execute(f"SHOW CREATE TABLE {_quote_ident(table)}")
        row = cur.fetchone()
    return str(row.get("Create Table") or row.get("Create Table ") or list(row.values())[-1])


def _insertable_columns(conn, table: str) -> list[str]:
    sql = """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s
          AND TABLE_NAME=%s
          AND EXTRA NOT LIKE '%%GENERATED%%'
        ORDER BY ORDINAL_POSITION
    """
    with conn.cursor() as cur:
        cur.execute(sql, (_db_config()["database"], table))
        return [str(row["COLUMN_NAME"]) for row in cur.fetchall()]


def _row_count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) c FROM {_quote_ident(table)}")
        return int(cur.fetchone()["c"])


def _escape_value(conn, value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = _json_dumps(value)
    return conn.escape(value)


def _write_inserts(
    conn,
    out: TextIO,
    *,
    table: str,
    columns: list[str],
    batch_rows: int,
) -> int:
    if not columns:
        return 0
    col_sql = ", ".join(_quote_ident(col) for col in columns)
    select_sql = f"SELECT {col_sql} FROM {_quote_ident(table)}"
    written = 0
    with conn.cursor() as cur:
        cur.execute(select_sql)
        while True:
            rows = cur.fetchmany(batch_rows)
            if not rows:
                break
            values_sql: list[str] = []
            for row in rows:
                values = ", ".join(_escape_value(conn, row.get(col)) for col in columns)
                values_sql.append(f"({values})")
            out.write(f"INSERT INTO {_quote_ident(table)} ({col_sql}) VALUES\n")
            out.write(",\n".join(values_sql))
            out.write(";\n")
            written += len(rows)
    return written


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def export_tables(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    guard = _guard_local(args)
    tables = _model_tables()
    row_counts: dict[str, int] = {}
    inserted_counts: dict[str, int] = {}

    conn = _db_conn()
    try:
        with _open_output(args.output_path) as out:
            out.write("-- Dingchang standardized model-table dump\n")
            out.write(f"-- Created at: {datetime.now().isoformat(timespec='seconds')}\n")
            out.write(f"-- Source database: {guard['database']}\n")
            out.write("SET NAMES utf8mb4;\n")
            out.write("SET FOREIGN_KEY_CHECKS=0;\n")
            out.write("SET UNIQUE_CHECKS=0;\n")
            out.write("SET SQL_NOTES=0;\n\n")

            for table in reversed(tables):
                out.write(f"DROP TABLE IF EXISTS {_quote_ident(table)};\n")
            out.write("\n")

            for table in tables:
                row_counts[table] = _row_count(conn, table)
                out.write(f"--\n-- Table structure for {table}\n--\n")
                out.write(_show_create_table(conn, table))
                out.write(";\n\n")
                out.write(f"--\n-- Data for {table}\n--\n")
                columns = _insertable_columns(conn, table)
                inserted_counts[table] = _write_inserts(
                    conn,
                    out,
                    table=table,
                    columns=columns,
                    batch_rows=args.batch_rows,
                )
                out.write("\n")

            out.write("SET SQL_NOTES=1;\n")
            out.write("SET UNIQUE_CHECKS=1;\n")
            out.write("SET FOREIGN_KEY_CHECKS=1;\n")
    finally:
        conn.close()

    sha = _sha256(args.output_path)
    ok = row_counts == inserted_counts
    report = {
        "ok": ok,
        "mode": "export_current_model_tables",
        "guard": guard,
        "output_path": str(args.output_path),
        "sha256": sha,
        "size": args.output_path.stat().st_size,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "summary": {
            "table_count": len(tables),
            "row_total": sum(row_counts.values()),
        },
        "tables": tables,
        "row_counts": row_counts,
        "inserted_counts": inserted_counts,
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(_json_dumps(report, pretty=True), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export current SQLAlchemy model tables from local MySQL.")
    parser.add_argument("--allow-non-local", action="store_true")
    parser.add_argument("--batch-rows", type=int, default=200)
    parser.add_argument(
        "--output-path",
        type=Path,
        default=Path(f"logs/standardized-model-tables-{datetime.now().strftime('%Y%m%d%H%M%S')}.sql.gz"),
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path(f"logs/standardized-model-tables-export-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.output_path = args.output_path.resolve()
    args.report_path = args.report_path.resolve()
    args.batch_rows = max(1, int(args.batch_rows or 200))
    report = export_tables(args)
    print(
        _json_dumps(
            {
                "ok": report["ok"],
                "output": str(args.output_path),
                "report": str(args.report_path),
                "sha256": report["sha256"],
                "summary": report["summary"],
            },
            pretty=True,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
