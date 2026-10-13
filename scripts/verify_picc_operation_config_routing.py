# -*- coding: utf-8 -*-
"""Focused regression checks for account-owned PICC operation routing."""

from __future__ import annotations

import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.quote_assistant_service import (
    _complete_renewal_lookup_without_sms,
    _apply_platform_account_runtime_status,
    _platform_default_config_payload,
    _selected_task_operation_config_snapshot,
    _snapshot_with_account_operation_config,
    refresh_platform_account_operation_configs,
    select_platform_account_operation_config,
)
from app.api.v1.ai_assistant import (
    refresh_platform_account_operation_configs as refresh_platform_account_operation_configs_api,
)
from app.services.quote_platforms.base import PlatformAccountContext, PlatformRuntimeResult
from app.services.quote_platforms.session_manager import (
    AccountSessionSnapshot,
    QuotePlatformSessionStore,
    SESSION_STATUS_AUTHENTICATED,
)
from app.services.quote_platforms.platforms.picc.business import (
    PICC_COMMON_PLATFORM_DEFAULTS,
    PiccBusinessAdapter,
    _motor_quote_profile,
    _picc_business_defaults,
    _picc_runtime_quote_overrides,
)
from app.services.quote_platforms.platforms.picc.operation_config import (
    operation_config_id_from_response,
    operation_config_items_from_responses,
    operation_config_state,
    public_operation_config_state,
    strip_account_managed_default_fields,
)


RAW_CONFIG_A = {
    "id": "QT362200191780885063489",
    "name": "修理厂",
    "validStatus": "1",
    "riskCode": "DZA",
    "comCode": "36220019",
    "makecom": "36220019",
    "comCName": "宜春市分公司袁山大道营销服务部修理厂业务部",
    "operateCode": "36044347",
    "operateName": "何颖",
    "handlerCode": "36044347",
    "handlerName": "何颖",
    "handler1Code": "36044347",
    "handler1Name": "何颖",
    "agentType": "2",
    "agentCode": "360021100116",
    "agentName": "江西永康保险代理有限公司高安分公司",
    "businessNature": "2",
    "businessNatureName": "专业代理业务",
}

RAW_CONFIG_B = {
    **RAW_CONFIG_A,
    "id": "QT362200711111111111111",
    "name": "直属业务部",
    "comCode": "36220071",
    "makecom": "36220071",
    "comCName": "宜春市分公司",
}


def operation_context(selected_id: str = RAW_CONFIG_A["id"]) -> PlatformAccountContext:
    items = operation_config_items_from_responses(
        {"status": 0, "data": {"operateConfigList": [RAW_CONFIG_A, RAW_CONFIG_B]}}
    )
    return PlatformAccountContext(
        platform_code="PICC",
        platform_name="人保",
        account_id=1,
        account_username="test",
        payload={
            "operation_config_snapshot": operation_config_state(
                {"items": items, "selected_id": selected_id, "status": "ready"}
            )
        },
    )


class PiccOperationConfigRoutingTests(unittest.TestCase):
    def test_platform_rows_are_normalized_and_multiple_items_remain_selectable(self) -> None:
        items = operation_config_items_from_responses(
            {"status": 0, "data": {"operateConfigList": [RAW_CONFIG_A, RAW_CONFIG_B]}}
        )
        self.assertEqual({item["id"] for item in items}, {RAW_CONFIG_A["id"], RAW_CONFIG_B["id"]})
        state = operation_config_state({"items": items, "selected_id": RAW_CONFIG_B["id"]})
        self.assertEqual(state["selected"]["com_code"], "36220071")
        self.assertEqual(state["selected_id"], RAW_CONFIG_B["id"])

    def test_unrelated_response_ids_never_become_operation_configs(self) -> None:
        items = operation_config_items_from_responses(
            {
                "status": 0,
                "data": {
                    "currentUser": {"id": "83613400", "userName": "测试用户", "comCode": "36220071"},
                    "page": {"id": 1, "total": 2},
                    "operateConfigList": [RAW_CONFIG_A],
                },
            }
        )
        self.assertEqual([item["id"] for item in items], [RAW_CONFIG_A["id"]])
        self.assertEqual(
            operation_config_id_from_response({"data": {"defaultOperateConfigId": RAW_CONFIG_A["id"]}}),
            RAW_CONFIG_A["id"],
        )

    def test_operation_config_list_is_not_silently_truncated(self) -> None:
        rows = [
            {
                **RAW_CONFIG_A,
                "id": f"QT36220019{i:012d}",
                "name": f"配置{i}",
            }
            for i in range(225)
        ]
        items = operation_config_items_from_responses({"data": {"operateConfigList": rows}})
        self.assertEqual(len(items), 225)

    def test_account_routing_overrides_and_removes_all_legacy_default_routes(self) -> None:
        defaults = _picc_business_defaults(
            {
                "归属机构代码": "36040200",
                "操作机构代码": "36040213",
                "操作配置ID": "QT360402131762390540324",
                "业务性质代码": "99",
                "业务性质名称": "错误业务性质",
                "第三者责任险": "500",
            },
            operation_context(),
        )
        self.assertEqual(defaults["归属机构代码"], "36220019")
        self.assertEqual(defaults["操作机构代码"], "36220019")
        self.assertEqual(defaults["操作配置ID"], RAW_CONFIG_A["id"])
        self.assertEqual(defaults["业务性质代码"], "2")
        self.assertEqual(defaults["第三者责任险"], "500")
        self.assertNotIn("归属机构代码", PICC_COMMON_PLATFORM_DEFAULTS)
        self.assertNotIn("操作机构代码", PICC_COMMON_PLATFORM_DEFAULTS)
        self.assertNotIn("操作配置ID", PICC_COMMON_PLATFORM_DEFAULTS)

    def test_final_quote_form_carries_the_complete_selected_route(self) -> None:
        adapter = PiccBusinessAdapter()
        defaults = _picc_business_defaults({"第三者责任险": "300"}, operation_context())
        profile = _motor_quote_profile("油车-旧")
        vehicle = {
            "licenseNo": "赣C12345",
            "engineNo": "ENGINE001",
            "vin": "LTESTVIN000000001",
            "enrollDate": "2024-01-01",
            "startDateBI": "2026-10-14",
            "startDateCI": "2026-10-14",
            "modelName": "测试车型",
            "actualValue": "100000",
            "purchasePrice": "100000",
            "seatCount": "5",
            # This value is returned by a separate checker query and must not
            # replace the account-selected route.
            "mainComCode": "36040213",
        }
        owner = {"ownerName": "测试", "ownerIdNo": "360402199001011234", "ownerPhone": "13900000000"}
        selected_vehicle = {"vehicleId": "MODEL001", "vehicleModelCode": "PLAT001", "purchasePrice": "100000"}
        products = adapter._used_fuel_products(defaults, profile=profile, actual_value="100000", seat_count="5")
        form = adapter._build_used_fuel_quote_form(
            defaults,
            vehicle,
            owner,
            selected_vehicle,
            {},
            products,
            profile=profile,
            strict_config=False,
        )
        self.assertEqual(form["prpCmain.comCode"], "36220019")
        self.assertEqual(form["opconfComCode"], "36220019")
        self.assertEqual(form["opconf_makecomCode"], "36220019")
        self.assertEqual(form["selectedOperateConfigId"], RAW_CONFIG_A["id"])
        self.assertEqual(form["selectedOperateConfig"], RAW_CONFIG_A["id"])
        self.assertEqual(form["businesNature"], "2")
        self.assertEqual(form["prpCmain.businesNature"], "2")
        self.assertEqual(form["prpCmain.agentCode"], "360021100116")
        self.assertEqual(form["prpCmain.handlerCode"], "36044347")
        self.assertEqual(form["prpCmain.handler1Code"], "36044347")
        self.assertEqual(form["prpCmain.operateCode"], "36044347")
        self.assertNotIn("monopolyCode", form)

        manual_defaults = _picc_business_defaults({}, operation_context())
        manual_defaults.update(
            _picc_runtime_quote_overrides(
                {
                    "quote_field_overrides": {
                        "送修码启用": "1",
                        "送修码": "3604731000027",
                        "送修码名称": "濂溪区金鑫汽车修理厂",
                        "操作机构代码": "FORGED-ROUTE",
                        "业务性质代码": "99",
                    }
                }
            )
        )
        manual_products = adapter._used_fuel_products(
            manual_defaults,
            profile=profile,
            actual_value="100000",
            seat_count="5",
        )
        manual_form = adapter._build_used_fuel_quote_form(
            manual_defaults,
            vehicle,
            owner,
            selected_vehicle,
            {},
            manual_products,
            profile=profile,
            strict_config=False,
        )
        self.assertEqual(manual_form["monopolyCode"], "3604731000027")
        self.assertEqual(manual_form["groupCodeValidStatus"], "1")
        self.assertEqual(manual_form["opconfComCode"], "36220019")
        self.assertEqual(manual_form["businesNature"], "2")

    def test_precise_vehicle_query_carries_selected_operation_config_id(self) -> None:
        class CaptureClient:
            config = SimpleNamespace(base_url="https://example.invalid")

            def __init__(self) -> None:
                self.params = {}

            def request_json(self, method, path, **kwargs):
                del method, path
                self.params = dict(kwargs.get("params") or {})
                return {"status": 0, "data": []}

        client = CaptureClient()
        defaults = _picc_business_defaults({}, operation_context())
        PiccBusinessAdapter()._query_precise_vehicle(
            client,
            {
                "vin": "LTESTVIN000000001",
                "licenseNo": "赣C12345",
                "enrollDate": "2024-01-01",
            },
            defaults,
            {"vehicleId": "MODEL001", "vehicleSeat": "5"},
            profile=_motor_quote_profile("油车-旧"),
        )
        self.assertEqual(client.params["selectedOperateConfigId"], RAW_CONFIG_A["id"])

    def test_missing_account_operation_config_stops_before_platform_quote(self) -> None:
        adapter = PiccBusinessAdapter()
        ctx = PlatformAccountContext(
            platform_code="PICC",
            platform_name="人保",
            account_id=1,
            account_username="test",
            account_type_name="油车-旧",
            payload={"operation_config_snapshot": {"items": [], "selected_id": ""}},
        )
        result = adapter._quote_sync(
            ctx,
            {
                "vehicle_type_detect": {"config_type_name": "油车-旧"},
                "normalized_data": {},
                "default_config_json": {"第三者责任险": "300"},
            },
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("error_code"), "PICC_OPERATION_CONFIG_MISSING")
        self.assertEqual(
            result.data.get("missing_operation_config_fields"),
            ["操作配置ID", "机构码", "业务性质代码"],
        )
        self.assertIn("机构码", result.message)

    def test_default_config_api_and_storage_filter_all_account_owned_fields(self) -> None:
        raw = {
            "第三者责任险": "300",
            "操作机构代码": "36040213",
            "selectedOperateConfigId": "OLD",
            "prpCmain.agentCode": "OLD-AGENT",
        }
        self.assertEqual(strip_account_managed_default_fields(raw), {"第三者责任险": "300"})
        row = SimpleNamespace(
            id=1,
            platform_code="PICC",
            platform_name="人保",
            account_type_name="油车-旧",
            default_values_json=raw,
            enabled=True,
            created_by=1,
            updated_by=1,
            created_at=None,
            updated_at=None,
        )
        self.assertEqual(_platform_default_config_payload(row)["default_values"], {"第三者责任险": "300"})

    def test_task_snapshot_freezes_complete_selected_config(self) -> None:
        account = SimpleNamespace(
            id=7,
            platform_code="PICC",
            credential_payload={
                "operation_configs": operation_config_state(
                    {"items": [{**RAW_CONFIG_A, "futureField": "keep-me"}], "selected_id": RAW_CONFIG_A["id"]}
                )
            },
        )
        snapshot = _snapshot_with_account_operation_config({"default_config_json": {}}, account)
        frozen = snapshot["operation_config_snapshot"]
        self.assertEqual(snapshot["operation_config_account_id"], 7)
        self.assertEqual(frozen["selected_id"], RAW_CONFIG_A["id"])
        self.assertEqual(frozen["selected"]["raw"]["futureField"], "keep-me")
        account.credential_payload["operation_configs"] = operation_config_state(
            {"items": [RAW_CONFIG_B], "selected_id": RAW_CONFIG_B["id"]}
        )
        self.assertEqual(frozen["selected_id"], RAW_CONFIG_A["id"])
        same_account_retry = _snapshot_with_account_operation_config(snapshot, account)
        self.assertEqual(same_account_retry["operation_config_snapshot"]["selected_id"], RAW_CONFIG_A["id"])
        empty_frozen = {
            "operation_config_account_id": account.id,
            "operation_config_snapshot": operation_config_state({"items": [], "selected_id": ""}),
        }
        refreshed_after_empty_freeze = _snapshot_with_account_operation_config(empty_frozen, account)
        self.assertEqual(
            refreshed_after_empty_freeze["operation_config_snapshot"]["selected_id"],
            RAW_CONFIG_B["id"],
        )
        other_account = SimpleNamespace(
            id=8,
            platform_code="PICC",
            credential_payload={
                "operation_configs": operation_config_state(
                    {"items": [RAW_CONFIG_B], "selected_id": RAW_CONFIG_B["id"]}
                )
            },
        )
        switched_account = _snapshot_with_account_operation_config(snapshot, other_account)
        self.assertEqual(switched_account["operation_config_snapshot"]["selected_id"], RAW_CONFIG_B["id"])
        public = public_operation_config_state(frozen)
        self.assertNotIn("raw", public["selected"])
        self.assertEqual(frozen["selected"]["raw"]["futureField"], "keep-me")

    def test_task_route_reader_ignores_empty_structural_snapshot(self) -> None:
        empty_state = operation_config_state({"items": [], "selected_id": ""})
        selected_state = operation_config_state(
            {"items": [RAW_CONFIG_B], "selected_id": RAW_CONFIG_B["id"]}
        )
        selected = _selected_task_operation_config_snapshot(
            {"operation_config_snapshot": empty_state},
            {"platform_account": {"operation_configs": selected_state}},
        )
        self.assertEqual(selected["selected_id"], RAW_CONFIG_B["id"])
        self.assertEqual(
            _selected_task_operation_config_snapshot(
                {"operation_config_snapshot": empty_state},
                {"platform_account": {"operation_configs": empty_state}},
            ),
            {},
        )


class _FakeDb:
    def __init__(self) -> None:
        self.flush_count = 0

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        return None


class _SessionStoreDb:
    def __init__(self, credential_payload=None) -> None:
        self.refresh_calls = []
        self.added = []
        self.credential_payload = credential_payload

    async def refresh(self, account, **kwargs) -> None:
        self.refresh_calls.append(kwargs)
        if self.credential_payload is not None:
            account.credential_payload = self.credential_payload
        else:
            account.credential_payload = {
                "operation_configs": operation_config_state(
                    {"items": [RAW_CONFIG_B], "selected_id": RAW_CONFIG_B["id"]}
                )
            }

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class PiccOperationConfigServiceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def account(state=None):
        return SimpleNamespace(
            id=11,
            owner_user_id=3,
            platform_code="PICC",
            platform_name="人保",
            account_username="test-user",
            password_ciphertext="",
            login_phone="",
            login_phone_mask="",
            email="",
            account_owner_name="",
            account_type_name="油车-旧",
            browser_env_key="",
            credential_payload={"operation_configs": state or {}},
            updated_at=None,
        )

    async def test_forged_selection_is_rejected_and_valid_selection_is_saved(self) -> None:
        state = operation_config_state(
            {"items": [RAW_CONFIG_A, RAW_CONFIG_B], "selected_id": RAW_CONFIG_A["id"]}
        )
        account = self.account(state)
        db = _FakeDb()
        with patch(
            "app.services.quote_assistant_service.get_platform_account_profile",
            new=AsyncMock(return_value=account),
        ):
            with self.assertRaisesRegex(ValueError, "不属于当前账号"):
                await select_platform_account_operation_config(
                    db, owner_user_id=3, account_id=11, selected_id="QT-FORGED-ROUTE"
                )
            saved = await select_platform_account_operation_config(
                db, owner_user_id=3, account_id=11, selected_id=RAW_CONFIG_B["id"]
            )
        self.assertEqual(saved["selected_id"], RAW_CONFIG_B["id"])
        self.assertEqual(account.credential_payload["operation_configs"]["selected_id"], RAW_CONFIG_B["id"])

    async def test_refresh_uses_platform_default_for_multiple_items(self) -> None:
        account = self.account()
        db = _FakeDb()
        result = PlatformRuntimeResult(
            status="success",
            data={
                "operation_configs": {
                    "items": [RAW_CONFIG_A, RAW_CONFIG_B],
                    "platform_default_id": RAW_CONFIG_B["id"],
                }
            },
        )
        with (
            patch(
                "app.services.quote_assistant_service.get_platform_account_profile",
                new=AsyncMock(return_value=account),
            ),
            patch(
                "app.services.quote_assistant_service.quote_platform_runtime.query_operation_configs",
                new=AsyncMock(return_value=result),
            ),
        ):
            state = await refresh_platform_account_operation_configs(db, owner_user_id=3, account_id=11)
        self.assertEqual(len(state["items"]), 2)
        self.assertEqual(state["selected_id"], RAW_CONFIG_B["id"])
        self.assertEqual(state["status"], "ready")

    async def test_refresh_auto_selects_single_item(self) -> None:
        account = self.account()
        db = _FakeDb()
        result = PlatformRuntimeResult(
            status="success",
            data={"operation_configs": {"items": [RAW_CONFIG_A], "platform_default_id": ""}},
        )
        with (
            patch(
                "app.services.quote_assistant_service.get_platform_account_profile",
                new=AsyncMock(return_value=account),
            ),
            patch(
                "app.services.quote_assistant_service.quote_platform_runtime.query_operation_configs",
                new=AsyncMock(return_value=result),
            ),
        ):
            state = await refresh_platform_account_operation_configs(db, owner_user_id=3, account_id=11)
        self.assertEqual(state["selected_id"], RAW_CONFIG_A["id"])

    async def test_missing_route_fields_create_persistent_account_notice(self) -> None:
        account = self.account()
        account.login_status = "authenticated"
        result = PlatformRuntimeResult(
            status="failed",
            message="人保账号操作配置不完整，缺少：机构码、业务性质代码",
            data={
                "error_code": "PICC_OPERATION_CONFIG_MISSING",
                "missing_operation_config_fields": ["机构码", "业务性质代码"],
            },
        )
        _apply_platform_account_runtime_status(account, result, default_error="平台报价失败")
        notice = account.credential_payload["inspection_notice"]
        self.assertEqual(notice["type"], "operation_config_missing")
        self.assertEqual(notice["payload"]["missing_fields"], ["机构码", "业务性质代码"])
        self.assertEqual(account.login_status, "authenticated")

    async def test_refresh_failure_preserves_last_valid_selection(self) -> None:
        previous = operation_config_state(
            {"items": [RAW_CONFIG_A, RAW_CONFIG_B], "selected_id": RAW_CONFIG_A["id"], "status": "ready"}
        )
        account = self.account(previous)
        db = _FakeDb()
        result = PlatformRuntimeResult(status="network_error", message="平台暂时不可用")
        with (
            patch(
                "app.services.quote_assistant_service.get_platform_account_profile",
                new=AsyncMock(return_value=account),
            ),
            patch(
                "app.services.quote_assistant_service.quote_platform_runtime.query_operation_configs",
                new=AsyncMock(return_value=result),
            ),
        ):
            state = await refresh_platform_account_operation_configs(
                db, owner_user_id=3, account_id=11, raise_on_error=False
            )
        self.assertEqual(state["status"], "stale")
        self.assertEqual(state["selected_id"], RAW_CONFIG_A["id"])
        self.assertEqual(len(state["items"]), 2)
        self.assertIn("平台暂时不可用", state["last_error"])

    async def test_partial_refresh_merges_previous_complete_route(self) -> None:
        previous = operation_config_state(
            {"items": [RAW_CONFIG_A], "selected_id": RAW_CONFIG_A["id"], "status": "ready"}
        )
        account = self.account(previous)
        db = _FakeDb()
        partial = {
            "id": RAW_CONFIG_A["id"],
            "name": "修理厂（列表简项）",
            "comCode": RAW_CONFIG_A["comCode"],
            "makecom": RAW_CONFIG_A["makecom"],
        }
        result = PlatformRuntimeResult(
            status="success",
            data={"operation_configs": {"items": [partial], "platform_default_id": RAW_CONFIG_A["id"]}},
        )
        with (
            patch(
                "app.services.quote_assistant_service.get_platform_account_profile",
                new=AsyncMock(return_value=account),
            ),
            patch(
                "app.services.quote_assistant_service.quote_platform_runtime.query_operation_configs",
                new=AsyncMock(return_value=result),
            ),
        ):
            state = await refresh_platform_account_operation_configs(db, owner_user_id=3, account_id=11)
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["selected"]["business_nature"], RAW_CONFIG_A["businessNature"])
        self.assertEqual(state["selected"]["agent_code"], RAW_CONFIG_A["agentCode"])

    async def test_first_partial_refresh_is_rejected_before_quote(self) -> None:
        account = self.account()
        db = _FakeDb()
        partial = {
            "id": RAW_CONFIG_A["id"],
            "name": "修理厂（列表简项）",
            "comCode": RAW_CONFIG_A["comCode"],
            "makecom": RAW_CONFIG_A["makecom"],
        }
        result = PlatformRuntimeResult(
            status="success",
            data={"operation_configs": {"items": [partial], "platform_default_id": RAW_CONFIG_A["id"]}},
        )
        with (
            patch(
                "app.services.quote_assistant_service.get_platform_account_profile",
                new=AsyncMock(return_value=account),
            ),
            patch(
                "app.services.quote_assistant_service.quote_platform_runtime.query_operation_configs",
                new=AsyncMock(return_value=result),
            ),
        ):
            state = await refresh_platform_account_operation_configs(
                db,
                owner_user_id=3,
                account_id=11,
                raise_on_error=False,
            )
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["items"], [])
        self.assertIn("业务性质代码", state["last_error"])

    async def test_session_save_locks_and_preserves_concurrent_selection(self) -> None:
        account = self.account(
            operation_config_state({"items": [RAW_CONFIG_A], "selected_id": RAW_CONFIG_A["id"]})
        )
        account.secret_payload_ciphertext = None
        db = _SessionStoreDb()
        store = QuotePlatformSessionStore()
        store.get_state = AsyncMock(return_value=None)
        store.load_redis_mirror = AsyncMock(return_value=None)
        store.save_redis_mirror = AsyncMock()
        snapshot = AccountSessionSnapshot(
            platform_code="PICC",
            account_id=account.id,
            owner_user_id=account.owner_user_id,
            status=SESSION_STATUS_AUTHENTICATED,
        )
        saved = await store.save(
            db,
            account,
            snapshot,
            account_metadata={
                "operation_config_snapshot": operation_config_state(
                    {"items": [RAW_CONFIG_A], "selected_id": RAW_CONFIG_A["id"]}
                )
            },
        )
        self.assertTrue(saved)
        self.assertEqual(db.refresh_calls[0]["with_for_update"], True)
        self.assertEqual(
            account.credential_payload["operation_configs"]["selected_id"],
            RAW_CONFIG_B["id"],
        )

    async def test_session_save_restores_operation_config_when_refresh_returns_empty(self) -> None:
        account = self.account({})
        account.secret_payload_ciphertext = None
        db = _SessionStoreDb({})
        store = QuotePlatformSessionStore()
        store.get_state = AsyncMock(return_value=None)
        store.load_redis_mirror = AsyncMock(return_value=None)
        store.save_redis_mirror = AsyncMock()
        snapshot = AccountSessionSnapshot(
            platform_code="PICC",
            account_id=account.id,
            owner_user_id=account.owner_user_id,
            status=SESSION_STATUS_AUTHENTICATED,
        )
        incoming = operation_config_state(
            {"items": [RAW_CONFIG_A, RAW_CONFIG_B], "selected_id": RAW_CONFIG_A["id"]}
        )
        saved = await store.save(
            db,
            account,
            snapshot,
            account_metadata={"operation_config_snapshot": incoming},
        )
        self.assertTrue(saved)
        self.assertEqual(
            account.credential_payload["operation_configs"]["selected_id"],
            RAW_CONFIG_A["id"],
        )
        self.assertEqual(len(account.credential_payload["operation_configs"]["items"]), 2)

    async def test_session_save_does_not_select_over_new_multi_config_state(self) -> None:
        current = operation_config_state(
            {"items": [RAW_CONFIG_A, RAW_CONFIG_B], "selected_id": "", "status": "needs_selection"}
        )
        account = self.account(current)
        account.secret_payload_ciphertext = None
        db = _SessionStoreDb({"operation_configs": current})
        store = QuotePlatformSessionStore()
        store.get_state = AsyncMock(return_value=None)
        store.load_redis_mirror = AsyncMock(return_value=None)
        store.save_redis_mirror = AsyncMock()
        snapshot = AccountSessionSnapshot(
            platform_code="PICC",
            account_id=account.id,
            owner_user_id=account.owner_user_id,
            status=SESSION_STATUS_AUTHENTICATED,
        )
        saved = await store.save(
            db,
            account,
            snapshot,
            account_metadata={
                "operation_config_snapshot": operation_config_state(
                    {"items": [RAW_CONFIG_A], "selected_id": RAW_CONFIG_A["id"]}
                )
            },
        )
        self.assertTrue(saved)
        self.assertEqual(account.credential_payload["operation_configs"]["selected_id"], "")

    async def test_renewal_lookup_to_quote_keeps_original_frozen_route(self) -> None:
        frozen_state = operation_config_state(
            {"items": [RAW_CONFIG_A], "selected_id": RAW_CONFIG_A["id"], "status": "ready"}
        )
        account = self.account(
            operation_config_state(
                {"items": [RAW_CONFIG_B], "selected_id": RAW_CONFIG_B["id"], "status": "ready"}
            )
        )
        account.enabled = True
        account.login_status = "authenticated"
        account.quota_status = "available"
        account.auto_login = True
        account.created_at = None
        account.last_login_at = None
        account.last_check_at = None
        account.last_used_at = None
        account.last_error = None
        case = SimpleNamespace(
            id=21,
            case_no="QC-21",
            source_type="manual",
            order_id=31,
            session_id="session-21",
            platform_code="PICC",
            platform_name="人保",
            normalized_data={
                "owner_name": "测试车主",
                "vin": "LTESTVIN000000001",
                "engine_no": "ENGINE001",
                "plate_no": "赣C12345",
                "account_type_name": "油车-旧",
            },
            draft_order_data={},
            status="ready",
            current_task_id=41,
            updated_at=None,
        )
        snapshot = {
            "quote_case": {"id": case.id, "platform_code": "PICC", "platform_name": "人保"},
            "normalized_data": dict(case.normalized_data),
            "images_by_slot": {},
            "operation_config_account_id": account.id,
            "operation_config_snapshot": frozen_state,
        }
        task = SimpleNamespace(
            id=41,
            request_payload={
                "platform_account": {
                    "id": account.id,
                    "operation_configs": frozen_state,
                }
            },
            response_payload={},
            result_payload={},
            submitted_snapshot=snapshot,
            status="running",
            login_state="authenticated",
            error_detail=None,
            finished_at=None,
            updated_at=None,
        )
        renewal_result = PlatformRuntimeResult(
            status="success",
            data={
                "renewal_found": True,
                "renewal_lookup": {
                    "vehicle": {"vin": "LTESTVIN000000001"},
                    "selected": {
                        "policy_no": "PDZA202600000000000001",
                        "risk_code": "DAA",
                        "license_type": "02",
                        "end_date": "2026-10-13",
                    },
                    "candidates": [],
                },
            },
        )
        continue_quote = AsyncMock(return_value=("ok", {"status": "success"}))
        db = _FakeDb()
        with (
            patch(
                "app.services.quote_assistant_service.quote_platform_runtime.query_renewal",
                new=AsyncMock(return_value=renewal_result),
            ),
            patch(
                "app.services.quote_assistant_service._lock_quote_task",
                new=AsyncMock(return_value=task),
            ),
            patch(
                "app.services.quote_assistant_service._quote_task_was_cancelled",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.services.quote_assistant_service._quote_snapshot_material_is_current",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.services.quote_assistant_service._mark_platform_account_used",
                new=AsyncMock(),
            ),
            patch(
                "app.services.quote_assistant_service._add_event",
                new=AsyncMock(),
            ),
            patch(
                "app.services.quote_assistant_service._active_images_by_slot",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.quote_assistant_service._continue_quote_with_platform_account",
                new=continue_quote,
            ),
        ):
            await _complete_renewal_lookup_without_sms(
                db,
                case=case,
                owner_user_id=3,
                snapshot=snapshot,
                trace_id="trace-renewal",
                platform_account=account,
                login_mode="reuse_authenticated",
                task=task,
            )

        quote_snapshot = continue_quote.await_args.kwargs["snapshot"]
        self.assertEqual(quote_snapshot["operation_config_account_id"], account.id)
        self.assertEqual(
            quote_snapshot["operation_config_snapshot"]["selected_id"],
            RAW_CONFIG_A["id"],
        )
        self.assertNotEqual(
            quote_snapshot["operation_config_snapshot"]["selected_id"],
            RAW_CONFIG_B["id"],
        )

    async def test_legacy_renewal_task_with_empty_route_rebinds_account_selection(self) -> None:
        empty_state = operation_config_state({"items": [], "selected_id": ""})
        selected_state = operation_config_state(
            {"items": [RAW_CONFIG_B], "selected_id": RAW_CONFIG_B["id"], "status": "ready"}
        )
        account = self.account(selected_state)
        for key, value in {
            "enabled": True,
            "login_status": "authenticated",
            "quota_status": "available",
            "auto_login": True,
            "created_at": None,
            "last_login_at": None,
            "last_check_at": None,
            "last_used_at": None,
            "last_error": None,
        }.items():
            setattr(account, key, value)
        case = SimpleNamespace(
            id=22,
            case_no="QC-22",
            source_type="manual",
            order_id=32,
            session_id="session-22",
            platform_code="PICC",
            platform_name="人保",
            normalized_data={"vin": "LTESTVIN000000002", "account_type_name": "油车-旧"},
            draft_order_data={},
            status="ready",
            current_task_id=42,
            updated_at=None,
        )
        snapshot = {
            "quote_case": {"id": case.id, "platform_code": "PICC", "platform_name": "人保"},
            "normalized_data": dict(case.normalized_data),
            "images_by_slot": {},
            "operation_config_account_id": account.id,
            "operation_config_snapshot": empty_state,
        }
        task = SimpleNamespace(
            id=42,
            request_payload={
                "platform_account": {
                    "id": account.id,
                    "operation_configs": empty_state,
                }
            },
            response_payload={},
            result_payload={},
            submitted_snapshot=snapshot,
            status="running",
            login_state="authenticated",
            error_detail=None,
            finished_at=None,
            updated_at=None,
        )
        renewal_call = AsyncMock(
            return_value=PlatformRuntimeResult(
                status="success",
                message="没有此车辆信息或不是可续保车辆",
                data={
                    "business_status": "renewal_not_found",
                    "renewal_found": False,
                    "renewal_lookup": {"vehicle": {}, "candidates": []},
                },
            )
        )
        db = _FakeDb()
        with (
            patch(
                "app.services.quote_assistant_service.quote_platform_runtime.query_renewal",
                new=renewal_call,
            ),
            patch(
                "app.services.quote_assistant_service._lock_quote_task",
                new=AsyncMock(return_value=task),
            ),
            patch(
                "app.services.quote_assistant_service._quote_task_was_cancelled",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.services.quote_assistant_service._quote_snapshot_material_is_current",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.services.quote_assistant_service._mark_platform_account_used",
                new=AsyncMock(),
            ),
            patch(
                "app.services.quote_assistant_service._add_event",
                new=AsyncMock(),
            ),
        ):
            await _complete_renewal_lookup_without_sms(
                db,
                case=case,
                owner_user_id=3,
                snapshot=snapshot,
                trace_id="trace-legacy-renewal",
                platform_account=account,
                login_mode="sms_verified",
                task=task,
                auto_probe=True,
            )

        runtime_ctx = renewal_call.await_args.args[0]
        runtime_state = operation_config_state(runtime_ctx.payload.get("operation_config_snapshot"))
        self.assertEqual(runtime_state["selected_id"], RAW_CONFIG_B["id"])
        self.assertEqual(
            task.submitted_snapshot["operation_config_snapshot"]["selected_id"],
            RAW_CONFIG_B["id"],
        )
        self.assertEqual(
            task.request_payload["platform_account"]["operation_configs"]["selected_id"],
            RAW_CONFIG_B["id"],
        )

    async def test_refresh_api_never_returns_raw_platform_payload(self) -> None:
        state = operation_config_state(
            {"items": [{**RAW_CONFIG_A, "internalOnly": "secret"}], "selected_id": RAW_CONFIG_A["id"]}
        )
        db = _FakeDb()
        with (
            patch("app.api.v1.ai_assistant._owner_user_id_or_401", return_value=3),
            patch("app.api.v1.ai_assistant._ensure_platform_account_manage"),
            patch(
                "app.api.v1.ai_assistant._refresh_platform_account_operation_configs",
                new=AsyncMock(return_value=state),
            ),
        ):
            response = await refresh_platform_account_operation_configs_api(
                account_id=11,
                ctx=SimpleNamespace(),
                db=db,
            )

        public_state = response["data"]["operation_configs"]
        self.assertNotIn("raw", public_state["selected"])
        self.assertTrue(all("raw" not in item for item in public_state["items"]))


if __name__ == "__main__":
    unittest.main()
