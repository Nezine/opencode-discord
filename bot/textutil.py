"""Helpers for getting arbitrary model output into Discord messages.

Discord hard-caps a message at 2000 characters and renders unbalanced code
fences badly, so long answers are split on natural boundaries while keeping
fences balanced.

The implementation now lives in the native engine (``cpp/text.cpp``); this
module re-exports it so existing imports keep working. Lengths are counted in
code points, not bytes, which is what Discord measures.
"""

from __future__ import annotations

from ._engine import (
    LIMIT,
    clip,
    duration,
    human_cost,
    human_tokens,
    join_nonempty,
    rel_time,
    sanitize_mentions,
    split_text,
)

__all__ = [
    "LIMIT",
    "clip",
    "duration",
    "human_cost",
    "human_tokens",
    "join_nonempty",
    "rel_time",
    "sanitize_mentions",
    "split_text",
]
