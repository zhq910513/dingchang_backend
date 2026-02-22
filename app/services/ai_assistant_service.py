# app/services/ai_assistant_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------
# 基础配置（北京时间）
# -----------------------------
TZ_BJ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    # 统一北京时间，输出 ISO（前端 new Date 可直接解析）
    return datetime.now(TZ_BJ).isoformat()


def _safe_str(v: Any, default: str = "") -> str:
    if v is None:
      return default
    try:
      return str(v)
    except Exception:
      return default


def _uuid() -> str:
    return uuid.uuid4().hex


@dataclass
class _StoreConfig:
    file_path: Path


class QuoteAssistantStore:
    """
    轻量 JSON 持久化存储（先打通链路）
    结构：
    {
      "sessions": {
        "<session_id>": {
          "session_id": "...",
          "owner_user_id": "123",
          "title": "...",
          "created_at": "...",
          "updated_at": "...",
          "deleted": false,
          "messages": [ ... ]
        }
      }
    }
    """

    def __init__(self) -> None:
        base_dir = Path(os.getenv("STORAGE_DIR", "storage"))
        base_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = _StoreConfig(file_path=base_dir / "quote_assistant_sessions.json")
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"sessions": {}}
        self._load()

    # -----------------------------
    # 文件读写
    # -----------------------------
    def _load(self) -> None:
        with self._lock:
            if not self.cfg.file_path.exists():
                self._data = {"sessions": {}}
                self._flush()
                return
            try:
                raw = self.cfg.file_path.read_text(encoding="utf-8")
                obj = json.loads(raw) if raw.strip() else {}
                if not isinstance(obj, dict):
                    obj = {}
                if "sessions" not in obj or not isinstance(obj["sessions"], dict):
                    obj["sessions"] = {}
                self._data = obj
            except Exception:
                # 文件坏了也别让服务起不来，降级重建
                self._data = {"sessions": {}}
                self._flush()

    def _flush(self) -> None:
        with self._lock:
            tmp = self.cfg.file_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.cfg.file_path)

    # -----------------------------
    # Session
    # -----------------------------
    def get_session(self, *, owner_user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            s = self._data["sessions"].get(session_id)
            if not s:
                return None
            if s.get("deleted"):
                return None
            if _safe_str(s.get("owner_user_id")) != _safe_str(owner_user_id):
                return None
            return deepcopy(s)

    def create_session(self, *, owner_user_id: str, title: Optional[str] = None) -> Dict[str, Any]:
        now = _now_iso()
        sid = _uuid()
        row = {
            "session_id": sid,
            "owner_user_id": _safe_str(owner_user_id),
            "title": _safe_str(title, "新会话") or "新会话",
            "created_at": now,
            "updated_at": now,
            "deleted": False,
            "messages": [],
        }
        with self._lock:
            self._data["sessions"][sid] = row
            self._flush()
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
        owner = _safe_str(owner_user_id)
        with self._lock:
            rows = []
            for s in self._data["sessions"].values():
                if s.get("deleted"):
                    continue
                if _safe_str(s.get("owner_user_id")) != owner:
                    continue
                msg_count = len(s.get("messages") or [])
                last_msg_preview = ""
                if msg_count:
                    last = (s.get("messages") or [])[-1]
                    last_msg_preview = _safe_str(last.get("content"))[:120]
                rows.append(
                    {
                        "session_id": s.get("session_id"),
                        "title": s.get("title") or "新会话",
                        "created_at": s.get("created_at"),
                        "updated_at": s.get("updated_at"),
                        "message_count": msg_count,
                        "last_message_preview": last_msg_preview,
                    }
                )
            rows.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
            return deepcopy(rows[: max(1, min(int(limit or 50), 200))])

    def delete_session(self, *, owner_user_id: str, session_id: str) -> bool:
        with self._lock:
            s = self._data["sessions"].get(session_id)
            if not s:
                return False
            if s.get("deleted"):
                return False
            if _safe_str(s.get("owner_user_id")) != _safe_str(owner_user_id):
                return False
            s["deleted"] = True
            s["updated_at"] = _now_iso()
            self._flush()
        return True

    # -----------------------------
    # Messages
    # -----------------------------
    def list_messages(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        s = self.get_session(owner_user_id=owner_user_id, session_id=session_id)
        if not s:
            raise ValueError("会话不存在或无权限访问")

        items = s.get("messages") or []
        # 前端当前是“全部刷新”为主，这里仍保留 cursor 兼容
        lim = max(1, min(int(limit or 50), 200))

        # cursor 约定为 message_id（取更早消息）；先做兼容，不复杂化
        if cursor:
            idx = -1
            for i, m in enumerate(items):
                if _safe_str(m.get("id")) == _safe_str(cursor):
                    idx = i
                    break
            if idx > 0:
                sliced = items[max(0, idx - lim):idx]
                next_cursor = sliced[0]["id"] if len(sliced) == lim and len(sliced) < idx else None
                has_more = bool(next_cursor)
                return {"items": sliced, "next_cursor": next_cursor, "has_more": has_more}

        sliced = items[-lim:]
        has_more = len(items) > len(sliced)
        next_cursor = sliced[0]["id"] if has_more and sliced else None
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
        now = _now_iso()
        with self._lock:
            s = self._data["sessions"].get(session_id)
            if not s or s.get("deleted"):
                raise ValueError("会话不存在")
            if _safe_str(s.get("owner_user_id")) != _safe_str(owner_user_id):
                raise ValueError("无权限访问该会话")

            msg = {
                "id": _uuid(),
                "role": role,
                "content": _safe_str(content),
                "created_at": now,
                "metadata": metadata or {},
            }
            s.setdefault("messages", []).append(msg)

            # 自动补标题（首条用户消息）
            if (not s.get("title")) or s.get("title") == "新会话":
                if role == "user":
                    s["title"] = (_safe_str(content).strip() or "新会话")[:24]

            s["updated_at"] = now
            self._flush()
            return deepcopy(msg)


_store = QuoteAssistantStore()

# -----------------------------
# 规则引擎（伪AI）
# -----------------------------
def _build_reply(text: str) -> Tuple[str, Dict[str, Any]]:
    raw = _safe_str(text).strip()
    low = raw.lower()

    # 轻量平台报价指令识别：xxx报价
    if raw.endswith("报价"):
        platform = raw[:-2].strip() or "目标平台"
        content = (
            f"已识别报价指令：{platform}报价\n"
            f"我会按当前会话已上传资料进行报价准备（资料校验 → 字段提取 → 平台请求预留）。\n"
            f"当前版本为规则引擎流程，平台实际报价接口可在 services 下按“一个平台一个文件”接入。"
        )
        meta = {
            "status": "success",
            "intent": "quote",
            "trace_id": _uuid()[:16],
            "actions": [
                {"type": "suggest", "label": "查看当前材料状态"},
                {"type": "suggest", "label": f"{platform}报价"},
            ],
        }
        return content, meta

    if ("查订单" in raw) or ("订单" in raw and "查" in raw):
        content = (
            "已识别为订单查询意图。\n"
            "当前演示版先完成会话与规则链路打通；下一步可接入订单查询服务并返回结构化结果。"
        )
        meta = {
            "status": "success",
            "intent": "query_order",
            "trace_id": _uuid()[:16],
            "actions": [
                {"type": "suggest", "label": "订单备注是什么意思"},
                {"type": "suggest", "label": "太平洋报价"},
            ],
        }
        return content, meta

    if "订单备注" in raw:
        content = "订单备注（remark）是订单侧备注字段，用于展示与沟通说明；按你既定规则，导出不包含该字段。"
        meta = {
            "status": "success",
            "intent": "explain_field",
            "trace_id": _uuid()[:16],
        }
        return content, meta

    if raw in {"查看当前材料状态", "材料状态"}:
        content = (
            "当前为会话规则引擎演示版：材料上传与会话消息已打通。\n"
            "如需展示“各槽位图片数量 / OCR完成状态 / 可报价平台列表”，可在下一步把材料汇总接口补上。"
        )
        meta = {"status": "success", "intent": "material_status", "trace_id": _uuid()[:16]}
        return content, meta

    # 默认回复
    content = (
        "已收到指令。\n"
        "这是报价助手（规则引擎版），当前支持基础意图识别与会话记录。\n"
        "你可以输入“太平洋报价”这类指令来触发报价流程。"
    )
    meta = {
        "status": "success",
        "intent": "chat",
        "trace_id": _uuid()[:16],
        "actions": [
            {"type": "suggest", "label": "太平洋报价"},
            {"type": "suggest", "label": "查订单"},
        ],
    }
    return content, meta


# -----------------------------
# 对外导出（供 API 调用）
# -----------------------------
def get_or_create_session(*, owner_user_id: str, session_id: Optional[str] = None, title: Optional[str] = None) -> Dict[str, Any]:
    return _store.get_or_create_session(owner_user_id=owner_user_id, session_id=session_id, title=title)


def create_session(*, owner_user_id: str, title: Optional[str] = None) -> Dict[str, Any]:
    return _store.create_session(owner_user_id=owner_user_id, title=title)


def list_sessions(*, owner_user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    return _store.list_sessions(owner_user_id=owner_user_id, limit=limit)


def delete_session(*, owner_user_id: str, session_id: str) -> bool:
    return _store.delete_session(owner_user_id=owner_user_id, session_id=session_id)


def list_session_messages(
    *,
    owner_user_id: str,
    session_id: str,
    cursor: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    return _store.list_messages(owner_user_id=owner_user_id, session_id=session_id, cursor=cursor, limit=limit)


def send_session_message(
    *,
    owner_user_id: str,
    session_id: str,
    text: str,
    client_msg_id: Optional[str] = None,
    page_context: Optional[Dict[str, Any]] = None,
    use_stream: bool = False,
) -> Dict[str, Any]:
    # 先落用户消息
    user_msg = _store.append_message(
        owner_user_id=owner_user_id,
        session_id=session_id,
        role="user",
        content=text,
        metadata={
            "status": "success",
            "intent": "user_input",
            "client_msg_id": client_msg_id,
            "page_context": page_context or {},
        },
    )

    # 规则引擎回复
    reply_text, reply_meta = _build_reply(text)
    assistant_msg = _store.append_message(
        owner_user_id=owner_user_id,
        session_id=session_id,
        role="assistant",
        content=reply_text,
        metadata=reply_meta,
    )

    return {
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        "stream": None,  # 预留
    }


# ---- 兼容旧命名（你项目里 ai_assistant.py 可能用这些） ----
def get_session_messages(*, owner_user_id: str, session_id: str, cursor: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    return list_session_messages(owner_user_id=owner_user_id, session_id=session_id, cursor=cursor, limit=limit)


def send_message(*, owner_user_id: str, session_id: str, text: str, client_msg_id: Optional[str] = None, page_context: Optional[Dict[str, Any]] = None, use_stream: bool = False) -> Dict[str, Any]:
    return send_session_message(
        owner_user_id=owner_user_id,
        session_id=session_id,
        text=text,
        client_msg_id=client_msg_id,
        page_context=page_context,
        use_stream=use_stream,
    )
