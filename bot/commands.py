"""Slash commands. Every command replies ephemerally inside the DM."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from .messaging import Messenger
from .oc import OpenCodeError
from .runner import BusyError, Conversation
from .service import Service
from .textutil import clip, rel_time, split_text
from .ui import EM_DASH, SelectOnlyView, status_embed

log = logging.getLogger("commands")

HELP = """**OpenCode over Discord** — chat in this DM, control it with slash commands.

**Talking**
• Just type a message — it goes to the model.
• `/new [title]` — start a fresh conversation
• `/sessions [query]` — browse past conversations
• `/use <conversation>` — jump to one by id or title
• `/history [n]` — replay recent messages
• `/stop` — interrupt the running turn

**Controls**
• `/model [provider/model]` — pick the model (autocomplete)
• `/effort [level]` — reasoning effort for the current model
• `/agent [name]` — switch agent (build / plan)
• `/dir [path]` — working directory for new conversations
• `/status` — current session, model, cost

**Session tools**
• `/undo` — roll back the last exchange
• `/redo` — cancel a pending rollback
• `/compact` — summarise the context
• `/rename <title>` — set the conversation title
• `/fork` — branch the conversation
• `/forget` — drop it from the list
• `/delete` — delete it on the server

**Housekeeping**
• `/refresh` — reload models and agents from the server
• `/sync` — publish these commands to your DMs immediately
• `/help` — this message

Type `/` in the DM to see everything. Uploads (images, files) are forwarded to the model.
"""


def setup(tree: app_commands.CommandTree, svc: Service) -> None:
    def messenger(interaction: discord.Interaction) -> Messenger:
        return Messenger(interaction=interaction, ephemeral=True)

    def deferred(interaction: discord.Interaction) -> Messenger:
        """A messenger for commands that already deferred their response."""
        return Messenger.after_defer(interaction)

    def conv(interaction: discord.Interaction) -> Conversation:
        return svc.conversation(interaction.user.id)

    def failure(exc: OpenCodeError) -> str:
        """Render an API error, adding a hint for the common 409 case."""
        text = f"❌ `{clip(exc.message, 300)}`"
        if exc.conflict:
            text += "\n-#a turn is still running — `/stop` it first"
        return text

    # ------------------------------------------------------------- autocomplete

    async def model_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            models = await svc.models()
        except OpenCodeError:
            return []
        needle = current.strip().lower()
        seen: set[str] = set()
        choices: list[app_commands.Choice[str]] = []
        for model in models:
            provider = model.get("providerID", "")
            ref = f"{provider}/{model.get('id')}"
            if ref in seen:
                continue
            haystack = f"{ref} {model.get('name', '')}".lower()
            if needle and needle not in haystack:
                continue
            seen.add(ref)
            variants = [v.get("id") for v in (model.get("variants") or []) if v.get("id")]
            suffix = f" — effort: {', '.join(variants[:4])}" if variants else ""
            choices.append(
                app_commands.Choice(name=f"{model.get('name') or model.get('id')}{suffix}"[:100], value=ref)
            )
            if len(choices) >= 25:
                break
        return choices

    async def effort_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        conversation = conv(interaction)
        models = await svc.models()
        model = svc.find_model(models, f"{conversation.state.provider_id}/{conversation.state.model_id}")
        options = ["default", *svc.model_variants(model)]
        needle = current.strip().lower()
        return [app_commands.Choice(name=o, value=o) for o in options if not needle or needle in o]

    async def agent_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        agents = await svc.agents()
        needle = current.strip().lower()
        out = []
        for agent in agents:
            if agent.get("hidden"):
                continue
            value = agent.get("id") or agent.get("name", "")
            if needle and needle not in value.lower() and needle not in str(agent.get("name", "")).lower():
                continue
            out.append(app_commands.Choice(name=f"{agent.get('name') or value} ({value})"[:100], value=value))
            if len(out) >= 25:
                break
        return out

    async def session_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        conversation = conv(interaction)
        needle = current.strip().lower()
        try:
            sessions = await svc.list_user_sessions(conversation, scope="all")
        except OpenCodeError:
            return []
        out: list[app_commands.Choice[str]] = []
        for session in sessions:
            title = session.get("title") or "untitled"
            model = (session.get("model") or {}).get("id", EM_DASH)
            haystack = f"{title} {session['id']} {model}".lower()
            if needle and needle not in haystack:
                continue
            label = f"{title} · {rel_time((session.get('time') or {}).get('updated'))} · {model}"
            out.append(app_commands.Choice(name=label[:100], value=session["id"]))
            if len(out) >= 25:
                break
        return out

    # ------------------------------------------------------------------- help

    @tree.command(name="help", description="Show the command list and how to use the bot")
    async def cmd_help(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(HELP, ephemeral=True)

    # ---------------------------------------------------------- conversations

    @tree.command(name="new", description="Start a new conversation")
    @app_commands.describe(
        title="Optional name for the conversation",
        directory="Working directory for this conversation",
    )
    async def cmd_new(
        interaction: discord.Interaction,
        title: str | None = None,
        directory: str | None = None,
    ) -> None:
        conversation = conv(interaction)
        try:
            await conversation.create_session(title=title, directory=directory)
        except BusyError as exc:
            await interaction.response.send_message(f"⏳ {exc}", ephemeral=True)
            return
        except OpenCodeError as exc:
            await interaction.response.send_message(f"❌ `{clip(exc.message, 300)}`", ephemeral=True)
            return
        await interaction.response.send_message(
            "🆕 New conversation"
            + (f" **{title}**" if title else "")
            + f"\n-#`{conversation.session_id}` · {conversation.model_label() or 'server default'}"
            f" · `{conversation.directory}`",
            ephemeral=True,
        )

    @tree.command(name="sessions", description="Browse and pick past conversations")
    @app_commands.describe(
        query="Filter by title or id",
        scope="mine = started here, all = everything on the server",
    )
    @app_commands.choices(scope=[app_commands.Choice(name="mine", value="mine"), app_commands.Choice(name="all", value="all")])
    async def cmd_sessions(
        interaction: discord.Interaction,
        query: str | None = None,
        scope: str = "mine",
    ) -> None:
        conversation = conv(interaction)
        await interaction.response.defer(ephemeral=True)
        m = deferred(interaction)
        try:
            await svc.show_sessions(m, conversation, query=query, scope=scope)
        except OpenCodeError as exc:
            await m.send(f"❌ `{clip(exc.message, 300)}`")

    @tree.command(name="use", description="Switch to a past conversation")
    @app_commands.describe(conversation="Conversation id, or part of its title")
    @app_commands.autocomplete(conversation=session_autocomplete)
    async def cmd_use(interaction: discord.Interaction, conversation: str) -> None:
        conv_obj = conv(interaction)
        try:
            session = await conv_obj.use_session(conversation)
        except OpenCodeError as exc:
            await interaction.response.send_message(
                f"❌ Could not switch: `{clip(exc.message, 200)}`", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Now chatting in **{session.get('title') or 'untitled'}**\n"
            f"-#`{session['id']}` · {conv_obj.model_label(session)}",
            ephemeral=True,
        )

    @tree.command(name="history", description="Replay the last messages of this conversation")
    @app_commands.describe(limit="How many messages to show (max 20)")
    async def cmd_history(interaction: discord.Interaction, limit: int = 10) -> None:
        conv_obj = conv(interaction)
        await interaction.response.defer(ephemeral=True)
        m = deferred(interaction)
        try:
            entries = await conv_obj.transcript(limit=max(1, min(20, limit)))
        except OpenCodeError as exc:
            await m.send(failure(exc))
            return
        if not entries:
            await m.send("_No messages in this conversation yet._")
            return
        lines = ["**Recent messages**", ""]
        for entry in entries:
            who = "🧑 you" if entry["role"] == "user" else "🤖 model"
            body = " ".join(entry["text"].split())
            lines.append(f"{who}: {clip(body, 300)}")
            lines.append("")
        for chunk in split_text("\n".join(lines), 1900):
            await m.send(chunk)

    # -------------------------------------------------------------- settings

    @tree.command(name="model", description="Choose the model (leave empty to see the list)")
    @app_commands.describe(model="provider/model, e.g. google/gemini-3.8-flash")
    @app_commands.autocomplete(model=model_autocomplete)
    async def cmd_model(interaction: discord.Interaction, model: str | None = None) -> None:
        conv_obj = conv(interaction)
        if not model:
            conversation = conv_obj
            current = conversation.state
            if not current.has_model:
                await svc.apply_default_model(conversation)
            models = await svc.models()
            chosen = svc.find_model(models, f"{current.provider_id}/{current.model_id}")
            variants = svc.model_variants(chosen)
            text = (
                f"**Current model:** `{current.model_label}`"
                f"\n**Effort levels:** {', '.join(f'`{v}`' for v in variants) if variants else '_none_'}"
                f"\n**Directory:** `{conversation.directory}`"
                "\n\nUse `/model provider/model` to switch — start typing to autocomplete."
            )
            await interaction.response.send_message(text, ephemeral=True)
            return
        models = await svc.models()
        chosen = svc.find_model(models, model)
        if chosen is None:
            await interaction.response.send_message(
                f"❌ Unknown model `{model}`. Start typing `/model` to see the list.", ephemeral=True
            )
            return
        variant = conv_obj.state.variant
        if variant and variant not in svc.model_variants(chosen):
            variant = None
        try:
            await conv_obj.set_model(chosen["providerID"], chosen["id"], variant)
        except BusyError as exc:
            await interaction.response.send_message(f"⏳ {exc}", ephemeral=True)
            return
        except OpenCodeError as exc:
            await interaction.response.send_message(f"❌ `{clip(exc.message, 300)}`", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🧠 Model set to `{chosen['providerID']}/{chosen['id']}`"
            + (f" · effort `{variant}`" if variant else "")
            + "\n-#applies to the current conversation and future ones",
            ephemeral=True,
        )

    @tree.command(name="effort", description="Set the reasoning effort (leave empty to pick)")
    @app_commands.describe(level="low / medium / high / … depends on the model")
    @app_commands.autocomplete(level=effort_autocomplete)
    async def cmd_effort(interaction: discord.Interaction, level: str | None = None) -> None:
        conv_obj = conv(interaction)
        if not conv_obj.state.has_model:
            await svc.apply_default_model(conv_obj)
        models = await svc.models()
        chosen = svc.find_model(models, f"{conv_obj.state.provider_id}/{conv_obj.state.model_id}")
        available = svc.model_variants(chosen)
        if not available:
            await interaction.response.send_message(
                f"ℹ️ `{conv_obj.state.model_label}` does not expose effort levels.", ephemeral=True
            )
            return
        if level is None:
            options = [
                discord.SelectOption(
                    label="default",
                    description="Let the provider decide",
                    value="default",
                    default=not conv_obj.state.variant,
                )
            ]
            options += [
                discord.SelectOption(
                    label=v,
                    description=f"effort: {v}",
                    value=v,
                    default=v == conv_obj.state.variant,
                )
                for v in available
            ]

            async def picked(inter: discord.Interaction, value: str) -> None:
                await _apply_effort(inter, conv_obj, value)

            view = SelectOnlyView(options, "Pick an effort level…", picked, "effort:pick:0")
            await interaction.response.send_message(
                f"**Effort for** `{conv_obj.state.model_label}`", ephemeral=True, view=view
            )
            return
        await _apply_effort(interaction, conv_obj, level)

    async def _apply_effort(
        interaction: discord.Interaction, conv_obj: Conversation, level: str
    ) -> None:
        models = await svc.models()
        chosen = svc.find_model(models, f"{conv_obj.state.provider_id}/{conv_obj.state.model_id}")
        available = svc.model_variants(chosen)
        value = None if level in ("default", "none", "") else level
        if value and value not in available:
            await interaction.response.send_message(
                f"❌ `{level}` is not available for this model. Options: {', '.join(available)}",
                ephemeral=True,
            )
            return
        try:
            await conv_obj.set_effort(value)
        except BusyError as exc:
            await interaction.response.send_message(f"⏳ {exc}", ephemeral=True)
            return
        except OpenCodeError as exc:
            await interaction.response.send_message(failure(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"⚡ Effort set to `{value or 'default'}` for `{conv_obj.state.model_label}`",
            ephemeral=True,
        )

    @tree.command(name="agent", description="Switch agent (leave empty to pick)")
    @app_commands.describe(name="Agent id, e.g. build or plan")
    @app_commands.autocomplete(name=agent_autocomplete)
    async def cmd_agent(interaction: discord.Interaction, name: str | None = None) -> None:
        conv_obj = conv(interaction)
        agents = [a for a in await svc.agents() if not a.get("hidden")]
        if name is None:
            options = []
            for agent in agents[:25]:
                value = agent.get("id") or agent.get("name", "")
                options.append(
                    discord.SelectOption(
                        label=f"{agent.get('name') or value} ({value})"[:100],
                        description=clip(agent.get("description") or "", 100) or None,
                        value=value,
                        default=value == conv_obj.state.agent,
                    )
                )
            if not options:
                await interaction.response.send_message("No agents available.", ephemeral=True)
                return

            async def picked(inter: discord.Interaction, value: str) -> None:
                await _apply_agent(inter, conv_obj, value)

            view = SelectOnlyView(options, "Pick an agent…", picked, "agent:pick:0")
            await interaction.response.send_message(
                f"**Current agent:** `{conv_obj.state.agent or svc.cfg.default_agent}`",
                ephemeral=True,
                view=view,
            )
            return
        await _apply_agent(interaction, conv_obj, name)

    async def _apply_agent(
        interaction: discord.Interaction, conv_obj: Conversation, name: str
    ) -> None:
        try:
            await conv_obj.set_agent(name)
        except OpenCodeError as exc:
            await interaction.response.send_message(failure(exc), ephemeral=True)
            return
        await interaction.response.send_message(f"🧩 Agent set to `{name}`", ephemeral=True)

    @tree.command(name="dir", description="Show or set the working directory")
    @app_commands.describe(path="Absolute path on this machine")
    async def cmd_dir(interaction: discord.Interaction, path: str | None = None) -> None:
        conv_obj = conv(interaction)
        if not path:
            await interaction.response.send_message(
                f"📁 Working directory: `{conv_obj.directory}`\n"
                "Set a new one with `/dir /path/to/project` (applies to the next `/new`).",
                ephemeral=True,
            )
            return
        from pathlib import Path

        target = Path(path).expanduser()
        if not target.is_dir():
            await interaction.response.send_message(f"❌ `{path}` is not a directory.", ephemeral=True)
            return
        await conv_obj.set_directory(str(target.resolve()))
        await interaction.response.send_message(f"📁 New conversations will use `{target.resolve()}`", ephemeral=True)

    @tree.command(name="status", description="Show the current session, model and usage")
    async def cmd_status(interaction: discord.Interaction) -> None:
        conv_obj = conv(interaction)
        await interaction.response.defer(ephemeral=True)
        m = deferred(interaction)
        try:
            session = await conv_obj.status()
            pending = len(await svc.client.permissions(session["id"]))
        except OpenCodeError as exc:
            await m.send(failure(exc))
            return
        await m.send(embed=status_embed(session, conv_obj.state, pending=pending))

    # ------------------------------------------------------------- turn tools

    @tree.command(name="stop", description="Interrupt the running turn")
    async def cmd_stop(interaction: discord.Interaction) -> None:
        conv_obj = conv(interaction)
        m = messenger(interaction)
        await conv_obj.stop(m)

    @tree.command(name="undo", description="Roll back the last exchange")
    async def cmd_undo(interaction: discord.Interaction) -> None:
        conv_obj = conv(interaction)
        await interaction.response.defer(ephemeral=True)
        m = deferred(interaction)
        try:
            session_id = await conv_obj.ensure_session()
            messages = await svc.client.messages(session_id)
        except OpenCodeError as exc:
            await m.send(failure(exc))
            return
        users = [msg for msg in messages if msg.get("type") == "user"]
        if not users:
            await m.send("_Nothing to undo._")
            return
        target = users[-1]
        try:
            await svc.client.stage_revert(session_id, target["id"])
        except OpenCodeError as exc:
            await m.send(failure(exc))
            return
        await m.send(
            f"↩️ Reverted to before __{clip(target.get('text') or '', 120)}__\n-#/redo cancels this"
        )

    @tree.command(name="redo", description="Cancel a pending rollback")
    async def cmd_redo(interaction: discord.Interaction) -> None:
        conv_obj = conv(interaction)
        try:
            session_id = await conv_obj.ensure_session()
            await svc.client.clear_revert(session_id)
        except OpenCodeError as exc:
            await interaction.response.send_message(failure(exc), ephemeral=True)
            return
        await interaction.response.send_message("↪️ Rollback cancelled.", ephemeral=True)

    @tree.command(name="compact", description="Summarise the context to save tokens")
    async def cmd_compact(interaction: discord.Interaction) -> None:
        conv_obj = conv(interaction)
        await interaction.response.defer(ephemeral=True)
        m = deferred(interaction)
        try:
            session_id = await conv_obj.ensure_session()
            await svc.client.compact(session_id)
        except OpenCodeError as exc:
            await m.send(failure(exc))
            return
        await m.send("🧹 Context compacted.")

    # ------------------------------------------------------- session editing

    @tree.command(name="rename", description="Rename the conversation")
    @app_commands.describe(title="New title")
    async def cmd_rename(interaction: discord.Interaction, title: str) -> None:
        conv_obj = conv(interaction)
        try:
            session_id = await conv_obj.ensure_session()
            await svc.client.update_session(session_id, title=title[:120])
        except OpenCodeError as exc:
            await interaction.response.send_message(failure(exc), ephemeral=True)
            return
        conv_obj.state.title = title[:120]
        svc.store.save_user(conv_obj.state)
        svc.store.remember(conv_obj.user_id, session_id, title[:120])
        await interaction.response.send_message(f"✏️ Renamed to **{title[:120]}**", ephemeral=True)

    @tree.command(name="fork", description="Branch this conversation into a new one")
    async def cmd_fork(interaction: discord.Interaction) -> None:
        conv_obj = conv(interaction)
        try:
            session_id = await conv_obj.ensure_session()
            forked = await svc.client.fork_session(session_id)
        except OpenCodeError as exc:
            await interaction.response.send_message(failure(exc), ephemeral=True)
            return
        await conv_obj.use_session(forked["id"])
        await interaction.response.send_message(
            f"🍴 Forked into a new conversation\n-#`{forked['id']}`", ephemeral=True
        )

    @tree.command(name="forget", description="Remove the conversation from the list (keeps the data)")
    async def cmd_forget(interaction: discord.Interaction) -> None:
        conv_obj = conv(interaction)
        if not conv_obj.session_id:
            await interaction.response.send_message("No conversation to forget.", ephemeral=True)
            return
        svc.store.forget(conv_obj.user_id, conv_obj.session_id)
        await interaction.response.send_message("🗑️ Removed from your list.", ephemeral=True)

    @tree.command(name="delete", description="Delete the conversation on the server")
    async def cmd_delete(interaction: discord.Interaction) -> None:
        conv_obj = conv(interaction)
        if not conv_obj.session_id:
            await interaction.response.send_message("No conversation to delete.", ephemeral=True)
            return
        session_id = conv_obj.session_id
        try:
            await svc.client.delete_session(session_id)
        except OpenCodeError as exc:
            await interaction.response.send_message(failure(exc), ephemeral=True)
            return
        svc.store.forget(conv_obj.user_id, session_id)
        conv_obj.state.session_id = None
        conv_obj.state.title = None
        svc.store.save_user(conv_obj.state)
        await interaction.response.send_message("🗑️ Deleted. `/new` starts a fresh one.", ephemeral=True)

    @tree.command(name="refresh", description="Reload models and agents from the server")
    async def cmd_refresh(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        m = deferred(interaction)
        try:
            models = await svc.refresh_models(force=True)
            agents = await svc.refresh_agents(force=True)
        except OpenCodeError as exc:
            await m.send(failure(exc))
            return
        await m.send(f"🔄 Loaded {len(models)} models and {len(agents)} agents.")

    @tree.command(name="sync", description="Re-register the slash commands with Discord")
    async def cmd_sync(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        m = deferred(interaction)
        bot = interaction.client
        count = await bot.publish_commands() if hasattr(bot, "publish_commands") else 0
        if not count:
            await m.send("❌ Could not register the commands. Check the bot logs.")
            return
        await m.send(
            f"✅ Re-registered {count} commands.\n"
            "-#Discord can take up to an hour to show a newly added command in DMs; "
            "already-visible ones update within a minute."
        )
