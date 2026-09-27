"""Run real slash-command callbacks against a fake interaction.

This is the layer the "stuck on thinking…" bug lived in: a command defers, then
sends through a followup. The fake interaction runs discord.py's own message
validation on every send, so any kwargs the library would reject fail here.

    .venv/bin/python -m tests.commands_flow
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
from discord.ext import commands as dpy_commands  # noqa: E402
from discord.http import handle_message_parameters  # noqa: E402

from bot.commands import setup  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.service import Service  # noqa: E402
from bot.store import Store  # noqa: E402
from bot.ui import PER_PAGE  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def sessions(count: int) -> list[dict]:
    now = int(time.time() * 1000)
    return [
        {
            "id": f"ses_{i:032d}",
            "title": f"Chat {i}",
            "time": {"created": now, "updated": now - i * 60_000},
            "model": {"providerID": "google", "id": "gemini-3.8-flash", "variant": "high"},
            "location": {"directory": "/tmp"},
            "agent": "build",
            "cost": 0.1,
            "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        }
        for i in range(count)
    ]


class StubClient:
    def __init__(self, count: int = 45) -> None:
        self.count = count
        self.prompts: list[tuple[str, str | None]] = []

    async def list_sessions(self, *, limit=50, cursor=None, directory=None, search=None, order="desc"):
        data = sessions(self.count)
        if directory not in (None, "/tmp", str(Path.home())):
            data = []
        return data, {"next": None}

    async def get_session(self, session_id: str) -> dict:
        for session in sessions(self.count):
            if session["id"] == session_id:
                return session
        # Sessions the bot invented during the test (/new, /fork) are fine too.
        now = int(time.time() * 1000)
        return {
            "id": session_id,
            "title": "generated",
            "time": {"created": now, "updated": now},
            "location": {"directory": "/tmp"},
            "model": {"providerID": "google", "id": "gemini-3.8-flash", "variant": "default"},
            "agent": "build",
            "cost": 0.0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        }

    async def create_session(self, *, title=None, agent=None, model=None, directory=None) -> dict:
        return {
            "id": "ses_new000000000000000000000000",
            "title": title or "untitled",
            "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
            "location": {"directory": directory or "/tmp"},
        }

    async def permissions(self, session_id: str) -> list[dict]:
        return []

    async def prompt(self, session_id, text, *, files=None, delivery=None) -> dict:
        self.prompts.append((text, delivery))
        return {"id": "msg_x"}

    async def models(self) -> list[dict]:
        return [
            {"id": "gemini-3.8-flash", "providerID": "google", "name": "Flash", "enabled": True,
             "variants": [{"id": "low"}, {"id": "high"}]},
        ]

    async def agents(self) -> list[dict]:
        return [{"id": "build", "name": "Build", "description": "d", "hidden": False}]

    async def default_model(self) -> dict:
        return {"providerID": "google", "id": "gemini-3.8-flash"}

    async def fork_session(self, session_id: str, before: str | None = None) -> dict:
        return {
            "id": "ses_fork000000000000000000000000",
            "title": "fork",
            "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
            "location": {"directory": "/tmp"},
        }

    async def close(self) -> None:
        return None

    def __getattr__(self, name: str):
        """Anything this test does not care about is a no-op.

        The point of this module is the send path, not API completeness, so
        unlisted endpoints quietly succeed instead of demanding stubs.
        """
        if name.startswith("_"):
            raise AttributeError(name)

        async def _noop(*args, **kwargs):
            return {}

        return _noop


class FakeResponse:
    def __init__(self, log: list) -> None:
        self.log = log
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, ephemeral=False, thinking=False):
        self.log.append(("defer", {"ephemeral": ephemeral}))
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self.log.append(("initial", {"content": content, **kwargs}))
        self.done = True

    async def edit_message(self, content=None, view=None, **kwargs):
        self.log.append(("edit", {"content": content, "view": view, **kwargs}))

    async def send_modal(self, *args, **kwargs):
        self.log.append(("modal", kwargs))


class FakeFollowup:
    def __init__(self, log: list) -> None:
        self.log = log

    async def send(self, content=None, **kwargs):
        self.log.append(("followup", {"content": content, **kwargs}))
        return object()


class FakeUser:
    id = 12345

    def __str__(self) -> str:
        return "tester"


async def _publish() -> int:
    """Stand-in for DMBot.publish_commands()."""
    return 20


class FakeInteraction:
    """Enough of an Interaction for a command callback to run against it."""

    def __init__(self) -> None:
        self.log: list[tuple[str, dict]] = []
        self.response = FakeResponse(self.log)
        self.followup = FakeFollowup(self.log)
        self.user = FakeUser()
        self.channel = discord.Object(id=1)
        self.guild = None
        self.message = None
        self.command_failed = False
        self.command = None
        # /sync asks the bot to re-register its commands.
        # staticmethod: a bare function in a class dict would bind self.
        self.client = type("FakeClient", (), {"publish_commands": staticmethod(_publish)})()

    @property
    def sends(self) -> list[dict]:
        return [payload for kind, payload in self.log if kind in {"initial", "followup"}]

    def validate_all(self) -> tuple[bool, str]:
        for kind, payload in self.log:
            if kind not in {"initial", "followup"}:
                continue
            # `ephemeral` is consumed by InteractionResponse/followup, not by
            # handle_message_parameters, so drop it before validating.
            params = {
                k: v
                for k, v in payload.items()
                if (v is not None or k == "content") and k != "ephemeral"
            }
            try:
                handle_message_parameters(**params)
            except Exception as exc:  # noqa: BLE001
                return False, f"{kind}: {type(exc).__name__}: {exc}"
        return True, ""


def build_tree(client: StubClient) -> tuple[app_commands.CommandTree, Service]:
    os.environ["DISCORD_TOKEN"] = "stub"
    os.environ["DISCORD_USER_IDS"] = "12345"
    cfg = Config.from_env()
    svc = Service(cfg, client, Store(":memory:"))  # type: ignore[arg-type]
    intents = discord.Intents.none()
    bot = dpy_commands.Bot(command_prefix=lambda m: [], intents=intents, help_command=None)
    tree = bot.tree  # a Bot already owns one; a second cannot be attached
    setup(tree, svc)
    return tree, svc


def call(tree: app_commands.CommandTree, name: str, **kwargs) -> FakeInteraction:
    command = tree.get_command(name)
    assert command is not None, name
    interaction = FakeInteraction()
    asyncio.run(command.callback(interaction, **kwargs))  # type: ignore[misc]
    return interaction


def main() -> int:
    print("\n/sessions (deferred, then a followup with a view)")
    tree, svc = build_tree(StubClient(45))
    interaction = call(tree, "sessions")
    ok, err = interaction.validate_all()
    check("every send passes discord.py validation", ok, err)
    check("it deferred first", interaction.log[0][0] == "defer", str(interaction.log[:1]))
    check("a message was sent", bool(interaction.sends), str(interaction.log))
    view = next((p.get("view") for p in interaction.sends if p.get("view")), None)
    check("the picker was attached", view is not None)
    if view is not None:
        check("picker has a select", any(isinstance(c, discord.ui.Select) for c in view.children))
        check("select is full", len(view.children[0].options) == PER_PAGE, str(len(view.children[0].options)))

    print("\n/sessions with a filter")
    interaction = call(tree, "sessions", query="Chat 3", scope="mine")
    ok, err = interaction.validate_all()
    check("filtered send validates", ok, err)
    check("something was sent", bool(interaction.sends))

    print("\n/other deferred commands")
    for name, kwargs in (
        ("history", {}),
        ("status", {}),
        ("model", {}),
        ("effort", {}),
        ("agent", {}),
        ("dir", {}),
        ("help", {}),
        ("stop", {}),
        ("compact", {}),
        ("undo", {}),
        ("redo", {}),
        ("fork", {}),
        ("refresh", {}),
        ("sync", {}),
    ):
        interaction = call(tree, name, **kwargs)
        ok, err = interaction.validate_all()
        check(f"/{name} sends cleanly", ok, err)
        check(f"/{name} replied", bool(interaction.sends), str(interaction.log))

    print("\n/no boolean and int options")
    interaction = call(tree, "history", limit=5)
    check("/history with a value replied", bool(interaction.sends))
    interaction = call(tree, "new", title="fresh one")
    ok, err = interaction.validate_all()
    check("/new with a value sends cleanly", ok, err)
    check("/new replied", bool(interaction.sends))

    svc.store.close()
    print("\n" + "=" * 40)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("every command sends valid payloads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
