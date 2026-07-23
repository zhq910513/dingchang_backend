from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

import pymysql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from env_loader import load_backend_env  # noqa: E402
from app.services.ocr_cleaner import (  # noqa: E402
    CLEANING_RULE_VERSION,
    clean_dynamic_data_for_ocr,
    describe_cleaning_rules,
)
from app.services.order_fact_service import build_order_fact_payload  # noqa: E402

load_backend_env()


WRITE_CONFIRM = "APPLY_CLEANED_OCR_DATA"
RESTORE_CONFIRM = "RESTORE_OCR_BACKUP"
DEFAULT_BATCH_SIZE = 500
KEY_FIELDS = (
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


def _open_text(path: Path, mode: str):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")
    return path.open(mode, encoding="utf-8")


def _env_value(key: str, default: str = "") -> str:
    value = os.getenv(key, default)
    return str(value or "").strip().strip('"').strip("'")


def _db_setting(name: str, default: str = "") -> str:
    return os.getenv(f"OCR_DB_{name}") or _env_value(f"OCR_DB_{name}", "") or _env_value(f"DB_{name}", default)


def _db_conn(*, autocommit: bool = False):
    return pymysql.connect(
        host=_db_setting("HOST", "127.0.0.1"),
        port=int(_db_setting("PORT", "3306")),
        user=_db_setting("USER"),
        password=_db_setting("PASSWORD"),
        database=_db_setting("NAME"),
        charset="utf8mb4",
        autocommit=autocommit,
        cursorclass=pymysql.cursors.DictCursor,
    )


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


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["dynamic_data"] = _as_dict(out.get("dynamic_data"))
    out["ocr_raw_json"] = _as_dict(out.get("ocr_raw_json"))
    return out


def _iter_order_rows(
    conn,
    *,
    batch_size: int,
    min_id: int,
    max_id: int | None,
    limit: int | None,
) -> Iterator[list[dict[str, Any]]]:
    seen = 0
    last_id = max(0, int(min_id or 0) - 1)
    while True:
        current_limit = int(batch_size)
        if limit is not None:
            remaining = int(limit) - seen
            if remaining <= 0:
                break
            current_limit = min(current_limit, remaining)

        clauses = ["id > %s", "dynamic_data IS NOT NULL"]
        params: list[Any] = [last_id]
        if max_id is not None:
            clauses.append("id <= %s")
            params.append(int(max_id))
        params.append(current_limit)

        sql = (
            "SELECT id, dynamic_data, ocr_raw_json, updated_at "
            "FROM order_new "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY id ASC LIMIT %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [_normalize_row(row) for row in cur.fetchall()]
        if not rows:
            break
        seen += len(rows)
        last_id = int(rows[-1]["id"])
        yield rows


def _fetch_fact_rows(conn, order_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = [int(x) for x in order_ids]
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    sql = (
        "SELECT order_id, owner_name, plate_no, vin, engine_no, vehicle_model, "
        "first_register_date, id_number, updated_at "
        f"FROM order_fact_new WHERE order_id IN ({placeholders})"
    )
    with conn.cursor() as cur:
        cur.execute(sql, ids)
        return {int(row["order_id"]): dict(row) for row in cur.fetchall()}


def _field_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changes[key] = {"before": before.get(key), "after": after.get(key)}
    return changes


def _record_change_stats(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    field_stats: dict[str, Counter],
    examples: dict[str, list[dict[str, Any]]],
    order_id: int,
    example_limit: int,
) -> None:
    changes = _field_changes(before, after)
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
        if len(examples[field_name]) < example_limit:
            examples[field_name].append({"order_id": order_id, "before": old, "after": new})


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    total = 0
    changed = 0
    unchanged = 0
    field_stats: dict[str, Counter] = defaultdict(Counter)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    risk_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    conn = _db_conn(autocommit=True)
    try:
        for rows in _iter_order_rows(
            conn,
            batch_size=args.batch_size,
            min_id=args.min_id,
            max_id=args.max_id,
            limit=args.limit,
        ):
            for row in rows:
                total += 1
                order_id = int(row["id"])
                before = row["dynamic_data"]
                after = clean_dynamic_data_for_ocr(before)
                if before == after:
                    unchanged += 1
                    continue
                changed += 1
                _record_change_stats(
                    before=before,
                    after=after,
                    field_stats=field_stats,
                    examples=examples,
                    order_id=order_id,
                    example_limit=args.example_limit,
                )
                for key in KEY_FIELDS:
                    if before.get(key) not in (None, "") and after.get(key) in (None, ""):
                        if len(risk_examples[key]) < args.example_limit:
                            risk_examples[key].append(
                                {"order_id": order_id, "before": before.get(key), "after": after.get(key)}
                            )
    finally:
        conn.close()

    report = {
        "mode": "audit",
        "ok": True,
        "rule_version": CLEANING_RULE_VERSION,
        "rules": describe_cleaning_rules(),
        "db_label": args.db_label,
        "scope": {"min_id": args.min_id, "max_id": args.max_id, "limit": args.limit},
        "summary": {
            "total_rows": total,
            "changed_rows": changed,
            "unchanged_rows": unchanged,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        },
        "field_stats": {key: dict(counter) for key, counter in sorted(field_stats.items())},
        "examples": {key: value for key, value in sorted(examples.items())},
        "risk_examples": {key: value for key, value in sorted(risk_examples.items())},
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(_json_dumps(report, pretty=True), encoding="utf-8")
    return report


def run_backup(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    row_count = 0
    fact_count = 0
    args.backup_path.parent.mkdir(parents=True, exist_ok=True)

    conn = _db_conn(autocommit=True)
    try:
        with _open_text(args.backup_path, "w") as out:
            meta = {
                "kind": "ocr_cleaning_backup_meta",
                "rule_version": CLEANING_RULE_VERSION,
                "db_label": args.db_label,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "scope": {"min_id": args.min_id, "max_id": args.max_id, "limit": args.limit},
            }
            out.write(_json_dumps(meta) + "\n")
            for rows in _iter_order_rows(
                conn,
                batch_size=args.batch_size,
                min_id=args.min_id,
                max_id=args.max_id,
                limit=args.limit,
            ):
                facts = _fetch_fact_rows(conn, [int(row["id"]) for row in rows])
                for row in rows:
                    order_id = int(row["id"])
                    fact = facts.get(order_id)
                    if fact:
                        fact_count += 1
                    record = {
                        "kind": "order_cleaning_backup",
                        "rule_version": CLEANING_RULE_VERSION,
                        "order_id": order_id,
                        "order_updated_at": row.get("updated_at"),
                        "dynamic_data": row.get("dynamic_data") or {},
                        "ocr_raw_json": row.get("ocr_raw_json") or {},
                        "order_fact": fact,
                    }
                    out.write(_json_dumps(record) + "\n")
                    row_count += 1
    finally:
        conn.close()

    manifest = {
        "mode": "backup",
        "ok": True,
        "rule_version": CLEANING_RULE_VERSION,
        "db_label": args.db_label,
        "backup_path": str(args.backup_path),
        "summary": {
            "order_rows": row_count,
            "fact_rows": fact_count,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(_json_dumps(manifest, pretty=True), encoding="utf-8")
    return manifest


def _validate_backup(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size <= 0:
        raise SystemExit(f"backup file not found or empty: {path}")
    with _open_text(path, "r") as handle:
        first = handle.readline()
    try:
        meta = json.loads(first)
    except Exception as exc:
        raise SystemExit(f"backup file is not valid jsonl: {path} ({exc})")
    if meta.get("kind") != "ocr_cleaning_backup_meta":
        raise SystemExit(f"backup file first row is not ocr_cleaning_backup_meta: {path}")
    return meta


def _validate_backup_for_db(path: Path, db_label: str, *, allow_label_mismatch: bool) -> dict[str, Any]:
    meta = _validate_backup(path)
    backup_label = str(meta.get("db_label") or "")
    current_label = str(db_label or "")
    if backup_label and current_label and backup_label != current_label and not allow_label_mismatch:
        raise SystemExit(
            "backup db_label mismatch: "
            f"backup={backup_label!r}, current={current_label!r}. "
            "Pass --allow-backup-label-mismatch only after manually verifying the target DB."
        )
    return meta


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


def run_apply(args: argparse.Namespace) -> dict[str, Any]:
    if args.confirm != WRITE_CONFIRM:
        raise SystemExit(f"apply refused: pass --confirm {WRITE_CONFIRM}")
    _validate_backup_for_db(
        args.backup_path,
        args.db_label,
        allow_label_mismatch=bool(args.allow_backup_label_mismatch),
    )

    started = time.perf_counter()
    scanned = 0
    changed = 0
    batches = 0
    apply_examples: list[dict[str, Any]] = []

    conn = _db_conn(autocommit=False)
    try:
        for rows in _iter_order_rows(
            conn,
            batch_size=args.batch_size,
            min_id=args.min_id,
            max_id=args.max_id,
            limit=args.limit,
        ):
            batches += 1
            try:
                with conn.cursor() as cur:
                    for row in rows:
                        scanned += 1
                        order_id = int(row["id"])
                        before = row["dynamic_data"]
                        after = clean_dynamic_data_for_ocr(before)
                        if before == after:
                            continue
                        changed += 1
                        if len(apply_examples) < args.example_limit:
                            apply_examples.append(
                                {
                                    "order_id": order_id,
                                    "changes": _field_changes(before, after),
                                }
                            )
                        if args.dry_run:
                            continue
                        cur.execute(
                            "UPDATE order_new SET dynamic_data=%s, updated_at=%s WHERE id=%s",
                            (_json_dumps(after), row.get("updated_at"), order_id),
                        )
                        _sync_order_fact(cur, order_id, after)
                if args.dry_run:
                    conn.rollback()
                else:
                    conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()

    report = {
        "mode": "apply",
        "ok": True,
        "dry_run": bool(args.dry_run),
        "rule_version": CLEANING_RULE_VERSION,
        "db_label": args.db_label,
        "backup_path": str(args.backup_path),
        "summary": {
            "scanned_rows": scanned,
            "changed_rows": changed,
            "batches": batches,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        },
        "examples": apply_examples,
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(_json_dumps(report, pretty=True), encoding="utf-8")
    return report


def _iter_backup_records(path: Path) -> Iterator[dict[str, Any]]:
    with _open_text(path, "r") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("kind") == "order_cleaning_backup":
                yield record


def run_restore(args: argparse.Namespace) -> dict[str, Any]:
    if args.confirm != RESTORE_CONFIRM:
        raise SystemExit(f"restore refused: pass --confirm {RESTORE_CONFIRM}")
    _validate_backup_for_db(
        args.backup_path,
        args.db_label,
        allow_label_mismatch=bool(args.allow_backup_label_mismatch),
    )

    started = time.perf_counter()
    restored = 0
    skipped = 0

    conn = _db_conn(autocommit=False)
    try:
        batch: list[dict[str, Any]] = []
        for record in _iter_backup_records(args.backup_path):
            order_id = int(record["order_id"])
            if order_id < int(args.min_id or 0):
                skipped += 1
                continue
            if args.max_id is not None and order_id > int(args.max_id):
                skipped += 1
                continue
            batch.append(record)
            if len(batch) >= args.batch_size:
                restored += _restore_batch(conn, batch, dry_run=args.dry_run)
                batch = []
        if batch:
            restored += _restore_batch(conn, batch, dry_run=args.dry_run)
    finally:
        conn.close()

    report = {
        "mode": "restore",
        "ok": True,
        "dry_run": bool(args.dry_run),
        "rule_version": CLEANING_RULE_VERSION,
        "db_label": args.db_label,
        "backup_path": str(args.backup_path),
        "summary": {
            "restored_rows": restored,
            "skipped_rows": skipped,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(_json_dumps(report, pretty=True), encoding="utf-8")
    return report


def _restore_batch(conn, batch: list[dict[str, Any]], *, dry_run: bool) -> int:
    try:
        with conn.cursor() as cur:
            for record in batch:
                order_id = int(record["order_id"])
                cur.execute(
                    "UPDATE order_new SET dynamic_data=%s, ocr_raw_json=%s, updated_at=%s WHERE id=%s",
                    (
                        _json_dumps(record.get("dynamic_data") or {}),
                        _json_dumps(record.get("ocr_raw_json") or {}),
                        record.get("order_updated_at"),
                        order_id,
                    ),
                )
                fact = record.get("order_fact")
                if isinstance(fact, dict):
                    _restore_fact(cur, order_id, fact)
                else:
                    cur.execute("DELETE FROM order_fact_new WHERE order_id=%s", (order_id,))
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return len(batch)
    except Exception:
        conn.rollback()
        raise


def _restore_fact(cur, order_id: int, fact: dict[str, Any]) -> None:
    sql = """
        INSERT INTO order_fact_new (
            order_id, owner_name, plate_no, vin, engine_no,
            vehicle_model, first_register_date, id_number, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON DUPLICATE KEY UPDATE
            owner_name=VALUES(owner_name),
            plate_no=VALUES(plate_no),
            vin=VALUES(vin),
            engine_no=VALUES(engine_no),
            vehicle_model=VALUES(vehicle_model),
            first_register_date=VALUES(first_register_date),
            id_number=VALUES(id_number),
            updated_at=VALUES(updated_at)
    """
    cur.execute(
        sql,
        (
            int(order_id),
            fact.get("owner_name"),
            fact.get("plate_no"),
            fact.get("vin"),
            fact.get("engine_no"),
            fact.get("vehicle_model"),
            fact.get("first_register_date"),
            fact.get("id_number"),
            fact.get("updated_at"),
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit, backup and backfill OCR-cleaned order dynamic_data.")
    parser.add_argument("--mode", choices=("audit", "backup", "apply", "restore"), required=True)
    parser.add_argument("--db-label", default=os.getenv("OCR_DB_LABEL", "local"))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--min-id", type=int, default=1)
    parser.add_argument("--max-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--example-limit", type=int, default=20)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--dry-run", action="store_true", help="For apply/restore, calculate and rollback writes.")
    parser.add_argument(
        "--allow-backup-label-mismatch",
        action="store_true",
        help="Allow applying/restoring with a backup whose db_label differs from --db-label.",
    )
    parser.add_argument(
        "--backup-path",
        type=Path,
        default=Path(f"logs/ocr-cleaning-backup-{datetime.now().strftime('%Y%m%d%H%M%S')}.jsonl.gz"),
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path(f"logs/ocr-cleaning-migration-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.batch_size = max(1, int(args.batch_size or DEFAULT_BATCH_SIZE))
    args.report_path = args.report_path.resolve()
    args.backup_path = args.backup_path.resolve()

    if args.mode == "audit":
        result = run_audit(args)
    elif args.mode == "backup":
        result = run_backup(args)
    elif args.mode == "apply":
        result = run_apply(args)
    elif args.mode == "restore":
        result = run_restore(args)
    else:
        raise SystemExit(f"unknown mode: {args.mode}")

    print(_json_dumps({"ok": result.get("ok"), "mode": args.mode, "report": str(args.report_path), "summary": result.get("summary")}, pretty=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
