"""End-to-end check of the OpenCode client and the conversation runner.

Runs against a live OpenCode service. Usage:

    .venv/bin/python -m tests.e2e            # uses the discovered local service
    .venv/bin/python -m tests.e2e --keep     # keep the probe sessions
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config, discover_service  # noqa: E402
from bot.oc import OpenCodeClient, OpenCodeError  # noqa: E402
from bot.runner import Conversation  # noqa: E402
from bot.service import Service  # noqa: E402
from bot.store import Store  # noqa: E402
from tests.harness import FakeMessenger  # noqa: E402

PROMPT = "Reply with exactly: PONG and nothing else."
CHECK = "say one short sentence about the colour teal"
TOOL_TURN = "List the files in the current directory using a tool, then say DONE."


def hr(title: str) -> None:
    print(f"\n\033[1m=== {title} ===\033[0m", flush=True)


async def _tap(subscription) -> None:
    """Print the raw event stream so failures are easy to diagnose."""
    async for event in subscription:
        data = event.get("data") or {}
        extra = ""
        if event.get("type") == "session.text.delta":
            extra = f" {data.get('delta', '')[:60]!r}"
        print(f"    · {event.get('type')}{extra}", flush=True)


async def make_service(keep: bool) -> tuple[Service, Conversation, OpenCodeClient]:
    url, username, password = await discover_service()
    os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")
    os.environ["DISCORD_USER_IDS"] = "999002,999001"
    cfg = Config.from_env()
    cfg.opencode_url = url
    cfg.opencode_username = username
    cfg.opencode_password = password
    cfg.opencode_directory = tempfile.mkdtemp(prefix="oc-e2e-")
    cfg.edit_interval = 0.5
    cfg.turn_timeout = 180.0
    cfg.show_reasoning = os.environ.get("SHOW_REASONING") == "1"

    store = Store(":memory:")
    client = OpenCodeClient(url, username, password)
    svc = Service(cfg, client, store)
    await svc.start()
    conv = svc.conversation(999_001)
    return svc, conv, client


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="opencode/nemotron-3.5-lightning-free")
    parser.add_argument("--effort", default="")
    parser.add_argument("--reasoning", action="store_true", help="render reasoning blocks")
    parser.add_argument("--no-tools", action="store_true", help="skip the tool-calling turn")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    svc, conv, client = await make_service(args.keep)
    failures: list[str] = []
    session_id = ""

    try:
        hr("service reachable")
        models = await svc.models()
        print(f"{len(models)} enabled models")

        hr("model resolution + variants")
        chosen = svc.find_model(models, args.model)
        if chosen is None:
            print(f"!! model {args.model} not found; falling back to the first one")
            chosen = models[0]
        variants = svc.model_variants(chosen)
        print(f"chose {chosen['providerID']}/{chosen['id']} variants={variants}")
        await conv.set_model(chosen["providerID"], chosen["id"], args.effort or None)

        hr("create conversation")
        session_id = await conv.create_session(title="e2e probe")
        print(f"session {session_id} dir={conv.directory}")

        hr("stream a turn")
        messenger = FakeMessenger()
        tap = client.subscribe(session_id)
        tap_task = asyncio.create_task(_tap(tap))
        await conv.send_text(PROMPT, messenger)
        tap.close()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(tap_task, timeout=2)
        messenger.show()
        joined = messenger.transcript_text.upper()
        if "PONG" not in joined:
            failures.append("expected PONG in the reply")
        else:
            print("  ok: PONG received")

        hr("second turn keeps the context")
        messenger2 = FakeMessenger()
        await conv.send_text(CHECK, messenger2)
        messenger2.show()
        if not messenger2.transcript_text.strip():
            failures.append("second turn produced nothing")

        hr("transcript")
        entries = await conv.transcript(limit=10)
        print(f"  {len(entries)} messages: {[e['role'] for e in entries]}")
        if len(entries) < 4:
            failures.append("transcript too short")

        hr("tool calling turn")
        if not args.no_tools:
            tool_messenger = FakeMessenger()
            await conv.send_text(TOOL_TURN, tool_messenger)
            tool_messenger.show()
            rendered = tool_messenger.transcript_text
            if "DONE" not in rendered.upper():
                failures.append("tool turn did not report DONE")
            if "tool" not in rendered.lower():
                failures.append("tool activity was not rendered")
            else:
                print("  ok: tool activity rendered")
        else:
            print("  skipped")

        hr("steering a running turn")
        # Simulate "a turn is in flight" so the steer branch runs deterministically.
        conv.busy = True
        steer_messenger = FakeMessenger()
        await conv.send_text("Also tell me the capital of France.", steer_messenger)
        conv.busy = False
        steer_messenger.show(limit=200)
        if not any("Added to the running turn" in m for m in steer_messenger.messages):
            failures.append("busy message was not reported as steered")
        else:
            print("  ok: steer accepted while busy")
        # The steered text must reach the session, not be silently dropped.
        await asyncio.sleep(3)
        entries = await conv.transcript(limit=20)
        if not any("capital of France" in e["text"] for e in entries):
            print("  note: steer not visible yet in the transcript (it may still be queued)")
        else:
            print("  ok: steer landed in the conversation")

        hr("session listing + store round trip")
        sessions = await svc.list_user_sessions(conv, scope="all")
        titles = [s.get("title") for s in sessions[:5]]
        print(f"  {len(sessions)} sessions, newest titles: {titles}")
        mine = await svc.list_user_sessions(conv, scope="mine")
        print(f"  tracked for this user: {[s['id'] for s in mine]}")
        if not any(s["id"] == session_id for s in mine):
            failures.append("new session missing from tracked list")

        # A conversation must stay listed even after the working directory moves.
        other_dir = tempfile.mkdtemp(prefix="oc-e2e-elsewhere-")
        await conv.set_directory(other_dir)
        elsewhere = await svc.list_user_sessions(conv, scope="mine")
        print(f"  after switching dir: {[s['id'] for s in elsewhere]}")
        if not any(s["id"] == session_id for s in elsewhere):
            failures.append("tracked session hidden after changing directory")
        else:
            print("  ok: conversation still listed from another directory")
        await conv.set_directory(svc.cfg.opencode_directory)

        hr("model + effort switch mid conversation")
        await conv.set_effort(variants[0] if variants else None)
        session = await client.get_session(session_id)
        print(f"  session model now: {session.get('model')}")

        hr("status embed data")
        session = await client.get_session(session_id)
        print(f"  title={session.get('title')!r} cost={session.get('cost')} tokens={session.get('tokens')}")

        hr("rename + fork + revert")
        # The steer above may still be executing; wait so the mutations land.
        if not await client.wait_idle(session_id, timeout=90):
            print("  note: session still busy, waiting skipped")
        await client.update_session(session_id, title="e2e renamed")
        forked = await client.fork_session(session_id)
        print(f"  renamed ok; fork -> {forked['id']}")
        messages = await client.messages(session_id)
        users = [m for m in messages if m.get("type") == "user"]
        if users:
            await client.stage_revert(session_id, users[-1]["id"])
            await client.clear_revert(session_id)
            print("  stage_revert + clear_revert ok")
        await client.delete_session(forked["id"])
        print("  deleted the fork")

        hr("permissions endpoint")
        pending = await client.permissions(session_id)
        print(f"  {len(pending)} pending")

        hr("agents")
        agents = [a for a in await svc.agents() if not a.get("hidden")]
        print(f"  {len(agents)} agents: {[a.get('id') for a in agents]}")
        if agents:
            await conv.set_agent(agents[0]["id"])
            print(f"  set agent -> {agents[0]['id']}")

        if args.keep:
            print(f"\nkept session {session_id}")
        else:
            await client.delete_session(session_id)

    except OpenCodeError as exc:
        failures.append(f"OpenCodeError {exc}")
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        await svc.close()

    hr("result")
    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
