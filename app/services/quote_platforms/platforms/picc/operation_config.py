# encoding: utf-8
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping


OPERATE_CONFIG_LIST_PATH = "/khyx/newFront/qt/myinfo/operator/getOperateConfigByUserCode.do"
DEFAULT_OPERATOR_PATH = "/khyx/newFront/qth/myinfo/operator/getDefOperator.do"
SELECTED_OPERATOR_PATH = "/khyx/newFront/qth/myinfo/operator/getSelectedOperator.do"

OPERATION_CONFIG_CREDENTIAL_KEY = "operation_configs"

_OPERATION_CONFIG_ID_RE = re.compile(r"^QT[A-Z0-9_-]{4,126}$", re.IGNORECASE)
_EXPLICIT_OPERATION_CONFIG_ID_KEYS = (
    "operateConfigId",
    "operateConfigID",
    "operationConfigId",
    "selectedOperateConfigId",
)
_OPERATION_CONFIG_FEATURE_KEYS = frozenset(
    {
        "operateConfigName",
        "configName",
        "comCode",
        "comID",
        "comId",
        "makecom",
        "makeCom",
        "comCName",
        "makecomName",
        "businessNature",
        "businesNature",
        "agentCode",
        "handlerCode",
        "handler1Code",
        "operateCode",
    }
)

# These values are supplied by the authenticated PICC account. They must not
# be editable or recoverable from the product/default-parameter configuration.
PICC_ACCOUNT_MANAGED_DEFAULT_FIELD_NAMES = frozenset(
    {
        "归属机构代码",
        "归属机构名称",
        "机构代码",
        "机构名称",
        "操作机构代码",
        "操作机构名称",
        "出单机构代码",
        "操作配置ID",
        "操作配置名称",
        "车型配置ID",
        "业务性质代码",
        "业务性质名称",
        "代理机构代码",
        "代理机构名称",
        "操作员代码",
        "操作员名称",
        "经办人代码",
        "经办人名称",
        "comCode",
        "comID",
        "comId",
        "comCName",
        "makecom",
        "makeCom",
        "makecomName",
        "comName",
        "organizationName",
        "opconfComCode",
        "opconf_makecomCode",
        "operateConfigId",
        "operateConfigID",
        "operationConfigId",
        "operateConfigName",
        "configName",
        "selectedOperateConfigId",
        "selectedOperateConfig",
        "businesNature",
        "businesNatureName",
        "businessNature",
        "businessNatureName",
        "agentCode",
        "agentName",
        "agentType",
        "operateCode",
        "operateName",
        "handlerCode",
        "handlerName",
        "handler1Code",
        "handler1Name",
        "prpCmain.comCode",
        "prpCmain.businesNature",
        "prpCmain.agentCode",
        "prpCmain.handlerCode",
        "prpCmain.handler1Code",
        "prpCmain.operateCode",
    }
)


def _text(value: Any) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return ""
    return str(value).strip()


def _first_text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    return ""


def _operation_config_id(row: Mapping[str, Any]) -> str:
    return _first_text(row, "id", *_EXPLICIT_OPERATION_CONFIG_ID_KEYS)


def _looks_like_operation_config(row: Mapping[str, Any]) -> bool:
    config_id = _operation_config_id(row)
    if not config_id:
        return False
    if _OPERATION_CONFIG_ID_RE.fullmatch(config_id):
        return True
    # Some deployments may use a non-QT identifier. Accept those only when
    # the response explicitly calls it an operation-config ID and also carries
    # routing fields; a generic user/organization/page object with an `id`
    # must never become selectable.
    has_explicit_id = any(_text(row.get(key)) for key in _EXPLICIT_OPERATION_CONFIG_ID_KEYS)
    feature_count = sum(1 for key in _OPERATION_CONFIG_FEATURE_KEYS if _text(row.get(key)))
    return has_explicit_id and feature_count >= 2


def _safe_platform_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:128]:
            key = _text(raw_key)[:128]
            if not key:
                continue
            safe_value = _safe_platform_value(raw_value, depth=depth + 1)
            if safe_value is not None:
                out[key] = safe_value
        return out
    if isinstance(value, (list, tuple)):
        return [_safe_platform_value(item, depth=depth + 1) for item in list(value)[:128]]
    return _text(value)[:4096]


def normalize_operation_config(row: Mapping[str, Any]) -> Dict[str, Any]:
    source = dict(row or {})
    raw_source = source.get("raw")
    if isinstance(raw_source, Mapping):
        source = {**dict(raw_source), **source}
    if not _looks_like_operation_config(source):
        return {}
    config_id = _operation_config_id(source)
    com_code = _first_text(source, "com_code", "comCode", "comID", "comId", "makecom")
    makecom_code = _first_text(source, "makecom_code", "makecom", "makeCom", "comCode", "comID", "comId")
    return {
        "id": config_id,
        "name": _first_text(source, "name", "operateConfigName", "configName", "operateName") or config_id,
        "valid_status": _first_text(source, "valid_status", "validStatus", "status"),
        "risk_code": _first_text(source, "risk_code", "riskCode"),
        "com_code": com_code,
        "makecom_code": makecom_code,
        "organization_name": _first_text(
            source,
            "organization_name",
            "comCName",
            "makecomName",
            "comName",
            "organizationName",
        ),
        "operate_code": _first_text(source, "operate_code", "operateCode", "userCode"),
        "operate_name": _first_text(source, "operate_name", "operateName", "userName"),
        "handler_code": _first_text(source, "handler_code", "handlerCode", "handler1Code", "operateCode", "userCode"),
        "handler_name": _first_text(source, "handler_name", "handlerName", "handler1Name", "operateName", "userName"),
        "handler1_code": _first_text(source, "handler1_code", "handler1Code", "handlerCode", "operateCode", "userCode"),
        "handler1_name": _first_text(source, "handler1_name", "handler1Name", "handlerName", "operateName", "userName"),
        "agent_type": _first_text(source, "agent_type", "agentType"),
        "agent_code": _first_text(source, "agent_code", "agentCode"),
        "agent_name": _first_text(source, "agent_name", "agentName"),
        "business_nature": _first_text(source, "business_nature", "businessNature", "businesNature"),
        "business_nature_name": _first_text(source, "business_nature_name", "businessNatureName", "businesNatureName"),
        "monopoly_code": _first_text(source, "monopoly_code", "monopolyCode"),
        "monopoly_name": _first_text(source, "monopoly_name", "monopolyName"),
        "raw": _safe_platform_value(raw_source if isinstance(raw_source, Mapping) else source),
    }


def _walk_candidate_rows(value: Any, *, depth: int = 0) -> Iterable[Mapping[str, Any]]:
    if depth > 6:
        return
    if isinstance(value, Mapping):
        if _looks_like_operation_config(value):
            yield value
        for nested in value.values():
            yield from _walk_candidate_rows(nested, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_candidate_rows(nested, depth=depth + 1)


def operation_config_items_from_responses(*responses: Any) -> list[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for response in responses:
        for row in _walk_candidate_rows(response):
            normalized = normalize_operation_config(row)
            config_id = _text(normalized.get("id"))
            if not config_id:
                continue
            previous = by_id.get(config_id)
            if previous:
                merged_raw = {**dict(previous.get("raw") or {}), **dict(normalized.get("raw") or {})}
                normalized = normalize_operation_config(merged_raw)
            by_id[config_id] = normalized
    return sorted(by_id.values(), key=lambda item: (_text(item.get("name")), _text(item.get("id"))))


def operation_config_missing_quote_fields(config: Any) -> list[str]:
    selected = normalize_operation_config(config) if isinstance(config, Mapping) else {}
    missing: list[str] = []
    if not _text(selected.get("id")):
        missing.append("操作配置ID")
    if not _text(selected.get("com_code") or selected.get("makecom_code")):
        missing.append("机构码")
    if not _text(selected.get("business_nature")):
        missing.append("业务性质代码")
    return missing


def operation_config_id_from_response(value: Any, *, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(value, Mapping):
        for key in (*_EXPLICIT_OPERATION_CONFIG_ID_KEYS, "defaultOperateConfigId", "id"):
            candidate = _text(value.get(key))
            if _OPERATION_CONFIG_ID_RE.fullmatch(candidate):
                return candidate
        for nested in value.values():
            candidate = operation_config_id_from_response(nested, depth=depth + 1)
            if candidate:
                return candidate
    elif isinstance(value, (list, tuple)):
        for nested in value[:500]:
            candidate = operation_config_id_from_response(nested, depth=depth + 1)
            if candidate:
                return candidate
    else:
        candidate = _text(value)
        if _OPERATION_CONFIG_ID_RE.fullmatch(candidate):
            return candidate
    return ""


def operation_config_state(value: Any) -> Dict[str, Any]:
    source = dict(value) if isinstance(value, Mapping) else {}
    items = operation_config_items_from_responses(source.get("items") or [])
    selected_id = _text(source.get("selected_id"))
    selected = next((item for item in items if item.get("id") == selected_id), None)
    if selected is None and len(items) == 1:
        selected = items[0]
        selected_id = _text(selected.get("id"))
    return {
        "supported": bool(source.get("supported", True)),
        "status": _text(source.get("status")) or ("ready" if items else "empty"),
        "items": items,
        "selected_id": selected_id if selected else "",
        "selected": selected or {},
        "refreshed_at": _text(source.get("refreshed_at")),
        "last_error": _text(source.get("last_error"))[:500],
        "last_error_at": _text(source.get("last_error_at")),
    }


def public_operation_config_state(value: Any) -> Dict[str, Any]:
    state = operation_config_state(value)

    def without_raw(item: Any) -> Dict[str, Any]:
        source = dict(item) if isinstance(item, Mapping) else {}
        return {key: val for key, val in source.items() if key != "raw"}

    items = [without_raw(item) for item in state.get("items") or []]
    selected_id = _text(state.get("selected_id"))
    selected = next((item for item in items if _text(item.get("id")) == selected_id), {})
    return {
        **state,
        "items": items,
        "selected": selected,
    }


def selected_operation_config_from_context_payload(payload: Any) -> Dict[str, Any]:
    source = dict(payload) if isinstance(payload, Mapping) else {}
    snapshot = source.get("operation_config_snapshot")
    if not isinstance(snapshot, Mapping):
        credential = source.get("credential_payload")
        credential = dict(credential) if isinstance(credential, Mapping) else {}
        snapshot = credential.get(OPERATION_CONFIG_CREDENTIAL_KEY)
    state = operation_config_state(snapshot)
    return dict(state.get("selected") or {})


def operation_config_quote_fields(config: Any) -> Dict[str, Any]:
    selected = normalize_operation_config(config) if isinstance(config, Mapping) else {}
    if not selected:
        return {}
    com_code = _text(selected.get("com_code") or selected.get("makecom_code"))
    makecom_code = _text(selected.get("makecom_code") or selected.get("com_code"))
    business_nature = _text(selected.get("business_nature"))
    business_nature_name = _text(selected.get("business_nature_name"))
    fields: Dict[str, Any] = {
        "归属机构代码": com_code,
        "操作机构代码": makecom_code,
        "操作配置ID": _text(selected.get("id")),
        "业务性质代码": business_nature,
        "业务性质名称": business_nature_name,
        "代理机构代码": _text(selected.get("agent_code")),
        "代理机构名称": _text(selected.get("agent_name")),
        "操作员代码": _text(selected.get("operate_code")),
        "操作员名称": _text(selected.get("operate_name")),
        "经办人代码": _text(selected.get("handler_code")),
        "经办人名称": _text(selected.get("handler_name")),
        "handler1Code": _text(selected.get("handler1_code")),
        "handler1Name": _text(selected.get("handler1_name")),
        "agentType": _text(selected.get("agent_type")),
    }
    return {key: value for key, value in fields.items() if _text(value)}


def strip_account_managed_default_fields(values: Any) -> Dict[str, Any]:
    source = dict(values) if isinstance(values, Mapping) else {}
    return {
        key: value
        for key, value in source.items()
        if _text(key) not in PICC_ACCOUNT_MANAGED_DEFAULT_FIELD_NAMES
    }


def strip_account_managed_deleted_fields(values: Any) -> list[str]:
    source = values if isinstance(values, (list, tuple, set)) else []
    out: list[str] = []
    for value in source:
        name = _text(value)
        if name and name not in PICC_ACCOUNT_MANAGED_DEFAULT_FIELD_NAMES and name not in out:
            out.append(name)
    return out
