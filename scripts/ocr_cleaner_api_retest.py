from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pymysql
import requests


BASE = os.getenv("OCR_API_TEST_BASE", "http://127.0.0.1:8000/api").rstrip("/")
RUN_ID = os.getenv("OCR_API_TEST_RUN_ID") or datetime.now().strftime("%Y%m%d%H%M%S")
LOG_PATH = Path(os.getenv("OCR_API_TEST_LOG", f"logs/ocr-cleaner-api-retest-{RUN_ID}.json"))
ADMIN_USER = os.getenv("SEED_SUPER_USERNAME", "dingchang_admin")
ADMIN_PASS = os.getenv("SEED_SUPER_PASSWORD", "dingchang_admin@123456")
TIMEOUT = int(os.getenv("OCR_API_TEST_TIMEOUT", "30") or "30")

steps: list[dict[str, Any]] = []
failures: list[dict[str, Any]] = []
created: dict[str, Any] = {
    "orders": [],
    "customer_group_id": None,
    "channel_group_id": None,
    "customer_code": None,
    "channel_code": None,
}


def _env_value(key: str, default: str = "") -> str:
    env_path = Path(".env")
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    match = re.search(rf"(?m)^{re.escape(key)}=(.*)$", text)
    value = match.group(1).strip() if match else os.getenv(key, default)
    return str(value or "").strip().strip('"').strip("'")


def _db_conn():
    return pymysql.connect(
        host=_env_value("DB_HOST", "127.0.0.1"),
        port=int(_env_value("DB_PORT", "3306")),
        user=_env_value("DB_USER"),
        password=_env_value("DB_PASSWORD"),
        database=_env_value("DB_NAME"),
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if str(key).lower() in {"token", "session_token", "password"}:
                out[key] = "***REDACTED***"
            else:
                out[key] = _redact(value)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _json_default(obj: Any) -> str:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return str(obj)


def _record(
    label: str,
    ok: bool,
    *,
    method: str = "ASSERT",
    path: str = "",
    status: int | None = None,
    elapsed_ms: float = 0,
    extra: Any = None,
) -> None:
    item = {
        "label": label,
        "ok": bool(ok),
        "method": method,
        "path": path,
        "status": status,
        "elapsed_ms": round(float(elapsed_ms or 0), 2),
        "extra": _redact(extra or {}),
    }
    steps.append(item)
    if not ok:
        failures.append(item)


def _request(
    label: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    expected: tuple[int, ...] = (200,),
    **kwargs: Any,
) -> Any:
    headers = kwargs.pop("headers", {}) or {}
    if token:
        headers["X-Session-Token"] = token
    started = time.perf_counter()
    resp = requests.request(method, BASE + path, headers=headers, timeout=TIMEOUT, **kwargs)
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        body: Any = resp.json()
    except Exception:
        body = resp.text[:2000]
    ok = resp.status_code in expected
    _record(
        label,
        ok,
        method=method,
        path=path,
        status=resp.status_code,
        elapsed_ms=elapsed_ms,
        extra={"body": None if ok else body, "x_perf_ms": resp.headers.get("X-Perf-Ms")},
    )
    if not ok:
        raise AssertionError(f"{label} expected {expected}, got {resp.status_code}: {body}")
    return body


def _assert_equal(label: str, actual: Any, expected: Any) -> None:
    ok = actual == expected
    _record(label, ok, extra={"actual": actual, "expected": expected})
    if not ok:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def _assert_true(label: str, condition: bool, extra: Any = None) -> None:
    _record(label, bool(condition), extra=extra)
    if not condition:
        raise AssertionError(label)


def _login() -> str:
    body = _request(
        "login_admin",
        "POST",
        "/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
    )
    token = body.get("token") if isinstance(body, dict) else None
    _assert_true("login_admin_token_present", bool(token))
    return str(token)


def _create_fixture(token: str) -> dict[str, Any]:
    customer_payload = {
        "customer_code": f"OCR-CUST-{RUN_ID}",
        "customer_name": f"OCR Customer {RUN_ID}",
        "market": f"OCR Market {RUN_ID}",
        "region": "OCR Region",
        "contacts": [],
    }
    customer = _request(
        "create_customer_group",
        "POST",
        "/customer-channel/customer-groups",
        token=token,
        json=customer_payload,
    )
    created["customer_group_id"] = int(customer["id"])
    created["customer_code"] = customer_payload["customer_code"]

    channel_payload = {
        "channel_code": f"OCR-CH-{RUN_ID}",
        "channel_name": f"OCR Channel {RUN_ID}",
        "region": "OCR Region",
        "contacts": [],
    }
    channel = _request(
        "create_channel_group",
        "POST",
        "/customer-channel/channel-groups",
        token=token,
        json=channel_payload,
    )
    created["channel_group_id"] = int(channel["id"])
    created["channel_code"] = channel_payload["channel_code"]

    return {
        "customer_group_id": int(customer["id"]),
        "channel_group_id": int(channel["id"]),
    }


def _dirty_dynamic_data() -> dict[str, Any]:
    return {
        "owner_name": "  \u5f20\u4e09  ",
        "plate_no": " \u8d63 A12345 ",
        "vin": "LC0C76C41R4095092 5\u4eba",
        "engine_no": " A024676-5 ",
        "vehicle_model": " \u6bd4\u4e9a\u8fea \u79e6PLUS ",
        "first_register_date": "20240112",
        "issue_date": "2024/01/12",
        "id_number": "360426199101134023\u516c",
        "id_birth_date": "",
        "id_validity": "2013.03.05 - \u957f\u671f",
        "approved_passenger_count": "2+3\u4eba",
        "manufacturer_name": "\u8054\u7cfb\u4eba:\u9a6c\u9b41\u57fa/\u8054\u7cfb\u7535\u8bdd:023-67921044",
        "vehicle_brand_name": "\u4e2d\u56fd",
        "id_birth": "19910113",
        "id_nation": "\u6c49",
        "register_date": "20240101",
        "id_issue_authority": " \u5fb7\u5b89\u53bf\u516c\u5b89\u5c40 ",
        "dla_approved_passengers": "7\u4eba",
        "dl_legacy_noise": "should be removed",
    }


def _expected_after_create() -> dict[str, Any]:
    return {
        "owner_name": "\u5f20\u4e09",
        "plate_no": "\u8d63A12345",
        "vin": "LC0C76C41R4095092",
        "engine_no": "A0246765",
        "vehicle_model": "\u6bd4\u4e9a\u8fea \u79e6PLUS",
        "first_register_date": "2024-01-12",
        "issue_date": "2024-01-12",
        "id_number": "360426199101134023",
        "id_birth_date": "1991-01-13",
        "id_valid_from": "2013-03-05",
        "id_valid_to": "\u957f\u671f",
        "approved_passenger_count": "5",
        "manufacturer_name": None,
        "vehicle_brand_name": None,
        "id_ethnicity": "\u6c49",
        "id_issuer": "\u5fb7\u5b89\u53bf\u516c\u5b89\u5c40",
    }


def _create_dirty_order(token: str, fixture: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "module": "order",
        "customer_group_id": fixture["customer_group_id"],
        "channel_group_id": fixture["channel_group_id"],
        "dynamic_data": _dirty_dynamic_data(),
        "ocr_raw_json": {
            "raw_marker": " raw ocr must stay untouched ",
            "words_result": {"vin": {"words": "LC0C76C41R4095092 5\u4eba"}},
        },
        "order_info": {"remark": f"OCR cleaner API retest {RUN_ID}"},
    }
    order = _request("create_dirty_order", "POST", "/orders", token=token, json=payload)
    order_id = int(order["id"])
    created["orders"].append(order_id)
    return order


def _assert_dynamic_data(label_prefix: str, dd: dict[str, Any], expected: dict[str, Any]) -> None:
    for key, value in expected.items():
        _assert_equal(f"{label_prefix}_{key}", dd.get(key), value)

    for removed_key in (
        "id_birth",
        "id_nation",
        "register_date",
        "id_issue_authority",
        "dla_approved_passengers",
        "dl_legacy_noise",
    ):
        _assert_equal(f"{label_prefix}_removed_{removed_key}", removed_key in dd, False)


def _fetch_order_fact(order_id: int) -> dict[str, Any] | None:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_id, owner_name, plate_no, vin, engine_no,
                       vehicle_model, first_register_date, id_number
                FROM order_fact_new
                WHERE order_id=%s
                """,
                (int(order_id),),
            )
            return cur.fetchone()
    finally:
        conn.close()


def _assert_order_fact(order_id: int) -> None:
    row = _fetch_order_fact(order_id)
    _assert_true("order_fact_exists", bool(row), {"row": row})
    assert row is not None
    _assert_equal("order_fact_owner_name", row.get("owner_name"), "\u5f20\u4e09")
    _assert_equal("order_fact_plate_no", row.get("plate_no"), "\u8d63A12345")
    _assert_equal("order_fact_vin", row.get("vin"), "LC0C76C41R4095092")
    _assert_equal("order_fact_engine_no", row.get("engine_no"), "A0246765")
    _assert_equal("order_fact_first_register_date", str(row.get("first_register_date")), "2024-01-12")
    _assert_equal("order_fact_id_number", row.get("id_number"), "360426199101134023")


def _assert_list_search(token: str, order_id: int) -> None:
    cases = [
        ("list_by_vin", {"vin": "LC0C76C41R4095092"}),
        ("list_by_plate", {"plate_no": "\u8d63A12345"}),
        ("list_by_id_number", {"id_number": "360426199101134023"}),
        ("list_by_first_register_date", {"first_register_date_start": "2024-01-12", "first_register_date_end": "2024-01-12"}),
    ]
    for label, params in cases:
        body = _request(
            label,
            "GET",
            "/orders",
            token=token,
            params={"page": 1, "page_size": 50, **params},
        )
        items = body.get("items") if isinstance(body, dict) else []
        ids = [int(x.get("id") or 0) for x in items if isinstance(x, dict)]
        _assert_true(f"{label}_contains_order", int(order_id) in ids, {"ids": ids[:20], "total": body.get("total")})


def _assert_update_path(token: str, order_id: int) -> None:
    body = _request(
        "update_dirty_fields",
        "PUT",
        f"/orders/{order_id}",
        token=token,
        json={
            "dynamic_data": {
                "engine_no": "*8999",
                "vehicle_brand_name": "\u767d",
                "manufacturer_name": "(m1)/\u6700\u5927\u51c0\u529f\u7387(kW)",
            }
        },
    )
    dd = body.get("dynamic_data") or {}
    _assert_equal("update_engine_no_cleaned", dd.get("engine_no"), "8999")
    _assert_equal("update_vehicle_brand_null", dd.get("vehicle_brand_name"), None)
    _assert_equal("update_manufacturer_null", dd.get("manufacturer_name"), None)

    row = _fetch_order_fact(order_id)
    _assert_equal("update_order_fact_engine_no", row.get("engine_no") if row else None, "8999")


def _cleanup() -> dict[str, int]:
    order_ids = [int(x) for x in created["orders"] if int(x or 0) > 0]
    customer_code = created.get("customer_code")
    channel_code = created.get("channel_code")
    residual: dict[str, int] = {}
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            if order_ids:
                placeholders = ",".join(["%s"] * len(order_ids))
                for table, column in [
                    ("image_ocr_result_new", "order_id"),
                    ("order_slot_result_new", "order_id"),
                    ("order_image_new", "order_id"),
                    ("finance_record_new", "order_id"),
                    ("order_fact_new", "order_id"),
                    ("order_info_new", "order_id"),
                    ("order_new", "id"),
                ]:
                    try:
                        cur.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", order_ids)
                    except Exception:
                        pass
                try:
                    cur.execute(
                        f"DELETE FROM ocr_task_new WHERE scope_type='order' AND scope_id IN ({placeholders})",
                        order_ids,
                    )
                except Exception:
                    pass

            if customer_code:
                cur.execute("DELETE FROM customer_group_new WHERE customer_code=%s", (customer_code,))
            if channel_code:
                cur.execute("DELETE FROM channel_group_new WHERE channel_code=%s", (channel_code,))
            conn.commit()

            if order_ids:
                placeholders = ",".join(["%s"] * len(order_ids))
                cur.execute(f"SELECT COUNT(*) c FROM order_new WHERE id IN ({placeholders})", order_ids)
                residual["orders"] = int(cur.fetchone()["c"])
            if customer_code:
                cur.execute("SELECT COUNT(*) c FROM customer_group_new WHERE customer_code=%s", (customer_code,))
                residual["customer_group"] = int(cur.fetchone()["c"])
            if channel_code:
                cur.execute("SELECT COUNT(*) c FROM channel_group_new WHERE channel_code=%s", (channel_code,))
                residual["channel_group"] = int(cur.fetchone()["c"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return residual


def main() -> int:
    try:
        token = _login()
        fixture = _create_fixture(token)
        created_order = _create_dirty_order(token, fixture)
        order_id = int(created_order["id"])
        _assert_dynamic_data("create_dynamic_data", created_order.get("dynamic_data") or {}, _expected_after_create())
        _assert_equal(
            "ocr_raw_json_preserved",
            (created_order.get("ocr_raw_json") or {}).get("raw_marker"),
            " raw ocr must stay untouched ",
        )

        detail = _request("get_order_detail", "GET", f"/orders/{order_id}", token=token)
        _assert_dynamic_data("detail_dynamic_data", detail.get("dynamic_data") or {}, _expected_after_create())
        _assert_order_fact(order_id)
        _assert_list_search(token, order_id)
        _assert_update_path(token, order_id)
    except Exception as exc:
        failures.append({"label": "script_exception", "error": str(exc)})
    finally:
        cleanup_error = None
        residual = None
        try:
            residual = _cleanup()
        except Exception as exc:
            cleanup_error = str(exc)
        if cleanup_error:
            failures.append({"label": "cleanup_failed", "error": cleanup_error})
        else:
            all_zero = all(value == 0 for value in (residual or {}).values())
            _record("cleanup_local_db_residual_zero", all_zero, extra=residual)

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "ok": not failures,
            "run_id": RUN_ID,
            "created": created,
            "summary": {"total": len(steps), "failures": len(failures)},
            "steps": steps,
            "failures": failures,
        }
        LOG_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        print(
            json.dumps(
                {
                    "ok": not failures,
                    "run_id": RUN_ID,
                    "log": str(LOG_PATH),
                    "summary": doc["summary"],
                    "failures": _redact(failures[:5]),
                },
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            )
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
