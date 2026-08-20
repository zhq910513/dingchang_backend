# -*- coding: utf-8 -*-
"""Per-request chat-session lock binding for long platform IO.

The HTTP chat endpoint holds an asyncio session lock while handling a message.
Quote platform calls may take tens of seconds; keeping that lock across IO
blocks later messages (except dedicated interrupt writers). Callers bind the
lock when acquired, then wrap platform IO with
``release_chat_session_lock_for_platform_io`` so the lock is released for the
duration of the network call and re-acquired afterward.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from typing import AsyncIterator, Optional

_held_chat_session_lock: ContextVar[Optional[asyncio.Lock]] = ContextVar(
    "held_chat_session_lock",
    default=None,
)
_chat_session_lock_release_depth: ContextVar[int] = ContextVar(
    "chat_session_lock_release_depth",
    default=0,
)


def bind_chat_session_lock(lock: Optional[asyncio.Lock]) -> Token:
    """Remember the asyncio lock owned by the current chat request task."""
    return _held_chat_session_lock.set(lock)


def reset_chat_session_lock(token: Token) -> None:
    _held_chat_session_lock.reset(token)


def current_chat_session_lock() -> Optional[asyncio.Lock]:
    return _held_chat_session_lock.get()


@asynccontextmanager
async def release_chat_session_lock_for_platform_io() -> AsyncIterator[None]:
    """Temporarily release the bound chat lock around platform network IO.

    Re-entrant: nested releases only re-acquire when the outermost scope exits.
    No-op when no lock is bound (tests, account login outside chat, etc.).
    """
    lock = _held_chat_session_lock.get()
    if lock is None:
        yield
        return

    depth = int(_chat_session_lock_release_depth.get() or 0)
    if depth > 0:
        _chat_session_lock_release_depth.set(depth + 1)
        try:
            yield
        finally:
            _chat_session_lock_release_depth.set(depth)
        return

    owned = lock.locked()
    if owned:
        lock.release()
    _chat_session_lock_release_depth.set(1)
    try:
        yield
    finally:
        _chat_session_lock_release_depth.set(0)
        if owned:
            await lock.acquire()
