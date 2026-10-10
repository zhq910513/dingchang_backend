# -*- coding: utf-8 -*-
"""Preview or migrate saved quote default-config aliases to the display name.

Run in the backend container. --apply requires a fresh, absolute backup path
on its persistent /app/storage volume. Other quote history is not modified.
"""

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
from app.services.quote_assistant_service import (
    JOINT_SALES_CONFIG_FIELD_ALIASES,
    JOINT_SALES_CONFIG_FIELD_NAME,
    canonicalize_joint_sales_config_fields,
)
from app.services.quote_platforms.platforms.picc.business import PICC_DELETED_DEFAULT_FIELDS_KEY


def migrated_values(stored):
    values = dict(stored or {})
    deleted = values.get(PICC_DELETED_DEFAULT_FIELDS_KEY, [])
    has_alias = any(name in values for name in JOINT_SALES_CONFIG_FIELD_ALIASES)
    if deleted and not isinstance(deleted, list):
        if has_alias:
            raise ValueError("Config with joint-sales alias has invalid deleted fields")
        return values
    if not has_alias and not any(name in JOINT_SALES_CONFIG_FIELD_ALIASES for name in deleted):
        return values
    had_deleted = PICC_DELETED_DEFAULT_FIELDS_KEY in values
    values.pop(PICC_DELETED_DEFAULT_FIELDS_KEY, None)
    normalized, normalized_deleted = canonicalize_joint_sales_config_fields(
        values, deleted
    )
    if had_deleted:
        normalized[PICC_DELETED_DEFAULT_FIELDS_KEY] = normalized_deleted
    return normalized


async def migrate(*, apply, backup):
    async with async_session_factory() as db:
        async with db.begin():
            statement = select(QuotePlatformDefaultConfig).order_by(QuotePlatformDefaultConfig.id)
            if apply:
                statement = statement.with_for_update()
            rows = (await db.execute(statement)).scalars().all()
            changes = []
            for row in rows:
                before = dict(row.default_values_json or {})
                after = migrated_values(before)
                if before == after:
                    continue
                present = [key for key in JOINT_SALES_CONFIG_FIELD_ALIASES if key in before]
                values = {json.dumps(before[key], ensure_ascii=False, sort_keys=True) for key in present}
                changes.append({
                    "id": row.id, "platform": row.platform_code,
                    "account_type": row.account_type_name,
                    "aliases": present, "different_alias_values": len(values) > 1,
                    "before": before, "after": after, "row": row,
                })
            conflicts = [item["id"] for item in changes if item["different_alias_values"]]
            print(json.dumps({
                "total_configs": len(rows), "changed_configs": len(changes),
                "conflicting_config_ids": conflicts,
                "rows": [{key: item[key] for key in (
                    "id", "platform", "account_type", "aliases", "different_alias_values"
                )} for item in changes],
            }, ensure_ascii=False))
            if not apply or not changes:
                return
            if conflicts:
                raise RuntimeError(f"Conflicting amounts in config IDs {conflicts}; resolve before applying")
            snapshot = {
                "table": QuotePlatformDefaultConfig.__tablename__,
                "canonical_name": JOINT_SALES_CONFIG_FIELD_NAME,
                "rows": [{"id": item["id"], "default_values_json": item["before"]} for item in changes],
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


async def verify_backup(backup):
    snapshot = json.loads(backup.read_text(encoding="utf-8"))
    if snapshot.get("table") != QuotePlatformDefaultConfig.__tablename__:
        raise ValueError("Backup table does not match default config table")
    originals = {row["id"]: row["default_values_json"] for row in snapshot["rows"]}
    if len(originals) != len(snapshot["rows"]) or not originals:
        raise ValueError("Backup has duplicate IDs or no rows")
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(QuotePlatformDefaultConfig).where(QuotePlatformDefaultConfig.id.in_(originals))
        )).scalars().all()
    actual = {row.id: row.default_values_json for row in rows}
    mismatches = [id_ for id_, before in originals.items() if actual.get(id_) != migrated_values(before)]
    print(json.dumps({"checked": len(originals), "mismatched_ids": mismatches}, ensure_ascii=False))
    if mismatches:
        raise RuntimeError(f"Migrated config does not match backup for IDs {mismatches}")


def main():
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
    async def run():
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
