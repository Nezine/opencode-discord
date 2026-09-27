"""Turn state machine: turns OpenCode events into renderable state.

This module knows nothing about Discord. ``TurnState`` consumes the event
stream for one execution and exposes text plus tool activity; ``render_step`` and
``render_status`` turn that state into message text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .textutil import clip, duration, human_cost, human_tokens, join_nonempty

TOOL_ICON = {"completed": "✅", "error": "❌", "running": "⏳"}


@dataclass
class ToolActivity:
    call_id: str
    name: str = "…"
    status: str = "running"
    detail: str = ""
    error: str = ""

    @property
    def icon(self) -> str:
        return TOOL_ICON.get(self.status, "•")

    def label(self) -> str:
        label = self.name if self.name and self.name != "…" else "tool"
        if self.detail:
            return f"{label}({self.detail})"
        return label


@dataclass
class Step:
    message_id: str
    segments: dict[int, str] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    reasoning: dict[int, str] = field(default_factory=dict)
    reasoning_order: list[int] = field(default_factory=list)
    tools: dict[str, ToolActivity] = field(default_factory=dict)
    finished: bool = False
    finish: str | None = None
    error: str | None = None
    cost: float = 0.0
    tokens: dict[str, Any] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def add_text(self, ordinal: int, delta: str) -> None:
        if ordinal not in self.segments:
            self.segments[ordinal] = ""
            self.order.append(ordinal)
        self.segments[ordinal] += delta

    def add_reasoning(self, ordinal: int, delta: str) -> None:
        if ordinal not in self.reasoning:
            self.reasoning[ordinal] = ""
            self.reasoning_order.append(ordinal)
        self.reasoning[ordinal] += delta

    @property
    def text(self) -> str:
        return "".join(self.segments[o] for o in sorted(self.order))

    @property
    def thinking(self) -> str:
        return "".join(self.reasoning[o] for o in sorted(self.reasoning_order)).strip()

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started


@dataclass
class TurnState:
    session_id: str
    started: float = field(default_factory=time.monotonic)
    steps: list[Step] = field(default_factory=list)
    current: Step | None = None
    cost: float = 0.0
    tokens: dict[str, Any] = field(default_factory=dict)
    outcome: str | None = None
    error: str | None = None
    retrying: bool = False
    permissions: dict[str, dict] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started

    @property
    def text(self) -> str:
        return "\n\n".join(s.text for s in self.steps if s.has_text).strip()

    @property
    def pending_permissions(self) -> dict[str, dict]:
        """Permission requests that have not been answered yet."""
        return self.permissions

    def step_for(self, message_id: str) -> Step | None:
        for step in self.steps:
            if step.message_id == message_id:
                return step
        return None

    def tool_names(self) -> dict[str, str]:
        names: dict[str, str] = {}
        for step in self.steps:
            for call_id, tool in step.tools.items():
                if tool.name and tool.name != "…":
                    names[f"{step.message_id}:{call_id}"] = tool.name
        return names


def step_started(state: TurnState, data: dict) -> Step:
    message_id = data.get("assistantMessageID") or ""
    step = state.step_for(message_id)
    if step is None:
        step = Step(message_id=message_id)
        state.steps.append(step)
    state.current = step
    return step


def apply_event(state: TurnState, event: dict) -> None:
    """Fold one OpenCode event into the turn state."""
    kind = event.get("type", "")
    data = event.get("data") or {}

    if kind == "session.step.started":
        step_started(state, data)

    elif kind == "session.text.started":
        step = state.current or step_started(state, data)
        ordinal = int(data.get("ordinal") or 0)
        step.segments.setdefault(ordinal, "")
        if ordinal not in step.order:
            step.order.append(ordinal)

    elif kind == "session.text.delta":
        step = state.current or step_started(state, data)
        step.add_text(int(data.get("ordinal") or 0), data.get("delta") or "")

    elif kind == "session.reasoning.started":
        step = state.current or step_started(state, data)
        step.add_reasoning(int(data.get("ordinal") or 0), "")

    elif kind == "session.reasoning.delta":
        step = state.current or step_started(state, data)
        step.add_reasoning(int(data.get("ordinal") or 0), data.get("delta") or "")

    elif kind == "session.reasoning.ended":
        step = state.current or step_started(state, data)
        if data.get("text"):
            step.reasoning[int(data.get("ordinal") or 0)] = data["text"]

    elif kind == "session.retry.scheduled":
        state.retrying = True

    elif kind == "session.retry.started":
        state.retrying = True

    elif kind == "session.tool.called":
        step = state.current or step_started(state, data)
        call_id = data.get("id") or ""
        tool = step.tools.get(call_id)
        if tool is None:
            tool = ToolActivity(call_id=call_id)
            step.tools[call_id] = tool
        tool.status = "running"
        tool.detail = tool_detail(data.get("input") or {})

    elif kind == "session.tool.success":
        tool = _find_tool(state, data)
        if tool:
            tool.status = "completed"
            tool.detail = tool_detail(data.get("input") or {}) or tool.detail

    elif kind == "session.tool.failed":
        tool = _find_tool(state, data)
        if tool:
            tool.status = "error"
            error = data.get("error") or {}
            tool.error = str(error.get("message") or error)[:200]

    elif kind == "session.step.ended":
        step = state.current or step_started(state, data)
        step = state.step_for(data.get("assistantMessageID") or step.message_id) or step
        step.finished = True
        step.finish = data.get("finish")
        step.cost = float(data.get("cost") or 0.0)
        step.tokens = data.get("tokens") or {}
        state.cost = max(state.cost, step.cost)
        state.tokens = step.tokens

    elif kind == "session.step.failed":
        step = state.current or step_started(state, data)
        step = state.step_for(data.get("assistantMessageID") or step.message_id) or step
        step.finished = True
        error = data.get("error") or {}
        step.error = str(error.get("message") or error)[:300]

    elif kind == "session.usage.updated":
        state.cost = float(data.get("cost") or state.cost)
        state.tokens = data.get("tokens") or state.tokens

    elif kind == "permission.asked":
        if data.get("id"):
            state.permissions[data["id"]] = data

    elif kind == "permission.replied":
        state.permissions.pop(data.get("requestID") or "", None)

    elif kind == "session.execution.succeeded":
        state.outcome = "succeeded"
        state.retrying = False

    elif kind in {"session.execution.failed", "session.execution.aborted", "session.error"}:
        state.outcome = "failed"
        error = data.get("error") or {}
        if isinstance(error, dict):
            state.error = str(error.get("message") or error.get("type") or "execution failed")[:300]
        else:
            state.error = str(error)[:300]

    elif kind == "session.interrupted" or kind == "session.execution.interrupted":
        state.outcome = "interrupted"
        if not state.error:
            state.error = "The session was interrupted."


def _find_tool(state: TurnState, data: dict) -> ToolActivity | None:
    """Locate the tool a success/failure event refers to, creating it if needed."""
    call_id = data.get("id") or ""
    step = state.step_for(data.get("assistantMessageID") or "") or state.current
    if not step:
        step = step_started(state, data)
    if call_id in step.tools:
        return step.tools[call_id]
    for candidate in reversed(state.steps):
        if call_id in candidate.tools:
            return candidate.tools[call_id]
    # The stream can report a tool we never saw called (reconnects, dropped
    # events); record it rather than losing the error.
    tool = ToolActivity(call_id=call_id)
    step.tools[call_id] = tool
    return tool


def tool_detail(payload: Any, width: int = 42) -> str:
    """A one-line hint of what a tool was called with."""
    if not isinstance(payload, dict) or not payload:
        return ""
    for key in ("command", "filePath", "path", "pattern", "query", "url", "prompt", "description"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            if len(text) > width:
                text = text[: width - 1] + "…"
            return text
    keys = ",".join(str(k) for k in list(payload)[:3])
    return keys[:width]


def hydrate_from_message(state: TurnState, message: dict) -> None:
    """Fill in authoritative tool names/statuses from a fetched message.

    Live tool events carry the call id but not the tool name, so the names are
    resolved from the session message once it exists.
    """
    if not message:
        return
    step = state.step_for(message.get("id", ""))
    if step is None:
        return
    for part in message.get("content") or []:
        kind = part.get("type")
        if kind == "reasoning":
            if part.get("text") and not step.thinking:
                step.add_reasoning(0, part["text"])
            continue
        if kind != "tool":
            continue
        call_id = part.get("id") or ""
        tool = step.tools.get(call_id)
        if tool is None:
            tool = ToolActivity(call_id=call_id)
            step.tools[call_id] = tool
        if part.get("name"):
            tool.name = part["name"]
        part_state = part.get("state") or {}
        status = part_state.get("status")
        if status in TOOL_ICON:
            tool.status = status
        if not tool.detail:
            tool.detail = tool_detail(part_state.get("input"))
        error = part_state.get("error")
        if error and not tool.error:
            tool.error = str(error)[:200]


def render_status(state: TurnState, model_label: str = "") -> str:
    if state.retrying:
        return f"🔁 {join_nonempty(['provider hiccup — retrying', model_label])}"
    step = state.current
    if step is None:
        return f"⏳ {join_nonempty(['thinking', model_label])}"
    running = [t for t in step.tools.values() if t.status == "running"]
    if running:
        names = ", ".join(t.name if t.name != "…" else "tool" for t in running[:3])
        return f"⏳ {join_nonempty([f'running {names}', model_label])}"
    if step.thinking and not step.has_text:
        return f"🧠 {join_nonempty(['thinking it through', model_label])}"
    if not step.has_text:
        return f"⏳ {join_nonempty(['thinking', model_label])}"
    return f"💭 {join_nonempty(['thinking', model_label])}"


def render_reasoning(text: str, limit: int = 700) -> str:
    """Reasoning as a dimmed blockquote so it stays out of the way."""
    body = " ".join((text or "").split())
    if not body:
        return ""
    quoted = "\n".join(f"> {line}" for line in body[:limit].splitlines()) or f"> {body[:limit]}"
    return f"🧠 *thinking*\n{quoted}"


def render_tools(step: Step, limit: int = 300) -> str:
    if not step.tools:
        return ""
    shown = list(step.tools.values())
    summary = " ".join(f"{t.icon} `{t.name if t.name != '…' else 'tool'}`" for t in shown)
    errors = [t for t in shown if t.status == "error"]
    if errors:
        detail = "; ".join(f"{t.name}: {t.error}" for t in errors[:2] if t.error)
        if detail:
            summary += f"\n⚠️ {clip(detail, 160)}"
    return clip(summary, limit)


def render_footer(state: TurnState, step: Step, model_label: str = "") -> str:
    bits = [
        model_label,
        human_cost(step.cost or state.cost),
        human_tokens(step.tokens or state.tokens),
        duration(step.duration),
    ]
    return join_nonempty([b for b in bits if b and b not in {"$0.00", "0 tokens"}])
