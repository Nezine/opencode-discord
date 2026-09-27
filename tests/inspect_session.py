"""Inspect the most recent conversation as the bot stored it.

Answers questions like "was that one turn or two?" without guessing.

    .venv/bin/python -m tests.inspect_session [N]
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config, discover_service  # noqa: E402
from bot.oc import OpenCodeClient  # noqa: E402


async def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    url, username, password = await discover_service()
    os.environ.setdefault("DISCORD_TOKEN", "stub")
    cfg = Config.from_env()
    client = OpenCodeClient(url, username, password)
    await client.start()
    try:
        sessions, _ = await client.list_sessions(limit=count, directory=cfg.opencode_directory)
        for session in sessions:
            print(f"\n=== {session.get('title')} ({session['id']}) ===")
            print(f"    dir={cfg.opencode_directory} model={session.get('model')}")
            messages = await client.messages(session["id"])
            print(f"    {len(messages)} stored messages (oldest last):")
            for message in reversed(messages):
                kind = message.get("type")
                time_info = message.get("time") or {}
                stamp = time_info.get("created")
                if kind == "user":
                    detail = repr((message.get("text") or "")[:70])
                elif kind == "assistant":
                    text = "".join(
                        c.get("text") or ""
                        for c in (message.get("content") or [])
                        if c.get("type") == "text"
                    )
                    tools = [
                        c.get("name")
                        for c in (message.get("content") or [])
                        if c.get("type") == "tool"
                    ]
                    detail = f"{text[:70]!r} tools={tools}"
                elif kind == "idle":
                    detail = f"outcome={message.get('outcome')}"
                else:
                    detail = str(message.get("model") or message.get("title") or "")
                print(f"      {stamp} {kind:14} {detail}")
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
