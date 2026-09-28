"""Discord UI components (selects, buttons).

Every ``custom_id`` is self-describing (``action|arg|arg``) so views keep working
after a bot restart: the handler rebuilds state from the id instead of relying on
in-memory objects.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord

from .textutil import rel_time

if TYPE_CHECKING:  # pragma: no cover
    from .runner import Conversation
    from .service import Service

log = logging.getLogger("ui")

MAX_OPTIONS = 25
PER_PAGE = 20
EM_DASH = "—"


def _clip_option(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short_id(session_id: str) -> str:
    return session_id[-6:]


class SessionSelectView(discord.ui.View):
    """Paginated picker for past conversations."""

    def __init__(
        self,
        svc: "Service",
        conversation: "Conversation",
        sessions: list[dict],
        *,
        page: int = 0,
        total: int | None = None,
        scope: str = "mine",
        timeout: float = 900.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.svc = svc
        self.conversation = conversation
        self.sessions = sessions
        self.page = page
        self.total = total if total is not None else len(sessions)
        self.scope = scope
        self._render()

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // PER_PAGE))

    def body(self) -> str:
        text = session_select_message(self.conversation, self.sessions, self.total, self.page)
        if self.scope != "mine":
            text += "\n-#showing every session the service knows about"
        return text

    # ------------------------------------------------------------- rendering

    def _page_items(self) -> list[dict]:
        start = self.page * PER_PAGE
        return self.sessions[start : start + PER_PAGE]

    def _render(self) -> None:
        self.clear_items()
        items = self._page_items()

        if items:
            options: list[discord.SelectOption] = []
            for session in items:
                session_id = session.get("id", "")
                title = _clip_option(session.get("title") or "untitled", 88)
                label = f"{title} · {_short_id(session_id)}"
                model = session.get("model") or {}
                model_text = model.get("id") or "—"
                variant = model.get("variant")
                if variant and variant != "default":
                    model_text += f" ({variant})"
                description = _clip_option(
                    f"{model_text} · {rel_time((session.get('time') or {}).get('updated'))}",
                    100,
                )
                options.append(
                    discord.SelectOption(
                        label=label[:100],
                        description=description,
                        value=session_id,
                        default=session_id == (self.conversation.session_id or ""),
                    )
                )
            select = discord.ui.Select(
                placeholder="Pick a conversation to continue…",
                custom_id=f"session:pick:{self.page}",
                options=options[:MAX_OPTIONS],
                row=0,
            )
            select.callback = self._on_pick
            self.add_item(select)
        else:
            self.add_item(
                discord.ui.Button(
                    label="No conversations found", style=discord.ButtonStyle.secondary, disabled=True, row=0
                )
            )

        pages = self.pages
        for button in (
            self._nav_button(
                "◀",
                f"session:page:{max(0, self.page - 1)}",
                self._on_page(max(0, self.page - 1)),
                disabled=self.page <= 0,
            ),
            self._nav_button(
                f"Page {self.page + 1}/{pages}",
                f"session:noop:{self.page}",
                self._on_noop(),
                disabled=True,
            ),
            self._nav_button(
                "▶",
                f"session:page:{self.page + 1}",
                self._on_page(self.page + 1),
                disabled=self.page + 1 >= pages,
            ),
            self._nav_button("➕ New conversation", "session:new", self._on_new(), primary=True),
        ):
            self.add_item(button)

    # -------------------------------------------------------------- handlers

    def _nav_button(
        self,
        label: str,
        custom_id: str,
        callback: Any,
        *,
        disabled: bool = False,
        primary: bool = False,
    ) -> discord.ui.Button:
        """Build a nav button with its handler already attached.

        Assigning ``.callback`` is not optional in discord.py 2.7: a button with a
        custom_id and no callback is built without complaint and then does nothing
        when clicked, which is exactly the bug this guards against.
        """
        style = discord.ButtonStyle.primary if primary else discord.ButtonStyle.secondary
        button = discord.ui.Button(
            label=label, style=style, custom_id=custom_id, disabled=disabled, row=1
        )
        button.callback = callback
        return button

    def _on_page(self, page: int) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            self.page = max(0, min(page, self.pages - 1))
            self._render()
            await interaction.response.edit_message(content=self.body(), view=self)

        return callback

    def _on_new(self) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            await self.svc.handle_new_conversation(interaction, self.conversation)

        return callback

    def _on_noop(self) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            if not interaction.response.is_done():
                await interaction.response.defer()

        return callback

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        await self.svc.handle_session_pick(interaction, self.conversation, str(interaction.data["values"][0]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return self.svc.cfg.allowed(interaction.user.id)


def session_select_message(
    conversation: "Conversation", sessions: list[dict], total: int, page: int
) -> str:
    active = conversation.session_id
    lines = [
        f"**Conversations** — {total} total" + (f", page {page + 1} of {-(-total // PER_PAGE)}" if total > PER_PAGE else ""),
        "",
    ]
    if not sessions:
        lines.append("_Nothing here yet. Use `/new` to start one._")
        return "\n".join(lines)
    # Preview the page actually on screen, not always the first one.
    window = sessions[page * PER_PAGE : page * PER_PAGE + PER_PAGE][:6]
    for session in window:
        marker = "▶ " if session.get("id") == active else "   "
        model = (session.get("model") or {}).get("id", EM_DASH)
        stamp = rel_time((session.get("time") or {}).get("updated"))
        lines.append(f"{marker}`{session.get('id', '')[-8:]}` {session.get('title') or 'untitled'}")
        lines.append(f"    {model} · {stamp}")
    if total > len(window):
        lines.append(f"    _…and {total - len(window)} more, use the menu to page through._")
    return "\n".join(lines)


class SelectOnlyView(discord.ui.View):
    """A single select menu with an optional cancel button."""

    def __init__(
        self,
        options: list[discord.SelectOption],
        placeholder: str,
        handler: Callable[[discord.Interaction, str], Awaitable[None]],
        custom_id: str,
        *,
        cancel_label: str = "Cancel",
        timeout: float = 900.0,
    ) -> None:
        super().__init__(timeout=timeout)
        select = discord.ui.Select(
            placeholder=placeholder[:150], custom_id=custom_id, options=options[:MAX_OPTIONS], row=0
        )
        async def on_select(interaction: discord.Interaction) -> None:
            await handler(interaction, str(interaction.data["values"][0]))

        select.callback = on_select
        self.add_item(select)
        cancel = discord.ui.Button(
            label=cancel_label, style=discord.ButtonStyle.secondary, custom_id="noop", row=1
        )

        async def _cancel(interaction: discord.Interaction) -> None:
            await interaction.response.defer()

        cancel.callback = _cancel
        self.add_item(cancel)


class PermissionView(discord.ui.View):
    """Approve / always approve / deny a pending tool permission request."""

    def __init__(self, service: "Service", session_id: str, request_id: str, timeout: float = 1800.0) -> None:
        super().__init__(timeout=timeout)
        self.svc = service
        self.session_id = session_id
        self.request_id = request_id
        self._done = False

    def _embed_state(self, interaction: discord.Interaction) -> discord.Embed:
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.set_footer(text=f"answered · request {self.request_id[-6:]}")
        return embed

    async def _decide(self, interaction: discord.Interaction, decision: str) -> None:
        if self._done:
            await interaction.response.send_message("Already answered.", ephemeral=True)
            return
        from .oc import OpenCodeError

        try:
            await self.svc.client.reply_permission(self.session_id, self.request_id, decision)
        except OpenCodeError as exc:
            await interaction.response.send_message(f"❌ `{exc.message}`", ephemeral=True)
            return
        self._done = True
        label = {"once": "Allowed once", "always": "Always allowed", "reject": "Denied"}[decision]
        embed = self._embed_state(interaction)
        await interaction.response.edit_message(embed=embed, view=None)
        await interaction.followup.send(f"✅ {label}.", ephemeral=True)

    @discord.ui.button(label="Allow once", style=discord.ButtonStyle.success, custom_id="perm:once")
    async def _once(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._decide(interaction, "once")

    @discord.ui.button(label="Always allow", style=discord.ButtonStyle.primary, custom_id="perm:always")
    async def _always(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._decide(interaction, "always")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="perm:reject")
    async def _deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._decide(interaction, "reject")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return self.svc.cfg.allowed(interaction.user.id)


def _field_options(field: dict) -> list[discord.SelectOption]:
    """Discord options for a form field's choices, capped to the API limit."""
    options: list[discord.SelectOption] = []
    for option in field.get("options") or []:
        label = _clip_option(option.get("label") or option.get("value") or "", 100)
        if not label:
            continue
        options.append(
            discord.SelectOption(
                label=label,
                description=_clip_option(option.get("description") or "", 100) or None,
                value=option.get("value") or label,
            )
        )
    return options[:MAX_OPTIONS]


class FormView(discord.ui.View):
    """Let the user answer a form: one select (or Yes/No buttons) per field.

    Fields without a fixed set of choices (free text, numbers) are shown in the
    embed but not rendered as controls; the turn waits until the server settles
    the form some other way.
    """

    def __init__(self, service: "Service", session_id: str, form: dict, timeout: float = 1800.0) -> None:
        super().__init__(timeout=timeout)
        self.svc = service
        self.session_id = session_id
        self.form_id = form.get("id", "")
        self.title = form.get("title") or "Answer needed"
        self.fields = form.get("fields") or []
        self._done = False
        self._answers: dict[str, Any] = {}
        self._render()

    @property
    def answerable(self) -> list[dict]:
        # Booleans are always answerable (Yes/No); other fields need an options
        # list to be rendered as a control.
        return [f for f in self.fields if f.get("options") or f.get("type") == "boolean"]

    def _render(self) -> None:
        self.clear_items()
        # Discord rules: a select must sit alone in its row, buttons may share.
        # Select fields therefore get one row each, then the boolean fields share
        # a single button row (capped at five Yes/No pairs worth of buttons).
        row = 0
        boolean_fields = [f for f in self.answerable if f.get("type") == "boolean"]
        for field in self.answerable:
            if field.get("type") == "boolean":
                continue
            if row > 4:
                break
            self._add_select(field, row)
            row += 1
        if boolean_fields and row <= 4:
            self._add_boolean_buttons(boolean_fields, row)

    def _add_boolean_buttons(self, fields: list[dict], row: int) -> None:
        def make_callback(field: dict, value: str):
            async def callback(interaction: discord.Interaction) -> None:
                self._answers[field["key"]] = value == "true"
                await self._maybe_reply(interaction)

            return callback

        # Discord caps a row at five buttons; a boolean form rarely exceeds that.
        for field in fields[:2]:
            yes = discord.ui.Button(
                label=f"{field.get('title') or field.get('key') or 'Yes'} · Yes",
                style=discord.ButtonStyle.primary,
                row=row,
            )
            yes.callback = make_callback(field, "true")
            self.add_item(yes)
            no = discord.ui.Button(
                label="No",
                style=discord.ButtonStyle.secondary,
                row=row,
            )
            no.callback = make_callback(field, "false")
            self.add_item(no)

    def _add_select(self, field: dict, row: int) -> None:
        options = _field_options(field)
        if not options:
            return
        is_multi = field.get("type") == "multiselect"
        select = discord.ui.Select(
            placeholder=(field.get("title") or field.get("key"))[:150],
            custom_id=f"form:{self.form_id}:{field['key']}",
            options=options,
            min_values=1,
            max_values=len(options) if is_multi else 1,
            row=row,
        )

        async def on_pick(interaction: discord.Interaction) -> None:
            values = [str(v) for v in interaction.data.get("values", [])]
            self._answers[field["key"]] = values if is_multi else values[0]
            await self._maybe_reply(interaction)

        select.callback = on_pick
        self.add_item(select)

    async def _maybe_reply(self, interaction: discord.Interaction) -> None:
        # A single-choice form replies immediately; a multi-field form only
        # submits once every answerable field has been picked.
        missing = [f["key"] for f in self.answerable if f["key"] not in self._answers]
        if len(self.answerable) > 1 and missing:
            await interaction.response.defer()
            return
        await self._submit(interaction)

    async def _submit(self, interaction: discord.Interaction) -> None:
        if self._done:
            await interaction.response.defer()
            return
        self._done = True
        from .oc import OpenCodeError

        try:
            await self.svc.client.reply_form(self.session_id, self.form_id, self._answers)
        except OpenCodeError as exc:
            self._done = False
            await interaction.response.send_message(f"❌ `{exc.message}`", ephemeral=True)
            return
        await interaction.response.edit_message(content=f"✅ Answered **{self.title}**.", view=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return self.svc.cfg.allowed(interaction.user.id)


def form_embed(form: dict) -> discord.Embed:
    title = form.get("title") or "Answer needed"
    embed = discord.Embed(
        title=f"🔎 {title}",
        description="Pick an option below to answer.",
        color=discord.Colour.blurple(),
    )
    for field in form.get("fields") or []:
        name = field.get("title") or field.get("key") or "field"
        if field.get("description"):
            name += f" — {field.get('description')}"
        if field.get("options"):
            value = "\n".join(
                f"• `{(o.get('value') or o.get('label') or '')[:60]}`" for o in field.get("options")[:8]
            )
        else:
            value = "_free-text field — not answerable from Discord_"
        embed.add_field(name=name[:256], value=value or "_no options_", inline=False)
    return embed


def permission_embed(data: dict) -> discord.Embed:
    action = data.get("action") or "action"
    resources = data.get("resources") or []
    save = data.get("save") or []
    embed = discord.Embed(
        title="🔐 Permission requested",
        description=f"The agent wants to **{action}**:\n"
        + "\n".join(f"• `{_clip_option(r, 90)}`" for r in resources[:8]),
        color=discord.Colour.gold(),
    )
    if save:
        embed.add_field(
            name="Remembering this means",
            value="\n".join(f"• `{_clip_option(s, 90)}`" for s in save[:8]),
            inline=False,
        )
    embed.set_footer(text=f"session {data.get('sessionID', '')[-8:]}")
    return embed


def status_embed(session: dict, state: Any, *, pending: int = 0) -> discord.Embed:
    model = session.get("model") or {}
    model_line = EM_DASH
    if model.get("id"):
        model_line = f"`{model.get('providerID')}/{model.get('id')}`"
        variant = model.get("variant")
        if variant and variant != "default":
            model_line += f" · effort `{variant}`"
    time_info = session.get("time") or {}
    tokens = session.get("tokens") or {}
    from .textutil import human_cost, human_tokens

    embed = discord.Embed(
        title=(session.get("title") or "untitled")[:256],
        color=discord.Colour.blurple(),
    )
    embed.add_field(name="Model", value=model_line, inline=True)
    embed.add_field(name="Effort", value=f"`{model.get('variant') or 'default'}`", inline=True)
    embed.add_field(name="Agent", value=f"`{session.get('agent') or state.agent or 'build'}`", inline=True)
    embed.add_field(
        name="Directory", value=f"`{_clip_option((session.get('location') or {}).get('directory', ''), 90)}`", inline=False
    )
    embed.add_field(name="Session", value=f"`{session.get('id', '')}`", inline=False)
    embed.add_field(name="Tokens", value=human_tokens(tokens), inline=True)
    embed.add_field(name="Cost", value=human_cost(session.get("cost")), inline=True)
    embed.add_field(name="Last activity", value=rel_time(time_info.get("updated")), inline=True)
    if pending:
        embed.add_field(name="Pending permissions", value=str(pending), inline=True)
    embed.set_footer(text="/status for session info · /new starts a fresh conversation")
    return embed
