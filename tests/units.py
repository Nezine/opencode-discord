"""Unit tests for the pure logic. No network, no Discord.

    .venv/bin/python -m tests.units
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.store import Store  # noqa: E402
from bot.textutil import (  # noqa: E402
    clip,
    duration,
    human_cost,
    human_tokens,
    join_nonempty,
    rel_time,
    sanitize_mentions,
    split_text,
)
from bot.turn import (  # noqa: E402
    TurnState,
    apply_event,
    hydrate_from_message,
    render_reasoning,
    render_status,
    render_tools,
    tool_detail,
)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def eq(name: str, actual, expected) -> None:
    check(name, actual == expected, f"\n       got:      {actual!r}\n       expected: {expected!r}")


# --------------------------------------------------------------------- textutil


def test_split_text() -> None:
    print("\nsplit_text")
    eq("short text is one chunk", split_text("hello", 1900), ["hello"])
    eq("empty is empty", split_text("", 1900), [])

    long_text = "\n".join(f"line {i}" for i in range(500))
    chunks = split_text(long_text, 200)
    check("splits into several chunks", len(chunks) > 1, f"got {len(chunks)}")
    check("every chunk fits", all(len(c) <= 200 for c in chunks), f"max {max(len(c) for c in chunks)}")
    eq("no content lost", "".join(chunks).replace("\n", ""), long_text.replace("\n", ""))

    fence = "before\n```python\n" + "\n".join(f"x{i} = {i}" for i in range(80)) + "\n```\nafter"
    chunks = split_text(fence, 200)
    check("fenced chunks fit", all(len(c) <= 200 for c in chunks), f"max {max(len(c) for c in chunks)}")
    check(
        "fences are balanced per chunk",
        all(c.count("```") % 2 == 0 for c in chunks),
        str([c.count("```") for c in chunks]),
    )
    check("code payload preserved", all(f"x{i} = {i}" in "\n".join(chunks) for i in (0, 40, 79)))
    check("prose preserved", "before" in chunks[0] and "after" in chunks[-1])
    check("language hint kept", any("```python" in c for c in chunks))

    unterminated = "text\n```\n" + "\n".join(f"line{i}" for i in range(60))
    chunks = split_text(unterminated, 120)
    check("unterminated fence balanced", all(c.count("```") % 2 == 0 for c in chunks))
    check("unterminated fence fits", all(len(c) <= 120 for c in chunks))

    only_code = "```\n" + "y" * 500 + "\n```"
    chunks = split_text(only_code, 100)
    check("huge single code line split", all(len(c) <= 100 for c in chunks))
    check("huge code balanced", all(c.count("```") % 2 == 0 for c in chunks))

    single = "x" * 500
    chunks = split_text(single, 100)
    check("oversized line is hard split", all(len(c) <= 100 for c in chunks))
    eq("oversized line kept", "".join(chunks), single)


def test_helpers() -> None:
    print("\nhelpers")
    eq("clip short", clip("abc", 10), "abc")
    eq("clip long", clip("abcdef", 5), "ab...")
    eq("join_nonempty drops blanks", join_nonempty(["a", None, "", "b"]), "a · b")
    eq("join_nonempty custom sep", join_nonempty(["a", "b"], sep=" | "), "a | b")
    eq("cost tiny", human_cost(0.0004), "$0.0004")
    eq("cost normal", human_cost(1.5), "$1.50")
    eq("tokens", human_tokens({"input": 1200, "output": 300, "reasoning": 0, "cache": {"read": 5000, "write": 0}}), "6.5k tokens")
    eq("tokens millions", human_tokens({"input": 2_000_000}), "2.0M tokens")
    check("duration ms", duration(0.25).endswith("ms"))
    eq("duration s", duration(4.2), "4.2s")
    eq("duration m", duration(75), "1m 15s")
    check("rel time", rel_time(None) == "unknown")
    check("mentions escaped", "@everyone" not in sanitize_mentions("hey @everyone and @here"))


# ------------------------------------------------------------------------ turn


def ev(kind: str, **data) -> dict:
    return {"type": kind, "data": {"sessionID": "ses_test", **data}}


def test_turn_state() -> None:
    print("\nturn state machine")
    state = TurnState(session_id="ses_test")
    apply_event(state, ev("session.step.started", assistantMessageID="msg_1"))
    apply_event(state, ev("session.text.started", assistantMessageID="msg_1", ordinal=0))
    apply_event(state, ev("session.text.delta", assistantMessageID="msg_1", ordinal=0, delta="Hel"))
    apply_event(state, ev("session.text.delta", assistantMessageID="msg_1", ordinal=0, delta="lo"))
    eq("deltas accumulate in order", state.text, "Hello")
    check("status shows thinking", "thinking" in render_status(state, "m/x"))
    check("status shows the model", "m/x" in render_status(state, "m/x"))

    apply_event(
        state,
        ev("session.tool.called", assistantMessageID="msg_1", id="call_1", input={"command": "ls -la"}),
    )
    tool = state.steps[0].tools["call_1"]
    eq("tool starts running", tool.status, "running")
    eq("tool detail from input", tool.detail, "ls -la")
    check("tool line rendered", "tool" in render_tools(state.steps[0]))

    hydrate_from_message(
        state,
        {
            "id": "msg_1",
            "content": [
                {"type": "tool", "id": "call_1", "name": "bash", "state": {"status": "completed", "input": {"command": "ls"}}},
                {"type": "reasoning", "text": "I should list files"},
            ],
        },
    )
    eq("tool name resolved", tool.name, "bash")
    eq("tool status resolved", tool.status, "completed")
    eq("reasoning hydrated", state.steps[0].thinking, "I should list files")
    check("reasoning rendered", "thinking" in render_reasoning(state.steps[0].thinking))

    apply_event(
        state,
        ev("session.tool.failed", assistantMessageID="msg_1", id="call_2", error={"type": "tool.execution", "message": "boom"}),
    )
    failed = state.steps[0].tools["call_2"]
    eq("failure recorded", (failed.status, failed.error), ("error", "boom"))

    apply_event(
        state,
        ev(
            "session.step.ended",
            assistantMessageID="msg_1",
            finish="stop",
            cost=0.5,
            tokens={"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        ),
    )
    check("step finished", state.steps[0].finished)
    eq("cost recorded", state.cost, 0.5)

    # a second step must become its own Step object
    apply_event(state, ev("session.step.started", assistantMessageID="msg_2"))
    apply_event(state, ev("session.text.delta", assistantMessageID="msg_2", ordinal=0, delta="second"))
    eq("two steps tracked", len(state.steps), 2)
    eq("first step text intact", state.steps[0].text, "Hello")
    eq("second step text", state.steps[1].text, "second")
    eq("turn text joins steps", state.text, "Hello\n\nsecond")

    apply_event(state, ev("session.execution.succeeded"))
    eq("outcome recorded", state.outcome, "succeeded")


def test_turn_failures() -> None:
    print("\nturn failures")
    state = TurnState(session_id="ses_test")
    apply_event(state, ev("session.step.started", assistantMessageID="msg_1"))
    apply_event(state, ev("session.step.failed", assistantMessageID="msg_1", error={"type": "aborted", "message": "Step interrupted"}))
    apply_event(state, ev("session.execution.failed", error={"type": "aborted", "message": "aborted by user"}))
    eq("outcome failed", state.outcome, "failed")
    eq("error message kept", state.error, "aborted by user")
    check("step error kept", state.steps[0].error == "Step interrupted")

    retry = TurnState(session_id="ses_test")
    apply_event(retry, ev("session.step.started", assistantMessageID="msg_1"))
    apply_event(retry, ev("session.retry.scheduled"))
    check("retry surfaced", "retry" in render_status(retry).lower())


def test_tool_detail() -> None:
    print("\ntool detail")
    eq("command", tool_detail({"command": "ls"}), "ls")
    eq("path clipped", len(tool_detail({"path": "x" * 200})), 42)
    eq("falls back to keys", tool_detail({"weird": 1, "other": 2}), "weird,other")
    eq("empty", tool_detail({}), "")


# ----------------------------------------------------------------------- store


def test_store() -> None:
    print("\nstore")
    store = Store(":memory:")
    empty = store.get_user(42)
    eq("unknown user has no session", empty.session_id, None)
    eq("no model yet", empty.has_model, False)

    store.save_user(
        type(empty)(
            user_id=42,
            session_id="ses_a",
            provider_id="google",
            model_id="gemini",
            variant="high",
            agent="build",
            directory="/tmp",
            title="first",
        )
    )
    loaded = store.get_user(42)
    eq("session round trips", loaded.session_id, "ses_a")
    eq("model round trips", loaded.model_label, "google/gemini · high")
    eq("directory round trips", loaded.directory, "/tmp")

    updated = store.update(42, session_id="ses_b", title="second")
    eq("update applies", (updated.session_id, updated.title), ("ses_b", "second"))
    eq("update persists", store.get_user(42).session_id, "ses_b")
    eq("other user untouched", store.get_user(43).session_id, None)

    store.remember(42, "ses_a", "first")
    store.remember(42, "ses_b", "second")
    ids = store.conversation_ids(42)
    eq("both conversations tracked", sorted(ids), ["ses_a", "ses_b"])
    eq("most recent first", ids[0], "ses_b")
    store.forget(42, "ses_a")
    eq("forget removes one", store.conversation_ids(42), ["ses_b"])
    store.forget(42, "ses_b")
    eq("forget removes the rest", store.conversation_ids(42), [])
    store.close()


def test_sse_parser() -> None:
    print("\nSSE parser")
    from bot.oc import SSEParser

    parser = SSEParser()
    eq("no data yet", parser.feed(b'data: {"type":"a"}\n'), [{"type": "a"}])

    # an event split across three network chunks
    parser = SSEParser()
    eq("partial chunk", parser.feed(b'data: {"type":"b"'), [])
    eq("still partial", parser.feed(b',"x":1}'), [])
    eq("completed on newline", parser.feed(b"\n"), [{"type": "b", "x": 1}])

    # a single line far larger than aiohttp's 512 KiB read limit
    parser = SSEParser()
    payload = {"type": "session.tool.success", "data": {"content": "y" * 900_000}}
    raw = f"data: {json.dumps(payload)}\n".encode()
    check("oversized line is over 512 KiB", len(raw) > 524_288, f"{len(raw)} bytes")
    events = parser.feed(raw)
    eq("oversized event parsed", len(events), 1)
    eq("oversized payload intact", len(events[0]["data"]["content"]), 900_000)

    # and the same line delivered in small pieces, as a socket would
    parser = SSEParser()
    events = []
    for start in range(0, len(raw), 64 * 1024):
        events.extend(parser.feed(raw[start : start + 64 * 1024]))
    eq("chunked oversized event parsed", len(events), 1)
    eq("chunked payload intact", len(events[0]["data"]["content"]), 900_000)

    parser = SSEParser()
    events = parser.feed(
        b'data: {"type":"x"}\r\ndata: {"type":"y"}\n\ndata: not json\ndata: [DONE]\n'
    )
    eq("crlf handled, junk ignored", events, [{"type": "x"}, {"type": "y"}])

    parser = SSEParser()
    events = parser.feed(b'event: custom\ndata: {"foo":1}\n')
    eq("event: name applied", events, [{"foo": 1, "type": "custom"}])

    # a line past the hard cap is dropped instead of growing without bound
    parser = SSEParser()
    parser.MAX_LINE = 1024
    events = parser.feed(b'data: {"type":"big","pad":"' + b"z" * 5000)
    eq("oversized line dropped", events, [])
    eq("drop counted", parser.dropped, 1)
    eq("parser still usable", parser.feed(b'data: {"type":"after"}\n'), [{"type": "after"}])


def main() -> int:
    test_split_text()
    test_helpers()
    test_turn_state()
    test_turn_failures()
    test_tool_detail()
    test_sse_parser()
    test_store()
    print("\n" + ("=" * 40))
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all unit tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
