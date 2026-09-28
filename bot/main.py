"""Entry point: DM-only Discord bot backed by a local OpenCode service."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import sys
import time
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from .config import Config, discover_service, state_dir
from .messaging import Messenger
from .oc import OpenCodeClient, OpenCodeError
from .runner import BusyError
from .service import Service
from .store import Store
from .textutil import clip

log = logging.getLogger("bot")

STATE_DIR = state_dir()
DB_PATH = STATE_DIR / "state.db"
LOCK_PATH = STATE_DIR / "bot.lock"
NO_MENTIONS = discord.AllowedMentions.none()
DM_ONLY = app_commands.AppCommandContext(guild=False, dm_channel=True, private_channel=True)


def invite_url(application_id: int) -> str:
    """Zero-permission invite: the bot only needs to share a server to allow DMs."""
    return (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={application_id}&permissions=0&scope=bot%20applications.commands"
    )


class GatedTree(app_commands.CommandTree):
    """Command tree that only answers DMs from allow-listed users.

    Returning False marks the interaction as failed without a reply, so anyone
    else poking the bot in a guild simply gets silence.
    """

    def __init__(self, client: commands.Bot) -> None:
        super().__init__(client)
        self.allowed_user_ids: set[int] = set()
        self.allow_any_user = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.channel, discord.DMChannel):
            return False
        if self.allow_any_user:
            return True
        return interaction.user.id in self.allowed_user_ids


class DMBot(commands.Bot):
    # How often the same stranger may be told their own user id.
    HINT_COOLDOWN = 900.0

    def __init__(self, cfg: Config, svc: Service) -> None:
        intents = discord.Intents.none()
        intents.message_content = True
        intents.dm_messages = True
        intents.dm_typing = True
        super().__init__(
            command_prefix=lambda message: [],  # prefix commands are disabled on purpose
            intents=intents,
            tree_cls=GatedTree,
            allowed_contexts=DM_ONLY,
            help_command=None,
        )
        self.cfg = cfg
        self.svc = svc
        assert isinstance(self.tree, GatedTree)
        self.tree.allowed_user_ids = cfg.allowed_user_ids
        self.tree.allow_any_user = cfg.allow_any_user
        self._ready_event = asyncio.Event()
        self._hints: dict[int, float] = {}

    async def setup_hook(self) -> None:
        from .commands import setup

        setup(self.tree, self.svc)
        log.info("registered %d commands", len(self.tree.get_commands()))

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s)", self.user, self.user.id)
        if self.user:
            log.info("invite link: %s", invite_url(self.user.id))
        log.info(
            "waiting for DMs — the bot must share a server with you before Discord "
            "will let you message it"
        )
        await self.publish_commands()
        self._ready_event.set()

    async def publish_commands(self) -> int:
        """Register the slash commands with Discord.

        Only a *global* registration reaches DM channels. The old trick of
        registering to the "DM guild" (the bot's own user id) is rejected by
        Discord with 403 Missing Access, so it is not an option here. Global
        commands are the ones that appear in DMs, at the cost of propagation:
        Discord can take up to an hour to make a brand new command visible.
        """
        commands_ = self.tree.get_commands()
        try:
            body = await self.tree.sync()
        except discord.HTTPException as exc:
            log.error("could not register commands with Discord: %s", exc)
            return 0
        log.info("registered %d commands with Discord", len(commands_))
        if body is not None:
            log.info("Discord accepted: %s", getattr(body, "__class__", type(body)).__name__)
        return len(commands_)

    async def ready(self) -> None:
        await self._ready_event.wait()

    # ------------------------------------------------------------- DM routing

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not isinstance(message.channel, discord.DMChannel):
            return
        if not self.cfg.allowed(message.author.id):
            log.warning("ignoring DM from unauthorised user %s", message.author.id)
            await self._offer_user_id(message)
            return
        if self.user is None:
            return

        text = message.content or ""
        if await self._warn_if_command_as_text(message, text):
            return
        files: list[dict] = []
        for attachment in message.attachments:
            if attachment.size > 25 * 1024 * 1024:
                await message.reply(
                    f"⚠️ `{attachment.filename}` is too large (max 25 MB).",
                    allowed_mentions=NO_MENTIONS,
                )
                continue
            try:
                files.append(await self.svc.save_attachment(attachment))
            except OSError as exc:
                log.warning("could not save attachment: %s", exc)

        if not text and not files:
            return

        conversation = self.svc.conversation(message.author.id)
        messenger = Messenger(channel=message.channel, reference=message)
        typing = message.channel.typing()

        async def run() -> None:
            async with typing:
                try:
                    await conversation.send_text(text, messenger, files=files)
                except BusyError as exc:
                    await messenger.send(f"⏳ {exc}")
                except OpenCodeError as exc:
                    await messenger.send(
                        f"❌ **OpenCode error**\n`{clip(exc.message, 400)}`\n"
                        "-#is the `opencode` service running? Try `opencode service status`."
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("unhandled error while chatting")
                    await messenger.send("❌ Something went wrong. Check the bot logs.")

        asyncio.create_task(run(), name=f"dm-{message.author.id}")

    async def _warn_if_command_as_text(self, message: discord.Message, text: str) -> bool:
        """Stop a slash command that was typed as plain text from reaching the model.

        Without this, `/sessions` sent before Discord has published the commands is
        forwarded to the agent, which helpfully tries to *run* it — and an agent
        with shell access will create directories and files in the process.
        """
        stripped = text.strip()
        if not stripped.startswith("/") or len(stripped) > 120:
            return False
        rest = stripped[1:].split()
        if not rest:  # a bare "/" opens Discord's command box; it is not a command
            return False
        name = rest[0].split(":")[0].lower()
        if name not in {command.name for command in self.tree.get_commands()}:
            return False
        await message.reply(
            f"`{self._command_label(name)}` is a slash command, and Discord hasn't "
            "made it available in this DM yet.\n"
            "Type `/` in the box to see the list, or `/help` for what each one does.\n"
            f"-#nothing was sent to the model. Commands are registered on start-up, but "
            "Discord can take up to an hour to publish them to DMs.",
            allowed_mentions=NO_MENTIONS,
        )
        return True

    @staticmethod
    def _command_label(name: str) -> str:
        return f"/{name}"

    async def _offer_user_id(self, message: discord.Message) -> None:
        """Tell a stranger their own user id so they can add themselves.

        Otherwise a private bot is indistinguishable from a broken one: it just
        never answers. Rate limited, and the id is their own, so nothing leaks.
        """
        if not self.cfg.allowlist_hint:
            return
        now = time.monotonic()
        last = self._hints.get(message.author.id, 0.0)
        if now - last < self.HINT_COOLDOWN:
            return
        if len(self._hints) > 500:  # keep the map from growing forever
            self._hints = {uid: at for uid, at in self._hints.items() if now - at < self.HINT_COOLDOWN}
        self._hints[message.author.id] = now
        try:
            await message.reply(
                f"This bot is private, so I can't chat with you yet.\n"
                f"Your Discord user id is `{message.author.id}`\n"
                f"Add it to `DISCORD_USER_IDS` in `.env`, restart the bot, then send `/sync` here.",
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as exc:
            log.debug("could not send the allow-list hint: %s", exc)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.warning("command error from %s: %s", interaction.user.id, error, exc_info=error)
        message = "❌ Something went wrong handling that command."
        if isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, OpenCodeError):
            message = f"❌ `{clip(error.original.message, 300)}`"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def close(self) -> None:
        await self.svc.close()
        await super().close()


def acquire_lock() -> Any:
    """Take an exclusive lock so two instances cannot answer the same DMs.

    Without this a second copy silently connects to the gateway as well, and every
    reply gets sent twice. flock is released by the kernel when the process dies,
    so a stale lock cannot wedge a restart.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


async def connect_with_retry(bot: DMBot, token: str) -> None:
    """Log in to Discord, retrying transient connection/DNS failures.

    ``network-online.target`` only guarantees an interface is up, not that the
    resolver is answering, so a boot can race the bot and fail the first login
    with ``Temporary failure in name resolution``. Let that settle instead of
    letting the process exit and relying on a systemd restart.
    """
    deadline = time.monotonic() + 60.0
    attempt = 0
    while True:
        attempt += 1
        try:
            await bot.start(token)
            return
        except discord.LoginFailure:
            raise
        except (aiohttp.ClientConnectorError, discord.HTTPException) as exc:
            cause = getattr(exc, "__cause__", None)
            retryable = isinstance(exc, aiohttp.ClientConnectorError) or isinstance(
                cause, aiohttp.ClientConnectorError
            )
            if retryable and time.monotonic() < deadline:
                delay = min(2.0 * attempt, 10.0)
                log.warning(
                    "could not reach Discord (%s); retrying in %.0fs", exc, delay
                )
                await asyncio.sleep(delay)
                continue
            raise


async def main() -> int:
    lock = acquire_lock()
    if lock is None:
        print(
            f"Another instance is already running (see {LOCK_PATH}).\n"
            "Two copies would both answer your DMs, so refusing to start.",
            file=sys.stderr,
        )
        return 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    cfg = Config.from_env()
    url, username, password = await discover_service()
    cfg.opencode_url = url
    cfg.opencode_username = username
    cfg.opencode_password = password
    log.info("opencode service: %s (auth: %s)", cfg.opencode_url, "yes" if password else "no")

    store = Store(DB_PATH)
    client = OpenCodeClient(cfg.opencode_url, cfg.opencode_username, cfg.opencode_password)
    service = Service(cfg, client, store)

    try:
        await service.start()
    except OpenCodeError as exc:
        log.error("cannot reach the OpenCode service at %s: %s", cfg.opencode_url, exc.message)
        return 1

    bot = DMBot(cfg, service)
    async with bot:
        try:
            await connect_with_retry(bot, cfg.discord_token)
        except discord.LoginFailure:
            log.error("Discord rejected DISCORD_TOKEN. Check the value in .env.")
            return 1
        except discord.HTTPException as exc:
            log.error("could not log in to Discord: %s", exc)
            return 1
        finally:
            await service.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
