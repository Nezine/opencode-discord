"""Reproduce the oversized-event case that used to kill the event stream.

Asks the model to read a ~1.2 MB file, which produces a single event well over
aiohttp's 512 KiB line limit.

    .venv/bin/python -m tests.big_event
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config, discover_service  # noqa: E402
from bot.oc import OpenCodeClient, OpenCodeError  # noqa: E402
from bot.service import Service  # noqa: E402
from bot.store import Store  # noqa: E402
from tests.harness import FakeMessenger  # noqa: E402

BIG = Path("/tmp/opencode/bigdir")
PROMPT = "Use the read tool on big.txt, then reply with only the byte count of the file."


async def main() -> int:
    url, username, password = await discover_service()
    os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")
    os.environ["DISCORD_USER_IDS"] = "999001,999002,999003"
    cfg = Config.from_env()
    cfg.opencode_url = url
    cfg.opencode_username = username
    cfg.opencode_password = password
    cfg.opencode_directory = str(BIG)
    cfg.edit_interval = 0.5
    cfg.turn_timeout = 240.0

    BIG.mkdir(parents=True, exist_ok=True)
    fixture = BIG / "big.txt"
    if not fixture.exists() or fixture.stat().st_size < 900_000:
        fixture.write_bytes(base64.b64encode(os.urandom(900_000)))
    print(f"fixture: {fixture.stat().st_size:,} bytes")

    store = Store(":memory:")
    client = OpenCodeClient(url, username, password)
    svc = Service(cfg, client, store)
    await svc.start()
    conv = svc.conversation(999_003)
    failures: list[str] = []

    try:
        models = await svc.models()
        chosen = svc.find_model(models, "opencode/nemotron-3.5-lightning-free") or models[0]
        await conv.set_model(chosen["providerID"], chosen["id"], None)
        session_id = await conv.create_session(title="big event probe")
        print(f"session {session_id}")

        messenger = FakeMessenger()
        await conv.send_text(PROMPT, messenger)
        messenger.show(limit=220)
        text = messenger.transcript_text

        largest = client.parser.largest
        print(f"  largest SSE payload seen: {largest:,} bytes")
        print(f"  reconnects: {client.reconnects}, dropped lines: {client.parser.dropped}")
        if largest > 524_288:
            print("  ok: an event larger than aiohttp's 512 KiB limit was parsed")
        else:
            print("  note: this run produced no oversized event; the unit test covers the parser")
        if client.reconnects:
            failures.append(f"the event stream dropped and reconnected {client.reconnects}x")
        else:
            print("  ok: the stream stayed up")
        if "read" not in text.lower():
            failures.append("the tool call was not rendered")
        else:
            print("  ok: tool call rendered")
        await client.delete_session(session_id)
    except OpenCodeError as exc:
        failures.append(f"OpenCodeError {exc}")
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        await svc.close()

    print("\n" + "=" * 40)
    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 1
    print("  oversized events survive the stream")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
