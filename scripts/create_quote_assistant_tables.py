# encoding: utf-8
from __future__ import annotations

"""
Create quote-assistant tables only.

This script is safe for production rollout because it only calls create(checkfirst)
for the new quote-assistant tables and never alters existing tables/columns.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db import engine  # noqa: E402
from app.models.quote_assistant import QuoteCase, QuoteCaseEvent, QuoteCaseImage, QuotePlatformAccount, QuoteTask  # noqa: E402


TABLES = [
    QuoteCase.__table__,
    QuoteCaseImage.__table__,
    QuoteTask.__table__,
    QuotePlatformAccount.__table__,
    QuoteCaseEvent.__table__,
]


async def main() -> None:
    try:
        async with engine.begin() as conn:
            for table in TABLES:
                await conn.run_sync(lambda sync_conn, t=table: t.create(bind=sync_conn, checkfirst=True))
                print(f"checked table: {table.name}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
