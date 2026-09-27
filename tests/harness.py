"""A Discord-free stand-in for the pieces the runner touches.

Lets the conversation runner be driven end to end against a real OpenCode
service in tests.
"""

from __future__ import annotations

from typing import Any


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.edits: list[str] = []
        self.deleted = False

    async def edit(self, content: str | None = None, **kwargs: Any) -> "FakeMessage":
        if content is not None:
            self.content = content
            self.edits.append(content)
        return self

    async def delete(self) -> None:
        self.deleted = True


class FakeMessenger:
    """Collects everything the bot would have sent."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.interaction = None
        self.ephemeral = False
        self.last: FakeMessage | None = None
        self.all: list[FakeMessage] = []
        self.embeds: list[Any] = []
        self.views: list[Any] = []
        self.files: list[Any] = []

    @property
    def target(self) -> None:
        return None

    @property
    def transcript_text(self) -> str:
        """Everything the user would see: sends plus each message's final content."""
        parts: list[str] = []
        for text, message in zip(self.messages, self.all, strict=False):
            if message.deleted:
                continue
            parts.append(message.content if message.content != text else text)
        return "\n".join(parts)

    def show(self, limit: int = 400) -> None:
        for text, message in zip(self.messages, self.all, strict=False):
            if message.deleted:
                print(f"  [deleted placeholder] {text[:limit]!r}")
                continue
            suffix = f" ({len(message.edits)} edits)" if message.edits else ""
            print(f"  [msg{suffix}] {message.content[:limit]!r}")

    async def defer(self, ephemeral: bool | None = None) -> None:
        return None

    async def send(self, content: str | None = None, **kwargs: Any) -> FakeMessage | None:
        embed = kwargs.get("embed")
        if embed is not None:
            self.embeds.append(embed)
        if kwargs.get("embeds"):
            self.embeds.extend(kwargs["embeds"])
        if kwargs.get("view") is not None:
            self.views.append(kwargs["view"])
        if kwargs.get("files"):
            self.files.append(kwargs["files"])
        if content is None and not kwargs.get("files"):
            # Embed/view-only message: still a message the user sees.
            self.messages.append(f"<embed: {getattr(embed, 'title', '') or 'message'}>")
            self.last = FakeMessage(self.messages[-1])
            self.all.append(self.last)
            return self.last
        text = content or ""
        self.messages.append(text)
        self.last = FakeMessage(text)
        self.all.append(self.last)
        return self.last

    def dump(self) -> str:
        return "\n---\n".join(self.messages)
