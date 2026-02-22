# app/services/ai_assistant_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TZ_BJ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(TZ_BJ).isoformat()


def _to_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        return str(v)
    except Exception:
        return default


def _new_id() -> str:
    return uuid.uuid4().hex


class _Store:
    """
    轻量 JSON 存储（先打通报价助手链路）
    文件：storage/quote_assistant_sessions.json
    """

    def __init__(self) -> None:
        base_dir = Path(os.getenv("STORAGE_DIR", "storage"))
        base_dir.mkdir(parents=True, exist_ok=True)
        self._file = base_dir / "quote_assistant_sessions.json"
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"sessions": {}}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self._file.exists():
                self._flush()
                return
            try:
                text = self._file.read_text(encoding="utf-8")
                obj = json.loads(text) if text.strip() else {}
                if not isinstance(obj, dict):
                    obj = {}
                if not isinstance(obj.get("sessions"), dict):
                    obj["sessions"] = {}
                self._data = obj
            except Exception:
                # 文件损坏时自动重建，避免服务起不来
                self._data = {"sessions": {}}
                self._flush()

    def _flush(self) -> None:
        with self._lock:
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._file)

    # -------- Session --------
    def create_session(self, *, owner_user_id: str, title: Optional[str] = None) -> Dict[str, Any]:
        now = _now_iso()
        sid = _new_id()
        row = {
            "session_id": sid,
            "owner_user_id": _to_str(owner_user_id),
            "title": (_to_str(title).strip() or "新会话"),
            "created_at": now,
            "updated_at": now,
            "deleted": False,
            "messages": [],
        }
        with self._lock:
            self._data["sessions"][sid] = row
            self._flush()
        return deepcopy(row)

    def get_session(self, *, owner_user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._data["sessions"].get(session_id)
            if not row:
                return None
            if row.get("deleted"):
                return None
            if _to_str(row.get("owner_user_id")) != _to_str(owner_user_id):
                return None
            return deepcopy(row)

    def get_or_create_session(
        self,
        *,
        owner_user_id: str,
        session_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        if session_id:
            found = self.get_session(owner_user_id=owner_user_id, session_id=session_id)
            if found:
                return found
        return self.create_session(owner_user_id=owner_user_id, title=title)

    def list_sessions(self, *, owner_user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        owner = _to_str(owner_user_id)
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for s in self._data["sessions"].values():
                if s.get("deleted"):
                    continue
                if _to_str(s.get("owner_user_id")) != owner:
                    continue

                msgs = s.get("messages") or []
                preview = ""
                if msgs:
                    preview = _to_str(msgs[-1].get("content"))[:120]

                rows.append(
                    {
                        "session_id": s.get("session_id"),
                        "title": s.get("title") or "新会话",
                        "created_at": s.get("created_at"),
                        "updated_at": s.get("updated_at"),
                        "message_count": len(msgs),
                        "last_message_preview": preview,
                    }
                )

            rows.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
            return deepcopy(rows[: max(1, min(int(limit or 50), 200))])

    def delete_session(self, *, owner_user_id: str, session_id: str) -> bool:
        with self._lock:
            row = self._data["sessions"].get(session_id)
            if not row or row.get("deleted"):
                return False
            if _to_str(row.get("owner_user_id")) != _to_str(owner_user_id):
                return False
            row["deleted"] = True
            row["updated_at"] = _now_iso()
            self._flush()
        return True

    # -------- Messages --------
    def list_messages(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        row = self.get_session(owner_user_id=owner_user_id, session_id=session_id)
        if not row:
            raise ValueError("会话不存在或无权限访问")

        msgs = row.get("messages") or []
        lim = max(1, min(int(limit or 50), 200))

        if cursor:
            idx = -1
            for i, m in enumerate(msgs):
                if _to_str(m.get("id")) == _to_str(cursor):
                    idx = i
                    break
            if idx > 0:
                sliced = msgs[max(0, idx - lim): idx]
                has_more = (idx - lim) > 0
                next_cursor = sliced[0]["id"] if (has_more and sliced) else None
                return {"items": sliced, "next_cursor": next_cursor, "has_more": has_more}

        sliced = msgs[-lim:]
        has_more = len(msgs) > len(sliced)
        next_cursor = sliced[0]["id"] if (has_more and sliced) else None
        return {"items": sliced, "next_cursor": next_cursor, "has_more": has_more}

    def append_message(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            row = self._data["sessions"].get(session_id)
            if not row or row.get("deleted"):
                raise ValueError("会话不存在")
            if _to_str(row.get("owner_user_id")) != _to_str(owner_user_id):
                raise ValueError("无权限访问该会话")

            msg = {
                "id": _new_id(),
                "role": _to_str(role),
                "content": _to_str(content),
                "created_at": _now_iso(),
                "metadata": metadata or {},
            }
            row.setdefault("messages", []).append(msg)

            # 首条用户消息自动生成标题
            if (row.get("title") in (None, "", "新会话")) and msg["role"] == "user":
                row["title"] = (msg["content"].strip() or "新会话")[:24]

            row["updated_at"] = msg["created_at"]
            self._flush()
            return deepcopy(msg)


_store = _Store()


def _rule_reply(text: str) -> Tuple[str, Dict[str, Any]]:
    t = _to_str(text).strip()

    if t.endswith("报价"):
        platform = t[:-2].strip() or "目标平台"
        return (
            f"已识别报价指令：{platform}报价\n"
            "我会按当前会话材料执行：资料校验 → OCR结果汇总 → 平台报价服务（预留接口）。\n"
            "当前版本已打通会话与规则链路，平台服务可按“一个平台一个文件”继续接入。",
            {
                "status": "success",
                "intent": "quote",
                "trace_id": _new_id()[:16],
                "actions": [
                    {"type": "suggest", "label": "查看当前材料状态"},
                    {"type": "suggest", "label": f"{platform}报价"},
                ],
            },
        )

    if "查订单" in t:
        return (
            "已识别为订单查询意图。\n当前报价助手已打通会话链路；订单查询接口可在下一步接入。",
            {
                "status": "success",
                "intent": "query_order",
                "trace_id": _new_id()[:16],
                "actions": [
                    {"type": "suggest", "label": "太平洋报价"},
                    {"type": "suggest", "label": "查看当前材料状态"},
                ],
            },
        )

    if t in {"查看当前材料状态", "材料状态"}:
        return (
            "当前材料状态功能已预留：可展示各槽位图片数量、OCR状态、可报价平台。\n你上传图片后再发平台报价指令即可。",
            {
                "status": "success",
                "intent": "material_status",
                "trace_id": _new_id()[:16],
            },
        )

    return (
        "已收到指令。\n这是报价助手（规则引擎版），可先输入“太平洋报价”这类指令触发报价流程。",
        {
            "status": "success",
            "intent": "chat",
            "trace_id": _new_id()[:16],
            "actions": [{"type": "suggest", "label": "太平洋报价"}],
        },
    )


# =============================
# 对外导出函数（给 API 层 import）
# =============================

def get_or_create_session(
    *,
    owner_user_id: str,
    session_id: Optional[str] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    return _store.get_or_create_session(owner_user_id=owner_user_id, session_id=session_id, title=title)


def create_session(*, owner_user_id: str, title: Optional[str] = None) -> Dict[str, Any]:
    return _store.create_session(owner_user_id=owner_user_id, title=title)


def list_sessions(*, owner_user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    return _store.list_sessions(owner_user_id=owner_user_id, limit=limit)


def delete_session(*, owner_user_id: str, session_id: str) -> bool:
    return _store.delete_session(owner_user_id=owner_user_id, session_id=session_id)


def get_session_messages(
    *,
    owner_user_id: str,
    session_id: str,
    cursor: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    return _store.list_messages(owner_user_id=owner_user_id, session_id=session_id, cursor=cursor, limit=limit)


# ✅ 兼容 API 路由导入名：返回纯消息列表（对齐 ai_assistant.py 的历史接口使用方式）
def list_messages(
    *,
    owner_user_id: str,
    session_id: str,
    cursor: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    res = _store.list_messages(owner_user_id=owner_user_id, session_id=session_id, cursor=cursor, limit=limit)
    items = res.get("items") if isinstance(res, dict) else None
    if not isinstance(items, list):
        return []
    return items


def send_message(
    *,
    owner_user_id: str,
    session_id: Optional[str] = None,

    # 路由当前调用风格（通用聊天壳）
    message: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    stream: Optional[bool] = None,
    context: Optional[Dict[str, Any]] = None,

    # 规则引擎原有风格（兼容保留）
    text: Optional[str] = None,
    client_msg_id: Optional[str] = None,
    page_context: Optional[Dict[str, Any]] = None,
    use_stream: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    兼容两套调用签名：
    1) API 路由当前传参（message/context/stream/history...）
    2) 旧规则引擎调用（text/page_context/use_stream）

    未使用参数（history/system_prompt/model/temperature/max_tokens）先接住，避免 TypeError。
    """
    del history, system_prompt, temperature, max_tokens  # 当前规则版暂不使用，先显式忽略

    # 统一参数名
    final_text = _to_str(message if message is not None else text).strip()
    final_context = context if isinstance(context, dict) else (page_context if isinstance(page_context, dict) else {})
    final_stream = bool(stream if stream is not None else use_stream)

    if not final_text:
        raise ValueError("消息内容不能为空")

    # ✅ 兼容“首次不传 session_id”：自动创建会话
    sess = _store.get_or_create_session(owner_user_id=_to_str(owner_user_id), session_id=session_id)
    real_session_id = _to_str(sess.get("session_id"))

    user_msg = _store.append_message(
        owner_user_id=owner_user_id,
        session_id=real_session_id,
        role="user",
        content=final_text,
        metadata={
            "status": "success",
            "intent": "user_input",
            "client_msg_id": client_msg_id,
            "page_context": final_context,
            "use_stream": final_stream,
            "model": _to_str(model, default="rule-engine") or "rule-engine",
        },
    )

    reply_text, reply_meta = _rule_reply(final_text)

    assistant_msg = _store.append_message(
        owner_user_id=owner_user_id,
        session_id=real_session_id,
        role="assistant",
        content=reply_text,
        metadata=reply_meta,
    )

    meta = assistant_msg.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    # ✅ 返回兼容字段，供 API 层直接组 AiChatResponse
    return {
        "session_id": real_session_id,
        "reply": _to_str(assistant_msg.get("content")),
        "intent": _to_str(meta.get("intent"), "chat") or "chat",
        "trace_id": _to_str(meta.get("trace_id"), _new_id()[:16]) or _new_id()[:16],
        "confidence": 1.0,
        "actions": meta.get("actions") if isinstance(meta.get("actions"), list) else [],
        "usage": None,
        "model": _to_str(model, "rule-engine") or "rule-engine",

        # 保留旧结构兼容
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        "stream": None,
    }


__all__ = [
    "get_or_create_session",
    "create_session",
    "list_sessions",
    "delete_session",
    "get_session_messages",
    "list_messages",
    "send_message",
]
