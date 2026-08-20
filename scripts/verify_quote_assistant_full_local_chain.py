# -*- coding: utf-8 -*-
"""Local full-chain checks for the quote assistant without calling real PICC.

The script drives the same send_message entrypoint used by the frontend and
uses isolated assistant sessions. Platform runtime calls are monkeypatched to
deterministic local results so this never consumes real quote quota.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.constants import ROLE_SUPER_ADMIN
from app.core.db import async_session_factory, engine
from app.models.quote_assistant import QuoteCase, QuotePlatformAccountProfile, QuoteTask
from app.services import ai_assistant_service
from app.services.ai_assistant_service import db_create_session, db_delete_session, send_message
from app.services.quote_platforms.base import PlatformRuntimeResult
from app.services.quote_platforms.session_models import AccountSessionSnapshot, iso_now
from app.services.quote_platforms import runtime as quote_platform_runtime
from app.services.quote_platforms import session_manager as quote_session_module
from app.services.quote_assistant_service import (
    ACCOUNT_LOGIN_AUTHENTICATED,
    ACCOUNT_QUOTA_AVAILABLE,
    QUOTE_ACCOUNT_TYPE_OPTIONS,
    _extract_transfer_vehicle_command,
    detect_quote_config_override_signal,
    detect_quote_data_override_signal,
    detect_quote_signal,
    extract_quote_fields,
)
from app.services.quote_assistant_service import _now


OWNER_USER_ID = 1
CONTEXT = {
    "current_user_id": OWNER_USER_ID,
    "role_name": ROLE_SUPER_ADMIN,
    "team_names": [],
}


TEST_MATERIALS: Dict[str, str] = {
    "油车-新": (
        "手工资料 报价类型 油车-新 车主 张三 手机 13900000001 "
        "身份证 360402199001011234 发动机号 A126524A00000064 "
        "车架号 LSJEM4092TK037865 车型名称 测试牌燃油轿车"
    ),
    "油车-旧": (
        "手工资料 报价类型 油车-旧 车主 李四 手机 13900000002 "
        "身份证 360402199001011235 车牌 赣G12345 发动机号 R20322895 "
        "车架号 LHGCY1628T8046465 初登日期 2024-01-02 车型名称 测试牌燃油旧车"
    ),
    "新能源车-新": (
        "手工资料 报价类型 新能源车-新 车主 王五 手机 13900000003 "
        "身份证 360402199001011236 发动机号 NEA126524A0001 "
        "车架号 LGBH52E09TY123456 车型名称 测试牌纯电动轿车 纯电动轿车"
    ),
    "新能源车-旧": (
        "手工资料 报价类型 新能源车-旧 车主 赵六 手机 13900000004 "
        "身份证 360402199001011237 车牌 赣GD68721 发动机号 NER20322895 "
        "车架号 LGBH52E09SY654321 初登日期 2023-03-04 车型名称 测试牌纯电动旧车 该产品为新能源车辆"
    ),
}


class LocalRuntimePatch:
    def __init__(self) -> None:
        self.quote_calls: List[Dict[str, Any]] = []
        self.renewal_calls: List[Dict[str, Any]] = []
        self.quote_result_variant = "recorded_contract"
        self._old_quote = quote_platform_runtime.quote
        self._old_query_renewal = quote_platform_runtime.query_renewal
        self._old_query_joint_sales_plan = quote_platform_runtime.query_joint_sales_plan
        self._old_query_repair_codes = quote_platform_runtime.query_repair_codes

    async def __aenter__(self) -> "LocalRuntimePatch":
        async def fake_quote(ctx, quote_payload, db=None):
            normalized = quote_payload.get("normalized_data") if isinstance(quote_payload, dict) else {}
            request_body = quote_payload.get("request_body") if isinstance(quote_payload, dict) else {}
            default_config = quote_payload.get("platform_default_config") if isinstance(quote_payload, dict) else {}
            is_renewal = str((quote_payload or {}).get("quote_flow_type") or "") == "renewal_motor_quote"
            overrides = (normalized or {}).get("quote_field_overrides") if isinstance(normalized, dict) else {}
            renewal_seat_adjusted = (
                is_renewal
                and str((overrides or {}).get("车上人员责任险（司机）") or "") == "30000"
                and str((overrides or {}).get("车上人员责任险（乘客）") or "") == "30000"
            )
            account_type_name = (
                (default_config or {}).get("resolved_type_name")
                or getattr(ctx, "account_type_name", "")
                or (normalized or {}).get("account_type_name")
            )
            self.quote_calls.append(
                {
                    "ctx_account_type": getattr(ctx, "account_type_name", ""),
                    "account_type_name": account_type_name,
                    "normalized": normalized,
                    "request_body": request_body,
                    "quote_flow_type": (quote_payload or {}).get("quote_flow_type"),
                }
            )
            risk_score = 44 if renewal_seat_adjusted else (45 if is_renewal else 36 + len(self.quote_calls))
            premium_total = "2921.46" if renewal_seat_adjusted else ("2596.79" if is_renewal else "5760.19")
            commercial_premium = "1766.46" if renewal_seat_adjusted else ("1741.79" if is_renewal else "4810.19")
            compulsory_premium = "855.00" if is_renewal else "950.00"
            vehicle_tax = "300.00" if renewal_seat_adjusted else "0.00"
            total_without_vehicle_tax = "2621.46" if renewal_seat_adjusted else premium_total
            quote_result = {
                # This is a de-identified, recorded-result contract fixture.
                # It only exists inside this monkeypatched local test process.
                "mode": "picc_used_fuel_real",
                "status": "quoted",
                "platform_code": "PICC",
                "platform_name": "人保",
                "quote_provenance": {
                    "source": "platform_quote_response",
                    "platform_code": "PICC",
                    "response_status": 0,
                    "core_premium_evidence": [
                        {
                            "name": "commercial",
                            "source": "quote_response.data.biPremium",
                            "value": commercial_premium,
                        },
                        {
                            "name": "compulsory",
                            "source": "quote_response.data.ciPremium",
                            "value": compulsory_premium,
                        },
                    ],
                    "joint_sales_evidence": [
                        {
                            "name": "joint_sales",
                            "source": "quote_response.data.sumYelPremium",
                            "value": "398.00",
                        }
                    ],
                    "normalized_amounts": {
                        "commercial": {
                            "value": commercial_premium,
                            "source": "quote_response.data.biPremium",
                        },
                        "compulsory": {
                            "value": compulsory_premium,
                            "source": "quote_response.data.ciPremium",
                        },
                        "vehicle_tax": {
                            "value": vehicle_tax,
                            "source": "quote_response.data.sumPayTax",
                        },
                        "joint_sales": {
                            "value": "398.00",
                            "source": "quote_response.data.sumYelPremium",
                        },
                        "total_without_vehicle_tax": {
                            "value": total_without_vehicle_tax,
                            "source": "quote_response.data.sumPremium",
                        },
                        "total_with_vehicle_tax": {
                            "value": premium_total,
                            "source": "quote_response.data.totalPremium",
                        },
                    },
                },
                "trace_id": f"local-{len(self.quote_calls)}",
                "risk_score": risk_score,
                "premium_total": premium_total,
                "price_items": [
                    {"name": "商业险", "amount": commercial_premium},
                    {"name": "交强险", "amount": compulsory_premium},
                    {"name": "车船税", "amount": vehicle_tax},
                ],
                "result_card": {
                    "risk_score": risk_score,
                    "title": "报价结果",
                    "total_premium": premium_total,
                    "commercial_premium": commercial_premium,
                    "compulsory_premium": compulsory_premium,
                    "vehicle_tax": vehicle_tax,
                    "car_owner": (normalized or {}).get("owner_name") or "本地测试",
                    "license_no": (normalized or {}).get("plate_no") or "新车",
                    "vin": (normalized or {}).get("vin") or "",
                    "commercial_start_datetime": "2026-08-15 00:00",
                    "compulsory_start_datetime": "2026-08-15 00:00",
                    "joint_sales_premium": "398.00",
                    "joint_sales_amount": "200000.00",
                    "coverage_items": [
                        {"name": "机动车损失保险", "amount": "32000"},
                        {"name": "机动车第三者责任保险", "amount": "300万"},
                    ],
                },
                "joint_sales_source": "platform_quote_response",
                "request_body": request_body,
            }
            if is_renewal:
                quote_result["quotation_no"] = "FDAA202636040008979424"
                quote_result["renewal"] = {
                    "old_policy_no_bi": "PDAA202536040000208495",
                    "old_policy_no_ci": "PDZA202536040000299779",
                }
            if self.quote_result_variant == "untrusted_marker":
                quote_result["mode"] = "stub"

            return PlatformRuntimeResult(
                status="success",
                message="人保报价完成",
                data={
                    "business_status": "quoted",
                    "quote_result": quote_result,
                    "request_body": request_body,
                    "platform_usage": {"queried_today": len(self.quote_calls)},
                },
            )

        async def fake_query_renewal(ctx, quote_payload, db=None):
            normalized = quote_payload.get("normalized_data") if isinstance(quote_payload, dict) else {}
            vehicle = {
                "plate_no": (normalized or {}).get("plate_no") or "赣G12345",
                "engine_no": (normalized or {}).get("engine_no") or "R20322895",
                "vin": (normalized or {}).get("vin") or "LHGCY1628T8046465",
                "license_type": (normalized or {}).get("license_type") or "02",
                "license_color_code": (normalized or {}).get("license_color_code") or "01",
            }
            self.renewal_calls.append({"normalized": normalized, "vehicle": vehicle})
            return PlatformRuntimeResult(
                status="success",
                message="已查询到可续保保单",
                data={
                    "business_status": "renewal_found",
                    "renewal_found": True,
                    "renewal_lookup": {
                        "vehicle": vehicle,
                        "candidates": [
                            {
                                "policy_no": "LOCALRENEW001",
                                "policy_no_encode": "ENC-LOCALRENEW001",
                                "risk_code": "DZA",
                                "license_no": vehicle["plate_no"],
                                "license_type": vehicle["license_type"],
                                "engine_no": vehicle["engine_no"],
                                "vin": vehicle["vin"],
                                "insured_name": (normalized or {}).get("owner_name") or "本地测试",
                                "end_date": "2026-09-18 00:00:00",
                                "renewal_or_copy_flag": "1",
                            }
                        ],
                    },
                },
            )

        async def fake_joint_sales(ctx, quote_payload, db=None):
            return PlatformRuntimeResult(
                status="success",
                message="本地模拟途家安顺保额查询成功",
                data={
                    "business_status": "success",
                    "premium": "598",
                    "amount": "200000",
                    "joint_sales_plan": {
                        "success": True,
                        "premium": "598",
                        "amount": "200000",
                        "candidate_count": 2,
                        "match_count": 1,
                        "selected_plan": {"premium": "598", "amount": "200000"},
                        "selection_rule": "same_premium_highest_amount",
                    },
                },
            )

        async def fake_repair_codes(ctx, quote_payload, db=None):
            return PlatformRuntimeResult(
                status="success",
                message="本地模拟送修码查询成功",
                data={
                    "business_status": "success",
                    "rows": [
                        {
                            "flag": "1",
                            "monopolyCode": "3604731000027",
                            "monopolyName": "濂溪区金鑫汽车修理厂",
                            "carchecker": "陈宛杰",
                        }
                    ],
                },
            )

        quote_platform_runtime.quote = fake_quote
        quote_platform_runtime.query_renewal = fake_query_renewal
        quote_platform_runtime.query_joint_sales_plan = fake_joint_sales
        quote_platform_runtime.query_repair_codes = fake_repair_codes
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        quote_platform_runtime.quote = self._old_quote
        quote_platform_runtime.query_renewal = self._old_query_renewal
        quote_platform_runtime.query_joint_sales_plan = self._old_query_joint_sales_plan
        quote_platform_runtime.query_repair_codes = self._old_query_repair_codes


async def _ensure_local_pccc_account(db) -> int:
    account = (
        await db.execute(
            select(QuotePlatformAccountProfile)
            .where(
                QuotePlatformAccountProfile.platform_code == "PICC",
                QuotePlatformAccountProfile.enabled == True,  # noqa: E712
            )
            .order_by(QuotePlatformAccountProfile.id.asc())
            .limit(1)
        )
    ).scalars().first()
    if account is None:
        raise RuntimeError("本地没有启用的人保账号，无法验证账号选择链路")
    account.login_status = ACCOUNT_LOGIN_AUTHENTICATED
    account.quota_status = ACCOUNT_QUOTA_AVAILABLE
    account.last_error = None
    account.updated_at = _now()
    snapshot = AccountSessionSnapshot(
        platform_code="PICC",
        account_id=int(account.id),
        owner_user_id=int(account.owner_user_id or OWNER_USER_ID),
        status="authenticated",
        session_version=1,
        session_generation=f"local-test-{uuid.uuid4().hex[:12]}",
        last_login_at=iso_now(),
        last_authenticated_at=iso_now(),
        last_business_at=iso_now(),
        runtime_meta={"local_full_chain_test": True},
    )
    await quote_session_module.session_manager.store.save(db, account, snapshot)
    await db.flush()
    return int(account.id)


async def _send(db, *, session_id: str, message: str, context_extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ctx = dict(CONTEXT)
    if context_extra:
        ctx.update(context_extra)
    result = await send_message(
        owner_user_id=str(OWNER_USER_ID),
        session_id=session_id,
        message=message,
        context=ctx,
        client_msg_id=f"local_full_chain_{uuid.uuid4().hex}",
        db=db,
    )
    await db.commit()
    return result


def _payload(result: Dict[str, Any]) -> Dict[str, Any]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    return payload


def _result_status(result: Dict[str, Any]) -> str:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return str(data.get("result_status") or "")


async def _cleanup_sessions(db, session_ids: List[str]) -> None:
    for session_id in session_ids:
        await db_delete_session(db, owner_user_id=OWNER_USER_ID, session_id=session_id)
        await db.execute(
            delete(QuoteCase).where(
                QuoteCase.owner_user_id == OWNER_USER_ID,
                QuoteCase.session_id == session_id,
            )
        )
    await db.commit()


async def main() -> None:
    session_ids: List[str] = []
    try:
        async with async_session_factory() as db:
            await _ensure_local_pccc_account(db)
            await db.commit()

            async with LocalRuntimePatch() as runtime_patch:
                for account_type in QUOTE_ACCOUNT_TYPE_OPTIONS:
                    session = await db_create_session(
                        db,
                        owner_user_id=OWNER_USER_ID,
                        title=f"Codex 本地全链路 {account_type}",
                    )
                    session_id = str(session["session_id"])
                    session_ids.append(session_id)
                    await db.commit()

                    material_result = await _send(db, session_id=session_id, message=TEST_MATERIALS[account_type])
                    assert material_result.get("ui_visible") is False, (account_type, material_result)

                    quote_result = await _send(db, session_id=session_id, message="人保报价")
                    reply = str(quote_result.get("reply") or "")
                    payload = _payload(quote_result)
                    quote_payload = payload.get("quote_result") if isinstance(payload.get("quote_result"), dict) else {}
                    assert quote_result.get("intent") == "quote", (account_type, quote_result)
                    assert _result_status(quote_result) == "success", (account_type, quote_result)
                    assert "人保风险水平：" in reply and "分" in reply, (account_type, reply)
                    assert isinstance(quote_payload.get("result_image"), dict), (
                        account_type,
                        quote_payload,
                    )
                    assert quote_payload["result_image"].get("provider") == "bos", quote_payload
                    assert quote_payload["result_image"].get("image_url"), quote_payload

                    call = runtime_patch.quote_calls[-1]
                    normalized = call["normalized"]
                    assert normalized.get("account_type_name") == account_type, (account_type, normalized)
                    expected_license_type = "52" if "新能源" in account_type else "02"
                    actual_license_type = (
                        normalized.get("license_type")
                        or (normalized.get("license_type_decision") or {}).get("license_type")
                        or ""
                    )
                    assert actual_license_type == expected_license_type, (account_type, normalized)
                    request_body_text = str(call["request_body"])
                    assert expected_license_type in request_body_text, (account_type, call["request_body"])

                # Product-mode commands should reuse the latest material and alter exclusions.
                session = await db_create_session(db, owner_user_id=OWNER_USER_ID, title="Codex 本地命令链路")
                command_session_id = str(session["session_id"])
                session_ids.append(command_session_id)
                await db.commit()
                await _send(db, session_id=command_session_id, message=TEST_MATERIALS["油车-旧"])
                for command, excluded in [
                    ("人保交三", "机动车损失保险"),
                    ("人保单商", "交强"),
                    ("全保", ""),
                ]:
                    result = await _send(db, session_id=command_session_id, message=command)
                    assert _result_status(result) == "success", (command, result)
                    normalized = runtime_patch.quote_calls[-1]["normalized"]
                    exclusions = normalized.get("quote_product_exclusions") or []
                    if excluded:
                        assert excluded in exclusions, (command, exclusions)
                    else:
                        assert not exclusions, (command, exclusions)

                product_adjustment_checks = [
                    ("人保单商", "司乘改3万", "交强", {"车上人员责任险（司机）": "30000", "车上人员责任险（乘客）": "30000"}),
                    ("人保交三", "三者200万", "机动车损失保险", {"第三者责任险": "2000000", "医保外医疗费用责任险（第三者责任险）": "2000000"}),
                ]
                for command, adjustment, excluded, expected_overrides in product_adjustment_checks:
                    result = await _send(db, session_id=command_session_id, message=command)
                    assert _result_status(result) == "success", (command, result)
                    before_adjustment_quote_count = len(runtime_patch.quote_calls)
                    adjustment_result = await _send(db, session_id=command_session_id, message=adjustment)
                    assert _result_status(adjustment_result) == "success", (command, adjustment, adjustment_result)
                    assert len(runtime_patch.quote_calls) == before_adjustment_quote_count + 1, (command, adjustment_result)
                    normalized = runtime_patch.quote_calls[-1]["normalized"]
                    exclusions = normalized.get("quote_product_exclusions") or []
                    assert excluded in exclusions, (command, adjustment, exclusions)
                    overrides = normalized.get("quote_field_overrides") or {}
                    for label, expected_value in expected_overrides.items():
                        assert str(overrides.get(label)) == expected_value, (command, adjustment, overrides)

                await _send(db, session_id=command_session_id, message="人保单商")
                transfer_mode_before_adjustment = len(runtime_patch.quote_calls)
                transfer_mode_adjustment = await _send(db, session_id=command_session_id, message="非过户车")
                assert _result_status(transfer_mode_adjustment) == "success", transfer_mode_adjustment
                assert len(runtime_patch.quote_calls) == transfer_mode_before_adjustment + 1, transfer_mode_adjustment
                transfer_mode_normalized = runtime_patch.quote_calls[-1]["normalized"]
                assert "交强" in (transfer_mode_normalized.get("quote_product_exclusions") or []), transfer_mode_normalized
                assert transfer_mode_normalized.get("is_transfer_vehicle") is False, transfer_mode_normalized
                assert transfer_mode_normalized.get("transfer_vehicle_override") == "not_transfer", transfer_mode_normalized
                transfer_followup = await _send(db, session_id=command_session_id, message="车损改3万")
                assert _result_status(transfer_followup) == "success", transfer_followup
                transfer_followup_normalized = runtime_patch.quote_calls[-1]["normalized"]
                assert "交强" in (transfer_followup_normalized.get("quote_product_exclusions") or []), transfer_followup_normalized
                assert transfer_followup_normalized.get("is_transfer_vehicle") is False, transfer_followup_normalized
                assert transfer_followup_normalized.get("transfer_vehicle_override") == "not_transfer", transfer_followup_normalized

                full_reset = await _send(db, session_id=command_session_id, message="全保")
                assert _result_status(full_reset) == "success", full_reset
                full_reset_exclusions = runtime_patch.quote_calls[-1]["normalized"].get("quote_product_exclusions") or []
                assert not full_reset_exclusions, full_reset_exclusions

                quote_count_before_joint_sales = len(runtime_patch.quote_calls)
                joint_sales_result = await _send(db, session_id=command_session_id, message="非车598")
                assert _result_status(joint_sales_result) == "success", joint_sales_result
                assert len(runtime_patch.quote_calls) == quote_count_before_joint_sales, joint_sales_result
                joint_sales_payload = _payload(joint_sales_result).get("quote_result") or {}
                joint_sales_card = joint_sales_payload.get("result_card") or {}
                assert str(joint_sales_card.get("joint_sales_premium") or "").startswith("598"), joint_sales_card
                assert str(joint_sales_card.get("joint_sales_amount") or "").startswith("200000"), joint_sales_card

                transfer_result = await _send(db, session_id=command_session_id, message="非过户车")
                assert _result_status(transfer_result) == "success", transfer_result
                transfer_normalized = runtime_patch.quote_calls[-1]["normalized"]
                assert transfer_normalized.get("is_transfer_vehicle") is False, transfer_normalized
                assert transfer_normalized.get("transfer_vehicle_override") == "not_transfer", transfer_normalized

                repair_result = await _send(
                    db,
                    session_id=command_session_id,
                    message="送修码3604731000027-濂溪区金鑫汽车修理厂",
                )
                assert _result_status(repair_result) == "success", repair_result
                repair_normalized = runtime_patch.quote_calls[-1]["normalized"]
                repair_overrides = repair_normalized.get("quote_field_overrides") or {}
                assert str(repair_overrides.get("送修码启用")) == "1", repair_overrides
                assert str(repair_overrides.get("送修码")) == "3604731000027", repair_overrides

                # Renewal branch must continue from lookup to a real quote task.
                session = await db_create_session(db, owner_user_id=OWNER_USER_ID, title="Codex 本地续保链路")
                renewal_session_id = str(session["session_id"])
                session_ids.append(renewal_session_id)
                await db.commit()
                await _send(db, session_id=renewal_session_id, message=TEST_MATERIALS["油车-旧"])
                renewal_result = await _send(db, session_id=renewal_session_id, message="人保续保")
                renewal_reply = str(renewal_result.get("reply") or "")
                assert "人保风险水平：45 分" in renewal_reply, renewal_result
                assert _result_status(renewal_result) == "success", renewal_result
                assert len(runtime_patch.renewal_calls) >= 1, renewal_result
                renewal_quote_call = runtime_patch.quote_calls[-1]
                assert renewal_quote_call.get("quote_flow_type") == "renewal_motor_quote", renewal_quote_call
                renewal_quote_payload = _payload(renewal_result).get("quote_result") or {}
                assert str(renewal_quote_payload.get("premium_total")) == "2596.79", renewal_quote_payload

                # A later adjustment must keep the renewal quote flow and
                # reuse its selected policy instead of falling back to normal used-car quote.
                renewal_calls_before_adjustment = len(runtime_patch.renewal_calls)
                renewal_quote_calls_before_adjustment = len(runtime_patch.quote_calls)
                seat_adjustment = await _send(db, session_id=renewal_session_id, message="司乘改3万")
                assert _result_status(seat_adjustment) == "success", seat_adjustment
                assert len(runtime_patch.renewal_calls) == renewal_calls_before_adjustment, seat_adjustment
                assert len(runtime_patch.quote_calls) == renewal_quote_calls_before_adjustment + 1, seat_adjustment
                adjusted_call = runtime_patch.quote_calls[-1]
                assert adjusted_call.get("quote_flow_type") == "renewal_motor_quote", adjusted_call
                adjusted_overrides = adjusted_call.get("normalized", {}).get("quote_field_overrides") or {}
                assert str(adjusted_overrides.get("车上人员责任险（司机）")) == "30000", adjusted_overrides
                assert str(adjusted_overrides.get("车上人员责任险（乘客）")) == "30000", adjusted_overrides
                adjusted_payload = _payload(seat_adjustment).get("quote_result") or {}
                adjusted_card = adjusted_payload.get("result_card") or {}
                assert adjusted_payload.get("risk_score") == 44, adjusted_payload
                assert str(adjusted_card.get("commercial_premium")) == "1766.46", adjusted_card
                assert str(adjusted_card.get("compulsory_premium")) == "855.00", adjusted_card
                assert str(adjusted_card.get("vehicle_tax")) == "300.00", adjusted_card
                assert str(adjusted_payload.get("premium_total")) == "2921.46", adjusted_payload

                session = await db_create_session(db, owner_user_id=OWNER_USER_ID, title="Codex 本地续保单商调整链路")
                renewal_mode_session_id = str(session["session_id"])
                session_ids.append(renewal_mode_session_id)
                await db.commit()
                await _send(db, session_id=renewal_mode_session_id, message=TEST_MATERIALS["油车-旧"])
                renewal_mode_result = await _send(db, session_id=renewal_mode_session_id, message="人保续保单商")
                assert _result_status(renewal_mode_result) == "success", renewal_mode_result
                renewal_mode_call = runtime_patch.quote_calls[-1]
                assert renewal_mode_call.get("quote_flow_type") == "renewal_motor_quote", renewal_mode_call
                renewal_mode_exclusions = renewal_mode_call.get("normalized", {}).get("quote_product_exclusions") or []
                assert "交强" in renewal_mode_exclusions, renewal_mode_exclusions

                renewal_mode_lookup_count = len(runtime_patch.renewal_calls)
                renewal_mode_quote_count = len(runtime_patch.quote_calls)
                renewal_mode_adjustment = await _send(db, session_id=renewal_mode_session_id, message="三者200万")
                assert _result_status(renewal_mode_adjustment) == "success", renewal_mode_adjustment
                assert len(runtime_patch.renewal_calls) == renewal_mode_lookup_count, renewal_mode_adjustment
                assert len(runtime_patch.quote_calls) == renewal_mode_quote_count + 1, renewal_mode_adjustment
                renewal_mode_adjusted_call = runtime_patch.quote_calls[-1]
                assert renewal_mode_adjusted_call.get("quote_flow_type") == "renewal_motor_quote", renewal_mode_adjusted_call
                renewal_mode_adjusted = renewal_mode_adjusted_call.get("normalized", {})
                renewal_mode_adjusted_exclusions = renewal_mode_adjusted.get("quote_product_exclusions") or []
                assert "交强" in renewal_mode_adjusted_exclusions, renewal_mode_adjusted_exclusions
                renewal_mode_overrides = renewal_mode_adjusted.get("quote_field_overrides") or {}
                assert str(renewal_mode_overrides.get("第三者责任险")) == "2000000", renewal_mode_overrides
                assert str(renewal_mode_overrides.get("医保外医疗费用责任险（第三者责任险）")) == "2000000", renewal_mode_overrides

                # Missing material must be visible only after a quote command.
                session = await db_create_session(db, owner_user_id=OWNER_USER_ID, title="Codex 本地缺资料链路")
                missing_session_id = str(session["session_id"])
                session_ids.append(missing_session_id)
                await db.commit()
                silent_material = await _send(db, session_id=missing_session_id, message="车主 钱七")
                assert silent_material.get("ui_visible") is False, silent_material
                missing_quote = await _send(db, session_id=missing_session_id, message="人保报价")
                assert _result_status(missing_quote) == "need_more_info", missing_quote
                assert "缺少字段" in str(missing_quote.get("reply") or ""), missing_quote

                # Manual/supplement commands must be silent metadata forms.
                manual = await _send(db, session_id=missing_session_id, message="手工")
                assert manual.get("ui_visible") is False, manual
                assert _payload(manual).get("quote_material_form", {}).get("mode") == "manual", manual
                supplement = await _send(db, session_id=missing_session_id, message="补资料")
                assert supplement.get("ui_visible") is False, supplement
                assert _payload(supplement).get("quote_material_form", {}).get("mode") == "supplement", supplement

                unsupported = await _send(db, session_id=missing_session_id, message="太平洋报价")
                assert "暂未增加" in str(unsupported.get("reply") or ""), unsupported

                # An untrusted runtime payload must fail closed: no result card,
                # no image, and no successful quote count may be persisted.
                runtime_patch.quote_result_variant = "untrusted_marker"
                session = await db_create_session(
                    db,
                    owner_user_id=OWNER_USER_ID,
                    title="Codex 本地假结果拦截",
                )
                invalid_result_session_id = str(session["session_id"])
                session_ids.append(invalid_result_session_id)
                await db.commit()
                await _send(
                    db,
                    session_id=invalid_result_session_id,
                    message=TEST_MATERIALS["油车-旧"],
                )
                rejected = await _send(
                    db,
                    session_id=invalid_result_session_id,
                    message="人保报价",
                )
                assert _result_status(rejected) == "failed", rejected
                assert "占位或模拟报价结果" in str(rejected.get("reply") or ""), rejected
                rejected_payload = _payload(rejected)
                assert not (rejected_payload.get("quote_result") or {}).get("result_image"), rejected_payload
                rejected_case = (
                    await db.execute(
                        select(QuoteCase)
                        .where(
                            QuoteCase.owner_user_id == OWNER_USER_ID,
                            QuoteCase.session_id == invalid_result_session_id,
                        )
                        .order_by(QuoteCase.id.desc())
                        .limit(1)
                    )
                ).scalars().first()
                assert rejected_case is not None, rejected
                assert int(rejected_case.quote_count or 0) == 0, rejected_case.quote_count
                rejected_task = (
                    await db.execute(
                        select(QuoteTask)
                        .where(QuoteTask.quote_case_id == rejected_case.id)
                        .order_by(QuoteTask.id.desc())
                        .limit(1)
                    )
                ).scalars().first()
                assert rejected_task is not None, rejected
                assert str(rejected_task.status) == "failed", rejected_task.status
                assert rejected_task.result_payload == {}, rejected_task.result_payload
                runtime_patch.quote_result_variant = "recorded_contract"

                parser_checks = {
                    "人保报价": True,
                    "全保": True,
                    "人保交三": True,
                    "人保单商": True,
                    "续保": True,
                    "人保续保单商": True,
                    "太平洋报价": True,
                }
                for command, expected in parser_checks.items():
                    parsed = detect_quote_signal(command)
                    assert bool(parsed.get("is_quote")) is expected, (command, parsed)
                assert detect_quote_config_override_signal("非车0").get("is_override") is True
                assert detect_quote_config_override_signal("车损改3万").get("is_override") is True
                assert detect_quote_config_override_signal("司乘3万").get("is_override") is True
                assert detect_quote_data_override_signal("初登日期2024-01-01").get("is_override") is True
                assert _extract_transfer_vehicle_command("非过户车").get("is_transfer_vehicle") is False
                assert extract_quote_fields("车主 张三 手机 13900000001 车架号 LSJEM4O92TKO37865").get("vin") == "LSJEM4092TK037865"

                print("PASS local full chain: four account types, product modes, renewal lookup, missing-material, forms, unsupported platform, and command parsing")
                print(f"quote_calls={len(runtime_patch.quote_calls)} renewal_calls={len(runtime_patch.renewal_calls)}")
    finally:
        async with async_session_factory() as cleanup_db:
            await _cleanup_sessions(cleanup_db, session_ids)
            print("CLEANUP local full-chain sessions deleted")
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
