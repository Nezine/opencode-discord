"""Per-user conversation state machine.

One ``Conversation`` per Discord user owns the active session, the running turn
and every command that mutates them. Plain DM text goes straight to the model;
slash commands drive the same object.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from typing import TYPE_CHECKING

from .messaging import LiveMessage, Messenger
from .oc import OpenCodeClient, OpenCodeError
from .store import UserState
from ._engine import PromptQueue, transcript_entries
from .textutil import clip, duration, human_cost, human_tokens, split_text
from .turn import (
    TurnState,
    apply_event,
    hydrate_from_message,
    render_reasoning,
    render_status,
    render_tools,
)

if TYPE_CHECKING:  # pragma: no cover
    from .service import Service

log = logging.getLogger("runner")

TERMINAL = {
    "session.execution.succeeded",
    "session.execution.completed",
    "session.execution.failed",
    "session.execution.aborted",
    "session.execution.interrupted",
    "session.interrupted",
    "session.error",
}

STOP_EMOJI = "🛑"


class BusyError(RuntimeError):
    pass


def idle_action(method):
    """Serialize session/settings changes against prompt admission."""
    @functools.wraps(method)
    async def invoke(self, *args, **kwargs):
        self.require_idle()
        async with self._lock:
            return await method(self, *args, **kwargs)
    return invoke


class Conversation:
    def __init__(self, service: "Service", user_id: int) -> None:
        self.svc = service
        self.user_id = user_id
        self.state: UserState = service.store.get_user(user_id)
        self.turn_task: asyncio.Task | None = None
        self.busy = False
        self._lock = asyncio.Lock()
        self._sub = None
        self._stop_requested = False
        self._stopping = False
        self.queue = PromptQueue()
        self.queue.restore(service.store.load_queue(user_id))
        self._queue_messengers: dict[int, Messenger] = {}

    def require_idle(self) -> None:
        if self.busy or self._stopping or self._lock.locked() or self.queue.size:
            raise BusyError("Finish or stop the current turn and clear the queue before changing conversations/settings.")

    def _save_queue(self) -> None:
        self.svc.store.save_queue(self.user_id, self.queue.snapshot())

    def _mutate_queue(self, operation, *args):
        """Keep memory and SQLite consistent if a write fails (e.g. full disk)."""
        before = self.queue.snapshot()
        try:
            result = operation(*args)
            self._save_queue()
            return result
        except Exception:
            self.queue.restore(before)
            raise

    async def remove_queued(self, item_id: int | None = None) -> int:
        async with self._lock:
            return self._remove_queued(item_id)

    def _remove_queued(self, item_id: int | None) -> int:
        if item_id is None:
            count = self.queue.size
            self._mutate_queue(self.queue.clear)
            self._queue_messengers.clear()
        else:
            count = int(self._mutate_queue(self.queue.remove, item_id))
            self._queue_messengers.pop(item_id, None)
        return count

    # ------------------------------------------------------------- properties

    @property
    def client(self) -> OpenCodeClient:
        return self.svc.client

    @property
    def session_id(self) -> str | None:
        return self.state.session_id

    @property
    def directory(self) -> str:
        return self.state.directory or self.svc.cfg.opencode_directory

    def model_label(self, session: dict | None = None) -> str:
        source = session or {}
        model = source.get("model") or (
            {
                "providerID": self.state.provider_id,
                "id": self.state.model_id,
                "variant": self.state.variant,
            }
            if self.state.has_model
            else None
        )
        if not model:
            return ""
        label = f"{model.get('providerID')}/{model.get('id')}"
        variant = model.get("variant")
        if variant and variant != "default":
            label += f" · {variant}"
        return label

    # ------------------------------------------------------------- persistence

    def _save(self) -> None:
        self.svc.store.save_user(self.state)

    def _remember(self, session: dict | None = None) -> None:
        if self.state.session_id:
            title = (session or {}).get("title") or self.state.title
            self.svc.store.remember(self.user_id, self.state.session_id, title)
            self.state.title = title

    # --------------------------------------------------------- session control

    async def ensure_session(self) -> str:
        """Return the active session id, creating one if needed."""
        if self.state.session_id:
            try:
                await self.client.get_session(self.state.session_id)
                return self.state.session_id
            except OpenCodeError as exc:
                if not exc.not_found:
                    raise
                log.info("session %s vanished, creating a new one", self.state.session_id)
        return await self._create_session()

    @idle_action
    async def create_session(
        self, title: str | None = None, directory: str | None = None
    ) -> str:
        return await self._create_session(title, directory)

    async def _create_session(
        self, title: str | None = None, directory: str | None = None
    ) -> str:
        if self.busy:
            raise BusyError("a turn is still running — stop it first")
        target_dir = directory or self.directory
        model = None
        if self.state.has_model:
            model = {"providerID": self.state.provider_id, "id": self.state.model_id}
            if self.state.variant and self.state.variant != "default":
                model["variant"] = self.state.variant
        session = await self.client.create_session(
            title=title,
            agent=self.state.agent or self.svc.cfg.default_agent,
            model=model,
            directory=target_dir,
        )
        self.state.session_id = session["id"]
        self.state.directory = target_dir
        self.state.title = session.get("title") or title
        self._save()
        self._remember(session)
        return session["id"]

    @idle_action
    async def use_session(self, session_id: str) -> dict:
        return await self._use_session(session_id)

    async def _use_session(self, session_id: str) -> dict:
        session = await self.client.get_session(session_id)
        model = session.get("model") or {}
        if model.get("providerID"):
            self.state.provider_id = model["providerID"]
            self.state.model_id = model.get("id")
            self.state.variant = model.get("variant")
        location = (session.get("location") or {}).get("directory")
        if location:
            self.state.directory = location
        self.state.agent = session.get("agent") or self.state.agent
        self.state.session_id = session_id
        self.state.title = session.get("title")
        self._save()
        self._remember(session)
        return session

    @idle_action
    async def set_model(self, provider_id: str, model_id: str, variant: str | None) -> None:
        if self.busy:
            raise BusyError("stop the running turn before switching models")
        if self.state.session_id:
            try:
                await self.client.set_model(
                    self.state.session_id, provider_id, model_id, variant
                )
            except OpenCodeError as exc:
                if not exc.not_found:
                    raise
        self.state.provider_id = provider_id
        self.state.model_id = model_id
        self.state.variant = variant or None
        self._save()

    @idle_action
    async def set_effort(self, variant: str | None) -> None:
        if not self.state.has_model:
            await self.svc.apply_default_model(self)
        if not self.state.has_model:
            raise ValueError("No model is available. Configure a provider in OpenCode first.")
        if self.state.session_id:
            await self.client.set_model(
                self.state.session_id, self.state.provider_id, self.state.model_id, variant
            )
        self.state.variant = variant or None
        self._save()

    @idle_action
    async def set_agent(self, agent: str) -> None:
        if self.state.session_id:
            try:
                await self.client.set_agent(self.state.session_id, agent)
            except OpenCodeError as exc:
                if not exc.not_found:
                    raise
        self.state.agent = agent
        self._save()

    @idle_action
    async def set_directory(self, directory: str) -> None:
        self.state.directory = directory
        self._save()

    # ------------------------------------------------------------- public turn

    async def send_text(
        self, text: str, messenger: Messenger, files: list[dict] | None = None,
        *, delivery: str | None = None,
    ) -> None:
        """Send a user message to the model and stream the answer back."""
        text = text.strip()
        if not text and not files:
            return

        if delivery not in (None, "queue", "steer"):
            raise ValueError("Choose queue or steer.")
        async with self._lock:
            if self._stopping:
                raise BusyError("The current turn is stopping. Please try again in a moment.")
            if self.busy or self.queue.size:
                steer = delivery == "steer" or (delivery is None and self.svc.cfg.steer_when_busy)
                if self.busy and steer and not self._stop_requested:
                    await self.client.prompt(self.session_id, text or "(see attachment)", files=files, delivery="steer")
                    await messenger.send("↪️ Added to the running turn. `/stop` cancels it.")
                else:
                    item_id = self._mutate_queue(self.queue.push, {"text": text, "files": files or [], "session_id": self.session_id})
                    self._queue_messengers[item_id] = messenger
                    hint = "Runs after the current turn." if self.busy else "Use `/queue resume` to continue."
                    await messenger.send(f"📥 Queued #{item_id} ({self.queue.size} waiting). {hint}")
                return
            session_id = await self.ensure_session()
            self._start_worker(session_id, text, messenger, files)
            task = self.turn_task
        await task

    def _start_worker(self, session_id: str, text: str, messenger: Messenger, files) -> None:
        self.busy = True
        self._stop_requested = False
        self.turn_task = asyncio.create_task(self._drain(session_id, text, messenger, files), name=f"turn-{self.user_id}")

    async def resume_queue(self, messenger: Messenger) -> None:
        async with self._lock:
            if self.busy or self._stopping:
                raise BusyError("A turn is still running.")
            items = self.queue.entries()
            if not items:
                await messenger.send("The queue is empty.")
                return
            if items[0]["session_id"] != self.session_id:
                raise BusyError("Queued messages belong to a different conversation. Clear the queue before continuing.")
            await self.client.get_session(self.session_id)
            if self.session_id in await self.client.active_sessions():
                raise BusyError("OpenCode is still working on this conversation. Use /pending or /stop before resuming.")
            item = self._mutate_queue(self.queue.pop)
            self._queue_messengers.pop(item["id"], None)
            self._start_worker(self.session_id, item["text"], messenger, item["files"])
            task = self.turn_task
        await task

    async def _drain(self, session_id: str, text: str, messenger: Messenger, files) -> None:
        try:
            while True:
                success = await self._run_turn(session_id, text, messenger, files)
                with contextlib.suppress(Exception):
                    await self._maybe_title(session_id)
                async with self._lock:
                    if not success or self._stop_requested or not self.queue.size:
                        break
                    item = self.queue.entries()[0]
                    if item["session_id"] != session_id:
                        break
                    next_messenger = self._queue_messengers.get(item["id"], messenger)
                    # Keep the item queued if Discord cannot announce it.
                    await next_messenger.send(f"▶️ Starting queued message #{item['id']}.")
                    self._mutate_queue(self.queue.pop)
                    messenger = self._queue_messengers.pop(item["id"], messenger)
                    text, files = item["text"], item["files"]
        except asyncio.CancelledError:
            log.info("turn cancelled for user %s", self.user_id)
        except Exception:  # noqa: BLE001
            log.exception("turn failed for user %s", self.user_id)
            with contextlib.suppress(Exception):
                await messenger.send("❌ The turn could not finish. Use `/history` to check the result and `/pending` for unanswered questions.")
        finally:
            self.busy = False
            self.turn_task = None
            if self.queue.size and not getattr(self.svc, "_closed", False):
                with contextlib.suppress(Exception):
                    await messenger.send(f"⏸️ {self.queue.size} queued messages paused. `/queue resume` continues; `/queue clear` removes them.")

    async def _run_turn(
        self,
        session_id: str,
        text: str,
        messenger: Messenger,
        files: list[dict] | None,
    ) -> bool:
        sub = self.client.subscribe(session_id)
        self._sub = sub
        state = TurnState(session_id=session_id)
        live = LiveMessage(messenger, interval=self.svc.cfg.edit_interval)
        model_label = self.model_label()
        # Each model step gets its own live message; this tracks which step the
        # current one belongs to (None = the pre-step placeholder).
        live_step: str | None = None
        adopted = False
        try:
            await live.start(render_status(state, model_label))
            try:
                await self.client.prompt(
                    session_id, text or "(see attachment)", files=files
                )
            except OpenCodeError as exc:
                await live.finish(f"❌ **Could not send the message**\n`{clip(exc.message, 300)}`")
                return False

            deadline = time.monotonic() + self.svc.cfg.turn_timeout
            started = False
            last_event = time.monotonic()
            last_hydrate = 0.0
            flushed: set[str] = set()
            perm_notified: set[str] = set()
            form_notified: set[str] = set()

            while True:
                timeout = max(1.0, min(15.0, deadline - time.monotonic()))
                try:
                    event = await asyncio.wait_for(sub.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    await live.finish(
                        "🔌 Lost the connection to OpenCode before the turn finished. "
                        "The answer may still be in the conversation — `/history` will show it."
                    )
                    return False
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if getattr(sub, "dropped", 0) or getattr(sub, "disconnected", False):
                        await live.finish("🔌 The event stream reconnected or lost data. Use `/history`, `/export` and `/pending` to recover; queued messages are paused.")
                        return False
                    if now > deadline:
                        await live.finish(
                            "⏱️ **Timed out** waiting for the model. `/stop` clears the turn."
                        )
                        with contextlib.suppress(Exception):
                            await self.client.interrupt(session_id)
                        return False
                    if (not state.pending_permissions and not state.pending_forms
                            and now - last_event > self.svc.cfg.stall_timeout):
                        await live.finish(
                            f"⏱️ No activity for {int(self.svc.cfg.stall_timeout)}s — the "
                            "connection to OpenCode looks stuck. `/stop` to give up, "
                            "`/status` to check the service."
                        )
                        with contextlib.suppress(Exception):
                            await self.client.interrupt(session_id)
                        return False
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.warning("event read failed: %s", exc)
                    await live.finish("🔌 Connection interrupted. Use `/history` and `/pending` to recover results and questions.")
                    return False
                last_event = time.monotonic()
                if getattr(sub, "dropped", 0) or getattr(sub, "disconnected", False):
                    await live.finish("⚠️ Some progress events were lost. Use `/history` or `/export` for the complete result; queued messages are paused.")
                    return False

                kind = event.get("type", "")
                log.debug("event %s", kind)
                apply_event(state, event)
                if time.monotonic() > deadline:
                    await live.finish("⏱️ Turn time limit reached. Use `/history` for the partial result.")
                    with contextlib.suppress(Exception):
                        await self.client.interrupt(session_id)
                    return False
                if kind in TERMINAL:
                    break
                if kind == "session.execution.started":
                    started = True
                if self._stop_requested and kind.endswith(".failed"):
                    break

                if kind == "permission.asked":
                    pid = (event.get("data") or {}).get("id", "")
                    if pid and pid not in perm_notified:
                        perm_notified.add(pid)
                        await self.svc.show_permission_request(
                            self, event.get("data") or {}, messenger
                        )
                    continue

                if kind == "form.created":
                    form = (event.get("data") or {}).get("form") or {}
                    fid = form.get("id", "")
                    if fid and fid not in form_notified:
                        form_notified.add(fid)
                        await self.svc.show_form(self, form, messenger)
                    continue

                step = state.current
                if step is None:
                    continue

                if step.message_id != live_step:
                    if adopted:
                        # A new step begins: close the previous one and open a fresh message.
                        await live.finish()
                        live = LiveMessage(messenger, interval=self.svc.cfg.edit_interval)
                        await live.start(render_status(state, model_label))
                    live_step = step.message_id
                    adopted = True

                if kind in {"session.tool.called", "session.tool.success", "session.tool.failed"}:
                    now = time.monotonic()
                    if now - last_hydrate > 1.0:
                        last_hydrate = now
                        await self._hydrate(state, step.message_id)
                await live.update(self._compose(state, step, model_label))
                if step.finished and step.message_id not in flushed:
                    flushed.add(step.message_id)
                    await self._flush_step(state, step, messenger, live, model_label)

            if not started and not state.steps and state.outcome not in {"failed", "interrupted", "aborted"}:
                await live.finish(
                    "⚠️ The session did not start a reply. "
                    "Check `/status`, or try `/new` to start a clean conversation."
                )
                return False

            await self._finish_turn(state, messenger, live, model_label)
            return state.outcome == "succeeded"
        finally:
            self._sub = None
            sub.close()

    # -------------------------------------------------------------- rendering

    def _compose(self, state: TurnState, step, model_label: str) -> str:
        if not step.has_text:
            return render_status(state, model_label)
        parts: list[str] = []
        if self.svc.cfg.show_reasoning and step.thinking:
            parts.append(render_reasoning(step.thinking))
        tools = render_tools(step) if self.svc.cfg.show_tools else ""
        # Reserve space for metadata and separators within Discord's limit.
        overhead = sum(len(part) + 2 for part in parts)
        if tools:
            overhead += len(tools) + 2
        parts.append(clip(step.text, min(1700, 1900 - overhead)))
        if tools:
            parts.append(tools)
        return "\n\n".join(parts)

    async def _flush_step(
        self, state: TurnState, step, messenger: Messenger, live: LiveMessage, model_label: str
    ) -> None:
        """Emit a finished step as a standalone message."""
        await self._hydrate(state, step.message_id, force=True)
        tools = render_tools(step) if self.svc.cfg.show_tools else ""

        if not step.has_text:
            # A step that only ran tools (or nothing at all) has no prose to show.
            if step.error:
                await live.finish(f"❌ {clip(step.error, 300)}")
            elif tools:
                await live.finish(tools)
            else:
                await live.delete()
            return

        # The preview is deliberately clipped. Replace it with the first chunk
        # of the complete reply so the continuation cannot skip any text.
        parts: list[str] = []
        if self.svc.cfg.show_reasoning and step.thinking:
            parts.append(render_reasoning(step.thinking))
        parts.append(step.text)
        chunks = split_text("\n\n".join(parts), 1900)
        await live.finish(chunks[0])
        for extra in chunks[1:]:
            await messenger.send(extra)
        if tools:
            await messenger.send(tools)

    async def _finish_turn(
        self, state: TurnState, messenger: Messenger, live: LiveMessage, model_label: str
    ) -> None:
        if state.outcome == "interrupted" or (self._stop_requested and state.outcome != "succeeded"):
            await live.finish(f"{STOP_EMOJI} {clip(state.error or 'Stopped.', 300)}")
            return
        if state.outcome in {"failed", "aborted"}:
            detail = state.error or "The turn failed. Use /history to inspect the partial result."
            if "interrupt" in detail.lower() or self._stop_requested:
                detail = "Stopped by `/stop`."
            await live.finish(f"❌ {clip(detail, 400)}")
            return

        last = state.steps[-1] if state.steps else None
        if last is not None and not last.finished:
            await self._flush_step(state, last, messenger, live, model_label)
        else:
            await live.finish(None)

        if not state.text.strip():
            await messenger.send(
                "_The model finished without sending any text._ Use `/status` for session info."
            )
        if state.pending_permissions:
            pending = len(state.pending_permissions)
            await messenger.send(
                f"⏳ {pending} permission request{'s' if pending != 1 else ''} still unanswered — "
                "use the buttons above to let the turn continue."
            )
        if state.pending_forms:
            pending = state.pending_forms
            await messenger.send(
                f"⏳ {pending} question{'s' if pending != 1 else ''} still unanswered — "
                "pick an option above to let the turn continue."
            )
        await messenger.send(f"-#{self._turn_summary(state, model_label)}")

    def _turn_summary(self, state: TurnState, model_label: str) -> str:
        bits = [model_label, human_cost(state.cost), human_tokens(state.tokens), duration(state.duration)]
        tools = sum(len(step.tools) for step in state.steps)
        if tools:
            bits.append(f"{tools} tool call{'s' if tools != 1 else ''}")
        return " · ".join(bit for bit in bits if bit)

    async def _hydrate(self, state: TurnState, message_id: str, force: bool = False) -> None:
        if not message_id:
            return
        try:
            message = await self.client.message(state.session_id, message_id)
        except OpenCodeError:
            return
        hydrate_from_message(state, message)

    async def _maybe_title(self, session_id: str) -> None:
        """Pick up an auto-generated title for the conversation list."""
        with contextlib.suppress(OpenCodeError):
            session = await self.client.get_session(session_id)
            title = session.get("title")
            if title and title != self.state.title:
                self.state.title = title
                self._save()
            self._remember(session)

    async def stop(self, messenger: Messenger) -> bool:
        async with self._lock:
            if self._stopping:
                raise BusyError("The current turn is already stopping.")
            session_id, task = self.session_id, self.turn_task
            self._stopping = True
            self._stop_requested = True
        try:
            if not session_id:
                await messenger.send("There is no active conversation.")
                return False
            try:
                await self.client.interrupt(session_id)
            except OpenCodeError as exc:
                await messenger.send(f"❌ `{clip(exc.message, 200)}`")
                return False
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await messenger.send(f"{STOP_EMOJI} Stopped. Queued messages stay paused; `/queue resume` continues them.")
            return True
        finally:
            self._stopping = False

    # ------------------------------------------------------------- transcripts

    async def transcript(self, limit: int = 12) -> list[dict]:
        session_id = self.session_id
        if not session_id:
            return []
        messages = await self.client.messages(session_id)
        return transcript_entries(messages)[-limit:]

    async def status(self) -> dict:
        if not self.state.session_id:
            return {}
        return await self.client.get_session(self.state.session_id)
