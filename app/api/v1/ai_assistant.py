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
import os
from typing import Any, Dict, List, Tuple

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserContext, get_current_user_with_role_and_teams
from app.core.access_control import require_ai_assistant_access
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
    create_platform_account_profile as _create_platform_account_profile,
    get_platform_account_profile as _get_platform_account_profile,
    list_platform_account_profiles as _list_platform_account_profiles,
    list_platform_account_types as _list_platform_account_types,
    list_quote_platforms as _list_quote_platforms,
    recall_quote_case_images as _recall_quote_case_images,
    save_platform_account_type as _save_platform_account_type,
    start_platform_account_login as _start_platform_account_login,
    submit_platform_account_login_challenge as _submit_platform_account_login_challenge,
    update_platform_account_profile as _update_platform_account_profile,
)
from app.services.image_slot_classifier import SLOT_KEYS
from app.services.storage import StorageService

router = APIRouter(prefix="/ai-assistant", tags=["报价助手"])
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
        return "报价助手平台配置读取失败：数据库缺少报价助手相关表，请先执行 python scripts/create_quote_assistant_tables.py 后重启后端"
    if "unknown column" in low or "no such column" in low:
        return f"报价助手平台配置读取失败：数据库表结构与当前代码不一致，缺少字段或字段名不匹配。原始原因：{raw[:260]}"
    if "can't connect to mysql" in low or "connect to mysql" in low:
        return "报价助手平台配置读取失败：无法连接 MySQL，请确认数据库容器/服务已启动且连接配置正确"
    if "access denied" in low:
        return "报价助手平台配置读取失败：数据库账号权限不足，请检查 MySQL 用户权限"
    if "timeout" in low:
        return "报价助手平台配置读取失败：数据库响应超时，请稍后重试或检查数据库负载"
    return f"报价助手平台配置读取失败：{raw[:300]}"


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
        last_message_preview=str(_pick(row, "last_message_preview", default="") or ""),
        message_count=int(_pick(row, "message_count", default=0) or 0),
    )


def _owner_user_id_or_401(ctx: CurrentUserContext) -> int:
    require_ai_assistant_access(role_name=ctx.primary_role)
    owner_user_id = int(getattr(ctx.user, "id", 0) or 0)
    if owner_user_id <= 0:
        raise HTTPException(status_code=401, detail="无法识别当前用户")
    return owner_user_id


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
        raise HTTPException(status_code=500, detail=f"会话列表读取失败：{str(e) or e.__class__.__name__}")
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
        raise HTTPException(status_code=500, detail=f"会话创建失败：{str(e) or e.__class__.__name__}")
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
        raise HTTPException(status_code=500, detail="会话删除失败：%s" % (str(e) or e.__class__.__name__))
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
        raise HTTPException(status_code=404, detail=str(e) or "会话或图片不存在")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"图片撤回失败：{str(e) or e.__class__.__name__}")

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
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)

    try:
        page = await _get_session_messages(
            db,
            session_id=session_id,
            owner_user_id=owner_user_id,
            cursor=(cursor or "").strip() or None,
            limit=limit,
        ) or {}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e) or "会话不存在或无权限访问")
    rows = page.get("items") if isinstance(page, dict) else []
    items: List[Dict[str, Any]] = []
    for m in rows:
        items.append(
            {
                "id": _pick(m, "id", default=None),
                "role": _pick(m, "role", default="assistant"),
                "content": _pick(m, "content", "text", default="") or "",
                "created_at": _pick(m, "created_at", default=None),
                "metadata": _pick(m, "metadata", default={}) or {},
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


@router.get("/platform-account-types")
async def list_platform_account_types(
        platform_code: str | None = Query(default=None, max_length=32),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
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
    try:
        row = await _save_platform_account_type(db, owner_user_id=owner_user_id, values=body.dict())
        await db.commit()
        return {"ok": True, "data": {"item": _account_type_payload(row)}}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e) or "账号类型保存失败")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"账号类型保存失败：{str(e) or e.__class__.__name__}")


@router.put("/platform-account-types/{type_id}")
async def update_platform_account_type(
        type_id: int,
        body: AiPlatformAccountTypeIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
    try:
        row = await _save_platform_account_type(db, owner_user_id=owner_user_id, type_id=type_id, values=body.dict())
        await db.commit()
        return {"ok": True, "data": {"item": _account_type_payload(row)}}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e) or "账号类型保存失败")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"账号类型保存失败：{str(e) or e.__class__.__name__}")


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


@router.post("/images/upload")
async def upload_ai_assistant_image(
        slot_key: str = Form(default="related"),
        file: UploadFile = File(...),
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
):
    _owner_user_id_or_401(ctx)

    sk = str(slot_key or "related").strip() or "related"
    if sk not in QUOTE_UPLOAD_SLOTS:
        raise HTTPException(status_code=400, detail=f"非法图片卡槽：{slot_key}")
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
        raise HTTPException(status_code=502, detail=f"图片上传到对象存储失败：{str(e) or e.__class__.__name__}")

    try:
        url = storage.object_url_for_display(
            storage_key,
            signed=None,
            expires_in=900,
            allow_fallback_public=False,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"图片临时访问链接生成失败：{str(e) or e.__class__.__name__}")

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
        raise HTTPException(status_code=400, detail=str(e) or "平台账号信息不完整")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="平台账号保存失败：%s" % (str(e) or e.__class__.__name__))


@router.put("/platform-accounts/{account_id}")
async def update_platform_account(
        account_id: int,
        body: AiPlatformAccountProfileIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
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
        raise HTTPException(status_code=400, detail=str(e) or "平台账号保存失败")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="平台账号保存失败：%s" % (str(e) or e.__class__.__name__))


@router.post("/platform-accounts/{account_id}/login")
async def login_platform_account(
        account_id: int,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
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
        raise HTTPException(status_code=400, detail=str(e) or "平台登录失败")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="平台登录失败：%s" % (str(e) or e.__class__.__name__))


@router.post("/platform-account-login-tasks/{task_id}/challenge")
async def submit_platform_account_login_challenge(
        task_id: int,
        body: AiPlatformAccountLoginChallengeIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)
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
        raise HTTPException(status_code=400, detail=str(e) or "验证码提交失败")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"验证码提交失败：{str(e) or e.__class__.__name__}")


@router.post("/chat", response_model=AiChatOut)
async def ai_chat(
        body: AiChatIn,
        ctx: CurrentUserContext = Depends(get_current_user_with_role_and_teams),
        db: AsyncSession = Depends(get_db),
):
    owner_user_id = _owner_user_id_or_401(ctx)

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
        raise HTTPException(status_code=400, detail=str(e) or "消息处理失败")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"消息处理失败：{str(e) or e.__class__.__name__}")
    reply = str(_pick(result, "reply", "content", "text", default="") or "")
    return AiChatOut(
        reply=reply or "已处理",
        ok=True,
        session_id=_pick(result, "session_id", default=None),
        intent=_pick(result, "intent", default=None),
        trace_id=_pick(result, "trace_id", default=None),
        confidence=float(_pick(result, "confidence", default=0.0) or 0.0),
        actions=_pick(result, "actions", default=[]) or [],
        usage=_pick(result, "usage", default=None),
        model=_pick(result, "model", default=None),
        data=_pick(result, "data", default=None),
    )


@router.get("/health")
async def ai_assistant_health():
    return {"ok": True, "module": "quotation_assistant"}
