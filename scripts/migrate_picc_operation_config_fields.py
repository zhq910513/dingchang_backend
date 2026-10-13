# -*- coding: utf-8 -*-
"""Preview/remove account-owned PICC routing fields from saved defaults.

Run in the backend container. ``--apply`` requires a new absolute backup file
on persistent storage. The migration is idempotent and only changes PICC
default-config rows containing account-owned routing fields or tombstones.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db import async_session_factory, engine
from app.models.quote_assistant import QuotePlatformDefaultConfig
from app.services.quote_platforms.platforms.picc.business import PICC_DELETED_DEFAULT_FIELDS_KEY
from app.services.quote_platforms.platforms.picc.operation_config import (
    strip_account_managed_default_fields,
    strip_account_managed_deleted_fields,
)


def migrated_values(stored):
    values = dict(stored or {})
    raw_deleted = values.pop(PICC_DELETED_DEFAULT_FIELDS_KEY, [])
    values = strip_account_managed_default_fields(values)
    deleted = strip_account_managed_deleted_fields(raw_deleted)
    if raw_deleted or deleted:
        values[PICC_DELETED_DEFAULT_FIELDS_KEY] = deleted
    return values


async def migrate(*, apply: bool, backup: Path | None) -> None:
    async with async_session_factory() as db:
        async with db.begin():
            statement = (
                select(QuotePlatformDefaultConfig)
                .where(QuotePlatformDefaultConfig.platform_code == "PICC")
                .order_by(QuotePlatformDefaultConfig.id)
            )
            if apply:
                statement = statement.with_for_update()
            rows = (await db.execute(statement)).scalars().all()
            changes = []
            for row in rows:
                before = dict(row.default_values_json or {})
                after = migrated_values(before)
                if before != after:
                    changes.append({"id": row.id, "before": before, "after": after, "row": row})
            print(
                json.dumps(
                    {
                        "total_picc_configs": len(rows),
                        "changed_configs": len(changes),
                        "config_ids": [item["id"] for item in changes],
                    },
                    ensure_ascii=False,
                )
            )
            if not apply or not changes:
                return
            snapshot = {
                "table": QuotePlatformDefaultConfig.__tablename__,
                "migration": "remove_picc_account_operation_config_fields",
                "rows": [
                    {"id": item["id"], "default_values_json": item["before"]}
                    for item in changes
                ],
            }
            descriptor = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(snapshot, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            for item in changes:
                item["row"].default_values_json = item["after"]
            await db.flush()
        if apply and changes:
            print(json.dumps({"updated": len(changes), "backup": str(backup)}, ensure_ascii=False))


async def verify_backup(backup: Path) -> None:
    snapshot = json.loads(backup.read_text(encoding="utf-8"))
    if snapshot.get("table") != QuotePlatformDefaultConfig.__tablename__:
        raise ValueError("Backup table does not match default config table")
    originals = {row["id"]: row["default_values_json"] for row in snapshot.get("rows") or []}
    if not originals or len(originals) != len(snapshot.get("rows") or []):
        raise ValueError("Backup has duplicate IDs or no rows")
    async with async_session_factory() as db:
        rows = (
            await db.execute(select(QuotePlatformDefaultConfig).where(QuotePlatformDefaultConfig.id.in_(originals)))
        ).scalars().all()
    actual = {row.id: row.default_values_json for row in rows}
    mismatches = [row_id for row_id, before in originals.items() if actual.get(row_id) != migrated_values(before)]
    print(json.dumps({"checked": len(originals), "mismatched_ids": mismatches}, ensure_ascii=False))
    if mismatches:
        raise RuntimeError(f"Migrated config does not match backup for IDs {mismatches}")


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify-backup", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.apply and (not args.backup or not args.backup.is_absolute() or not args.backup.parent.is_dir()):
        parser.error("--apply requires a new absolute backup file in an existing directory")
    if args.verify_backup and (not args.backup or not args.backup.is_absolute() or not args.backup.is_file()):
        parser.error("--verify-backup requires an existing absolute backup file")

    async def run() -> None:
        try:
            if args.verify_backup:
                await verify_backup(args.backup)
            else:
                await migrate(apply=args.apply, backup=args.backup)
        finally:
            await engine.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
