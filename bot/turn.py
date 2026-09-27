"""Turn state machine: turns OpenCode events into renderable state.

This module knows nothing about Discord. ``TurnState`` consumes the event
stream for one execution and exposes text plus tool activity; ``render_status``
and the other render_* helpers turn that state into message text.

The implementation now lives in the native engine (``cpp/turn.cpp``); this
module re-exports it so existing imports keep working.
"""

from __future__ import annotations

from ._engine import (
    Step,
    ToolActivity,
    TurnState,
    apply_event,
    hydrate_from_message,
    render_footer,
    render_reasoning,
    render_status,
    render_tools,
    tool_detail,
)

__all__ = [
    "Step",
    "ToolActivity",
    "TurnState",
    "apply_event",
    "hydrate_from_message",
    "render_footer",
    "render_reasoning",
    "render_status",
    "render_tools",
    "tool_detail",
]
