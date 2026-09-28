"""Regression coverage for streamed previews and complete Discord replies."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.messaging import LiveMessage
from bot.runner import Conversation
from bot.turn import TurnState, apply_event, hydrate_from_message
from tests.harness import FakeMessenger


class RunnerOutputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        service = SimpleNamespace(
            cfg=SimpleNamespace(show_reasoning=True, show_tools=True),
            store=SimpleNamespace(get_user=lambda _: SimpleNamespace()),
        )
        self.conversation = Conversation(service, 1)
        self.conversation._hydrate = AsyncMock()

    def state(self, text, *, reasoning="", tools=False):
        state = TurnState(session_id="session")
        apply_event(state, {"type": "session.text.delta", "data": {
            "assistantMessageID": "message", "ordinal": 0, "delta": text,
        }})
        content = [{"type": "reasoning", "text": reasoning}]
        if tools:
            content.extend({"type": "tool", "id": str(i), "name": "read",
                            "state": {"status": "completed", "input": {"path": "x" * 100}}}
                           for i in range(8))
        hydrate_from_message(state, {"id": "message", "content": content})
        return state

    async def flush(self, state):
        messenger = FakeMessenger()
        live = LiveMessage(messenger)
        await live.start("thinking")
        await self.conversation._flush_step(state, state.current, messenger, live, "model")
        return [message.content for message in messenger.all if not message.deleted]

    async def test_plain_answers_preserve_every_character(self):
        for length in (1699, 1700, 1701, 1800, 1900, 1901, 5703):
            with self.subTest(length=length):
                text = ("0123456789á界" * 500)[:length]
                messages = await self.flush(self.state(text))
                self.assertEqual("".join(messages), text)
                self.assertTrue(all(len(message) <= 2000 for message in messages))

    async def test_code_blocks_keep_all_lines_and_balanced_fences(self):
        lines = [f"value_{i} = {i}" for i in range(350)]
        messages = await self.flush(self.state("```python\n" + "\n".join(lines) + "\n```"))
        actual = [line for message in messages for line in message.splitlines()
                  if not line.startswith("```")]
        self.assertEqual(actual, lines)
        self.assertTrue(all(message.count("```") % 2 == 0 for message in messages))
        self.assertTrue(all(len(message) <= 2000 for message in messages))

    async def test_reasoning_and_tools_fit_without_duplicate_tool_summary(self):
        state = self.state("answer " * 600, reasoning="reason " * 400, tools=True)
        preview = self.conversation._compose(state, state.current, "model")
        self.assertLessEqual(len(preview), 2000)
        messages = await self.flush(state)
        self.assertTrue(all(len(message) <= 2000 for message in messages))
        self.assertEqual(sum("✅" in message for message in messages), 1)
        self.assertEqual("".join(messages).count("answer"), 600)
        self.assertIn("reason", messages[0])

    async def test_short_answer_stays_in_original_message(self):
        self.assertEqual(await self.flush(self.state("hello")), ["hello"])

    async def test_hidden_metadata_stays_hidden(self):
        self.conversation.svc.cfg.show_reasoning = False
        self.conversation.svc.cfg.show_tools = False
        state = self.state("answer" * 800, reasoning="private reasoning", tools=True)
        messages = await self.flush(state)
        self.assertEqual("".join(messages), "answer" * 800)

    async def test_tool_only_step_keeps_one_summary(self):
        messages = await self.flush(self.state("", tools=True))
        self.assertEqual(len(messages), 1)
        self.assertIn("✅", messages[0])


if __name__ == "__main__":
    unittest.main()
