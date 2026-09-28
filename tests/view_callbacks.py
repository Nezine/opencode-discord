"""Every interactive item in every view must have a usable callback.

discord.py 2.7 builds a Button with a custom_id and no callback without
complaining, so a dead button only shows up when a user clicks it. This walks
each view and checks the handlers are actually attached and coroutines.

    .venv/bin/python -m tests.view_callbacks
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

from bot.config import Config  # noqa: E402
from bot.service import Service  # noqa: E402
from bot.store import Store  # noqa: E402
from bot.ui import (  # noqa: E402
    PER_PAGE,
    FormView,
    PermissionView,
    SessionSelectView,
    SelectOnlyView,
)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


class StubClient:
    async def models(self) -> list[dict]:
        return []

    async def agents(self) -> list[dict]:
        return []

    async def close(self) -> None:
        return None


def build():
    os.environ["DISCORD_TOKEN"] = "stub"
    os.environ["DISCORD_USER_IDS"] = "12345"
    cfg = Config.from_env()
    return Service(cfg, StubClient(), Store(":memory:"))  # type: ignore[arg-type]


def sessions(count: int) -> list[dict]:
    now = int(time.time() * 1000)
    return [
        {
            "id": f"ses_{i:032d}",
            "title": f"Conversation {i}",
            "time": {"updated": now - i * 60_000},
            "model": {"providerID": "google", "id": "gemini-3.8-flash", "variant": "high"},
        }
        for i in range(count)
    ]


def item_rows(view: discord.ui.View) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for child in view.children:
        out.append((str(getattr(child, "custom_id", "")), child))
    return out


def is_awaitable_callback(callback: object) -> bool:
    """True if discord.py can `await callback(interaction)`.

    Decorated buttons carry an ``ItemCallback`` wrapper, which is valid; a plain
    non-coroutine function is not, because calling it merely produces a coroutine
    that nobody awaits.
    """
    if callback is None:
        return False
    if inspect.iscoroutinefunction(callback):
        return True
    return hasattr(callback, "callback") and inspect.iscoroutinefunction(
        getattr(callback, "callback", None)
    )


def assert_live(view: discord.ui.View, label: str) -> None:
    for custom_id, item in item_rows(view):
        callback = getattr(item, "callback", None)
        check(
            f"{label}: {custom_id or item} has a callback",
            is_awaitable_callback(callback),
            f"got {callback!r}",
        )


def main() -> int:
    svc = build()
    conversation = svc.conversation(12345)

    print("\nSessionSelectView (one page)")
    view = SessionSelectView(svc, conversation, sessions(5), page=0, total=5)
    assert_live(view, "page1")
    for child in view.children:
        if isinstance(child, discord.ui.Button) and not child.disabled:
            check(
                f"button {child.custom_id} is labelled",
                bool(child.label or child.emoji),
                "no label",
            )

    print("\nSessionSelectView (many pages, first and last)")
    many = sessions(PER_PAGE * 3 + 7)
    first = SessionSelectView(svc, conversation, many, page=0, total=len(many))
    assert_live(first, "first")
    check("first page next enabled", not next(b for b in first.children if getattr(b, "custom_id", "") == "session:page:1").disabled)
    check("first page prev disabled", next(b for b in first.children if getattr(b, "custom_id", "") == "session:page:0").disabled)
    check("page counter", any("Page 1/4" == getattr(b, "label", "") for b in first.children))

    last = SessionSelectView(svc, conversation, many, page=3, total=len(many))
    assert_live(last, "last")
    check("last page next disabled", next(b for b in last.children if getattr(b, "custom_id", "") == "session:page:4").disabled)
    check("last page has the remainder", len(last.children[0].options) == 7)

    print("\nSessionSelectView (empty)")
    empty = SessionSelectView(svc, conversation, [], page=0, total=0)
    assert_live(empty, "empty")
    check("empty body says so", "Nothing here yet" in empty.body())

    print("\nPage navigation actually re-renders")
    pages_seen: list[int] = []

    class FakeResponse:
        def __init__(self) -> None:
            self.done = False
            self.edits: list[tuple[str, object]] = []

        def is_done(self) -> bool:
            return self.done

        async def edit_message(self, content=None, view=None, **kwargs):
            self.edits.append((content, view))

    class FakeInteraction:
        def __init__(self) -> None:
            self.response = FakeResponse()
            self.user = type("U", (), {"id": 12345})()

    interaction = FakeInteraction()
    next_button = next(b for b in first.children if getattr(b, "custom_id", "") == "session:page:1")
    asyncio.run(next_button.callback(interaction))
    check("clicking ▶ moved the page", first.page == 1, f"page={first.page}")
    check("the message was edited", len(interaction.response.edits) == 1)
    edited_content, edited_view = interaction.response.edits[0]
    check("body reflects the new page", "page 2 of 4" in edited_content, edited_content[:80])
    check("the same view instance was reattached", edited_view is first)
    check("select now shows page 2", "Conversation 20" in edited_content)
    pages_seen.append(first.page)

    print("\nPermissionView")
    permission = PermissionView(svc, "ses_abc", "per_xyz")
    assert_live(permission, "perm")
    labels = [b.label for b in permission.children]
    check("three decisions", labels == ["Allow once", "Always allow", "Deny"], str(labels))

    print("\nSelectOnlyView")
    only = SelectOnlyView(
        [discord.SelectOption(label="high", value="high"), discord.SelectOption(label="low", value="low")],
        "Pick",
        lambda i, v: asyncio.sleep(0),
        "effort:pick:0",
    )
    assert_live(only, "select")
    check("select has two options", len(only.children[0].options) == 2)

    print("\nFormView")
    form = FormView(
        svc,
        "ses_abc",
        {
            "id": "frm_1",
            "sessionID": "ses_abc",
            "title": "Pick one",
            "fields": [
                {
                    "key": "choice",
                    "type": "string",
                    "title": "Choice",
                    "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}],
                },
                {"key": "ok", "type": "boolean", "title": "Confirm"},
            ],
        },
    )
    assert_live(form, "form")
    check("form has a select and boolean buttons", len(form.children) == 3, str(len(form.children)))

    print("\nselect callback is a coroutine")
    for view, label in ((first, "session select"), (permission, "permission"), (only, "select-only")):
        selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
        for select in selects:
            check(
                f"{label}: select {select.custom_id} callback is awaitable",
                is_awaitable_callback(select.callback),
                f"got {select.callback!r}",
            )

    svc.store.close()
    print("\n" + "=" * 40)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("every view item is wired up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
