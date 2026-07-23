from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, TextIO

import pymysql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from env_loader import load_backend_env  # noqa: E402


load_backend_env()


CONFIRM_TEXT = "IMPORT_ONLINE_DUMP_TO_LOCAL"
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


def _db_conn(*, autocommit: bool = True):
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
        raise SystemExit(
            f"refused: DB_HOST={cfg['host']!r} is not local. "
            "Use --allow-non-local only after manually verifying the target DB."
        )
    if not args.dump_path.exists() or args.dump_path.stat().st_size <= 0:
        raise SystemExit(f"dump not found or empty: {args.dump_path}")
    return {"host": cfg["host"], "port": cfg["port"], "database": cfg["database"], "user": cfg["user"]}


def _open_dump(path: Path) -> TextIO:
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def iter_sql_statements(handle: TextIO) -> Iterator[str]:
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    block_comment = False

    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        i = 0
        while i < len(chunk):
            ch = chunk[i]
            nxt = chunk[i + 1] if i + 1 < len(chunk) else ""

            if block_comment:
                buf.append(ch)
                if ch == "*" and nxt == "/":
                    buf.append(nxt)
                    i += 2
                    block_comment = False
                    continue
                i += 1
                continue

            if quote:
                buf.append(ch)
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = None
                i += 1
                continue

            if ch == "-" and nxt == "-":
                while i < len(chunk) and chunk[i] not in "\r\n":
                    i += 1
                continue

            if ch == "#":
                while i < len(chunk) and chunk[i] not in "\r\n":
                    i += 1
                continue

            if ch == "/" and nxt == "*":
                block_comment = True
                buf.append(ch)
                buf.append(nxt)
                i += 2
                continue

            if ch in ("'", '"', "`"):
                quote = ch
                buf.append(ch)
                i += 1
                continue

            if ch == ";":
                statement = "".join(buf).strip()
                buf.clear()
                if statement:
                    yield statement
                i += 1
                continue

            buf.append(ch)
            i += 1

    tail = "".join(buf).strip()
    if tail:
        yield tail


def _statement_kind(statement: str) -> str:
    text = statement.lstrip()
    if text.startswith("/*!"):
        inner = re.sub(r"^/\\*![0-9]*\\s*", "", text)
        inner = re.sub(r"\\*/\\s*$", "", inner).strip()
        return inner.split(None, 1)[0].upper() if inner else "VERSION_COMMENT"
    return text.split(None, 1)[0].upper() if text else ""


def _normalize_statement(statement: str) -> str | None:
    text = statement.strip()
    if not text:
        return None
    upper = text.upper()
    if upper.startswith("LOCK TABLES") or upper.startswith("UNLOCK TABLES"):
        return None
    return text


def _show_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        rows = cur.fetchall()
    return sorted([str(list(row.values())[0]) for row in rows])


def _row_counts(conn, tables: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT COUNT(*) c FROM `{table.replace('`', '``')}`")
            out[table] = int(cur.fetchone()["c"])
    return out


def import_dump(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    guard = _guard_local(args)
    stats: dict[str, int] = {}
    executed = 0
    skipped = 0
    last_kind = ""

    conn = _db_conn(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            cur.execute("SET UNIQUE_CHECKS=0")
            cur.execute("SET SQL_NOTES=0")

            with _open_dump(args.dump_path) as handle:
                for raw_statement in iter_sql_statements(handle):
                    statement = _normalize_statement(raw_statement)
                    kind = _statement_kind(raw_statement)
                    last_kind = kind
                    stats[kind] = stats.get(kind, 0) + 1
                    if statement is None:
                        skipped += 1
                        continue
                    try:
                        cur.execute(statement)
                    except Exception as exc:
                        raise RuntimeError(
                            f"import failed after {executed} statements; kind={kind}; "
                            f"error={exc}; statement_prefix={statement[:500]!r}"
                        ) from exc
                    executed += 1
                    if args.progress_every and executed % args.progress_every == 0:
                        print(_json_dumps({"executed": executed, "skipped": skipped, "last_kind": last_kind}))

            cur.execute("SET FOREIGN_KEY_CHECKS=1")
            cur.execute("SET UNIQUE_CHECKS=1")
            cur.execute("SET SQL_NOTES=1")

        tables = _show_tables(conn)
        counts = _row_counts(conn, tables)
    finally:
        conn.close()

    non_empty = {table: count for table, count in counts.items() if count > 0}
    report = {
        "ok": True,
        "mode": "import_mysql_dump_with_pymysql",
        "guard": guard,
        "dump_path": str(args.dump_path),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "summary": {
            "executed_statements": executed,
            "skipped_statements": skipped,
            "table_count": len(tables),
            "non_empty_table_count": len(non_empty),
            "row_total": sum(counts.values()),
        },
        "statement_stats": stats,
        "tables": tables,
        "row_counts": counts,
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(_json_dumps(report, pretty=True), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import a gzip/plain mysqldump into the local MySQL database.")
    parser.add_argument("--dump-path", type=Path, required=True)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--allow-non-local", action="store_true")
    parser.add_argument("--progress-every", type=int, default=500)
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path(f"logs/mysql-dump-import-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.dump_path = args.dump_path.resolve()
    args.report_path = args.report_path.resolve()
    report = import_dump(args)
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
