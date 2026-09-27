"""Async client for the OpenCode HTTP API plus a global event (SSE) bus."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

# SSEParser lives in the native engine (cpp/sse.cpp). It stays importable from
# this module because bot.runner and the test suite reference bot.oc.SSEParser.
from ._engine import SSEParser  # noqa: F401

log = logging.getLogger("oc")

Event = dict[str, Any]


class OpenCodeError(RuntimeError):
    """An error returned by the OpenCode service."""

    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message
        self.payload = payload

    @property
    def not_found(self) -> bool:
        return self.status == 404

    @property
    def conflict(self) -> bool:
        return self.status == 409


# SSEParser moved to the native engine. The port is covered by
# tests/parser_parity.py, which runs the same corpus through this module's
# re-export and the original Python implementation.


class OpenCodeClient:
    def __init__(self, url: str, username: str = "opencode", password: str = "", timeout: float = 60.0):
        self.base = url.rstrip("/")
        self._auth = aiohttp.BasicAuth(username, password) if password else None
        self._timeout = aiohttp.ClientTimeout(total=timeout, sock_read=timeout)
        # The event stream is long lived: it needs no overall deadline, only a
        # read timeout so a dead connection is noticed.
        self._stream_timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=90)
        self._session: aiohttp.ClientSession | None = None
        self._subscribers: set[_Subscription] = set()
        self._event_task: asyncio.Task[None] | None = None
        self._closing = False
        # Exposed for diagnostics: how big the biggest event payload has been.
        self.parser = SSEParser()
        self.reconnects = 0

    # ------------------------------------------------------------------ setup

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(auth=self._auth, timeout=self._timeout)
        self._closing = False
        info = await self.request("GET", "/api/info")
        log.info("connected to opencode %s at %s", (info or {}).get("version"), self.base)
        self._event_task = asyncio.create_task(self._event_loop(), name="opencode-events")

    async def close(self) -> None:
        self._closing = True
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._event_task = None
        for sub in list(self._subscribers):
            sub.close()
        self._subscribers.clear()
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------- low level

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._session is None:
            raise RuntimeError("client not started")
        url = f"{self.base}{path}"
        async with self._session.request(method, url, **kwargs) as resp:
            text = await resp.text()
            if resp.status >= 400:
                message = text[:400]
                payload: Any = None
                try:
                    payload = json.loads(text)
                    if isinstance(payload, dict):
                        message = str(payload.get("message") or payload.get("error") or message)
                except ValueError:
                    pass
                raise OpenCodeError(resp.status, message, payload)
            if not text:
                return None
            try:
                return json.loads(text)
            except ValueError:
                return text

    async def get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return await self.request("GET", path, params=clean or None)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self.request("POST", path, json=body if body is not None else {})

    async def patch(self, path: str, body: Any) -> Any:
        return await self.request("PATCH", path, json=body)

    # --------------------------------------------------------------- sessions

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        directory: str | None = None,
        search: str | None = None,
        order: str = "desc",
    ) -> tuple[list[dict], dict]:
        data = await self.get(
            "/api/session",
            limit=str(limit),
            cursor=cursor,
            directory=directory,
            search=search,
            order=order,
        )
        return (data or {}).get("data", []), (data or {}).get("cursor") or {}

    async def create_session(
        self,
        *,
        title: str | None = None,
        agent: str | None = None,
        model: dict | None = None,
        directory: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = model
        if directory:
            body["location"] = {"directory": directory}
        return (await self.post("/api/session", body))["data"]

    async def get_session(self, session_id: str) -> dict:
        return (await self.get(f"/api/session/{session_id}"))["data"]

    async def update_session(self, session_id: str, *, title: str | None = None) -> dict | None:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if not body:
            return None
        return (await self.patch(f"/api/session/{session_id}", body) or {}).get("data")

    async def delete_session(self, session_id: str) -> None:
        await self.request("DELETE", f"/api/session/{session_id}")

    async def fork_session(self, session_id: str, before: str | None = None) -> dict:
        return (await self.post(f"/api/session/{session_id}/fork", {"before": before}))["data"]

    async def set_model(
        self, session_id: str, provider_id: str, model_id: str, variant: str | None = None
    ) -> None:
        ref: dict[str, Any] = {"providerID": provider_id, "id": model_id}
        if variant:
            ref["variant"] = variant
        await self.post(f"/api/session/{session_id}/model", {"model": ref})

    async def set_agent(self, session_id: str, agent: str) -> None:
        await self.post(f"/api/session/{session_id}/agent", {"agent": agent})

    # --------------------------------------------------------------- messages

    async def prompt(
        self,
        session_id: str,
        text: str,
        *,
        files: list[dict] | None = None,
        delivery: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"text": text}
        if files:
            body["files"] = files
        if delivery:
            body["delivery"] = delivery
        return (await self.post(f"/api/session/{session_id}/prompt", body))["data"]

    async def messages(self, session_id: str) -> list[dict]:
        return (await self.get(f"/api/session/{session_id}/message") or {}).get("data", [])

    async def message(self, session_id: str, message_id: str) -> dict:
        return (await self.get(f"/api/session/{session_id}/message/{message_id}"))["data"]

    async def interrupt(self, session_id: str) -> None:
        await self.post(f"/api/session/{session_id}/interrupt", {})

    async def compact(self, session_id: str) -> dict | None:
        return (await self.post(f"/api/session/{session_id}/compact", {}) or {}).get("data")

    async def stage_revert(self, session_id: str, message_id: str) -> None:
        await self.post(f"/api/session/{session_id}/revert/stage", {"messageID": message_id})

    async def clear_revert(self, session_id: str) -> None:
        await self.request("DELETE", f"/api/session/{session_id}/revert")

    async def permissions(self, session_id: str) -> list[dict]:
        return (await self.get(f"/api/session/{session_id}/permission") or {}).get("data", [])

    async def reply_permission(
        self, session_id: str, request_id: str, decision: str, message: str | None = None
    ) -> dict | None:
        body: dict[str, Any] = {"decision": decision}
        if message:
            body["message"] = message
        result = await self.post(f"/api/session/{session_id}/permission/{request_id}/reply", body)
        # The endpoint answers 204 with no body when it accepts the decision.
        return result.get("data") if isinstance(result, dict) else None

    # --------------------------------------------------------- config lookups

    async def models(self) -> list[dict]:
        return (await self.get("/api/model") or {}).get("data", [])

    async def agents(self) -> list[dict]:
        return (await self.get("/api/agent") or {}).get("data", [])

    async def active_sessions(self) -> dict[str, str]:
        """Map of session id -> state for sessions with a live execution."""
        data = (await self.get("/api/session/active") or {}).get("data", {})
        return {sid: str(value.get("type", "")) for sid, value in data.items() if isinstance(value, dict)}

    async def wait_idle(self, session_id: str, timeout: float = 120.0) -> bool:
        """Block until the session has no running execution (or the timeout)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if session_id not in await self.active_sessions():
                return True
            await asyncio.sleep(1.0)
        return False

    async def config(self) -> dict:
        return (await self.get("/api/config") or {}).get("data", {}) or {}

    async def default_model(self) -> dict | None:
        return (await self.get("/api/model/default") or {}).get("data")

    # ------------------------------------------------------------------ events

    async def _event_loop(self) -> None:
        backoff = 1.0
        while not self._closing:
            try:
                headers = {"Accept": "text/event-stream"}
                async with self._session.get(  # type: ignore[union-attr]
                    f"{self.base}/api/event", headers=headers, timeout=self._stream_timeout
                ) as resp:
                    if resp.status != 200:
                        raise OpenCodeError(resp.status, f"event stream returned {resp.status}")
                    backoff = 1.0
                    log.info("subscribed to /api/event")
                    async for chunk in resp.content.iter_any():
                        for event in self.parser.feed(chunk):
                            self._dispatch(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._closing:
                    return
                log.warning(
                    "event stream disconnected: %s: %s; retrying in %.1fs",
                    type(exc).__name__,
                    exc or repr(exc),
                    backoff,
                )
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _dispatch(self, event: Event) -> None:
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

    def push(self, event: Event) -> None:
        if self.session_id is not None and event.get("data", {}).get("sessionID") != self.session_id:
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
