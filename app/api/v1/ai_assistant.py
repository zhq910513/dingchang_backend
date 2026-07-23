# app/api/v1/ai_assistant.py
# encoding: utf-8
from __future__ import annotations

"""
报价助手 API（Schema 已冻结）：
- app.schemas.ai_assistant.AiChatIn / AiChatOut
- app.schemas.ai_assistant.AiSessionListOut / AiSessionItem

注意：
- 保留 /sessions /chat /health
- /sessions/{id}/history 作为调试辅助，不强制 schema（返回 dict）
"""

import hashlib
import logging
import os
from copy import deepcopy
from typing import Any, Dict, List, Tuple

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserContext, get_current_user_with_role_and_teams
from app.core.access_control import (
    require_ai_assistant_access,
    require_quote_assistant_quote_use_access,
    require_quote_default_config_manage_access,
    require_quote_platform_account_manage_access,
)
from app.core.db import get_db
from app.schemas.ai_assistant import (
    AiChatIn,
    AiChatOut,
    AiSessionItem,
    AiSessionListOut,
    AiCreateSessionIn,
    AiDeleteSessionOut,
    AiPlatformAccountLoginChallengeIn,
    AiPlatformAccountProfileIn,
    AiPlatformAccountTypeIn,
    AiPlatformDefaultConfigIn,
    AiRecallSessionImageIn,
)
from app.services.ai_assistant_service import (
    db_create_session as _create_session,
    db_delete_session as _delete_session,
    db_list_messages as _get_session_messages,
    db_list_sessions as _list_sessions,
    db_recall_session_images as _recall_session_images,
    send_message as _send_message,  # async
)
from app.services.quote_assistant_service import (
    _account_type_payload,
    _credential_public_payload,
    _load_account_quota_map,
    create_platform_account_profile as _create_platform_account_profile,
    delete_platform_default_config as _delete_platform_default_config,
    get_platform_account_profile as _get_platform_account_profile,
    list_platform_default_configs as _list_platform_default_configs,
    list_platform_account_profiles as _list_platform_account_profiles,
    list_platform_account_types as _list_platform_account_types,
    list_quote_platforms as _list_quote_platforms,
    recall_quote_case_images as _recall_quote_case_images,
    resolve_platform_default_config as _resolve_platform_default_config,
    save_platform_default_config as _save_platform_default_config,
    save_platform_account_type as _save_platform_account_type,
    start_platform_account_login as _start_platform_account_login,
    sanitize_quote_user_message,
    submit_platform_account_login_challenge as _submit_platform_account_login_challenge,
    update_platform_account_profile as _update_platform_account_profile,
)
from app.services.image_slot_classifier import SLOT_KEYS
from app.services.quote_result_image import save_quote_result_card_image
from app.services.storage import StorageService

router = APIRouter(prefix="/ai-assistant", tags=["报价助手"])
logger = logging.getLogger(__name__)
storage = StorageService()
QUOTE_UPLOAD_SLOTS = set(SLOT_KEYS)
MAX_QUOTE_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_QUOTE_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/bmp",
    "image/gif",
    "image/heic",
    "image/heif",
}
QUOTE_IMAGE_EXT_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def _quote_api_error_detail(error: Exception) -> str:
    raw = str(error or "").strip() or error.__class__.__name__
    low = raw.lower()
    if "doesn't exist" in low or "does not exist" in low or "no such table" in low:
        return "报价助手平台配置读取失败：数据库缺少报价助手相关表，请先创建报价助手数据表后重启后端"
    if "unknown column" in low or "no such column" in low:
        return "报价助手平台配置读取失败：数据库表结构与当前代码不一致，缺少字段或字段名不匹配"
    if "can't connect to mysql" in low or "connect to mysql" in low:
        return "报价助手平台配置读取失败：无法连接数据库，请确认数据库服务已启动且连接配置正确"
    if "access denied" in low:
        return "报价助手平台配置读取失败：数据库账号权限不足，请检查数据库用户权限"
    if "timeout" in low:
        return "报价助手平台配置读取失败：数据库响应超时，请稍后重试或检查数据库负载"
    return f"报价助手平台配置读取失败：{sanitize_quote_user_message(raw[:300], '服务器处理异常，请查看后端日志')}"


def _pick(obj: Any, *keys: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj.get(k)
        return default
    for k in keys:
        if hasattr(obj, k):
            return getattr(obj, k)
    return default


def _to_session_item(row: Any) -> AiSessionItem:
    return AiSessionItem(
        session_id=str(_pick(row, "session_id", "id", default="")),
        title=str(_pick(row, "title", default="") or ""),
        created_at=str(_pick(row, "created_at", default="") or "") or None,
        updated_at=str(_pick(row, "updated_at", default="") or "") or None,
        last_message_preview=sanitize_quote_user_message(_pick(row, "last_message_preview", default="") or "", ""),
        message_count=int(_pick(row, "message_count", default=0) or 0),
    )


def _owner_user_id_or_401(ctx: CurrentUserContext) -> int:
    require_ai_assistant_access(role_name=ctx.primary_role)
    owner_user_id = int(getattr(ctx.user, "id", 0) or 0)
    if owner_user_id <= 0:
        raise HTTPException(status_code=401, detail="无法识别当前用户")
    return owner_user_id


def _ensure_super_admin(ctx: CurrentUserContext) -> None:
    require_quote_default_config_manage_access(role_name=ctx.primary_role)


def _ensure_quote_use(ctx: CurrentUserContext) -> None:
    require_quote_assistant_quote_use_access(role_name=ctx.primary_role)


def _ensure_platform_account_manage(ctx: CurrentUserContext) -> None:
    require_quote_platform_account_manage_access(role_name=ctx.primary_role)


QUOTE_USE_INTENTS = {"quote", "quote_image_collect", "quote_material_status", "quote_credential", "quote_config_override"}
QUOTE_ACTION_KEYWORDS = ("报价", "材料状态", "平台账号", "短信验证码", "验证码")
QUOTE_HIDDEN_MESSAGE = "当前账号无权查看历史报价材料内容"


def _can_quote_use(ctx: CurrentUserContext) -> bool:
    try:
        require_quote_assistant_quote_use_access(role_name=ctx.primary_role)
        return True
    except HTTPException:
        return False


def _action_requires_quote_use(action: Any) -> bool:
    if not isinstance(action, dict):
        return False
    text = " ".join(
        str(action.get(key) or "")
        for key in ("label", "value", "target", "type", "platform_code", "platform_name")
    )
    if action.get("type") == "open_account_manager":
        return True
    return any(keyword in text for keyword in QUOTE_ACTION_KEYWORDS)


def _filter_actions_for_quote_permission(actions: Any, *, can_quote_use: bool) -> List[Dict[str, Any]]:
    if not isinstance(actions, list):
        return []
    items = [item for item in actions if isinstance(item, dict)]
    if can_quote_use:
        return items
    return [item for item in items if not _action_requires_quote_use(item)]


def _metadata_is_quote_material(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    intent = str(metadata.get("intent") or "").strip().lower()
    if intent in QUOTE_USE_INTENTS:
        return True
    if isinstance(metadata.get("images"), list) and metadata.get("images"):
        return True
    page_context = metadata.get("page_context")
    if isinstance(page_context, dict):
        if isinstance(page_context.get("images"), list) and page_context.get("images"):
            return True
        if isinstance(page_context.get("uploaded_images"), list) and page_context.get("uploaded_images"):
            return True
    data = metadata.get("data")
    if isinstance(data, dict):
        data_intent = str(data.get("intent") or "").strip().lower()
        if data_intent in QUOTE_USE_INTENTS:
            return True
        payload = data.get("payload")
        if isinstance(payload, dict):
            quote_keys = {
                "quote_case",
                "quote_task",
                "quote_result",
                "attached_images",
                "ready_slots",
                "missing_requirements",
                "current_task",
                "platform_account",
                "images_by_slot",
                "normalized_data",
                "quote_field_overrides",
            }
            if quote_keys.intersection(payload.keys()):
                return True
    return False


def _filter_metadata_for_quote_permission(metadata: Any, *, can_quote_use: bool) -> Dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    safe = deepcopy(metadata)
    if can_quote_use:
        if isinstance(safe.get("actions"), list):
            safe["actions"] = _filter_actions_for_quote_permission(safe.get("actions"), can_quote_use=True)
        return safe

    quote_material = _metadata_is_quote_material(safe)
    safe["actions"] = _filter_actions_for_quote_permission(safe.get("actions"), can_quote_use=False)
    if not quote_material:
        return safe

    return {
        "status": safe.get("status") or "hidden",
        "intent": str(safe.get("intent") or "quote_hidden"),
        "data": {
            "result_status": "hidden",
            "message": QUOTE_HIDDEN_MESSAGE,
        },
        "actions": [],
    }


def _regenerate_quote_result_image_from_result(result: Any, *, legacy_url: str = "") -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    card = result.get("result_card") or result.get("resultCard") or {}
    if not isinstance(card, dict) or not card:
        return {}
    trace_id = str(result.get("trace_id") or result.get("traceId") or "").strip()
    try:
        regenerated = save_quote_result_card_image(card, trace_id=trace_id) or {}
        if regenerated and legacy_url:
            regenerated["legacy_url"] = legacy_url
        return regenerated
    except Exception:
        logger.exception("legacy quote result image regenerate failed")
        return {}


def _is_legacy_quote_result_url(url: str) -> bool:
    normalized = str(url or "").replace("\\", "/").lower()
    return "/quote_results/" in normalized or normalized.startswith("quote_results/")


def _normalize_quote_result_image_ref(image: Any, *, result: Any = None) -> Any:
    if isinstance(image, str):
        url = image.strip()
        if not url:
            return image
        if _is_legacy_quote_result_url(url):
            regenerated = _regenerate_quote_result_image_from_result(result, legacy_url=url)
            if regenerated:
                return regenerated
        return {
            "kind": "quote_result",
            "slot_key": "related",
            "url": url,
            "image_url": url,
            "preview_url": url,
        }
    if isinstance(image, dict):
        url = str(image.get("preview_url") or image.get("url") or image.get("image_url") or "").strip()
        if not url:
            return image
        if _is_legacy_quote_result_url(url):
            regenerated = _regenerate_quote_result_image_from_result(result, legacy_url=url)
            if regenerated:
                return regenerated
        normalized = deepcopy(image)
        normalized.setdefault("kind", "quote_result")
        normalized.setdefault("slot_key", "related")
        normalized["url"] = str(normalized.get("url") or url)
        normalized["image_url"] = str(normalized.get("image_url") or url)
        normalized["preview_url"] = str(normalized.get("preview_url") or url)
        return normalized
    return image


def _normalize_quote_result_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    normalized = deepcopy(payload)
    for key in ("quote_result", "quoteResult"):
        result = normalized.get(key)
        if not isinstance(result, dict):
            continue
        result_copy = deepcopy(result)
        image = result_copy.get("result_image") if "result_image" in result_copy else result_copy.get("resultImage")
        if image is not None:
            wrapped = _normalize_quote_result_image_ref(image, result=result_copy)
            result_copy["result_image"] = wrapped
            if "resultImage" in result_copy:
                result_copy["resultImage"] = wrapped
        else:
            generated = _regenerate_quote_result_image_from_result(result_copy)
            if generated:
                result_copy["result_image"] = generated
        normalized[key] = result_copy
    return normalized


def _normalize_quote_result_metadata(metadata: Any) -> Any:
    if not isinstance(metadata, dict):
        return metadata
    normalized = deepcopy(metadata)
    if isinstance(normalized.get("data"), dict):
        data = deepcopy(normalized["data"])
        if isinstance(data.get("payload"), dict):
            data["payload"] = _normalize_quote_result_payload(data["payload"])
        normalized["data"] = data
    if isinstance(normalized.get("payload"), dict):
        normalized["payload"] = _normalize_quote_result_payload(normalized["payload"])
    if isinstance(normalized.get("quote_result"), dict):
        normalized["quote_result"] = _normalize_quote_result_payload({"quote_result": normalized["quote_result"]}).get("quote_result")
    if isinstance(normalized.get("quoteResult"), dict):
        normalized["quoteResult"] = _normalize_quote_result_payload({"quoteResult": normalized["quoteResult"]}).get("quoteResult")
    return normalized


def _chat_context(ctx: CurrentUserContext, body: AiChatIn) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if isinstance(body.context, dict):
        merged.update(body.context)
    body_order_id = body.order_id
    merged.update({
        "images": body.images,
        "current_user_id": int(getattr(ctx.user, "id", 0) or 0),
        "role_name": str(ctx.primary_role or ""),
        "team_names": list(ctx.team_names or ()),
    })
    page_context = merged.get("page_context")
    if isinstance(page_context, dict):
        if body_order_id is None and page_context.get("order_id") is not None:
            body_order_id = page_context.get("order_id")
        uploaded = page_context.get("uploaded_images")
        if isinstance(uploaded, list) and uploaded:
            merged.setdefault("uploaded_images", uploaded)
    if body_order_id is not None:
        merged["order_id"] = body_order_id
    return merged


async def _compute_upload_md5_and_size(file: UploadFile) -> Tuple[str, int]:
    h = hashlib.md5()
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
        size += len(chunk)
    await file.seek(0)
    return h.hexdigest(), size


def _guess_image_ext(filename: str, content_type: str) -> str:
    name = os.path.basename(str(filename or "")).lower()
    _, ext = os.path.splitext(name)
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic", ".heif"}:
        return ".jpg" if ext == ".jpeg" else ext
    ct = str(content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "bmp" in ct:
        return ".bmp"
    if "gif" in ct:
        return ".gif"
    if "heic" in ct:
        return ".heic"
    if "heif" in ct:
        return ".heif"
    return ".jpg"


def _filename_quote_image_ext(filename: str) -> str:
    name = os.path.basename(str(filename or "")).lower()
    _, ext = os.path.splitext(name)
    if ext in QUOTE_IMAGE_EXT_CONTENT_TYPES:
        return ".jpg" if ext == ".jpeg" else ext
    return ""


def _normalize_quote_image_content_type(filename: str, content_type: str) -> Tuple[str, str]:
    """Accept real images even when browsers omit MIME, but still reject non-images."""
    ct = (content_type or "").strip().lower()
    filename_ext = _filename_quote_image_ext(filename)
    if ct in ALLOWED_QUOTE_IMAGE_TYPES:
        return ct, filename_ext or _guess_image_ext(filename, ct)
    if ct in {"", "application/octet-stream", "binary/octet-stream"} and filename_ext:
        return QUOTE_IMAGE_EXT_CONTENT_TYPES[filename_ext], filename_ext
    raise HTTPException(status_code=400, detail="只支持 JPG、PNG、WEBP、BMP、GIF、HEIC/HEIF 图片")


@router.get("/sessions", response_model=AiSessionListOut)
async def list_ai_sessions(
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)

    try:
        rows = await _list_sessions(db, owner_user_id=owner_user_id) or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话列表读取失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '服务器处理异常')}")
    items = [_to_session_item(x) for x in rows]
    return AiSessionListOut(total=len(items), items=items)


@router.post("/sessions")
async def create_ai_session(
        body: AiCreateSessionIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)

    try:
        row = await _create_session(db, owner_user_id=owner_user_id, title=(body.title or "").strip() or None)
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"会话创建失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '服务器处理异常')}")
    return {
        "ok": True,
        "data": {
            "session_id": str(_pick(row, "session_id", "id", default="")),
            "title": str(_pick(row, "title", default="") or ""),
            "updated_at": str(_pick(row, "updated_at", default="") or "") or None,
        },
    }


@router.delete("/sessions/{session_id}", response_model=AiDeleteSessionOut)
async def delete_ai_session(
        session_id: str,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)

    try:
        ok = await _delete_session(db, session_id=session_id, owner_user_id=owner_user_id)
        if ok:
            await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="会话删除失败：%s" % sanitize_quote_user_message(str(e) or e.__class__.__name__, "服务器处理异常"))
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在或无权删除")
    return AiDeleteSessionOut(ok=True, session_id=session_id)


@router.post("/sessions/{session_id}/images/recall")
async def recall_ai_session_images(
        session_id: str,
        body: AiRecallSessionImageIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    _ensure_quote_use(ctx)
    storage_keys = [str(x or "").strip().lstrip("/") for x in (body.storage_keys or []) if str(x or "").strip()]
    if not storage_keys:
        raise HTTPException(status_code=400, detail="请选择要撤回的图片")

    try:
        session_result = await _recall_session_images(
            db,
            owner_user_id=str(owner_user_id),
            session_id=session_id,
            storage_keys=storage_keys,
            message_id=body.message_id,
        )
        quote_result = await _recall_quote_case_images(
            db,
            owner_user_id=owner_user_id,
            session_id=session_id,
            storage_keys=storage_keys,
        )
        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=404, detail=sanitize_quote_user_message(str(e), "会话或图片不存在"))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"图片撤回失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '服务器处理异常')}")

    return {
        "ok": True,
        "data": {
            "session": session_result,
            "quote": quote_result,
        },
    }


@router.get("/sessions/{session_id}/history")
async def get_ai_history(
        session_id: str,
        cursor: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=3, ge=1, le=50),
        today_only: bool = Query(default=False),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    can_quote_use = _can_quote_use(ctx)

    try:
        page = await _get_session_messages(
            db,
            session_id=session_id,
            owner_user_id=owner_user_id,
            cursor=(cursor or "").strip() or None,
            limit=limit,
            today_only_initial=bool(today_only) and not (cursor or "").strip(),
        ) or {}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=sanitize_quote_user_message(str(e), "会话不存在或无权限访问"))
    rows = page.get("items") if isinstance(page, dict) else []
    items: List[Dict[str, Any]] = []
    for m in rows:
        raw_metadata = _pick(m, "metadata", default={}) or {}
        filtered_metadata = _normalize_quote_result_metadata(
            _filter_metadata_for_quote_permission(raw_metadata, can_quote_use=can_quote_use)
        )
        raw_content = sanitize_quote_user_message(_pick(m, "content", "text", default="") or "", "")
        if not can_quote_use and _metadata_is_quote_material(raw_metadata):
            raw_content = QUOTE_HIDDEN_MESSAGE
        items.append(
            {
                "id": _pick(m, "id", default=None),
                "role": _pick(m, "role", default="assistant"),
                "content": raw_content,
                "created_at": _pick(m, "created_at", default=None),
                "metadata": filtered_metadata,
            }
        )
    return {
        "session_id": session_id,
        "items": items,
        "next_cursor": page.get("next_cursor") if isinstance(page, dict) else None,
        "has_more": bool(page.get("has_more")) if isinstance(page, dict) else False,
    }


@router.get("/platforms")
async def list_quote_platforms(
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    _owner_user_id_or_401(ctx)
    return {"ok": True, "data": {"platforms": _list_quote_platforms()}}


@router.get("/platform-default-configs")
async def list_platform_default_configs(
        platform_code: str | None = Query(default=None, max_length=32),
        account_type_name: str | None = Query(default=None, max_length=64),
        enabled: bool | None = Query(default=None),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    _owner_user_id_or_401(ctx)
    _ensure_super_admin(ctx)
    try:
        result = await _list_platform_default_configs(
            db,
            platform_code=platform_code,
            account_type_name=account_type_name,
            enabled=enabled,
        )
        return {"ok": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_quote_api_error_detail(e))


@router.get("/platform-default-configs/resolve")
async def resolve_platform_default_config(
        platform_code: str = Query(..., max_length=32),
        account_type_name: str | None = Query(default=None, max_length=64),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    _owner_user_id_or_401(ctx)
    _ensure_super_admin(ctx)
    try:
        result = await _resolve_platform_default_config(
            db,
            platform_code=platform_code,
            account_type_name=account_type_name,
        )
        return {"ok": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_quote_api_error_detail(e))


@router.post("/platform-default-configs")
async def create_platform_default_config(
        body: AiPlatformDefaultConfigIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    operator_user_id = _owner_user_id_or_401(ctx)
    _ensure_super_admin(ctx)
    try:
        row = await _save_platform_default_config(
            db,
            values=body.dict(),
            operator_user_id=operator_user_id,
        )
        await db.commit()
        return {"ok": True, "data": {"item": row and row.id}}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=sanitize_quote_user_message(str(e), "默认参数配置保存失败"))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"默认参数配置保存失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '服务器处理异常')}")


@router.put("/platform-default-configs/{config_id}")
async def update_platform_default_config(
        config_id: int,
        body: AiPlatformDefaultConfigIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    operator_user_id = _owner_user_id_or_401(ctx)
    _ensure_super_admin(ctx)
    try:
        row = await _save_platform_default_config(
            db,
            values=body.dict(),
            config_id=config_id,
            operator_user_id=operator_user_id,
        )
        await db.commit()
        return {"ok": True, "data": {"item": row and row.id}}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=sanitize_quote_user_message(str(e), "默认参数配置保存失败"))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"默认参数配置保存失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '服务器处理异常')}")


@router.delete("/platform-default-configs/{config_id}")
async def delete_platform_default_config(
        config_id: int,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    _owner_user_id_or_401(ctx)
    _ensure_super_admin(ctx)
    try:
        deleted = await _delete_platform_default_config(db, config_id=config_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="默认参数配置不存在或已删除")
        await db.commit()
        return {"ok": True, "data": {"deleted": True}}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=sanitize_quote_user_message(str(e), "默认参数配置删除失败"))
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"默认参数配置删除失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '服务器处理异常')}")


@router.get("/platform-account-types")
async def list_platform_account_types(
        platform_code: str | None = Query(default=None, max_length=32),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    _ensure_platform_account_manage(ctx)
    try:
        items = await _list_platform_account_types(db, owner_user_id=owner_user_id, platform_code=platform_code)
        return {"ok": True, "data": {"items": items, "total": len(items)}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_quote_api_error_detail(e))


@router.post("/platform-account-types")
async def create_platform_account_type(
        body: AiPlatformAccountTypeIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    _ensure_platform_account_manage(ctx)
    try:
        row = await _save_platform_account_type(db, owner_user_id=owner_user_id, values=body.dict())
        await db.commit()
        return {"ok": True, "data": {"item": _account_type_payload(row)}}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=sanitize_quote_user_message(str(e), "账号类型保存失败"))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"账号类型保存失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '服务器处理异常')}")


@router.put("/platform-account-types/{type_id}")
async def update_platform_account_type(
        type_id: int,
        body: AiPlatformAccountTypeIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    _ensure_platform_account_manage(ctx)
    try:
        row = await _save_platform_account_type(db, owner_user_id=owner_user_id, type_id=type_id, values=body.dict())
        await db.commit()
        return {"ok": True, "data": {"item": _account_type_payload(row)}}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=sanitize_quote_user_message(str(e), "账号类型保存失败"))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"账号类型保存失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '服务器处理异常')}")


@router.get("/platform-accounts")
async def list_platform_accounts(
        platform_code: str | None = Query(default=None, max_length=32),
        account_type_name: str | None = Query(default=None, max_length=64),
        enabled: bool | None = Query(default=None),
        login_status: str | None = Query(default=None, max_length=32),
        quota_status: str | None = Query(default=None, max_length=32),
        keyword: str | None = Query(default=None, max_length=128),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    _ensure_platform_account_manage(ctx)
    try:
        result = await _list_platform_account_profiles(
            db,
            owner_user_id=owner_user_id,
            filters={
                "platform_code": platform_code,
                "account_type_name": account_type_name,
                "enabled": enabled,
                "login_status": login_status,
                "quota_status": quota_status,
                "keyword": keyword,
            },
        )
        return {"ok": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_quote_api_error_detail(e))


@router.get("/platform-accounts/{account_id}")
async def get_platform_account(
        account_id: int,
        include_quota: bool = Query(default=True),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    _ensure_platform_account_manage(ctx)
    try:
        account = await _get_platform_account_profile(db, owner_user_id=owner_user_id, account_id=account_id)
        if not account:
            raise HTTPException(status_code=404, detail="平台账号不存在或无权查看")
        quota = None
        if include_quota:
            quota = (
                await _load_account_quota_map(db, [int(account.id or 0)], accounts_by_id={int(account.id or 0): account})
            ).get(int(account.id or 0))
        return {"ok": True, "data": {"account": _credential_public_payload(account, quota=quota, include_password=True) or {}}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_quote_api_error_detail(e))


@router.post("/images/upload")
async def upload_ai_assistant_image(
        slot_key: str = Form(default="related"),
        file: UploadFile = File(...),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    _owner_user_id_or_401(ctx)
    _ensure_quote_use(ctx)

    sk = str(slot_key or "related").strip() or "related"
    if sk not in QUOTE_UPLOAD_SLOTS:
        raise HTTPException(status_code=400, detail="非法图片卡槽，请重新选择图片类型")
    if not file:
        raise HTTPException(status_code=400, detail="请选择要上传的图片")

    original_name = (file.filename or "image").strip()
    content_type, ext = _normalize_quote_image_content_type(original_name, file.content_type or "")
    md5_hex, size = await _compute_upload_md5_and_size(file)
    if size <= 0:
        raise HTTPException(status_code=400, detail="图片文件为空")
    if size > MAX_QUOTE_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="图片不能超过 20MB")

    storage_key = storage.build_key_by_md5(scene=sk, md5_hex=md5_hex, ext=ext).lstrip("/")
    if not storage.validate_b1_key(scene=sk, storage_key=storage_key, md5_hex=md5_hex):
        raise HTTPException(status_code=400, detail="图片存储路径校验失败")

    try:
        exists, etag = await anyio.to_thread.run_sync(lambda: storage.head_object(storage_key))
        if not exists:
            await file.seek(0)
            etag = await anyio.to_thread.run_sync(
                lambda: storage.put_object(
                    storage_key,
                    data=file.file,
                    content_type=content_type,
                )
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"图片上传到对象存储失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '存储服务处理异常')}")

    try:
        url = storage.object_url_for_display(
            storage_key,
            signed=None,
            expires_in=900,
            allow_fallback_public=False,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"图片临时访问链接生成失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '存储服务处理异常')}")

    return {
        "ok": True,
        "data": {
            "slot_key": sk,
            "md5": md5_hex,
            "storage_key": storage_key,
            "etag": etag or None,
            "size": int(size or 0),
            "content_type": content_type,
            "original_name": original_name,
            "url": url,
        },
    }


@router.post("/platform-accounts")
async def create_platform_account(
        body: AiPlatformAccountProfileIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    _ensure_platform_account_manage(ctx)
    try:
        account = await _create_platform_account_profile(
            db,
            owner_user_id=owner_user_id,
            values=body.dict(exclude_unset=True),
            operator_user_id=owner_user_id,
        )
        await db.commit()
        return {"ok": True, "data": {"account": _credential_public_payload(account) or {}}}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=sanitize_quote_user_message(str(e), "平台账号信息不完整"))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="平台账号保存失败：%s" % sanitize_quote_user_message(str(e) or e.__class__.__name__, "服务器处理异常"))


@router.put("/platform-accounts/{account_id}")
async def update_platform_account(
        account_id: int,
        body: AiPlatformAccountProfileIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    _ensure_platform_account_manage(ctx)
    try:
        account = await _update_platform_account_profile(
            db,
            owner_user_id=owner_user_id,
            account_id=account_id,
            values=body.dict(exclude_unset=True),
            operator_user_id=owner_user_id,
            confirm_enabled_edit=bool(body.confirm_enabled_edit),
        )
        await db.commit()
        return {"ok": True, "data": {"account": _credential_public_payload(account) or {}}}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=sanitize_quote_user_message(str(e), "平台账号保存失败"))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="平台账号保存失败：%s" % sanitize_quote_user_message(str(e) or e.__class__.__name__, "服务器处理异常"))


@router.post("/platform-accounts/{account_id}/login")
async def login_platform_account(
        account_id: int,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    _ensure_platform_account_manage(ctx)
    try:
        result = await _start_platform_account_login(
            db,
            owner_user_id=owner_user_id,
            account_id=account_id,
            operator_user_id=owner_user_id,
        )
        await db.commit()
        return {"ok": True, "data": result}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=sanitize_quote_user_message(str(e), "平台登录失败"))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="平台登录失败：%s" % sanitize_quote_user_message(str(e) or e.__class__.__name__, "服务器处理异常"))


@router.post("/platform-account-login-tasks/{task_id}/challenge")
async def submit_platform_account_login_challenge(
        task_id: int,
        body: AiPlatformAccountLoginChallengeIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    _ensure_platform_account_manage(ctx)
    try:
        result = await _submit_platform_account_login_challenge(
            db,
            owner_user_id=owner_user_id,
            task_id=task_id,
            code=body.code,
            operator_user_id=owner_user_id,
        )
        await db.commit()
        return {"ok": True, "data": result}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=sanitize_quote_user_message(str(e), "验证码提交失败"))
    except Exception as e:
        await db.rollback()
        logger.exception("platform account challenge submit failed: task_id=%s owner_user_id=%s", task_id, owner_user_id)
        raise HTTPException(status_code=500, detail=f"验证码提交失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '服务器处理异常')}")


@router.post("/chat", response_model=AiChatOut)
async def ai_chat(
        body: AiChatIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    can_quote_use = _can_quote_use(ctx)

    try:
        result = await _send_message(
            owner_user_id=owner_user_id,
            session_id=body.session_id,
            message=body.message,
            history=body.history,
            system_prompt=None,
            model=None,
            temperature=None,
            max_tokens=None,
            stream=bool(body.stream),
            context=_chat_context(ctx, body),
            db=db,
        )
        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=sanitize_quote_user_message(str(e), "消息处理失败"))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"消息处理失败：{sanitize_quote_user_message(str(e) or e.__class__.__name__, '服务器处理异常')}")
    raw_response_metadata = {
        "intent": _pick(result, "intent", default=None),
        "actions": _pick(result, "actions", default=[]) or [],
        "data": _pick(result, "data", default=None),
    }
    filtered_response_metadata = _normalize_quote_result_metadata(
        _filter_metadata_for_quote_permission(
            raw_response_metadata,
            can_quote_use=can_quote_use,
        )
    )
    reply = sanitize_quote_user_message(_pick(result, "reply", "content", "text", default="") or "", "")
    if not can_quote_use and _metadata_is_quote_material(raw_response_metadata):
        reply = QUOTE_HIDDEN_MESSAGE
    return AiChatOut(
        reply=reply or "已处理",
        ok=True,
        session_id=_pick(result, "session_id", default=None),
        intent=_pick(result, "intent", default=None),
        trace_id=_pick(result, "trace_id", default=None),
        confidence=float(_pick(result, "confidence", default=0.0) or 0.0),
        actions=filtered_response_metadata.get("actions") or [],
        usage=_pick(result, "usage", default=None),
        model=_pick(result, "model", default=None),
        data=filtered_response_metadata.get("data") if isinstance(filtered_response_metadata.get("data"), dict) else None,
    )


@router.get("/health")
async def ai_assistant_health():
    return {"ok": True, "module": "quotation_assistant"}
