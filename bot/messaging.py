"""Sending helpers that work for both slash commands and plain DM messages."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord
from discord import app_commands

from .textutil import sanitize_mentions

log = logging.getLogger("messaging")

NO_MENTIONS = discord.AllowedMentions.none()
# A slash-command interaction token dies ~15 minutes after the initial response.
TOKEN_TTL = 13 * 60


class Messenger:
    """Sends messages into a DM, whichever way the user reached the bot."""

    def __init__(
        self,
        *,
        interaction: app_commands.Interaction | None = None,
        channel: discord.abc.Messageable | None = None,
        reference: discord.Message | None = None,
        mention_author: bool = True,
        ephemeral: bool = False,
        _deferred: bool = False,
    ) -> None:
        self.interaction = interaction
        self.channel = channel
        self.reference = reference
        self.mention_author = mention_author and reference is not None
        self.ephemeral = ephemeral
        self._done = _deferred
        self._reply: discord.Message | None = reference

    @property
    def target(self) -> discord.abc.Messageable | None:
        if self.interaction is not None:
            return self.interaction.channel
        return self.channel

    @classmethod
    def after_defer(cls, interaction: app_commands.Interaction) -> "Messenger":
        """For commands that already deferred: everything goes through followups."""
        return cls(interaction=interaction, ephemeral=True, _deferred=True)

    def _kwargs(self, **extra: Any) -> dict[str, Any]:
        """Build send kwargs, omitting anything unset.

        Passing ``files=None`` explicitly is not the same as omitting it: on the
        webhook path discord.py then iterates the None and raises
        "TypeError: 'NoneType' object is not iterable", which leaves a deferred
        command stuck showing "thinking…" forever. Same for ``view=None``.
        """
        kwargs: dict[str, Any] = {"allowed_mentions": NO_MENTIONS}
        if self.reference is not None and self._reply is None and "reference" not in extra:
            kwargs["reference"] = self.reference
            kwargs["mention_author"] = self.mention_author
            kwargs["silent"] = True
        for key, value in extra.items():
            if value is not None:
                kwargs[key] = value
        return kwargs

    async def defer(self, ephemeral: bool | None = None) -> None:
        if self.interaction is None or self.interaction.response.is_done():
            return
        flag = self.ephemeral if ephemeral is None else ephemeral
        try:
            await self.interaction.response.defer(ephemeral=flag)
            self._done = True
        except discord.HTTPException as exc:
            log.debug("defer failed: %s", exc)

    async def send(
        self,
        content: str | None = None,
        *,
        files: Any = None,
        view: discord.ui.View | None = None,
        ephemeral: bool | None = None,
        **extra: Any,
    ) -> discord.Message | None:
        # An embed (or a file) is a payload in its own right: /status and the
        # permission prompts send no text at all, and returning early here is
        # what left those commands stuck on "thinking…".
        has_embed = extra.get("embed") is not None or bool(extra.get("embeds"))
        if content is None and not files and not has_embed:
            return None
        # allowed_mentions blocks pings; sanitize_mentions is the belt to that
        # braces, in case a call site ever sends without the guard.
        content = sanitize_mentions(content)
        kwargs = self._kwargs(files=files or None, view=view, **extra)
        if self.interaction is not None and not self._done:
            flag = self.ephemeral if ephemeral is None else ephemeral
            kwargs.pop("reference", None)
            kwargs.pop("mention_author", None)
            kwargs.pop("silent", None)
            await self.interaction.response.send_message(content, ephemeral=flag, **kwargs)
            self._done = True
            return None
        if self.interaction is not None:
            kwargs.pop("reference", None)
            kwargs.pop("mention_author", None)
            kwargs.pop("silent", None)
            if ephemeral is not None and ephemeral != self.ephemeral:
                return await self.interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
            return await self.interaction.followup.send(content, **kwargs)
        target = self.target
        if target is None:
            return None
        return await target.send(content, **kwargs)


class LiveMessage:
    """A message that is edited in place while a turn streams.

    Edits are throttled to stay under Discord's rate limit, and the live message
    transparently re-posts itself if the interaction token is about to expire.
    """

    def __init__(self, messenger: Messenger, interval: float = 1.5) -> None:
        self.messenger = messenger
        self.interval = max(0.5, interval)
        self.message: discord.Message | None = None
        self._last_edit = 0.0
        self._created = 0.0
        self._content: str | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._repost_needed = False

    @property
    def created_at(self) -> float:
        return self._created

    async def start(self, content: str) -> None:
        if self._closed:
            return
        self._content = content
        self._created = time.monotonic()
        self._last_edit = self._created
        self.message = await self.messenger.send(content)
        if self.message is None and self.messenger.interaction is not None:
            # interaction replies are not editable; fall back to followups
            self._repost_needed = True
        else:
            self._repost_needed = False

    async def update(self, content: str, *, force: bool = False) -> None:
        if self._closed or content == self._content:
            return
        now = time.monotonic()
        if not force and (now - self._last_edit) < self.interval:
            return
        async with self._lock:
            if self._closed or content == self._content:
                return
            self._content = content
            self._last_edit = now
            if self._repost_needed or self._expired():
                self.message = await self.messenger.send(content)
                self._created = now
                self._repost_needed = False
                return
            try:
                await self.message.edit(content=content, allowed_mentions=NO_MENTIONS)
            except discord.NotFound:
                self.message = await self.messenger.send(content)
                self._created = now
            except discord.HTTPException as exc:
                log.debug("live edit failed: %s", exc)

    async def finish(self, content: str | None = None) -> None:
        if self._closed:
            return
        if content is not None:
            await self.update(content, force=True)
        self._closed = True

    async def delete(self) -> None:
        """Drop the placeholder entirely (used when a step produced nothing)."""
        if self._closed:
            return
        self._closed = True
        message, self.message = self.message, None
        if message is None:
            return
        try:
            await message.delete()
        except AttributeError:
            pass
        except discord.HTTPException as exc:
            log.debug("could not delete placeholder: %s", exc)

    def _expired(self) -> bool:
        if self.messenger.interaction is None:
            return False
        return (time.monotonic() - self._created) > TOKEN_TTL
