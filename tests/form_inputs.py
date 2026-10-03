"""Exercise native answer conversion and Discord modal/select callbacks offline."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot._engine import parse_form_input
from bot.ui import FormInputModal, FormView


def interaction(values=()):
    return SimpleNamespace(
        user=SimpleNamespace(id=1), data={"values": list(values)},
        response=SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock(),
                                 edit_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
    )


class NativeInputTests(unittest.TestCase):
    def test_text_preserves_unicode_and_indentation(self):
        text = "  tiếng Việt\n    print('hello')\n"
        self.assertEqual(parse_form_input({"type": "string"}, text), text)

    def test_numbers_are_typed(self):
        self.assertEqual(parse_form_input({"type": "number"}, " -1.25e2 "), -125.0)
        value = parse_form_input({"type": "integer"}, "42")
        self.assertIs(type(value), int)
        self.assertEqual(value, 42)

    def test_invalid_numbers_cannot_reach_server(self):
        for kind, values in [("number", ["NaN", "inf", "1e9999", "12abc", "1,2", " "]),
                             ("integer", ["1.5", "1e2", "999999999999999999999999"] )]:
            for value in values:
                with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                    parse_form_input({"type": kind}, value)

    def test_blank_required_and_optional_inputs(self):
        with self.assertRaises(ValueError):
            parse_form_input({"type": "text"}, " \n")
        self.assertEqual(parse_form_input({"type": "text", "required": False}, ""), "")
        self.assertIsNone(parse_form_input({"type": "number", "required": False}, ""))


class FormInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(reply_form=AsyncMock())
        self.service = SimpleNamespace(client=self.client, cfg=SimpleNamespace(allowed=lambda uid: uid == 1))

    def view(self, fields):
        return FormView(self.service, "session", {"id": "form", "title": "Details", "fields": fields})

    async def submit(self, view, field, value):
        modal = FormInputModal(view, field)
        modal.answer._value = value
        event = interaction()
        await modal.on_submit(event)
        return event

    async def test_button_opens_modal_and_submits_text(self):
        field = {"key": "name", "type": "string"}
        view = self.view([field])
        event = interaction()
        await view.children[0].callback(event)
        modal = event.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, FormInputModal)
        modal.answer._value = "my project"
        await modal.on_submit(event)
        self.client.reply_form.assert_awaited_once_with("session", "form", {"name": "my project"})
        event.response.defer.assert_awaited_once()
        event.edit_original_response.assert_awaited_once()

    async def test_mixed_form_waits_for_text_and_number(self):
        fields = [{"key": "mode", "type": "string", "options": [{"label": "A", "value": "a"}]},
                  {"key": "name", "type": "string"}, {"key": "count", "type": "integer"}]
        view = self.view(fields)
        await view.children[0].callback(interaction(["a"]))
        self.client.reply_form.assert_not_awaited()
        await self.submit(view, fields[1], "project")
        self.client.reply_form.assert_not_awaited()
        await self.submit(view, fields[2], "3")
        self.client.reply_form.assert_awaited_once_with(
            "session", "form", {"mode": "a", "name": "project", "count": 3})

    async def test_invalid_number_can_be_corrected(self):
        field = {"key": "count", "type": "number"}
        view = self.view([field])
        event = await self.submit(view, field, "abc")
        event.response.send_message.assert_awaited_once()
        self.client.reply_form.assert_not_awaited()
        await self.submit(view, field, "1.5")
        self.client.reply_form.assert_awaited_once_with("session", "form", {"count": 1.5})

    async def test_server_failure_keeps_answers_for_retry(self):
        field = {"key": "name", "type": "string"}
        view = self.view([field])
        self.client.reply_form.side_effect = RuntimeError("unavailable")
        event = await self.submit(view, field, "project")
        event.followup.send.assert_awaited_once()
        self.assertFalse(view._done)
        self.assertEqual(FormInputModal(view, field).answer.default, "project")
        self.client.reply_form.side_effect = None
        await self.submit(view, field, "project")
        self.assertTrue(view._done)

    async def test_every_field_in_large_form_is_accessible(self):
        fields = [{"key": str(i), "type": "boolean"} for i in range(9)]
        view = self.view(fields)
        for page in range(3):
            for index in range(min(4, len(fields) - page * 4)):
                await view.children[index * 2].callback(interaction())
            if page < 2:
                self.client.reply_form.assert_not_awaited()
                await view.children[-1].callback(interaction())
        self.client.reply_form.assert_awaited_once_with("session", "form", {str(i): True for i in range(9)})

    async def test_modal_checks_user_and_ignores_closed_form(self):
        field = {"key": "name", "type": "string"}
        view = self.view([field])
        modal = FormInputModal(view, field)
        event = interaction()
        event.user.id = 2
        self.assertFalse(await modal.interaction_check(event))
        view.stop()
        await self.submit(view, field, "project")
        self.client.reply_form.assert_not_awaited()

    async def test_unknown_field_does_not_allow_partial_submission(self):
        fields = [{"key": "name", "type": "string"}, {"key": "unknown", "type": "object"}]
        view = self.view(fields)
        await self.submit(view, fields[0], "project")
        self.client.reply_form.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
