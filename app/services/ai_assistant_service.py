# app/services/ai_assistant_service.py
# encoding: utf-8
from __future__ import annotations

import json
import os
import re
import time
import uuid
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional, Callable, Tuple


# =========================
# 配置
# =========================

def _now_dt() -> datetime:
    return datetime.now()


def _now_str() -> str:
    # 统一北京时间展示格式（你的全局习惯）
    return _now_dt().strftime("%Y-%m-%d %H:%M:%S")


def _env_bool(name: str, default: bool = False) -> bool:
    v = str(os.getenv(name, "")).strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


AI_ENABLED = _env_bool("AI_ASSISTANT_ENABLED", True)
AI_DEBUG = _env_bool("AI_ASSISTANT_DEBUG", False)
AI_INPUT_MAX_LEN = int(os.getenv("AI_ASSISTANT_INPUT_MAX_LEN", "1000") or 1000)

# 会话保留（内存版）
AI_SESSION_MAX_MESSAGES = int(os.getenv("AI_ASSISTANT_SESSION_MAX_MESSAGES", "50") or 50)
AI_SESSION_TTL_MINUTES = int(os.getenv("AI_ASSISTANT_SESSION_TTL_MINUTES", "30") or 30)

# 是否在回复里暴露调试信息（生产建议 false）
AI_EXPOSE_RULE_DEBUG = _env_bool("AI_ASSISTANT_EXPOSE_RULE_DEBUG", False)


# =========================
# 异常
# =========================

class AiProviderError(Exception):
    """兼容路由层原命名，现用于伪AI服务错误"""
    def __init__(self, message: str, code: str = "INTERNAL_ERROR"):
        super().__init__(message)
        self.code = code


# =========================
# 日志（轻量版）
# =========================

def _mask_text(s: str, max_len: int = 200) -> str:
    text = str(s or "")
    # 简单脱敏：手机号
    text = re.sub(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)", r"\1****\2", text)
    # 身份证（18位）
    text = re.sub(r"(?<!\w)(\d{6})\d{8}([0-9Xx]{4})(?!\w)", r"\1********\2", text)
    # 截断
    if len(text) > max_len:
        return text[:max_len] + "...(truncated)"
    return text


def _log_event(event: str, **kwargs) -> None:
    payload = {
        "ts": _now_str(),
        "event": event,
        **kwargs,
    }
    try:
        print("[AI_ASSISTANT]", json.dumps(payload, ensure_ascii=False))
    except Exception:
        print("[AI_ASSISTANT]", payload)


# =========================
# 内存会话存储（线程安全）
# =========================

@dataclass
class SessionMessage:
    role: str
    content: str
    name: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: str = field(default_factory=_now_str)


@dataclass
class SessionState:
    session_id: str
    owner_user_id: int
    title: str
    created_at: str
    updated_at: str
    messages: List[SessionMessage] = field(default_factory=list)

    def append(self, role: str, content: str, name: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        self.messages.append(SessionMessage(role=role, content=content, name=name, metadata=metadata))
        self.updated_at = _now_str()
        self._trim()

    def _trim(self) -> None:
        if len(self.messages) <= AI_SESSION_MAX_MESSAGES:
            return
        systems = [m for m in self.messages if m.role == "system"][:1]
        others = [m for m in self.messages if m.role != "system"]
        keep_n = max(0, AI_SESSION_MAX_MESSAGES - len(systems))
        self.messages = systems + others[-keep_n:]


class InMemorySessionStore:
    def __init__(self):
        self._data: Dict[str, SessionState] = {}
        self._lock = threading.RLock()

    def _purge_expired(self) -> None:
        # 轻量 TTL 清理（按 updated_at 字符串解析）
        now = _now_dt()
        expired_ids: List[str] = []
        for sid, st in self._data.items():
            try:
                dt = datetime.strptime(st.updated_at, "%Y-%m-%d %H:%M:%S")
                delta_min = (now - dt).total_seconds() / 60.0
                if delta_min > AI_SESSION_TTL_MINUTES:
                    expired_ids.append(sid)
            except Exception:
                continue
        for sid in expired_ids:
            self._data.pop(sid, None)

    def create(self, *, owner_user_id: int, title: str) -> SessionState:
        sid = uuid.uuid4().hex
        now = _now_str()
        st = SessionState(session_id=sid, owner_user_id=owner_user_id, title=title, created_at=now, updated_at=now)
        self._data[sid] = st
        return st

    def get(self, session_id: str, owner_user_id: int) -> Optional[SessionState]:
        with self._lock:
            self._purge_expired()
            st = self._data.get(str(session_id or "").strip())
            if not st:
                return None
            if int(st.owner_user_id or 0) != int(owner_user_id or 0):
                return None
            return st

    def get_or_create(self, session_id: Optional[str], owner_user_id: int, title: str) -> SessionState:
        with self._lock:
            self._purge_expired()
            sid = str(session_id or "").strip()
            if sid:
                st = self._data.get(sid)
                if st and int(st.owner_user_id or 0) == int(owner_user_id or 0):
                    return st
            return self.create(owner_user_id=owner_user_id, title=title)

    def list_sessions(self, owner_user_id: int) -> List[SessionState]:
        with self._lock:
            self._purge_expired()
            arr = [x for x in self._data.values() if int(x.owner_user_id or 0) == int(owner_user_id or 0)]
            arr.sort(key=lambda x: x.updated_at, reverse=True)
            return arr

    def delete(self, session_id: str, owner_user_id: int) -> bool:
        with self._lock:
            sid = str(session_id or "").strip()
            st = self._data.get(sid)
            if not st:
                return False
            if int(st.owner_user_id or 0) != int(owner_user_id or 0):
                return False
            del self._data[sid]
            return True


_SESSION_STORE = InMemorySessionStore()


# =========================
# 平台报价服务接入预留（你后续一个平台一个文件）
# =========================

# 你后续可以在 app/services/ai_platforms/ 下做：
# - platform_a_quote.py
# - platform_b_quote.py
# 每个文件导出 quote(platform_code, context, user_info) -> dict
_PLATFORM_QUOTE_HANDLERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = {}


def register_platform_quote_handler(platform_code: str, handler: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]) -> None:
    k = str(platform_code or "").strip().lower()
    if not k:
        return
    _PLATFORM_QUOTE_HANDLERS[k] = handler


# =========================
# 规则引擎（伪AI）
# =========================

@dataclass
class RuleResult:
    intent: str
    confidence: float
    reply: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    rule_name: str = "fallback"


@dataclass
class RuleDef:
    name: str
    intent: str
    priority: int
    matcher: Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]
    handler: Callable[[str, Dict[str, Any], Dict[str, Any]], RuleResult]


def _contains_all(text: str, words: List[str]) -> bool:
    t = str(text or "").lower()
    return all(str(w).lower() in t for w in words)


def _contains_any(text: str, words: List[str]) -> bool:
    t = str(text or "").lower()
    return any(str(w).lower() in t for w in words)


def _extract_platform_quote(text: str) -> Optional[str]:
    s = str(text or "").strip()
    if not s:
        return None
    # 平台A 报价 / 平台A报价 / A平台 报价
    m1 = re.search(r"平台\s*([A-Za-z0-9_\-\u4e00-\u9fa5]+)\s*报价", s)
    if m1 and m1.group(1):
        return f"platform_{m1.group(1).strip()}"
    m2 = re.search(r"([A-Za-z0-9_\-\u4e00-\u9fa5]+)\s*平台\s*报价", s)
    if m2 and m2.group(1):
        return f"platform_{m2.group(1).strip()}"
    # 兜底：xxx报价
    m3 = re.match(r"^(.+?)\s*报价$", s)
    if m3 and m3.group(1):
        return f"platform_{m3.group(1).strip()}"
    return None


def _reply_greeting(text: str, slots: Dict[str, Any], ctx: Dict[str, Any]) -> RuleResult:
    user_name = (ctx.get("user_info") or {}).get("username") or "你"
    return RuleResult(
        intent="greeting",
        confidence=0.98,
        rule_name="greeting_exact_or_keyword",
        reply=f"{user_name}，我在。你可以直接说“查订单”“查财务”“某字段是什么意思”，或者发“平台A 报价”。",
        actions=[
            {"type": "suggest", "label": "查订单"},
            {"type": "suggest", "label": "查财务"},
            {"type": "suggest", "label": "平台A 报价"},
        ],
    )


def _reply_help(text: str, slots: Dict[str, Any], ctx: Dict[str, Any]) -> RuleResult:
    return RuleResult(
        intent="help",
        confidence=0.95,
        rule_name="help_keywords",
        reply=(
            "我目前是伪AI助手（规则引擎），主要支持：\n"
            "1) 操作指引（怎么做）\n"
            "2) 字段说明（字段是什么意思）\n"
            "3) 页面导航建议（去哪一页）\n"
            "4) 平台报价指令识别（报价服务后续接入）\n\n"
            "说明：我不会假装执行未实现的写入操作。"
        ),
        actions=[
            {"type": "suggest", "label": "订单备注是什么意思"},
            {"type": "suggest", "label": "怎么查看财务列表"},
            {"type": "suggest", "label": "平台A 报价"},
        ],
    )


def _reply_order_query(text: str, slots: Dict[str, Any], ctx: Dict[str, Any]) -> RuleResult:
    return RuleResult(
        intent="query_orders",
        confidence=0.88,
        rule_name="orders_keywords",
        reply="你是在问订单相关操作。我目前可以做规则式指引：建议进入“订单列表”页，再按日期/状态筛选。若你告诉我想查“今日新增/某状态/某销售”，我可以给更具体步骤。",
        actions=[
            {"type": "navigate", "target": "/orders", "label": "打开订单列表"},
            {"type": "suggest", "label": "查今日新增订单"},
        ],
    )


def _reply_finance_query(text: str, slots: Dict[str, Any], ctx: Dict[str, Any]) -> RuleResult:
    return RuleResult(
        intent="query_finance",
        confidence=0.88,
        rule_name="finance_keywords",
        reply="你是在问财务相关内容。建议进入“财务列表”页查看应收/应付、回款、返点状态。你也可以直接问我某个字段口径（比如应收、应付、返点状态）。",
        actions=[
            {"type": "navigate", "target": "/finance", "label": "打开财务列表"},
            {"type": "suggest", "label": "应收应付字段口径"},
        ],
    )


def _reply_field_explain(text: str, slots: Dict[str, Any], ctx: Dict[str, Any]) -> RuleResult:
    # 先做几项高频字段示例，后续可扩配置表
    field_map = {
        "remark": "订单备注字段（remark），用于展示订单补充说明。按你的项目约定：列表可展示，导出不包含。",
        "订单备注": "订单备注字段（remark），用于展示订单补充说明。按你的项目约定：列表可展示，导出不包含。",
        "应收": "应收口径通常对应 customer_total（客户应收）。实际以后端返回字段为准。",
        "应付": "应付口径通常对应 channel_total（渠道应付）。实际以后端返回字段为准。",
        "vin": "VIN 是车辆识别代号（车架号），通常用于车辆唯一识别与查验。",
    }
    hit_key = None
    for k in field_map.keys():
        if k.lower() in str(text or "").lower():
            hit_key = k
            break

    if hit_key:
        reply = field_map[hit_key]
        conf = 0.92
    else:
        reply = "你是在问字段口径。我目前支持常见字段说明（如 remark/订单备注、应收、应付、VIN 等）。你可以直接发字段名，我按规则给你解释。"
        conf = 0.70

    return RuleResult(
        intent="field_explain",
        confidence=conf,
        rule_name="field_explain_keywords",
        reply=reply,
        actions=[{"type": "suggest", "label": "订单备注"}, {"type": "suggest", "label": "应收"}, {"type": "suggest", "label": "VIN"}],
    )


def _reply_quote_command(text: str, slots: Dict[str, Any], ctx: Dict[str, Any]) -> RuleResult:
    platform_code = slots.get("platform_code") or "platform_default"
    platform_name = platform_code.replace("platform_", "平台")

    # 若后续接入真实平台服务，这里直接路由
    handler = _PLATFORM_QUOTE_HANDLERS.get(platform_code.lower())
    if handler:
        try:
            out = handler(ctx, ctx.get("user_info") or {})
            return RuleResult(
                intent="quote_request",
                confidence=0.95,
                rule_name="quote_handler_registered",
                reply=str(out.get("reply") or f"{platform_name} 报价已完成"),
                actions=list(out.get("actions") or []),
            )
        except Exception as e:
            return RuleResult(
                intent="quote_request",
                confidence=0.60,
                rule_name="quote_handler_error",
                reply=f"已识别为 {platform_name} 报价指令，但平台服务执行异常：{str(e) or e.__class__.__name__}",
                actions=[],
            )

    # 当前占位行为（不假装成功）
    return RuleResult(
        intent="quote_request",
        confidence=0.90,
        rule_name="quote_command_parsed",
        reply=(
            f"已识别到 {platform_name} 报价指令。"
            "当前平台报价服务还未接入（你后续会按“一个平台一个服务文件”实现），"
            "我现在只能完成指令识别与流程引导，暂不能返回真实报价。"
        ),
        actions=[
            {"type": "suggest", "label": "查看报价接入清单"},
            {"type": "suggest", "label": "继续上传材料"},
        ],
    )


def _reply_fallback(text: str, slots: Dict[str, Any], ctx: Dict[str, Any]) -> RuleResult:
    return RuleResult(
        intent="fallback",
        confidence=0.20,
        rule_name="fallback_default",
        reply=(
            "我没命中明确规则。你可以试试这些表达：\n"
            "• 查订单\n"
            "• 查财务\n"
            "• 订单备注是什么意思\n"
            "• 平台A 报价"
        ),
        actions=[
            {"type": "suggest", "label": "查订单"},
            {"type": "suggest", "label": "查财务"},
            {"type": "suggest", "label": "订单备注是什么意思"},
            {"type": "suggest", "label": "平台A 报价"},
        ],
    )


def _match_greeting(text: str, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = str(text or "").strip().lower()
    if s in {"你好", "您好", "hi", "hello", "在吗", "在不在"}:
        return {}
    if _contains_any(s, ["你好", "您好", "help", "帮助"]):
        return {}
    return None


def _match_help(text: str, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if _contains_any(text, ["帮助", "能做什么", "你会什么", "怎么用"]):
        return {}
    return None


def _match_quote(text: str, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    platform_code = _extract_platform_quote(text)
    if platform_code:
        return {"platform_code": platform_code}
    return None


def _match_orders(text: str, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if _contains_any(text, ["订单", "order"]):
        return {}
    return None


def _match_finance(text: str, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if _contains_any(text, ["财务", "应收", "应付", "回款", "返点"]):
        return {}
    return None


def _match_field_explain(text: str, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if _contains_any(text, ["字段", "什么意思", "口径", "remark", "vin", "订单备注", "应收", "应付"]):
        return {}
    return None


RULES: List[RuleDef] = [
    RuleDef("greeting", "greeting", 100, _match_greeting, _reply_greeting),
    RuleDef("help", "help", 90, _match_help, _reply_help),
    RuleDef("quote", "quote_request", 85, _match_quote, _reply_quote_command),
    RuleDef("field_explain", "field_explain", 80, _match_field_explain, _reply_field_explain),
    RuleDef("orders", "query_orders", 70, _match_orders, _reply_order_query),
    RuleDef("finance", "query_finance", 70, _match_finance, _reply_finance_query),
]


def _run_rules(user_message: str, *, user_info: Dict[str, Any], page_context: Dict[str, Any]) -> RuleResult:
    ctx = {
        "user_info": user_info or {},
        "page_context": page_context or {},
        "time_policy": {
            "timezone": "Asia/Shanghai",
            "display_date_format": "YYYY-MM-DD",
        },
    }

    for rule in sorted(RULES, key=lambda x: x.priority, reverse=True):
        slots = rule.matcher(user_message, ctx)
        if slots is None:
            continue
        result = rule.handler(user_message, slots, ctx)
        if AI_EXPOSE_RULE_DEBUG:
            result.reply += f"\n\n[debug] rule={result.rule_name}"
        return result

    return _reply_fallback(user_message, {}, ctx)


# =========================
# 对外服务方法
# =========================

def _make_title_from_user_msg(msg: str) -> str:
    s = str(msg or "").strip().replace("\n", " ")
    if not s:
        return "新会话"
    return s[:24]


def _validate_input(user_message: str) -> str:
    if not AI_ENABLED:
        raise AiProviderError("AI助手已关闭", code="DISABLED")

    if user_message is None:
        raise AiProviderError("message 不能为空", code="INVALID_INPUT")

    msg = str(user_message).strip()
    if not msg:
        raise AiProviderError("message 不能为空", code="INVALID_INPUT")

    if len(msg) > AI_INPUT_MAX_LEN:
        raise AiProviderError(f"message 过长，最大 {AI_INPUT_MAX_LEN} 字符", code="TOO_LONG")

    return msg


def chat_once(
    *,
    session_id: Optional[str],
    user_message: str,
    history: List[Dict[str, Any]],  # 预留，当前规则引擎不依赖前端传 history
    system_prompt_override: Optional[str],  # 兼容旧签名，忽略
    model: Optional[str],  # 兼容旧签名，伪AI固定为 pseudo-rule-engine
    temperature: float,  # 兼容旧签名，忽略
    max_tokens: int,  # 兼容旧签名，忽略
    user_info: Dict[str, Any],
    page_context: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    _ = history
    _ = system_prompt_override
    _ = temperature
    _ = max_tokens

    trace_id = uuid.uuid4().hex
    t0 = time.time()

    msg = _validate_input(user_message)
    uid = int(user_info.get("user_id") or 0)

    st = _SESSION_STORE.get_or_create(
        session_id=session_id,
        owner_user_id=uid,
        title=_make_title_from_user_msg(msg),
    )

    # 会话里保留一个 system 提示（仅展示/审计）
    if not any(m.role == "system" for m in st.messages):
        st.append(
            "system",
            "伪AI助手（规则引擎）会话已创建。说明：当前不调用外部大模型，按规则匹配并返回可解释结果。",
            metadata={"trace_id": trace_id},
        )

    # 规则执行
    rule_result = _run_rules(
        msg,
        user_info=user_info or {},
        page_context=page_context or {},
    )

    cost_ms = int((time.time() - t0) * 1000)

    st.append("user", msg, metadata={"trace_id": trace_id})
    st.append(
        "assistant",
        rule_result.reply,
        metadata={
            "trace_id": trace_id,
            "intent": rule_result.intent,
            "confidence": rule_result.confidence,
            "rule_name": rule_result.rule_name,
            "actions": rule_result.actions,
            "cost_ms": cost_ms,
            "model": "pseudo-rule-engine",
        },
    )

    _log_event(
        "chat_once",
        trace_id=trace_id,
        session_id=st.session_id,
        user_id=uid,
        role_name=user_info.get("role_name"),
        input=_mask_text(msg),
        intent=rule_result.intent,
        rule_name=rule_result.rule_name,
        response_type="normal" if rule_result.intent != "fallback" else "fallback",
        cost_ms=cost_ms,
    )

    return {
        "ok": True,
        "session_id": st.session_id,
        "reply": rule_result.reply,
        "model": "pseudo-rule-engine",
        "intent": rule_result.intent,
        "confidence": float(rule_result.confidence),
        "actions": rule_result.actions,
        "trace_id": trace_id,
        "usage": {"cost_ms": cost_ms},
    }


def prepare_stream_session(
    *,
    session_id: Optional[str],
    user_message: str,
    history: List[Dict[str, Any]],
    system_prompt_override: Optional[str],
    model: Optional[str],
    temperature: float,
    max_tokens: int,
    user_info: Dict[str, Any],
    page_context: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    伪AI的流式：先算出完整回复，再按字符块切片输出（SSE）
    """
    _ = history
    _ = system_prompt_override
    _ = model
    _ = temperature
    _ = max_tokens

    trace_id = uuid.uuid4().hex
    msg = _validate_input(user_message)
    uid = int(user_info.get("user_id") or 0)

    st = _SESSION_STORE.get_or_create(
        session_id=session_id,
        owner_user_id=uid,
        title=_make_title_from_user_msg(msg),
    )

    if not any(m.role == "system" for m in st.messages):
        st.append(
            "system",
            "伪AI助手（规则引擎）会话已创建。说明：当前不调用外部大模型，按规则匹配并返回可解释结果。",
            metadata={"trace_id": trace_id},
        )

    t0 = time.time()
    rule_result = _run_rules(msg, user_info=user_info or {}, page_context=page_context or {})
    cost_ms = int((time.time() - t0) * 1000)

    return {
        "session": st,
        "trace_id": trace_id,
        "reply": rule_result.reply,
        "intent": rule_result.intent,
        "confidence": float(rule_result.confidence),
        "actions": rule_result.actions,
        "rule_name": rule_result.rule_name,
        "cost_ms": cost_ms,
        "user_message": msg,
        "user_info": user_info,
    }


def stream_chat_completion_sse(
    *,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> Generator[str, None, None]:
    """
    保留旧函数名以兼容路由导入，但伪AI不再使用外部 provider。
    这里仅提供一个兼容空实现（路由已改为直接用 prepare_stream_session + 本地切片）。
    """
    _ = model
    _ = messages
    _ = temperature
    _ = max_tokens
    yield "data: [DONE]\n\n"


def finalize_stream_reply(
    *,
    session: SessionState,
    user_message: str,
    assistant_reply: str,
    model: str,
    cost_ms: int,
    trace_id: Optional[str] = None,
    intent: str = "fallback",
    confidence: float = 0.0,
    actions: Optional[List[Dict[str, Any]]] = None,
    rule_name: Optional[str] = None,
    user_info: Optional[Dict[str, Any]] = None,
) -> None:
    if str(user_message or "").strip():
        session.append("user", user_message, metadata={"trace_id": trace_id})
    if str(assistant_reply or "").strip():
        session.append(
            "assistant",
            assistant_reply,
            metadata={
                "trace_id": trace_id,
                "intent": intent,
                "confidence": confidence,
                "actions": actions or [],
                "rule_name": rule_name,
                "cost_ms": cost_ms,
                "model": model,
            },
        )

    _log_event(
        "chat_stream_finalize",
        trace_id=trace_id,
        session_id=session.session_id,
        user_id=int((user_info or {}).get("user_id") or 0),
        role_name=(user_info or {}).get("role_name"),
        input=_mask_text(user_message),
        intent=intent,
        rule_name=rule_name,
        response_type="normal" if intent != "fallback" else "fallback",
        cost_ms=cost_ms,
    )


def list_sessions(*, owner_user_id: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for s in _SESSION_STORE.list_sessions(owner_user_id=owner_user_id):
        last_preview = None
        for m in reversed(s.messages):
            if m.role == "assistant" and (m.content or "").strip():
                last_preview = (m.content or "").strip()[:80]
                break
        out.append(
            {
                "session_id": s.session_id,
                "title": s.title,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "last_message_preview": last_preview,
                "message_count": len(s.messages),
            }
        )
    return out


def get_history(*, session_id: str, owner_user_id: int) -> List[Dict[str, Any]]:
    st = _SESSION_STORE.get(session_id, owner_user_id=owner_user_id)
    if not st:
        return []
    items: List[Dict[str, Any]] = []
    for m in st.messages:
        items.append(
            {
                "role": m.role,
                "content": m.content,
                "name": m.name,
                "metadata": m.metadata or None,
                "created_at": m.created_at,
            }
        )
    return items


def delete_session(*, session_id: str, owner_user_id: int) -> bool:
    return _SESSION_STORE.delete(session_id, owner_user_id=owner_user_id)
