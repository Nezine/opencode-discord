"""Opt-in live smoke: recover a saved queue, run two turns, export their text.

Uses an isolated temporary project/database and deletes only its own session.
Requires an enabled model; defaults to the same free model as tests.e2e.
No Discord login or messages, and the prompts explicitly request no tool calls.
"""

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import tempfile

from bot._engine import export_transcript
from bot.config import Config, discover_service
from bot.oc import OpenCodeClient
from bot.runner import Conversation
from bot.service import Service
from bot.store import Store
from tests.harness import FakeMessenger


async def run(model: str) -> None:
    os.environ.setdefault("DISCORD_TOKEN", "not-used-in-this-test")
    os.environ["DISCORD_USER_IDS"] = "999003"
    url, username, password = await discover_service()
    with tempfile.TemporaryDirectory(prefix="oc-workflow-") as directory:
        cfg = Config.from_env()
        cfg.opencode_directory = directory
        cfg.turn_timeout = 90
        cfg.stall_timeout = 30
        cfg.edit_interval = 0.5
        cfg.show_reasoning = False
        store = Store(Path(directory) / "state.db")
        client = OpenCodeClient(url, username, password)
        svc = Service(cfg, client, store)
        session_id = None
        try:
            await svc.start()
            chosen = svc.find_model(await svc.models(), model)
            if chosen is None:
                raise RuntimeError("Requested test model is not enabled. Choose one explicitly with --model provider/model.")
            conv = svc.conversation(999003)
            await conv.set_model(chosen["providerID"], chosen["id"], None)
            session_id = await conv.create_session(title="workflow smoke (temporary)")
            for marker in ("WORKFLOW_ONE", "WORKFLOW_TWO"):
                conv._mutate_queue(conv.queue.push, {"session_id": session_id,
                    "text": f"Reply exactly {marker}. Do not call any tools.", "files": []})
            restored = Conversation(svc, 999003)
            svc._conversations[999003] = restored
            assert restored.queue.size == 2 and not restored.busy
            messenger = FakeMessenger()
            await asyncio.wait_for(restored.resume_queue(messenger), 200)
            assert not restored.busy and restored.queue.size == 0, "queue did not drain completely"
            assert "WORKFLOW_ONE" in messenger.transcript_text, "first reply missing"
            assert "WORKFLOW_TWO" in messenger.transcript_text, "second reply missing"
            session = await client.get_session(session_id)
            messages = await client.messages(session_id)
            exported = json.loads(export_transcript(session, messages, "json"))
            answers = [entry["text"] for entry in exported["messages"] if entry["role"] == "assistant"]
            assert len(answers) >= 2
            assert "WORKFLOW_ONE" in answers[0] and "WORKFLOW_TWO" in answers[-1], repr([
                {"role": entry["role"], "one": "WORKFLOW_ONE" in entry["text"], "two": "WORKFLOW_TWO" in entry["text"]}
                for entry in exported["messages"]])
            assert not client._subscribers, "runner leaked an event subscription"
            print("PASS: saved queue restored; two real turns ran in order; full export verified; subscriptions released.")
        finally:
            try:
                if session_id:
                    with contextlib.suppress(Exception):
                        await client.interrupt(session_id)
                        await client.wait_idle(session_id, timeout=10)
                    await client.delete_session(session_id)
                    print("Removed the temporary test session (no user conversations touched).")
            finally:
                await svc.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="opencode/nemotron-3.5-lightning-free")
    args = parser.parse_args()
    asyncio.run(run(args.model))
