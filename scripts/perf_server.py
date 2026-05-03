from __future__ import annotations

import json
import os
import threading
import time
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep local measurement deterministic and avoid background work stealing DB time.
os.environ.setdefault("AUTO_SCHEMA_CHECK", "0")
os.environ.setdefault("AUTO_CREATE_TABLES", "0")
os.environ.setdefault("AUTO_ADD_COLUMNS", "0")
os.environ.setdefault("AUTO_ADD_INDEXES", "0")
os.environ.setdefault("AUTO_SEED_AUTH", "0")
os.environ.setdefault("AUTO_SEED_FIELDS", "0")
os.environ.setdefault("ORDER_FACT_BACKFILL_ENABLED", "0")
os.environ.setdefault("OCR_POLL_ENABLED", "0")

from app.main import app  # noqa: E402

BJ_TZ = ZoneInfo("Asia/Shanghai")
LOG_PATH = Path(os.getenv("PERF_LOG_PATH", "logs/frontend_perf.jsonl")).resolve()
_LOCK = threading.Lock()


@app.middleware("http")
async def perf_timing_middleware(request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000

    path = request.url.path or ""
    if path.startswith("/api/"):
        record = {
            "ts": datetime.now(BJ_TZ).isoformat(timespec="milliseconds"),
            "method": request.method,
            "path": path,
            "query": request.url.query,
            "status": response.status_code,
            "elapsed_ms": round(elapsed_ms, 3),
        }
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    response.headers["X-Perf-Ms"] = f"{elapsed_ms:.3f}"
    return response


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("PERF_HOST", "127.0.0.1"),
        port=int(os.getenv("PERF_PORT", "8000") or "8000"),
        access_log=False,
        log_level=os.getenv("PERF_LOG_LEVEL", "info"),
    )
