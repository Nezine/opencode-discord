"""Service layer: owns the OpenCode client, the store and per-user conversations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
import discord

from .config import Config
from .messaging import Messenger
from .oc import OpenCodeClient, OpenCodeError
from .runner import BusyError, Conversation
from .store import Store
from .textutil import clip
from .ui import FormView, PermissionView, SessionSelectView, form_embed, permission_embed

log = logging.getLogger("service")

CACHE_TTL = 300.0


class Service:
    def __init__(self, cfg: Config, client: OpenCodeClient, store: Store) -> None:
        self.cfg = cfg
        self.client = client
        self.store = store
        self._conversations: dict[int, Conversation] = {}
        self._models: list[dict] = []
        self._models_at = 0.0
        self._agents: list[dict] = []
        self._agents_at = 0.0
        self._closed = False

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        await self.client.start()
        await self.refresh_models(force=True)
        await self.refresh_agents(force=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for conversation in self._conversations.values():
            if conversation.turn_task:
                conversation.turn_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await conversation.turn_task
        await self.client.close()
        self.store.close()

    # ---------------------------------------------------------- conversations

    def conversation(self, user_id: int) -> Conversation:
        conv = self._conversations.get(user_id)
        if conv is None:
            conv = Conversation(self, user_id)
            self._conversations[user_id] = conv
        return conv

    # ---------------------------------------------------------------- caches

    async def models(self, force: bool = False) -> list[dict]:
        if force or not self._models or (time.time() - self._models_at) > CACHE_TTL:
            models = await self.client.models()
            self._models = [m for m in models if m.get("enabled")]
            self._models_at = time.time()
        return self._models

    async def refresh_models(self, force: bool = False) -> list[dict]:
        return await self.models(force=force)

    async def agents(self, force: bool = False) -> list[dict]:
        if force or not self._agents or (time.time() - self._agents_at) > CACHE_TTL:
            self._agents = await self.client.agents()
            self._agents_at = time.time()
        return self._agents

    async def refresh_agents(self, force: bool = False) -> list[dict]:
        return await self.agents(force=force)

    def find_model(self, models: list[dict], ref: str) -> dict | None:
        """Resolve 'provider/model' or a bare model id."""
        ref = ref.strip()
        for model in models:
            if f"{model.get('providerID')}/{model.get('id')}" == ref:
                return model
        for model in models:
            if model.get("id") == ref:
                return model
        lowered = ref.lower()
        for model in models:
            if str(model.get("id", "")).lower() == lowered:
                return model
        return None

    def model_variants(self, model: dict | None) -> list[str]:
        if not model:
            return []
        return [v.get("id") for v in (model.get("variants") or []) if v.get("id")]

    async def apply_default_model(self, conversation: Conversation) -> dict | None:
        """Seed the user's model preference from the server default."""
        models = await self.models()
        default = None
        with contextlib.suppress(OpenCodeError):
            default = await self.client.default_model()
        picked = None
        if default:
            picked = next(
                (
                    m
                    for m in models
                    if m.get("id") == default.get("id")
                    and m.get("providerID") == default.get("providerID")
                ),
                None,
            )
        if picked is None and self.cfg.default_model:
            picked = self.find_model(models, self.cfg.default_model)
        if picked is None and models:
            picked = models[0]
        if picked is None:
            return None
        variant = self.cfg.default_effort or None
        conversation.state.provider_id = picked.get("providerID")
        conversation.state.model_id = picked.get("id")
        conversation.state.variant = variant if variant in self.model_variants(picked) else None
        conversation.svc.store.save_user(conversation.state)
        return picked

    # ------------------------------------------------------- session listings

    async def list_user_sessions(
        self, conversation: Conversation, query: str | None = None, scope: str = "mine"
    ) -> list[dict]:
        """Sessions to show in the picker.

        ``mine`` is the bot's own history for this user, in most-recently-used
        order and regardless of which directory each one lives in; ``all`` walks
        everything the OpenCode service knows about in the working directory.
        """
        if scope == "all":
            sessions, _ = await self.client.list_sessions(
                limit=self.cfg.session_list_limit, directory=conversation.directory
            )
        else:
            known = self.store.conversation_ids(conversation.user_id, limit=200)
            if not known:
                return []
            # One listing of the current directory catches the common case; any
            # remembered session living elsewhere is fetched on its own, so
            # switching directories never hides old conversations.
            listed, _ = await self.client.list_sessions(
                limit=max(200, self.cfg.session_list_limit), directory=conversation.directory
            )
            known_set = set(known)
            found = {s["id"]: s for s in listed if s.get("id") in known_set}
            for session_id in [sid for sid in known if sid not in found][:25]:
                with contextlib.suppress(OpenCodeError):
                    found[session_id] = await self.client.get_session(session_id)
            sessions = [found[sid] for sid in known if sid in found]
        if query:
            needle = query.lower()
            sessions = [
                s
                for s in sessions
                if needle in (s.get("title") or "").lower() or needle in s.get("id", "")
            ]
        return sessions

    # ------------------------------------------------------------- UI actions

    async def show_sessions(
        self,
        messenger: Messenger,
        conversation: Conversation,
        *,
        query: str | None = None,
        scope: str = "mine",
        page: int = 0,
    ) -> None:
        sessions = await self.list_user_sessions(conversation, query=query, scope=scope)
        if not sessions and scope == "mine":
            # Nothing this user started here yet: fall back to the server's own
            # history so a brand new chat is not an empty screen.
            sessions = await self.list_user_sessions(conversation, query=query, scope="all")
            scope = "all"
        view = SessionSelectView(
            self, conversation, sessions, page=min(page, 999), total=len(sessions), scope=scope
        )
        await messenger.send(view.body(), view=view, ephemeral=True)

    async def handle_new_conversation(
        self, interaction: discord.Interaction, conversation: "Conversation"
    ) -> None:
        """The "+ New conversation" button on the picker."""
        if not self.cfg.allowed(interaction.user.id):
            await interaction.response.send_message("Not allowed.", ephemeral=True)
            return
        try:
            await conversation.create_session()
        except BusyError as exc:
            await interaction.response.send_message(f"⏳ {exc}", ephemeral=True)
            return
        except OpenCodeError as exc:
            hint = "\n-#a turn is still running — `/stop` it first" if exc.conflict else ""
            await interaction.response.send_message(f"❌ `{clip(exc.message, 200)}`{hint}", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=(
                f"🆕 New conversation started\n"
                f"-#`{conversation.session_id}` · "
                f"{conversation.model_label() or 'server default'} · `{conversation.directory}`"
            ),
            view=None,
        )

    async def handle_session_pick(
        self, interaction: discord.Interaction, conversation: Conversation, session_id: str
    ) -> None:
        if not self.cfg.allowed(interaction.user.id):
            await interaction.response.send_message("Not allowed.", ephemeral=True)
            return
        try:
            session = await conversation.use_session(session_id)
        except OpenCodeError as exc:
            await interaction.response.send_message(f"❌ `{clip(exc.message, 200)}`", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=(
                f"✅ Switched to **{session.get('title') or 'untitled'}**\n"
                f"-#`{session_id}` · {conversation.model_label(session)}"
            ),
            view=None,
        )

    async def show_permission_request(
        self, conversation: Conversation, data: dict, messenger: Messenger
    ) -> None:
        if not self.cfg.allowed(conversation.user_id):
            return
        view = PermissionView(self, data.get("sessionID", ""), data.get("id", ""))
        await messenger.send(embed=permission_embed(data), view=view)

    async def show_form(self, conversation: Conversation, form: dict, messenger: Messenger) -> None:
        """Surface a pending "choose an option" form so the user can answer it."""
        if not self.cfg.allowed(conversation.user_id):
            return
        session_id = form.get("sessionID") or conversation.session_id or ""
        view = FormView(self, session_id, form)
        await messenger.send(embed=form_embed(form), view=view)

    # ------------------------------------------------------------ attachments

    async def save_attachment(self, attachment: discord.Attachment) -> dict:
        directory = Path(self.cfg.attachment_dir)
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in attachment.filename if c.isalnum() or c in "._-")[:80] or "file"
        target = directory / f"{int(time.time())}-{safe}"
        await attachment.save(target)
        return {
            "uri": target.resolve().as_uri(),
            "name": attachment.filename,
            "description": f"Attached from Discord ({attachment.content_type or 'unknown'})",
        }
