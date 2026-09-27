"""Offline checks of the Discord surface: command tree, views, embeds.

    .venv/bin/python -m tests.discord_surface
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

from bot.commands import setup  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.service import Service  # noqa: E402
from bot.store import Store  # noqa: E402
from bot.ui import PermissionView, SessionSelectView, SelectOnlyView, status_embed  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def _valid_name(name: str) -> bool:
    """Discord requires lower-case option names matching ^[a-z0-9_\\-]{1,32}$."""
    import re

    return bool(re.fullmatch(r"[a-z0-9_\-]{1,32}", name or ""))


class StubClient:
    """Just enough OpenCodeClient for the command layer to build against."""

    def __init__(self) -> None:
        self.base = "http://stub"

    async def models(self) -> list[dict]:
        return [
            {
                "id": "gemini-3.8-flash",
                "providerID": "google",
                "name": "Gemini Flash",
                "enabled": True,
                "variants": [{"id": "low"}, {"id": "high"}],
            },
            {"id": "sonnet", "providerID": "anthropic", "name": "Sonnet", "enabled": True, "variants": []},
        ]

    async def agents(self) -> list[dict]:
        return [
            {"id": "build", "name": "Build", "description": "default", "mode": "primary"},
            {"id": "plan", "name": "Plan", "description": "read only", "mode": "primary"},
            {"id": "secret", "name": "Secret", "hidden": True},
        ]

    async def default_model(self) -> dict:
        return {"providerID": "google", "id": "gemini-3.8-flash"}

    async def permissions(self, session_id: str) -> list[dict]:
        return []

    async def get_session(self, session_id: str) -> dict:
        return {"id": session_id, "title": "t", "model": {}, "time": {}}

    async def close(self) -> None:
        return None


def build() -> tuple[app_commands.CommandTree, Service]:
    os.environ["DISCORD_TOKEN"] = "stub-token"
    os.environ["DISCORD_USER_IDS"] = "12345"
    cfg = Config.from_env()
    svc = Service(cfg, StubClient(), Store(":memory:"))  # type: ignore[arg-type]
    intents = discord.Intents.none()
    intents.message_content = True
    intents.dm_messages = True
    bot = discord.Client(intents=intents)
    tree = app_commands.CommandTree(bot)
    setup(tree, svc)
    return tree, svc


def test_tree() -> None:
    print("\ncommand tree")
    tree, _ = build()
    commands = {c.name: c for c in tree.get_commands()}
    expected = {
        "help", "new", "sessions", "use", "history", "model", "effort", "agent",
        "dir", "status", "stop", "undo", "redo", "compact", "rename", "fork",
        "forget", "delete", "refresh", "sync",
    }
    check("all commands registered", expected <= set(commands), f"missing {expected - set(commands)}")
    check("no unexpected commands", set(commands) <= expected, f"extra {set(commands) - expected}")
    for name, command in commands.items():
        check(f"/{name} has a description", bool(command.description))
        payload = command.to_dict(tree)
        for option in payload.get("options", []):
            check(
                f"/{name} {option.get('name')} is well formed",
                bool(option.get("type")) and bool(option.get("description")),
                str(option),
            )
            check(f"/{name} {option.get('name')} name is valid", _valid_name(option.get("name", "")))
    dupes = [c.name for c in tree.get_commands() if sum(1 for x in tree.get_commands() if x.name == c.name) > 1]
    check("no duplicate names", not dupes, str(dupes))


def test_views() -> None:
    print("\nviews")
    tree, svc = build()
    conversation = svc.conversation(12345)
    now = int(time.time() * 1000)
    sessions = [
        {
            "id": f"ses_{i:032d}",
            "title": f"Conversation number {i}",
            "time": {"updated": now - i * 60_000},
            "model": {"providerID": "google", "id": "gemini-3.8-flash", "variant": "high"},
        }
        for i in range(30)
    ]

    view = SessionSelectView(svc, conversation, sessions, page=0, total=30)
    ids = [getattr(item, "custom_id", None) for item in view.children]
    check("select present", any(str(i).startswith("session:pick") for i in ids), str(ids))
    check("pagination buttons", "session:page:0" in ids and "session:page:1" in ids, str(ids))
    check("new button", "session:new" in ids, str(ids))
    check("custom ids unique", len(ids) == len(set(ids)), str(ids))
    select = view.children[0]
    check("20 options per page", len(select.options) == 20, str(len(select.options)))
    check("option values are session ids", all(o.value.startswith("ses_") for o in select.options))
    check("option labels fit Discord", all(len(o.label) <= 100 for o in select.options))
    check("option descriptions fit", all(len(o.description or "") <= 100 for o in select.options))
    check("previous disabled on page 1", next(b for b in view.children if getattr(b, "custom_id", "") == "session:page:0").disabled)

    page2 = SessionSelectView(svc, conversation, sessions, page=1, total=30)
    check("second page has the remainder", len(page2.children[0].options) == 10)
    check("next disabled on last page", next(b for b in page2.children if getattr(b, "custom_id", "") == "session:page:2").disabled)

    empty = SessionSelectView(svc, conversation, [], page=0, total=0)
    check("empty list degrades gracefully", len(empty.children) >= 1)

    permission = PermissionView(svc, "ses_abc", "per_xyz")
    labels = [b.label for b in permission.children]
    check("permission buttons", labels == ["Allow once", "Always allow", "Deny"], str(labels))

    only = SelectOnlyView(
        [discord.SelectOption(label="high", value="high"), discord.SelectOption(label="low", value="low")],
        "Pick",
        lambda i, v: asyncio.sleep(0),
        "effort:pick:0",
    )
    check("select-only view built", len(only.children) == 2)


def test_embeds() -> None:
    print("\nembeds")
    tree, svc = build()
    conversation = svc.conversation(12345)
    session = {
        "id": "ses_abc",
        "title": "My chat",
        "agent": "build",
        "model": {"providerID": "google", "id": "gemini-3.8-flash", "variant": "high"},
        "location": {"directory": "/home/nezine/project"},
        "time": {"updated": int(time.time() * 1000)},
        "cost": 0.1234,
        "tokens": {"input": 1200, "output": 300, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    }
    embed = status_embed(session, conversation.state, pending=2)
    fields = {f.name: f.value for f in embed.fields}
    check("model shown", "gemini-3.8-flash" in fields["Model"])
    check("effort shown", fields["Effort"] == "`high`")
    check("directory shown", "/home/nezine/project" in fields["Directory"])
    check("cost shown", "0.12" in fields["Cost"])
    check("tokens shown", "1.5k" in fields["Tokens"])
    check("pending shown", fields["Pending permissions"] == "2")
    check("title shown", embed.title == "My chat")
    check("session id shown", "ses_abc" in fields["Session"])


def test_config() -> None:
    print("\nconfig")
    os.environ["DISCORD_USER_IDS"] = "1 2,3"
    cfg = Config.from_env()
    check("multiple ids parsed", cfg.allowed(1) and cfg.allowed(2) and cfg.allowed(3))
    check("stranger refused", not cfg.allowed(4))
    os.environ["DISCORD_USER_IDS"] = ""
    os.environ["ALLOW_ANY_USER"] = "1"
    check("allow_any_user works", Config.from_env().allowed(4))
    os.environ.pop("ALLOW_ANY_USER")
    try:
        Config.from_env()
        check("empty whitelist refused", False)
    except SystemExit as exc:
        check("empty whitelist refused", "DISCORD_USER_IDS" in str(exc))


def test_dm_gate() -> None:
    print("\nDM gate")
    os.environ["DISCORD_TOKEN"] = "stub-token"
    os.environ["DISCORD_USER_IDS"] = "12345"
    os.environ.pop("ALLOW_ANY_USER", None)
    cfg = Config.from_env()
    svc = Service(cfg, StubClient(), Store(":memory:"))  # type: ignore[arg-type]
    from bot.main import DMBot

    bot = DMBot(cfg, svc)
    tree = bot.tree

    class FakeChannel:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeUser:
        def __init__(self, user_id: int) -> None:
            self.id = user_id

    class FakeInteraction:
        def __init__(self, channel: object, user_id: int) -> None:
            self.channel = channel
            self.user = FakeUser(user_id)

    dm = discord.DMChannel.__new__(discord.DMChannel)  # isinstance only needs the type
    async def run() -> bool:
        return (
            await tree.interaction_check(FakeInteraction(dm, 12345)),  # type: ignore[arg-type]
            await tree.interaction_check(FakeInteraction(dm, 999)),  # type: ignore[arg-type]
            await tree.interaction_check(FakeInteraction(FakeChannel("#general"), 12345)),  # type: ignore[arg-type]
        )

    allowed_user, stranger, guild = asyncio.run(run())
    check("owner in DM allowed", allowed_user is True)
    check("stranger refused", stranger is False)
    check("guild channel refused", guild is False)
    check("commands are DM-only in the payload", tree.allowed_contexts.guild is False)

    from bot.main import invite_url

    url = invite_url(1553562822846451794)
    check("invite url points at the app", "client_id=1553562822846451794" in url)
    check("invite asks for no server permissions", "permissions=0" in url)
    check("invite scopes bot + commands", "scope=bot%20applications.commands" in url)
    asyncio.run(bot.close())


def test_command_as_text() -> None:
    """A slash command typed before Discord publishes it must not reach the model."""
    from bot.main import DMBot

    os.environ["DISCORD_TOKEN"] = "stub-token"
    os.environ["DISCORD_USER_IDS"] = "12345"
    cfg = Config.from_env()
    bot = DMBot(cfg, Service(cfg, StubClient(), Store(":memory:")))  # type: ignore[arg-type]
    asyncio.run(bot.setup_hook())  # what discord.py does during login
    print("\nslash command typed as text")
    known = {c.name for c in bot.tree.get_commands()}
    check("commands are known to the bot", "sessions" in known and "dir" in known)

    class FakeMessage:
        def __init__(self, content: str) -> None:
            self.content = content
            self.replies: list[str] = []

        async def reply(self, content: str, **kwargs):
            self.replies.append(content)
            return self

    async def guarded(text: str) -> FakeMessage:
        message = FakeMessage(text)
        caught = await bot._warn_if_command_as_text(message, text)
        message.caught = caught  # type: ignore[attr-defined]
        return message

    for text, expected in (
        ("/sessions", True),
        ("/dir ~/code/whatever", True),
        ("  /help  ", True),
        ("/sessions all", True),
        ("/model", True),
        ("/notacommand", False),
        ("/", False),
        ("/dir is a path I use", True),  # still someone reaching for /dir
        ("what does /dir do?", False),
        ("/sess" + "ionsx", False),
        ("a normal question about /sessions", False),
        ("", False),
    ):
        message = asyncio.run(guarded(text))
        check(
            f"{text!r} -> {'intercepted' if expected else 'passed through'}",
            getattr(message, "caught") is expected,
            f"caught={getattr(message, 'caught')}",
        )
        if expected:
            check(f"{text!r} replied with guidance", bool(message.replies))
    asyncio.run(bot.close())


def main() -> int:
    test_tree()
    test_views()
    test_embeds()
    test_config()
    test_dm_gate()
    test_command_as_text()
    print("\n" + "=" * 40)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all discord-surface tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
