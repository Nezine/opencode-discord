"""Helpers for getting arbitrary model output into Discord messages.

Discord hard-caps a message at 2000 characters and renders unbalanced code
fences badly, so long answers are split on natural boundaries while keeping
fences balanced.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
LIMIT = 1900


def clip(text: str, limit: int = LIMIT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _blocks(text: str) -> list[tuple[str, list[str]]]:
    """Split text into ("prose"|"code", lines) blocks.

    A code block runs from its opening fence to the matching close, so blocks can
    be re-packed without ever producing an unbalanced message.
    """
    lines = text.splitlines()
    blocks: list[tuple[str, list[str]]] = []
    prose: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = FENCE.match(line)
        if not match:
            prose.append(line)
            index += 1
            continue
        if prose:
            blocks.append(("prose", prose))
            prose = []
        marker = match.group(1)
        code = [line]
        index += 1
        while index < len(lines):
            code.append(lines[index])
            inner = FENCE.match(lines[index])
            if inner and inner.group(1)[0] == marker[0] and len(inner.group(1)) >= len(marker):
                index += 1
                break
            index += 1
        blocks.append(("code", code))
    if prose:
        blocks.append(("prose", prose))
    return blocks


def _split_code(marker: str, lines: list[str], limit: int) -> list[str]:
    """Break one fenced region into fence-balanced chunks of at most `limit`."""
    open_line = lines[0]
    info = open_line[len(marker) :]
    body = lines[1:]
    closed = False
    if body:
        tail = FENCE.match(body[-1])
        if tail and tail.group(1)[0] == marker[0]:
            body = body[:-1]
            closed = True

    open_line = f"{marker}{info}"
    budget = limit - len(open_line) - len(marker) - 2
    if budget < 20:
        budget = 20

    chunks: list[str] = []
    piece: list[str] = []
    size = 0

    def emit() -> None:
        nonlocal piece, size
        chunks.append("\n".join([open_line, *piece, marker]))
        piece = []
        size = 0

    for line in body:
        while len(line) > budget:
            if piece:
                emit()
            piece.append(line[:budget])
            line = line[budget:]
            emit()
        if piece and size + len(line) + 1 > budget:
            emit()
        piece.append(line)
        size += len(line) + 1
    if piece or not closed:
        emit()
    return chunks


def split_text(text: str, limit: int = LIMIT) -> list[str]:
    """Split text into Discord-sized chunks, keeping code fences balanced."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    out: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            out.append("\n".join(current))
            current = []
            size = 0

    for kind, lines in _blocks(text):
        if kind == "code":
            flush()
            out.extend(_split_code(FENCE.match(lines[0]).group(1), lines, limit))
            continue
        for line in lines:
            if size + len(line) + 1 > limit and current:
                flush()
            while len(line) > limit:
                flush()
                out.append(line[:limit])
                line = line[limit:]
            current.append(line)
            size += len(line) + 1
    flush()
    return [chunk for chunk in out if chunk.strip()]


def rel_time(ts: float | int | None) -> str:
    if not ts:
        return "unknown"
    delta = max(0.0, time.time() - float(ts) / 1000.0)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)}d ago"
    return time.strftime("%Y-%m-%d", time.localtime(float(ts) / 1000.0))


def duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s"


def human_cost(cost: float | None) -> str:
    if not cost:
        return "$0.00"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def human_tokens(tokens: dict | None) -> str:
    if not tokens:
        return "0 tokens"
    total = 0
    for key in ("input", "output", "reasoning"):
        try:
            total += int(tokens.get(key) or 0)
        except (TypeError, ValueError):
            pass
    cache = tokens.get("cache") or {}
    for key in ("read", "write"):
        try:
            total += int(cache.get(key) or 0)
        except (TypeError, ValueError):
            pass
    if total >= 1_000_000:
        return f"{total / 1_000_000:.1f}M tokens"
    if total >= 1_000:
        return f"{total / 1_000:.1f}k tokens"
    return f"{total} tokens"


def join_nonempty(parts: Iterable[str | None], sep: str = " · ") -> str:
    return sep.join(p for p in parts if p)


def sanitize_mentions(text: str) -> str:
    """Escape mass mentions.

    Every send path also passes ``allowed_mentions=AllowedMentions.none()``; this
    is a second layer so bot output cannot ping even if a call site forgets.
    """
    if not text:
        return ""
    zw = "\u200b"
    return text.replace("@everyone", f"@{zw}everyone").replace("@here", f"@{zw}here")
