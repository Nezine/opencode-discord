"""Byte-for-byte parity between the native SSE parser and the original Python one.

The C++ port in cpp/sse.cpp replaced a hand-rolled parser that carries a lot of
deliberate behaviour (dropping oversized lines without wedging, buffering an
`event:` name across ignored payloads, counting payload length in code points).
This module keeps a verbatim copy of the original implementation and asserts the
two agree on an adversarial corpus.

    .venv/bin/python -m tests.parser_parity
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot._engine import SSEParser  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def eq(name: str, actual: Any, expected: Any) -> None:
    check(name, actual == expected, f"\n       got:      {actual!r}\n       expected: {expected!r}")


class ReferenceSSEParser:
    """The original bot/oc.py::SSEParser, kept as the oracle."""

    MAX_LINE = 8 * 1024 * 1024

    def __init__(self) -> None:
        self._buffer = b""
        self._name: str | None = None
        self.dropped = 0
        self.largest = 0

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buffer += chunk
        events: list[dict[str, Any]] = []
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                if len(self._buffer) > self.MAX_LINE:
                    self.dropped += 1
                    self._buffer = b""
                break
            line = self._buffer[:index]
            self._buffer = self._buffer[index + 1 :]
            if len(line) > self.MAX_LINE:
                self.dropped += 1
                continue
            event = self._line(line)
            if event is not None:
                events.append(event)
        return events

    def _line(self, raw: bytes) -> dict[str, Any] | None:
        line = raw.decode("utf-8", "replace").rstrip("\r")
        if not line:
            return None
        if line.startswith("event:"):
            self._name = line[6:].strip()
            return None
        if not line.startswith("data:"):
            return None
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return None
        self.largest = max(self.largest, len(data))
        try:
            event = json.loads(data)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        if self._name and "type" not in event:
            event["type"] = self._name
        self._name = None
        return event


def chunkings(stream: bytes, size: int | None) -> list[list[bytes]]:
    """Whole-stream, fixed-size, and a couple of pseudo-random splits."""
    options = [[stream]]
    if size is not None:
        options.append([stream[i : i + size] for i in range(0, len(stream), size)])
    if stream:
        rng = random.Random(1234)
        for _ in range(3):
            parts: list[bytes] = []
            pos = 0
            while pos < len(stream):
                step = rng.randint(1, 64)
                parts.append(stream[pos : pos + step])
                pos += step
            options.append(parts)
    return options


def compare(name: str, chunks: list[bytes], max_line: int | None) -> None:
    reference = ReferenceSSEParser()
    native = SSEParser()
    if max_line is not None:
        reference.MAX_LINE = max_line
        native.MAX_LINE = max_line

    ref_events: list[dict[str, Any]] = []
    got_events: list[dict[str, Any]] = []
    for chunk in chunks:
        ref_events.extend(reference.feed(chunk))
        got_events.extend(native.feed(chunk))

    eq(f"{name}: events", got_events, ref_events)
    eq(f"{name}: dropped", native.dropped, reference.dropped)
    eq(f"{name}: largest", native.largest, reference.largest)


def run_case(name: str, stream: bytes, *, max_line: int | None = None, size: int | None = 4096) -> None:
    for index, chunks in enumerate(chunkings(stream, size)):
        compare(f"{name} [split {index}]", chunks, max_line)


def test_corpus() -> None:
    print("\ncorpus")

    run_case("simple", b'data: {"type":"a"}\ndata: {"type":"b"}\n')
    run_case("done marker", b'data: {"type":"a"}\ndata: [DONE]\n')
    run_case("crlf", b'data: {"type":"a"}\r\ndata: {"type":"b"}\r\n')
    run_case("event name injected", b'event: custom\ndata: {"foo":1}\n')
    run_case("event name empty", b'event:\ndata: {"foo":1}\n')
    run_case("event name not overridden", b'event: custom\ndata: {"type":"real"}\n')
    # The name survives an ignored data line, exactly as the original does.
    run_case("name survives done", b'event: custom\ndata: [DONE]\ndata: {"foo":1}\n')
    run_case("name survives garbage", b'event: custom\ndata: not json\ndata: {"foo":1}\n')
    run_case("comments and blanks", b': comment\n\ndata: {"type":"a"}\n\n')
    run_case("other fields", b'id: 7\nretry: 100\ndata: {"type":"a"}\n')
    run_case("leading whitespace", b'   data: {"type":"a"}\n')
    run_case("no space after colon", b'data:{"type":"a"}\n')
    run_case("unicode payload", 'data: {"text":"héllo 😀 漢字"}\n'.encode())

    # Non-dict and unparseable payloads are dropped.
    for label, payload in [
        ("array", b"[1,2,3]"),
        ("number", b"42"),
        ("string", b'"hello"'),
        ("null", b"null"),
        ("true", b"true"),
        ("unparseable", b"{not json"),
        ("empty", b""),
    ]:
        run_case(f"payload {label}", b"data: " + payload + b"\n")

    # Duplicate keys: both keep the last one.
    run_case("duplicate keys", b'data: {"a":1,"a":2}\n')
    # Nested structures survive the nlohmann round trip.
    run_case(
        "nested",
        b'data: {"type":"t","data":{"n":[1,2.5,true,null,"s"],"o":{"k":-3}}}\n',
    )

    # A single event past aiohttp's 512 KiB line cap is the whole reason this
    # parser exists.
    big = b'data: {"type":"big","pad":"' + b"z" * (600 * 1024) + b'"}\n'
    run_case("past 512KiB line", big, size=64 * 1024)

    # Oversized lines are dropped, counted, and the parser stays usable.
    run_case("oversized dropped", b'data: {"pad":"' + b"z" * 5000, max_line=1024)
    run_case(
        "recovers after drop",
        b'data: {"pad":"' + b"z" * 5000 + b'"}\ndata: {"type":"after"}\n',
        max_line=1024,
    )
    run_case("oversized complete line", b"data: " + b"z" * 5000 + b"\n" + b'data: {"type":"x"}\n', max_line=1024)

    # A line arrives in pieces, including mid-UTF-8-sequence and mid-JSON.
    run_case("split mid-sequence", 'data: {"t":"😀"}\n'.encode(), size=1)
    run_case("one byte at a time", b'event: n\ndata: {"a":1}\ndata: [DONE]\n', size=1)

    # Invalid UTF-8 is replaced, then the payload becomes unparseable.
    run_case("invalid utf-8", b'data: {"t":"\xff\xfe"}\n')


def main() -> int:
    test_corpus()
    print("\n" + ("=" * 40))
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("parser parity holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
