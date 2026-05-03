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


BASE = os.getenv("FILTER_TEST_BASE", "http://127.0.0.1:8000/api").rstrip("/")
RUN_ID = os.getenv("FILTER_TEST_RUN_ID") or datetime.now().strftime("%Y%m%d%H%M%S")
LOG_PATH = Path(os.getenv("FILTER_TEST_LOG", f"logs/filter-conditions-retest-{RUN_ID}.json"))
ADMIN_USER = os.getenv("SEED_SUPER_USERNAME", "dingchang_admin")
ADMIN_PASS = os.getenv("SEED_SUPER_PASSWORD", "dingchang_admin@123456")
TEMP_PASS = "CodexFilter@" + RUN_ID[-8:]
TIMEOUT = int(os.getenv("FILTER_TEST_TIMEOUT", "30") or "30")

steps: list[dict[str, Any]] = []
failures: list[dict[str, Any]] = []
created: dict[str, Any] = {
    "users": [],
    "orders": [],
    "customer_group_id": None,
    "channel_group_id": None,
    "customer_code": None,
    "channel_code": None,
    "team_name": None,
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
            lowered = str(key).lower()
            if lowered in {"token", "session_token"}:
                out[key] = "***REDACTED***"
            else:
                out[key] = _redact(value)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _record(label: str, ok: bool, *, method: str = "ASSERT", path: str = "", status: int | None = None, elapsed_ms: float = 0, extra=None):
    item = {
        "label": label,
        "ok": bool(ok),
        "method": method,
        "path": path,
        "status": status,
        "elapsed_ms": round(elapsed_ms, 2),
        "extra": _redact(extra or {}),
    }
    steps.append(item)
    if not ok:
        failures.append(item)
    return ok


def _request(label: str, method: str, path: str, *, token: str | None = None, expected: tuple[int, ...] = (200,), **kwargs):
    headers = kwargs.pop("headers", {}) or {}
    if token:
        headers["X-Session-Token"] = token
    started = time.perf_counter()
    resp = requests.request(method, BASE + path, headers=headers, timeout=TIMEOUT, **kwargs)
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        body = resp.json()
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
        extra={"body": body if not ok else None, "x_perf_ms": resp.headers.get("X-Perf-Ms")},
    )
    if not ok:
        raise AssertionError(f"{label} expected {expected}, got {resp.status_code}: {body}")
    return body


def _assert(label: str, condition: bool, extra=None):
    _record(label, condition, extra=extra)
    if not condition:
        raise AssertionError(label)


def _login(username: str, password: str, label: str) -> tuple[str, dict]:
    body = _request(label, "POST", "/auth/login", json={"username": username, "password": password})
    token = body.get("token") if isinstance(body, dict) else None
    _assert(label + "_token_present", bool(token))
    return str(token), body


def _items(body: dict) -> list[dict]:
    return body.get("items") if isinstance(body, dict) and isinstance(body.get("items"), list) else []


def _assert_contains_id(label: str, body: dict, target_id: int):
    ids = [int(x.get("id") or x.get("order_id") or 0) for x in _items(body)]
    _assert(label, int(target_id) in ids, {"target_id": target_id, "ids": ids[:20], "total": body.get("total")})


def _assert_order_contains(label: str, body: dict, order_id: int):
    ids = [int(x.get("id") or 0) for x in _items(body)]
    _assert(label, int(order_id) in ids, {"order_id": order_id, "ids": ids[:20], "total": body.get("total")})


def _today() -> str:
    return date.today().isoformat()


def _cleanup() -> dict[str, int]:
    user_names = [u["username"] for u in created["users"] if u.get("username")]
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

            user_ids: list[int] = []
            if user_names:
                placeholders = ",".join(["%s"] * len(user_names))
                cur.execute(f"SELECT id FROM user_new WHERE username IN ({placeholders})", user_names)
                user_ids = [int(row["id"]) for row in cur.fetchall()]
            if user_ids:
                placeholders = ",".join(["%s"] * len(user_ids))
                cur.execute(f"DELETE FROM user_session_new WHERE user_id IN ({placeholders})", user_ids)
                cur.execute(f"DELETE FROM user_role_new WHERE user_id IN ({placeholders})", user_ids)
                cur.execute(f"UPDATE user_new SET parent_id=NULL WHERE parent_id IN ({placeholders})", user_ids)
                cur.execute(f"DELETE FROM user_new WHERE id IN ({placeholders})", user_ids)

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
            if user_names:
                placeholders = ",".join(["%s"] * len(user_names))
                cur.execute(f"SELECT COUNT(*) c FROM user_new WHERE username IN ({placeholders})", user_names)
                residual["users"] = int(cur.fetchone()["c"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return residual


def _set_user_real_name_and_status(username: str, *, real_name: str | None = None, status: int | None = None):
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            parts = []
            args: list[Any] = []
            if real_name is not None:
                parts.append("real_name=%s")
                args.append(real_name)
            if status is not None:
                parts.append("status=%s")
                args.append(int(status))
            if parts:
                args.append(username)
                cur.execute(f"UPDATE user_new SET {', '.join(parts)}, updated_at=NOW() WHERE username=%s", args)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _create_fixture_data(admin_token: str):
    teams = _request("fixture_get_teams", "GET", "/orders/teams", token=admin_token)
    team_name = _items(teams)[0]["team_name"]
    created["team_name"] = team_name

    customer_payload = {
        "customer_code": f"FT-CUST-{RUN_ID}",
        "customer_name": f"筛选客户{RUN_ID}",
        "market": f"筛选市场{RUN_ID}",
        "region": f"筛选区域{RUN_ID}",
        "contacts": [],
    }
    customer = _request(
        "fixture_create_customer",
        "POST",
        "/customer-channel/customer-groups",
        token=admin_token,
        json=customer_payload,
    )
    created["customer_group_id"] = customer["id"]
    created["customer_code"] = customer_payload["customer_code"]

    channel_payload = {
        "channel_code": f"FT-CH-{RUN_ID}",
        "channel_name": f"筛选渠道{RUN_ID}",
        "region": f"筛选渠道区域{RUN_ID}",
        "contacts": [],
    }
    channel = _request(
        "fixture_create_channel",
        "POST",
        "/customer-channel/channel-groups",
        token=admin_token,
        json=channel_payload,
    )
    created["channel_group_id"] = channel["id"]
    created["channel_code"] = channel_payload["channel_code"]

    manager_username = f"codex_filter_manager_{RUN_ID}"
    manager_payload = {
        "username": manager_username,
        "password": TEMP_PASS,
        "role_name": "manager",
        "team_name": team_name,
        "team_names": team_name,
    }
    manager = _request("fixture_create_manager", "POST", "/users", token=admin_token, json=manager_payload)
    created["users"].append({"id": manager.get("id"), "username": manager_username, "role": "manager"})
    manager_token, _ = _login(manager_username, TEMP_PASS, "fixture_login_manager")

    active_sales_username = f"codex_filter_sales_on_{RUN_ID}"
    offline_sales_username = f"codex_filter_sales_off_{RUN_ID}"
    for username in (active_sales_username, offline_sales_username):
        user = _request(
            f"fixture_create_{username}",
            "POST",
            "/users",
            token=manager_token,
            json={
                "username": username,
                "password": TEMP_PASS,
                "role_name": "sales",
                "team_name": team_name,
            },
        )
        created["users"].append({"id": user.get("id"), "username": username, "role": "sales"})

    _set_user_real_name_and_status(active_sales_username, real_name=f"筛选姓名{RUN_ID}", status=1)
    _set_user_real_name_and_status(offline_sales_username, real_name=f"离线姓名{RUN_ID}", status=0)
    active_sales_token, _ = _login(active_sales_username, TEMP_PASS, "fixture_login_active_sales")

    common_dynamic = {
        "owner_name": f"筛选车主{RUN_ID}",
        "id_number": "36070219900101" + RUN_ID[-2:],
        "plate_no": "赣F" + RUN_ID[-5:],
        "engine_no": "ENG" + RUN_ID[-8:],
        "vin": "LDC613P23A130" + RUN_ID[-4:],
        "vehicle_model": f"筛选车型{RUN_ID}",
        "first_register_date": "2024-01-15",
    }
    common_info = {
        "insurance_expire_date": "2026-12-31",
        "owner_phone": "139" + RUN_ID[-8:],
        "remark": f"筛选备注{RUN_ID}",
    }
    active_sales_id = next(u["id"] for u in created["users"] if u["username"] == active_sales_username)

    finished_order = _request(
        "fixture_create_finished_candidate_order",
        "POST",
        "/orders",
        token=admin_token,
        json={
            "module": "order",
            "salesperson_id": active_sales_id,
            "customer_group_id": created["customer_group_id"],
            "channel_group_id": created["channel_group_id"],
            "dynamic_data": common_dynamic,
            "order_info": common_info,
        },
    )
    finished_order_id = int(finished_order["id"])
    created["orders"].append(finished_order_id)
    _request(
        "fixture_mark_finished_order",
        "PATCH",
        f"/orders/{finished_order_id}/status",
        token=admin_token,
        json={"is_finished": True},
    )
    _request(
        "fixture_mark_finance_flags",
        "PATCH",
        f"/finance/orders/{finished_order_id}/status",
        token=admin_token,
        json={"is_paid": True, "is_rebate": True},
    )

    unfinished_dynamic = dict(common_dynamic)
    unfinished_dynamic["plate_no"] = "赣U" + RUN_ID[-5:]
    unfinished_dynamic["owner_name"] = f"未完车主{RUN_ID}"
    unfinished_order = _request(
        "fixture_create_unfinished_order",
        "POST",
        "/orders",
        token=admin_token,
        json={
            "module": "order",
            "salesperson_id": active_sales_id,
            "customer_group_id": created["customer_group_id"],
            "channel_group_id": created["channel_group_id"],
            "dynamic_data": unfinished_dynamic,
            "order_info": common_info,
        },
    )
    unfinished_order_id = int(unfinished_order["id"])
    created["orders"].append(unfinished_order_id)

    return {
        "admin_token": admin_token,
        "manager_token": manager_token,
        "active_sales_token": active_sales_token,
        "team_name": team_name,
        "active_sales_username": active_sales_username,
        "active_sales_real_name": f"筛选姓名{RUN_ID}",
        "offline_sales_username": offline_sales_username,
        "active_sales_id": active_sales_id,
        "customer": customer_payload,
        "channel": channel_payload,
        "finished_order_id": finished_order_id,
        "unfinished_order_id": unfinished_order_id,
        "order_dynamic": common_dynamic,
        "order_info": common_info,
    }


def _test_user_filters(fx: dict):
    token = fx["manager_token"]
    active_username = fx["active_sales_username"]
    offline_username = fx["offline_sales_username"]
    active_id = next(u["id"] for u in created["users"] if u["username"] == active_username)
    offline_id = next(u["id"] for u in created["users"] if u["username"] == offline_username)

    cases = [
        ("user_keyword_username", {"keyword": active_username}, active_id),
        ("user_keyword_real_name", {"keyword": fx["active_sales_real_name"]}, active_id),
        ("user_role_sales", {"keyword": active_username, "role": "sales"}, active_id),
        ("user_status_enabled", {"keyword": active_username, "status": 1}, active_id),
        ("user_status_disabled", {"keyword": offline_username, "status": 0}, offline_id),
        ("user_online_true", {"keyword": active_username, "is_online": True}, active_id),
        ("user_online_false", {"keyword": offline_username, "is_online": False}, offline_id),
    ]
    for label, params, target_id in cases:
        body = _request(label + "_request", "GET", "/users", token=token, params={**params, "page": 1, "page_size": 100})
        _assert_contains_id(label, body, int(target_id))
        if label.startswith("user_online"):
            row = next((x for x in _items(body) if int(x.get("id") or 0) == int(target_id)), None)
            expected = label.endswith("true")
            _assert(label + "_flag", bool(row and row.get("is_online")) == expected, {"row": row})


def _test_customer_channel_filters(fx: dict):
    token = fx["admin_token"]
    cid = int(created["customer_group_id"])
    customer_cases = [
        ("customer_code", {"customer_code": fx["customer"]["customer_code"]}),
        ("customer_name", {"customer_name": fx["customer"]["customer_name"]}),
        ("customer_market", {"market": fx["customer"]["market"]}),
        ("customer_region", {"region": fx["customer"]["region"]}),
        ("customer_created_by_name", {"created_by_name": "dingchang"}),
    ]
    for label, params in customer_cases:
        body = _request(label + "_request", "GET", "/customer-channel/customer-groups", token=token, params={**params, "page": 1, "page_size": 100})
        _assert_contains_id(label, body, cid)

    chid = int(created["channel_group_id"])
    channel_cases = [
        ("channel_code", {"channel_code": fx["channel"]["channel_code"]}),
        ("channel_name", {"channel_name": fx["channel"]["channel_name"]}),
        ("channel_region", {"region": fx["channel"]["region"]}),
        ("channel_created_by_name", {"created_by_name": "dingchang"}),
    ]
    for label, params in channel_cases:
        body = _request(label + "_request", "GET", "/customer-channel/channel-groups", token=token, params={**params, "page": 1, "page_size": 100})
        _assert_contains_id(label, body, chid)


def _test_order_filters(fx: dict):
    token = fx["admin_token"]
    order_id = fx["finished_order_id"]
    unfinished_id = fx["unfinished_order_id"]
    dd = fx["order_dynamic"]
    info = fx["order_info"]
    base = {"page": 1, "page_size": 100}
    order_cases = [
        ("orders_created_date", {"created_date_start": _today(), "created_date_end": _today()}, order_id),
        ("orders_channel_group", {"channel_group_id": created["channel_group_id"]}, order_id),
        ("orders_customer_group", {"customer_group_id": created["customer_group_id"]}, order_id),
        ("orders_salesperson", {"salesperson_id": fx["active_sales_id"]}, order_id),
        ("orders_team", {"team_name": fx["team_name"]}, order_id),
        ("orders_owner_name", {"owner_name": dd["owner_name"]}, order_id),
        ("orders_id_number", {"id_number": dd["id_number"]}, order_id),
        ("orders_plate_no", {"plate_no": dd["plate_no"]}, order_id),
        ("orders_engine_no", {"engine_no": dd["engine_no"]}, order_id),
        ("orders_vin", {"vin": dd["vin"]}, order_id),
        ("orders_vehicle_model", {"vehicle_model": dd["vehicle_model"]}, order_id),
        ("orders_remark", {"remark": info["remark"]}, order_id),
        ("orders_finished_page", {"is_finished": True}, order_id),
        ("orders_unfinished_page", {"is_finished": False}, unfinished_id),
    ]
    for label, params, target_id in order_cases:
        body = _request(label + "_request", "GET", "/orders", token=token, params={**base, **params})
        _assert_order_contains(label, body, int(target_id))


def _test_finance_filters(fx: dict):
    token = fx["admin_token"]
    order_id = fx["finished_order_id"]
    dd = fx["order_dynamic"]
    info = fx["order_info"]
    base = {"page": 1, "page_size": 100}
    finance_cases = [
        ("finance_created_date", {"created_date_start": _today(), "created_date_end": _today()}),
        ("finance_channel_group", {"channel_group_id": created["channel_group_id"]}),
        ("finance_customer_group", {"customer_group_id": created["customer_group_id"]}),
        ("finance_market", {"market": fx["customer"]["market"]}),
        ("finance_team", {"team_name": fx["team_name"]}),
        ("finance_owner_name", {"owner_name": dd["owner_name"]}),
        ("finance_insurance_expire_date", {"insurance_expire_date": info["insurance_expire_date"]}),
        ("finance_first_register_date", {"first_register_date_start": dd["first_register_date"], "first_register_date_end": dd["first_register_date"]}),
        ("finance_is_paid", {"is_paid": True}),
        ("finance_is_rebate", {"is_rebate": True}),
    ]
    for label, params in finance_cases:
        body = _request(
            label + "_list_request",
            "GET",
            "/orders",
            token=token,
            params={**base, **params, "is_finished": True},
        )
        _assert_order_contains(label, body, int(order_id))
        _request(label + "_summary_request", "GET", "/finance/orders/summary", token=token, params=params)


def main() -> int:
    try:
        admin_token, _ = _login(ADMIN_USER, ADMIN_PASS, "login_admin")
        fx = _create_fixture_data(admin_token)
        _test_user_filters(fx)
        _test_customer_channel_filters(fx)
        _test_order_filters(fx)
        _test_finance_filters(fx)
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
        summary = {"total": len(steps), "failures": len(failures)}
        doc = {
            "ok": not failures,
            "run_id": RUN_ID,
            "created": created,
            "summary": summary,
            "steps": steps,
            "failures": failures,
        }
        LOG_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": not failures, "run_id": RUN_ID, "log": str(LOG_PATH), "summary": summary, "failures": _redact(failures[:5])}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
