"""Messenger must never hand discord.py a None where it expects a collection.

Regression test for a bug that left every deferred slash command stuck on
"thinking…": `Messenger.send` passed `files=None` explicitly, and discord.py's
webhook path then did `for attachment in attachments` on it.

The check runs the real `discord.http.handle_message_parameters`, so it fails for
the same reason the live command did.

    .venv/bin/python -m tests.messenger
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402
from discord.http import handle_message_parameters  # noqa: E402

from bot.messaging import Messenger  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def validate(**kwargs) -> tuple[bool, str]:
    """Run discord.py's own message-parameter handling over these kwargs.

    Mirrors what ``Webhook.send`` does: it passes ``files``/``view`` straight
    through and lets discord.py derive everything else. Unset parameters keep
    their Ellipsis default, which is the sentinel the library checks for.
    """
    params = {k: v for k, v in kwargs.items() if v is not None or k == "content"}
    try:
        handle_message_parameters(**params)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def main() -> int:
    print("\nthe bug: explicit None values")
    try:
        handle_message_parameters(content="hi", files=None)
        raw_ok, raw_err = True, ""
    except Exception as exc:  # noqa: BLE001
        raw_ok, raw_err = False, f"{type(exc).__name__}: {exc}"
    check("explicit files=None blows up in discord.py (this is the bug)", not raw_ok, raw_err)
    if not raw_ok:
        print(f"       -> {raw_err}")
    ok2, err2 = validate(content="hi")
    check("omitting files entirely is fine", ok2, err2)

    print("\nthe fix: Messenger omits them")
    plain = Messenger(channel=object())
    kwargs = plain._kwargs(files=None, view=None)
    check("no files key when there are none", "files" not in kwargs, str(kwargs))
    check("no view key when there is none", "view" not in kwargs, str(kwargs))
    check("allowed_mentions always present", "allowed_mentions" in kwargs)
    ok, err = validate(**kwargs, content="hi")
    check("plain send passes discord.py validation", ok, err)

    print("\nreal values are still forwarded")
    with_file = plain._kwargs(files=None, view=None)
    with_file["files"] = [discord.File(io.BytesIO(b"data"), filename="a.txt")]
    ok, err = validate(**with_file, content="hi")
    check("an actual file passes validation", ok, err)

    view = discord.ui.View(timeout=60)
    with_view = plain._kwargs(files=None, view=None)
    with_view["view"] = view
    ok, err = validate(**with_view, content="hi")
    check("an actual view passes validation", ok, err)

    print("\ndeferred interaction path")
    messenger = Messenger.after_defer(object())  # type: ignore[arg-type]
    kwargs = messenger._kwargs(files=None, view=None)
    check("deferred messenger also omits None", "files" not in kwargs and "view" not in kwargs)
    ok, err = validate(**kwargs, content="picker")
    check("deferred send passes discord.py validation", ok, err)

    print("\nthe real send path, end to end")

    class FakeChannel:
        def __init__(self) -> None:
            self.sent: list[tuple[str | None, dict]] = []

        async def send(self, content=None, **kwargs):
            self.sent.append((content, kwargs))
            return object()

    channel = FakeChannel()
    live = Messenger(channel=channel)  # type: ignore[arg-type]
    asyncio.run(live.send("hello @everyone and @here"))
    content, kwargs = channel.sent[0]
    check("message was sent", content is not None and "hello" in content, str(content))
    check("@everyone neutralised on the way out", "@everyone" not in (content or ""), str(content))
    check("@here neutralised on the way out", "@here" not in (content or ""), str(content))
    check("no files key reached the channel", "files" not in kwargs, str(sorted(kwargs)))
    check("no view key reached the channel", "view" not in kwargs, str(sorted(kwargs)))
    ok, err = validate(content=content, **kwargs)
    check("the real send passes discord.py validation", ok, err)

    print("\n" + "=" * 40)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("messenger kwargs are safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
