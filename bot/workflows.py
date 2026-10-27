"""Queue, project presets, transcript export/search and pending-request recovery."""

from __future__ import annotations

import functools
import io
import logging
from typing import Literal

import discord
from discord import app_commands

from ._engine import export_transcript, project_name, transcript_entries
from .messaging import Messenger
from .oc import OpenCodeError
from .runner import BusyError
from .textutil import clip, split_text

log = logging.getLogger("workflows")
PROJECT_FIELDS = ("session_id", "provider_id", "model_id", "variant", "agent", "directory", "title")


def handled(fn):
    @functools.wraps(fn)
    async def invoke(interaction, *args, **kwargs):
        await interaction.response.defer(ephemeral=True)
        try:
            return await fn(interaction, *args, **kwargs)
        except (ValueError, BusyError, OpenCodeError) as exc:
            await Messenger.after_defer(interaction).send(f"⚠️ {clip(str(exc), 400)}")
        except Exception:
            log.exception("workflow command failed")
            await Messenger.after_defer(interaction).send("❌ Could not complete this command. Please try again.")
    return invoke


def setup_workflows(tree: app_commands.CommandTree, svc) -> None:
    @tree.command(name="ask", description="Send a message now, queue it, or steer the current turn")
    @handled
    async def ask(interaction: discord.Interaction, text: str,
                  delivery: Literal["queue", "steer"] = "queue") -> None:
        if not text.strip():
            raise ValueError("Enter a message.")
        await Messenger.after_defer(interaction).send("Message received. Progress will appear in this DM.")
        await svc.conversation(interaction.user.id).send_text(text, Messenger(channel=interaction.channel), delivery=delivery)

    @tree.command(name="queue", description="List, remove, clear or resume your queued messages")
    @handled
    async def queue(interaction: discord.Interaction,
                    action: Literal["list", "remove", "clear", "resume"] = "list",
                    item_id: int | None = None) -> None:
        conv = svc.conversation(interaction.user.id)
        m = Messenger.after_defer(interaction)
        if action == "list":
            items = conv.queue.entries()
            if not items:
                await m.send("The queue is empty.")
                return
            lines = [f"**{len(items)} waiting** · {'running' if conv.busy else 'paused'}"]
            lines += [f"#{item['id']} · {clip(item['text'] or '(attachment)', 160)}" for item in items]
            lines.append("`/queue remove item_id:…` · `/queue clear` · `/queue resume`")
            for chunk in split_text("\n".join(lines), 1900):
                await m.send(chunk)
        elif action == "resume":
            await m.send("Checking the queue. Progress will appear in this DM.")
            await conv.resume_queue(Messenger(channel=interaction.channel))
        elif action == "remove":
            if item_id is None:
                raise ValueError("Provide the item_id shown by /queue.")
            count = await conv.remove_queued(item_id)
            await m.send(f"Removed queued message #{item_id}." if count else "That message is no longer queued.")
        else:
            await m.send(f"Removed {await conv.remove_queued()} queued messages. The current turn is unchanged.")

    @tree.command(name="export", description="Download this conversation as Markdown or JSON")
    @handled
    async def export(interaction: discord.Interaction,
                     format: Literal["markdown", "json"] = "markdown") -> None:
        conv = svc.conversation(interaction.user.id)
        if not conv.session_id:
            raise ValueError("There is no active conversation to export.")
        session_id = conv.session_id
        session = await svc.client.get_session(session_id)
        messages = await svc.client.messages(session_id)
        data = export_transcript(session, messages, format).encode("utf-8")
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("This export exceeds 8 MB. Export it from OpenCode on your computer.")
        extension = "md" if format == "markdown" else "json"
        with io.BytesIO(data) as buffer:
            file = discord.File(buffer, filename=f"conversation.{extension}")
            try:
                await Messenger.after_defer(interaction).send("Full text transcript (tool output and reasoning excluded).", files=[file])
            finally:
                file.close()

    @tree.command(name="search", description="Search the full text of your current conversation")
    @handled
    async def search(interaction: discord.Interaction, query: str) -> None:
        needle = query.strip().casefold()
        if not needle:
            raise ValueError("Enter something to search for.")
        conv = svc.conversation(interaction.user.id)
        if not conv.session_id:
            raise ValueError("There is no active conversation to search.")
        entries = transcript_entries(await svc.client.messages(conv.session_id))
        matches = [(index, entry) for index, entry in enumerate(entries, 1) if needle in entry["text"].casefold()]
        m = Messenger.after_defer(interaction)
        if not matches:
            await m.send("No matching messages in this conversation.")
            return
        await m.send(f"Found {len(matches)} matching messages. Showing the last 10; `/export` downloads the full text.")
        for index, entry in matches[-10:]:
            # Find a matching line so a late match is not hidden by clipping.
            lines = entry["text"].splitlines()
            snippet = next((line for line in lines if needle in line.casefold()), entry["text"])
            folded = snippet.casefold()
            # Keep Unicode indices aligned even when case folding expands a letter.
            start = folded.find(needle)
            if start > 200:
                offset = 0
                for index_in_line, char in enumerate(snippet):
                    if offset >= start:
                        snippet = snippet[max(0, index_in_line - 100):]
                        break
                    offset += len(char.casefold())
            await m.send(f"**Message {index} · {entry['role']}**\n{clip(snippet, 1500)}")

    @tree.command(name="project", description="Save or switch project settings and conversations")
    @handled
    async def project(interaction: discord.Interaction,
                      action: Literal["list", "save", "use", "remove"] = "list",
                      name: str | None = None, overwrite: bool = False) -> None:
        conv = svc.conversation(interaction.user.id)
        m = Messenger.after_defer(interaction)
        projects = svc.store.projects(conv.user_id)
        if action == "list":
            text = "\n".join(f"• **{key}** · `{clip(value.get('directory') or '', 120)}`" for key, value in projects.items())
            for chunk in split_text(text or "No saved projects. Set up a conversation, then use `/project save name:…`.", 1900):
                await m.send(chunk)
            return
        key = project_name(name or "")
        if action == "remove":
            removed = svc.store.delete_project(conv.user_id, key)
            await m.send("Project preset removed. Its conversation and files are kept." if removed else "Project not found.")
            return
        conv.require_idle()
        if action == "save":
            if key in projects and not overwrite:
                raise ValueError("That name already exists. Use overwrite:true to replace the preset.")
            if len(projects) >= 25 and key not in projects:
                raise ValueError("You can save up to 25 projects. Remove an unused preset first.")
            payload = {field: getattr(conv.state, field) for field in PROJECT_FIELDS}
            payload["directory"] = conv.directory
            svc.store.save_project(conv.user_id, key, payload)
            await m.send(f"Saved project **{key}**. `/project use name:{key}` restores it.")
            return
        if key not in projects:
            raise ValueError("Project not found. Use `/project list`.")
        saved = projects[key]
        # Validate before changing local state. Missing sessions use saved defaults
        # for a new conversation, created only when the next message is sent.
        session = None
        if saved.get("session_id"):
            try:
                session = await svc.client.get_session(saved["session_id"])
            except OpenCodeError as exc:
                if not exc.not_found:
                    raise
        conv.require_idle()
        for field in PROJECT_FIELDS:
            setattr(conv.state, field, saved.get(field))
        if session is None:
            conv.state.session_id = None
        else:
            model = session.get("model") or {}
            if model.get("providerID") and model.get("id"):
                conv.state.provider_id, conv.state.model_id = model["providerID"], model["id"]
                conv.state.variant = model.get("variant")
            conv.state.agent = session.get("agent") or conv.state.agent
            conv.state.directory = (session.get("location") or {}).get("directory") or conv.directory
            conv.state.title = session.get("title") or conv.state.title
        conv._save()
        conv._remember(session)
        await m.send(f"Using **{key}** in `{conv.directory}`. " + ("Conversation restored." if session else "Your next message starts a new conversation."))

    @project.autocomplete("name")
    async def project_names(interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=name, value=name) for name in svc.store.projects(interaction.user.id)
                if current.lower() in name][:25]

    @tree.command(name="pending", description="Restore unanswered questions and permission buttons")
    @handled
    async def pending(interaction: discord.Interaction) -> None:
        conv = svc.conversation(interaction.user.id)
        if not conv.session_id:
            raise ValueError("There is no active conversation.")
        session_id = conv.session_id
        permissions = await svc.client.permissions(session_id)
        forms = await svc.client.forms(session_id)
        m = Messenger.after_defer(interaction)
        if not permissions and not forms:
            await m.send("No unanswered questions or permissions. `/history` shows recent results.")
            return
        await m.send(f"{len(permissions)} permissions and {len(forms)} questions waiting. Showing up to 20 of each.")
        for permission in permissions[:20]:
            await svc.show_permission_request(conv, {**permission, "sessionID": session_id}, m)
        for form in forms[:20]:
            await svc.show_form(conv, {**form, "sessionID": session_id}, m)
