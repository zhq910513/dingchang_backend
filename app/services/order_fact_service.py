# encoding: utf-8
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Dict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

try:
    from sqlalchemy.dialects.mysql import insert as mysql_insert
except Exception:  # pragma: no cover
    mysql_insert = None  # type: ignore

from app.models.order import Order
from app.models.order_fact import OrderFact
from app.services.ocr_cleaner import clean_dynamic_data_for_ocr
from app.services.order_owner_name import resolve_owner_name as _resolve_owner_name

logger = logging.getLogger(__name__)

_FACT_FIELD_NAMES = (
    "owner_name",
    "plate_no",
    "vin",
    "engine_no",
    "vehicle_model",
    "first_register_date",
    "id_number",
)


def _trim_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_date_or_none(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()

    s = str(v).strip()
    if not s or s in {"-", "null", "none", "None"}:
        return None

    s2 = s.replace("/", "-").replace(".", "-")
    s2 = re.sub(r"\s+", "", s2)

    if len(s2) >= 10 and s2[4] == "-" and s2[7] == "-":
        try:
            return datetime.strptime(s2[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    if len(s2) == 8 and s2.isdigit():
        try:
            return datetime.strptime(s2, "%Y%m%d").date()
        except Exception:
            return None

    return None


def build_order_fact_payload(dynamic_data: Any) -> Dict[str, Any]:
    dd = clean_dynamic_data_for_ocr(dict(dynamic_data or {})) if isinstance(dynamic_data, dict) else {}

    owner_name = _trim_or_none(_resolve_owner_name(dd) or dd.get("owner_name"))
    plate_no = _trim_or_none(dd.get("plate_no"))
    vin = _trim_or_none(dd.get("vin"))
    engine_no = _trim_or_none(dd.get("engine_no"))
    vehicle_model = _trim_or_none(dd.get("vehicle_model"))
    first_register_date = _parse_date_or_none(dd.get("first_register_date"))
    id_number = _trim_or_none(dd.get("id_number"))

    return {
        "owner_name": owner_name,
        "plate_no": plate_no,
        "vin": vin,
        "engine_no": engine_no,
        "vehicle_model": vehicle_model,
        "first_register_date": first_register_date,
        "id_number": id_number,
    }


def _dialect_name(db: AsyncSession) -> str:
    try:
        bind = db.get_bind()
        return str(getattr(bind.dialect, "name", "") or "").lower()
    except Exception:
        return ""


async def sync_order_fact_from_dynamic_data(
    db: AsyncSession,
    *,
    order_id: int,
    dynamic_data: Any,
) -> None:
    payload = build_order_fact_payload(dynamic_data)

    if _dialect_name(db) in {"mysql", "mariadb"} and mysql_insert is not None:
        insert_stmt = mysql_insert(OrderFact).values(order_id=int(order_id), **payload)
        upsert_stmt = insert_stmt.on_duplicate_key_update(
            **{field_name: getattr(insert_stmt.inserted, field_name) for field_name in _FACT_FIELD_NAMES},
            updated_at=func.current_timestamp(),
        )
        await db.execute(upsert_stmt)
        return

    fact = (
        await db.execute(select(OrderFact).where(OrderFact.order_id == int(order_id)))
    ).scalar_one_or_none()

    if fact is None:
        db.add(OrderFact(order_id=int(order_id), **payload))
        await db.flush()
        return

    for field_name, field_value in payload.items():
        setattr(fact, field_name, field_value)
    await db.flush()


async def backfill_missing_order_facts(
    db: AsyncSession,
    *,
    batch_size: int = 500,
) -> int:
    limit = max(1, int(batch_size or 500))
    stmt = (
        select(
            Order.id.label("order_id"),
            Order.dynamic_data.label("dynamic_data"),
        )
        .select_from(Order)
        .outerjoin(OrderFact, OrderFact.order_id == Order.id)
        .where(OrderFact.order_id.is_(None))
        .order_by(Order.id.asc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).mappings().all()
    if not rows:
        return 0

    payloads = [
        {"order_id": int(row["order_id"]), **build_order_fact_payload(row.get("dynamic_data"))}
        for row in rows
    ]

    if _dialect_name(db) in {"mysql", "mariadb"} and mysql_insert is not None:
        insert_stmt = mysql_insert(OrderFact).values(payloads)
        upsert_stmt = insert_stmt.on_duplicate_key_update(
            **{field_name: getattr(insert_stmt.inserted, field_name) for field_name in _FACT_FIELD_NAMES},
            updated_at=func.current_timestamp(),
        )
        await db.execute(upsert_stmt)
        return len(payloads)

    for payload in payloads:
        order_id = int(payload["order_id"])
        fact = (await db.execute(select(OrderFact).where(OrderFact.order_id == order_id))).scalar_one_or_none()
        if fact is None:
            db.add(OrderFact(**payload))
            continue
        for field_name in _FACT_FIELD_NAMES:
            setattr(fact, field_name, payload.get(field_name))
    return len(payloads)


async def count_missing_order_facts(db: AsyncSession) -> int:
    stmt = (
        select(func.count(Order.id))
        .select_from(Order)
        .outerjoin(OrderFact, OrderFact.order_id == Order.id)
        .where(OrderFact.order_id.is_(None))
    )
    return int((await db.execute(stmt)).scalar() or 0)
