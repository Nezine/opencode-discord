"""Permission-request flow: the bot must surface the ask and resume after a reply.

    .venv/bin/python -m tests.permissions
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config, discover_service  # noqa: E402
from bot.oc import OpenCodeClient  # noqa: E402
from bot.service import Service  # noqa: E402
from bot.store import Store  # noqa: E402
from bot.ui import PermissionView  # noqa: E402
from tests.harness import FakeMessenger  # noqa: E402

# Reading a system file sits outside the session directory, which the build
# agent treats as an "external_directory" ask.
PROMPT = "Use the read tool on /etc/hostname and tell me the first line."


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    url, username, password = await discover_service()
    os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")
    os.environ["DISCORD_USER_IDS"] = "999002,999001"
    cfg = Config.from_env()
    cfg.opencode_url = url
    cfg.opencode_username = username
    cfg.opencode_password = password
    cfg.opencode_directory = tempfile.mkdtemp(prefix="oc-perm-")
    cfg.edit_interval = 0.5
    cfg.turn_timeout = 240.0

    store = Store(":memory:")
    client = OpenCodeClient(url, username, password)
    svc = Service(cfg, client, store)
    await svc.start()
    conv = svc.conversation(999_002)
    failures: list[str] = []

    try:
        models = await svc.models()
        chosen = svc.find_model(models, "opencode/nemotron-3.5-lightning-free") or models[0]
        await conv.set_model(chosen["providerID"], chosen["id"], None)
        session_id = await conv.create_session(title="permission probe")
        print(f"session {session_id} dir={conv.directory}")

        messenger = FakeMessenger()
        tap = client.subscribe(session_id)
        seen: list[str] = []

        async def watch() -> None:
            import json

            async for event in tap:
                kind = event.get("type", "?")
                seen.append(kind)
                print(f"    · {kind}", flush=True)
                if kind.startswith("permission."):
                    print(f"        {json.dumps(event.get('data'))[:300]}", flush=True)

        watcher = asyncio.create_task(watch())
        turn = asyncio.create_task(conv.send_text(PROMPT, messenger))

        view: PermissionView | None = None
        for _ in range(120):
            await asyncio.sleep(1)
            for candidate in messenger.views:
                if isinstance(candidate, PermissionView):
                    view = candidate
                    break
            if view is not None or turn.done():
                break

        if view is None:
            failures.append("no permission request was surfaced to the user")
            print("!! no permission view was delivered")
            print("   messenger messages:", messenger.messages)
        else:
            print(f"permission surfaced: request={view.request_id}")
            print(f"   embed title: {messenger.embeds[0].title if messenger.embeds else None}")
            print(f"   buttons: {[b.label for row in view.children for b in getattr(row, 'children', [row])]}")
            if not messenger.embeds:
                failures.append("permission message had no embed")
            pending = await client.permissions(session_id)
            print(f"   server-side pending: {len(pending)}")
            if not pending:
                failures.append("server reported no pending permission")
            print("   replying 'once' …")
            await client.reply_permission(session_id, view.request_id, "once")

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(turn, timeout=200)
        tap.close()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        print(f"  events seen: {seen}")
        messenger.show(limit=300)
        hostname = Path("/etc/hostname").read_text().strip().splitlines()[0].lower()
        if hostname not in messenger.transcript_text.lower():
            failures.append("turn did not resume with the file content")
        else:
            print("  ok: the turn resumed after the approval")
        await client.delete_session(session_id)
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
    print("  permission flow works")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
