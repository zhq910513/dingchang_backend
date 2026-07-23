from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pymysql
import requests

from env_loader import load_backend_env


load_backend_env()


BASE = os.getenv("IMAGE_AUTH_TEST_BASE", "http://127.0.0.1:8000/api").rstrip("/")
RUN_ID = os.getenv("IMAGE_AUTH_TEST_RUN_ID") or datetime.now().strftime("%Y%m%d%H%M%S")
LOG_PATH = Path(os.getenv("IMAGE_AUTH_TEST_LOG", f"logs/image-auth-retest-{RUN_ID}.json"))
ADMIN_USER = os.getenv("SEED_SUPER_USERNAME", "dingchang_admin")
ADMIN_PASS = os.getenv("SEED_SUPER_PASSWORD", "dingchang_admin@123456")
TEMP_PASS = "CodexImg@" + RUN_ID[-8:]
TIMEOUT = int(os.getenv("IMAGE_AUTH_TEST_TIMEOUT", "30") or "30")

# Tiny real PNG payload plus run-specific bytes so the BOS key is unique.
PNG_HEADER = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lm9Q3wAAAABJRU5ErkJggg=="
)
FILE_BYTES = PNG_HEADER + (b"\ncodex-image-auth-retest=" + RUN_ID.encode("ascii"))

steps: list[dict] = []
failures: list[dict] = []
created: dict = {
    "users": [],
    "order_id": None,
    "db_storage_keys": [],
    "cloud_storage_keys": [],
    "team_name": None,
}


def _env_value(key: str, default: str = "") -> str:
    value = os.getenv(key, default)
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


def _redact(obj):
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if lowered in {"token", "sessiontoken", "session_token", "secretaccesskey", "accesskeyid", "authorization"}:
                out[key] = "***REDACTED***"
            elif isinstance(value, str) and ("authorization=" in value or "x-bce-security-token=" in value):
                out[key] = "***SIGNED_URL_REDACTED***"
            else:
                out[key] = _redact(value)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    if isinstance(obj, str) and ("authorization=" in obj or "x-bce-security-token=" in obj):
        return "***SIGNED_URL_REDACTED***"
    return obj


def _record(label: str, method: str, path: str, expected: tuple[int, ...], status: int, elapsed_ms: float, *, body=None, extra=None):
    ok = status in expected
    item = {
        "label": label,
        "method": method,
        "path": path,
        "expected": list(expected),
        "status": status,
        "elapsed_ms": round(elapsed_ms, 2),
        "ok": ok,
        "body": _redact(body),
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
    _record(
        label,
        method,
        path,
        expected,
        resp.status_code,
        elapsed_ms,
        body=body,
        extra={"x_perf_ms": resp.headers.get("X-Perf-Ms")},
    )
    return resp, body


def _assert(label: str, condition: bool, extra=None):
    status = 200 if condition else 599
    _record(label, "ASSERT", label, (200,), status, 0, body={"ok": bool(condition)}, extra=extra)
    if not condition:
        raise AssertionError(label)


def _login(username: str, password: str, label: str) -> tuple[str, dict]:
    _, body = _request(label, "POST", "/auth/login", json={"username": username, "password": password})
    token = body.get("token") if isinstance(body, dict) else None
    _assert(label + "_token_present", bool(token))
    return str(token), body


def _first_item(label: str, body: dict) -> dict:
    items = body.get("items") if isinstance(body, dict) else None
    _assert(label + "_items_present", bool(items), {"body": body})
    return items[0]


def _verify_db_no_url(order_id: int, storage_key: str) -> dict | None:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT oi.image_url, im.url AS image_file_url
                FROM order_image_new oi
                LEFT JOIN image_file_new im ON im.id = oi.image_file_id
                WHERE oi.order_id=%s AND oi.storage_key=%s
                LIMIT 1
                """,
                (order_id, storage_key),
            )
            return cur.fetchone()
    finally:
        conn.close()


def _cleanup() -> dict:
    user_names = [u.get("username") for u in created["users"] if u.get("username")]
    order_id = created.get("order_id")
    keys = list(dict.fromkeys(created.get("db_storage_keys") or []))
    residual: dict[str, int] = {}
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            if order_id:
                for table in ("image_ocr_result_new", "order_slot_result_new"):
                    try:
                        cur.execute(f"DELETE FROM {table} WHERE order_id=%s", (order_id,))
                    except Exception:
                        pass
                cur.execute("DELETE FROM ocr_task_new WHERE scope_type='order' AND scope_id=%s", (order_id,))
                cur.execute("DELETE FROM order_image_new WHERE order_id=%s", (order_id,))
                cur.execute("DELETE FROM finance_record_new WHERE order_id=%s", (order_id,))
                cur.execute("DELETE FROM order_fact_new WHERE order_id=%s", (order_id,))
                cur.execute("DELETE FROM order_info_new WHERE order_id=%s", (order_id,))
                cur.execute("DELETE FROM order_new WHERE id=%s", (order_id,))

            if keys:
                placeholders = ",".join(["%s"] * len(keys))
                cur.execute(f"DELETE FROM ocr_image_cache_new WHERE storage_key IN ({placeholders})", keys)
                cur.execute(f"DELETE FROM image_file_new WHERE storage_key IN ({placeholders})", keys)

            user_ids: list[int] = []
            if user_names:
                placeholders = ",".join(["%s"] * len(user_names))
                cur.execute(f"SELECT id FROM user_new WHERE username IN ({placeholders})", user_names)
                user_ids = [int(row["id"]) for row in cur.fetchall()]
            if user_ids:
                placeholders = ",".join(["%s"] * len(user_ids))
                cur.execute(f"DELETE FROM user_session_new WHERE user_id IN ({placeholders})", user_ids)
                cur.execute(f"DELETE FROM user_role_new WHERE user_id IN ({placeholders})", user_ids)
                cur.execute(f"DELETE FROM user_new WHERE id IN ({placeholders})", user_ids)

            conn.commit()

            if order_id:
                checks = [
                    ("order", "SELECT COUNT(*) c FROM order_new WHERE id=%s", (order_id,)),
                    ("order_image", "SELECT COUNT(*) c FROM order_image_new WHERE order_id=%s", (order_id,)),
                    ("order_info", "SELECT COUNT(*) c FROM order_info_new WHERE order_id=%s", (order_id,)),
                    ("ocr_task", "SELECT COUNT(*) c FROM ocr_task_new WHERE scope_type='order' AND scope_id=%s", (order_id,)),
                ]
                for name, sql, args in checks:
                    cur.execute(sql, args)
                    residual[name] = int(cur.fetchone()["c"])
            if keys:
                placeholders = ",".join(["%s"] * len(keys))
                cur.execute(f"SELECT COUNT(*) c FROM image_file_new WHERE storage_key IN ({placeholders})", keys)
                residual["image_file"] = int(cur.fetchone()["c"])
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


def _signed(url: str) -> bool:
    return "authorization=" in (url or "") and "x-bce-security-token=" in (url or "")


def _run():
    _request("anonymous_bos_sts_401", "GET", "/orders/bos-sts", expected=(401,))
    admin_token, _ = _login(ADMIN_USER, ADMIN_PASS, "login_admin")

    _, teams_body = _request("get_teams", "GET", "/orders/teams", token=admin_token)
    team_name = _first_item("teams", teams_body).get("team_name")
    created["team_name"] = team_name

    _, customer_body = _request("get_customer_groups", "GET", "/orders/customer-groups?status=1", token=admin_token)
    customer_group_id = _first_item("customer_groups", customer_body).get("id")
    _, channel_body = _request("get_channel_groups", "GET", "/orders/channel-groups?status=1", token=admin_token)
    channel_group_id = _first_item("channel_groups", channel_body).get("id")

    users: dict[str, dict] = {}
    for role in ("sales", "finance", "market"):
        username = f"codex_img2_{role}_{RUN_ID}"
        payload = {
            "username": username,
            "password": TEMP_PASS,
            "role_name": role,
            "team_name": team_name,
        }
        _, body = _request(f"create_{role}_user", "POST", "/users", token=admin_token, json=payload)
        created["users"].append({"id": body.get("id"), "username": username, "role": role})
        token, login_body = _login(username, TEMP_PASS, f"login_{role}")
        users[role] = {"token": token, "body": login_body, "id": body.get("id")}

    _request("market_bos_sts_403", "GET", "/orders/bos-sts", token=users["market"]["token"], expected=(403,))
    _request(
        "finance_vehicle_cert_upload_403",
        "POST",
        "/orders/bos-upload",
        token=users["finance"]["token"],
        expected=(403,),
        files={"file": (f"finance-vc-{RUN_ID}.png", FILE_BYTES, "image/png")},
        data={"slot_key": "vehicle_cert"},
    )

    _, fin_related = _request(
        "finance_related_upload_200",
        "POST",
        "/orders/bos-upload",
        token=users["finance"]["token"],
        files={"file": (f"finance-related-{RUN_ID}.png", FILE_BYTES + b"finance", "image/png")},
        data={"slot_key": "related"},
    )
    fin_related_key = fin_related.get("storage_key")
    if fin_related_key:
        created["cloud_storage_keys"].append(fin_related_key)
        created["db_storage_keys"].append(fin_related_key)
    _assert("finance_related_url_is_signed", _signed(fin_related.get("url", "")))

    _, draft = _request(
        "sales_create_draft_order",
        "POST",
        "/orders/draft",
        token=users["sales"]["token"],
        json={
            "module": "order",
            "customer_group_id": customer_group_id,
            "channel_group_id": channel_group_id,
            "dynamic_data": {"owner_name": "Codex image retest", "plate_no": "TEST" + RUN_ID[-5:]},
            "order_info": {"owner_phone": "139" + RUN_ID[-8:]},
        },
    )
    order_id = int(draft["order_id"])
    created["order_id"] = order_id

    _request(
        "finance_bind_related_before_finished_400",
        "POST",
        f"/orders/{order_id}/images/bind",
        token=users["finance"]["token"],
        expected=(400,),
        json={
            "images": [
                {
                    "slot_key": "related",
                    "storage_key": fin_related_key,
                    "md5": fin_related.get("md5", ""),
                    "url": "https://evil.invalid/should-not-store.jpg",
                }
            ],
            "trigger_ocr": False,
        },
    )

    _, sales_upload = _request(
        "sales_vehicle_cert_upload_200",
        "POST",
        "/orders/bos-upload",
        token=users["sales"]["token"],
        files={"file": (f"vehicle-cert-{RUN_ID}.png", FILE_BYTES + b"sales", "image/png")},
        data={"slot_key": "vehicle_cert"},
    )
    sales_key = sales_upload.get("storage_key")
    if sales_key:
        created["cloud_storage_keys"].append(sales_key)
        created["db_storage_keys"].append(sales_key)
    _assert("sales_upload_url_is_signed", _signed(sales_upload.get("url", "")))

    _request(
        "sales_finalize_with_malicious_url_200",
        "POST",
        "/orders/finalize",
        token=users["sales"]["token"],
        json={
            "order_id": order_id,
            "images": [
                {
                    "slot_key": "vehicle_cert",
                    "storage_key": sales_key,
                    "md5": sales_upload.get("md5", ""),
                    "etag": sales_upload.get("etag"),
                    "size": sales_upload.get("size", 0),
                    "content_type": sales_upload.get("content_type"),
                    "original_name": sales_upload.get("original_name"),
                    "url": "https://evil.invalid/should-not-store.jpg",
                }
            ],
            "dynamic_data": {"owner_name": "Codex image retest"},
        },
    )

    db_row = _verify_db_no_url(order_id, sales_key)
    _assert(
        "db_did_not_store_frontend_url",
        bool(db_row) and not (db_row.get("image_url") or "") and not (db_row.get("image_file_url") or ""),
        {"db_row": db_row},
    )

    _, detail = _request("sales_order_detail_200", "GET", f"/orders/{order_id}", token=users["sales"]["token"])
    found_url = ""
    for slot in detail.get("slot_images") or []:
        if slot.get("slot_key") == "vehicle_cert":
            images = slot.get("images") or []
            if images:
                found_url = images[0].get("url") or ""
                break
    _assert("detail_image_url_is_signed", _signed(found_url), {"url": found_url})
    _assert("detail_image_url_is_not_malicious", "evil.invalid" not in found_url, {"url": found_url})

    started = time.perf_counter()
    signed_resp = requests.get(found_url, timeout=TIMEOUT)
    elapsed_ms = (time.perf_counter() - started) * 1000
    _record(
        "anonymous_fetch_signed_detail_url_200",
        "GET",
        urlparse(found_url).path,
        (200,),
        signed_resp.status_code,
        elapsed_ms,
        body={"content_type": signed_resp.headers.get("Content-Type"), "bytes": len(signed_resp.content)},
        extra={"url": found_url},
    )

    _request(
        "sales_mark_order_finished_200",
        "PATCH",
        f"/orders/{order_id}/status",
        token=users["sales"]["token"],
        json={"is_finished": True},
    )
    _request(
        "finance_bind_related_after_finished_200",
        "POST",
        f"/orders/{order_id}/images/bind",
        token=users["finance"]["token"],
        json={
            "images": [
                {
                    "slot_key": "related",
                    "storage_key": fin_related_key,
                    "md5": fin_related.get("md5", ""),
                    "url": "https://evil.invalid/finance-url.jpg",
                }
            ],
            "trigger_ocr": False,
        },
    )
    fin_db_row = _verify_db_no_url(order_id, fin_related_key)
    _assert(
        "finance_bind_db_did_not_store_frontend_url",
        bool(fin_db_row) and not (fin_db_row.get("image_url") or "") and not (fin_db_row.get("image_file_url") or ""),
        {"db_row": fin_db_row},
    )


def main() -> int:
    cleanup_error = None
    residual = None
    try:
        _run()
    except Exception as exc:
        failures.append({"label": "script_exception", "error": str(exc)})
    finally:
        try:
            residual = _cleanup()
        except Exception as exc:
            cleanup_error = str(exc)
        if cleanup_error:
            failures.append({"label": "cleanup_failed", "error": cleanup_error})
        else:
            all_zero = all(value == 0 for value in (residual or {}).values())
            _record("cleanup_local_db_residual_zero", "ASSERT", "cleanup", (200,), 200 if all_zero else 599, 0, body=residual)

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        summary = {"total": len(steps), "failures": len(failures)}
        doc = {
            "ok": not failures,
            "run_id": RUN_ID,
            "created": created,
            "summary": summary,
            "steps": steps,
            "failures": failures,
            "notes": [
                "Cloud objects are not deleted because the project does not currently expose a safe delete endpoint.",
            ],
        }
        LOG_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "ok": not failures,
                    "run_id": RUN_ID,
                    "log": str(LOG_PATH),
                    "summary": summary,
                    "created": _redact(created),
                    "failures": _redact(failures[:5]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
