"""Async client for the OpenCode HTTP API plus a global event (SSE) bus.

The transport, SSE framing and reconnect handling now live in the native engine
(``cpp/client.cpp``). The engine owns the stream thread and hands parsed events
to one bounded queue; the per-session fan-out below stays in Python because it is
asyncio-shaped.

``OpenCodeError`` is defined by the engine and re-exported here so that existing
``except OpenCodeError`` sites keep catching what the transport raises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ._engine import OpenCodeClient as _NativeClient
from ._engine import OpenCodeError, SSEParser  # noqa: F401

log = logging.getLogger("oc")

Event = dict[str, Any]


class _ParserStats:
    """Live view of the stream parser's diagnostics.

    The engine's parser is touched only by its stream thread, so these read the
    counters the thread publishes through atomics rather than the parser itself.
    """

    def __init__(self, native: _NativeClient) -> None:
        self._native = native

    @property
    def largest(self) -> int:
        return self._native.parser_largest

    @property
    def dropped(self) -> int:
        return self._native.parser_dropped


class OpenCodeClient:
    def __init__(
        self, url: str, username: str = "opencode", password: str = "", timeout: float = 60.0
    ) -> None:
        self._native = _NativeClient(url, username, password, timeout)
        self._subscribers: set[_Subscription] = set()
        self._pump_task: asyncio.Task[None] | None = None
        self._closing = False
        # Exposed for diagnostics: how big the biggest event payload has been.
        self.parser = _ParserStats(self._native)

    @property
    def base(self) -> str:
        return self._native.base

    @property
    def reconnects(self) -> int:
        return self._native.reconnects

    # ------------------------------------------------------------------ setup

    async def start(self) -> None:
        version = await asyncio.to_thread(self._native.start)
        log.info("connected to opencode %s at %s", version, self._native.base)
        self._closing = False
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump(), name="opencode-events")

    async def close(self) -> None:
        self._closing = True
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._pump_task = None
        for sub in list(self._subscribers):
            sub.close()
        self._subscribers.clear()
        await asyncio.to_thread(self._native.close)

    # --------------------------------------------------------------- sessions

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self._native, name)
        return await asyncio.to_thread(lambda: method(*args, **kwargs))

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        directory: str | None = None,
        search: str | None = None,
        order: str = "desc",
    ) -> tuple[list[dict], dict]:
        return await self._call(
            "list_sessions",
            limit=limit,
            cursor=cursor,
            directory=directory,
            search=search,
            order=order,
        )

    async def create_session(
        self,
        *,
        title: str | None = None,
        agent: str | None = None,
        model: dict | None = None,
        directory: str | None = None,
    ) -> dict:
        return await self._call(
            "create_session", title=title, agent=agent, model=model, directory=directory
        )

    async def get_session(self, session_id: str) -> dict:
        return await self._call("get_session", session_id)

    async def update_session(self, session_id: str, *, title: str | None = None) -> dict | None:
        return await self._call("update_session", session_id, title)

    async def delete_session(self, session_id: str) -> None:
        await self._call("delete_session", session_id)

    async def fork_session(self, session_id: str, before: str | None = None) -> dict:
        return await self._call("fork_session", session_id, before)

    async def set_model(
        self, session_id: str, provider_id: str, model_id: str, variant: str | None = None
    ) -> None:
        await self._call("set_model", session_id, provider_id, model_id, variant)

    async def set_agent(self, session_id: str, agent: str) -> None:
        await self._call("set_agent", session_id, agent)

    # ---------------------------------------------------------------- messages

    async def prompt(
        self,
        session_id: str,
        text: str,
        *,
        files: list[dict] | None = None,
        delivery: str | None = None,
    ) -> dict:
        return await self._call("prompt", session_id, text, files, delivery)

    async def messages(self, session_id: str) -> list[dict]:
        return await self._call("messages", session_id)

    async def message(self, session_id: str, message_id: str) -> dict:
        return await self._call("message", session_id, message_id)

    async def interrupt(self, session_id: str) -> None:
        await self._call("interrupt", session_id)

    async def compact(self, session_id: str) -> dict | None:
        return await self._call("compact", session_id)

    async def stage_revert(self, session_id: str, message_id: str) -> None:
        await self._call("stage_revert", session_id, message_id)

    async def clear_revert(self, session_id: str) -> None:
        await self._call("clear_revert", session_id)

    async def permissions(self, session_id: str) -> list[dict]:
        return await self._call("permissions", session_id)

    async def reply_permission(
        self, session_id: str, request_id: str, decision: str, message: str | None = None
    ) -> dict | None:
        return await self._call("reply_permission", session_id, request_id, decision, message)

    async def forms(self, session_id: str) -> list[dict]:
        return await self._call("forms", session_id)

    async def get_form(self, session_id: str, form_id: str) -> dict:
        return await self._call("get_form", session_id, form_id)

    async def reply_form(self, session_id: str, form_id: str, answer: dict) -> None:
        await self._call("reply_form", session_id, form_id, answer)

    async def cancel_form(self, session_id: str, form_id: str) -> None:
        await self._call("cancel_form", session_id, form_id)

    # ---------------------------------------------------------- config lookups

    async def models(self) -> list[dict]:
        return await self._call("models")

    async def agents(self) -> list[dict]:
        return await self._call("agents")

    async def active_sessions(self) -> dict[str, str]:
        """Map of session id -> state for sessions with a live execution."""
        return await self._call("active_sessions")

    async def wait_idle(self, session_id: str, timeout: float = 120.0) -> bool:
        """Block until the session has no running execution (or the timeout)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if session_id not in await self.active_sessions():
                return True
            await asyncio.sleep(1.0)
        return False

    async def config(self) -> dict:
        return await self._call("config")

    async def default_model(self) -> dict | None:
        return await self._call("default_model")

    # ------------------------------------------------------------------ events

    async def _pump(self) -> None:
        """Drain the engine's event queue into the per-session subscriptions."""
        while not self._closing:
            events = await asyncio.to_thread(self._native.poll, 256, 250)
            for event in events:
                for sub in list(self._subscribers):
                    sub.push(event)

    def subscribe(self, session_id: str | None = None) -> "_Subscription":
        sub = _Subscription(self, session_id)
        self._subscribers.add(sub)
        return sub


class _Subscription:
    """A bounded async queue of events, optionally filtered to one session."""

    def __init__(self, client: OpenCodeClient, session_id: str | None, maxsize: int = 4000) -> None:
        self._client = client
        self.session_id = session_id
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)
        self._dropped = 0
        self._closed = False

    def push(self, event: Event) -> None:
        if self._closed:
            return
        if self.session_id is not None:
            data = event.get("data") or {}
            session = data.get("sessionID")
            if session is None:
                # Form events nest the session id one level deeper than every
                # other event, under data.form.sessionID.
                session = (data.get("form") or {}).get("sessionID")
            if session != self.session_id:
                return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest event so a slow consumer cannot stall the bus.
            try:
                self._queue.get_nowait()
                self._dropped += 1
                self._queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    @property
    def dropped(self) -> int:
        return self._dropped

    def close(self) -> None:
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def __aiter__(self) -> "_Subscription":
        return self

    async def __anext__(self) -> Event:
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event
