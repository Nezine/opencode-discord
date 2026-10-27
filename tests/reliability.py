"""Failures must pause work visibly, release resources, and preserve final text."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot.messaging import LiveMessage, Messenger
from bot.oc import OpenCodeError, _Subscription
from bot.runner import Conversation
from bot.store import Store
from tests.harness import FakeMessenger


def event(kind, **data):
    return {"type": kind, "data": {"sessionID": "s", **data}}


class Stream:
    def __init__(self, events):
        self.events = iter(events)
        self.closed = False
        self.dropped = 0

    async def __anext__(self):
        value = next(self.events, StopAsyncIteration())
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        cfg = SimpleNamespace(edit_interval=0.5, turn_timeout=30, stall_timeout=10,
                              show_reasoning=False, show_tools=False)
        self.client = SimpleNamespace(prompt=AsyncMock(), interrupt=AsyncMock())
        self.svc = SimpleNamespace(store=self.store, client=self.client, cfg=cfg,
                                   show_permission_request=AsyncMock(), show_form=AsyncMock())
        self.conv = Conversation(self.svc, 1)
        self.conv._hydrate = AsyncMock()
        self.m = FakeMessenger()

    async def asyncTearDown(self):
        self.store.close()

    async def run_events(self, events, dropped=0):
        sub = Stream(events)
        sub.dropped = dropped
        self.client.subscribe = lambda _: sub
        result = await self.conv._run_turn("s", "hello", self.m, None)
        self.assertTrue(sub.closed)
        self.assertIsNone(self.conv._sub)
        return result

    async def test_terminal_variants_complete_and_flush(self):
        for kind in ("session.execution.succeeded", "session.execution.completed"):
            with self.subTest(kind=kind):
                self.m = FakeMessenger()
                result = await self.run_events([
                    event("session.execution.started"),
                    event("session.text.delta", assistantMessageID="a", ordinal=0, delta="answer" * 600),
                    event(kind)])
                self.assertTrue(result)
                self.assertEqual("".join(m.content for m in self.m.all).count("answer"), 600)
                self.assertTrue(all(len(m.content) <= 2000 for m in self.m.all))

    async def test_lost_stream_does_not_look_successful(self):
        self.assertFalse(await self.run_events([StopAsyncIteration()]))
        self.assertIn("Lost the connection", self.m.transcript_text)
        self.client.interrupt.assert_not_awaited()

    async def test_network_prompt_failure_is_caught_and_subscription_closed(self):
        self.client.prompt.side_effect = OpenCodeError(0, "connection failed")
        self.assertFalse(await self.run_events([]))
        self.assertIn("Could not send", self.m.transcript_text)

    async def test_execution_failure_before_started_keeps_actual_error(self):
        self.assertFalse(await self.run_events([event("session.execution.failed", error={"message": "provider rejected request"})]))
        self.assertIn("provider rejected request", self.m.transcript_text)

    async def test_execution_failure_without_message_is_not_a_success(self):
        self.assertFalse(await self.run_events([event("session.execution.failed")]))
        self.assertIn("failed", self.m.transcript_text)

    async def test_stream_exception_is_reported(self):
        with self.assertLogs("runner", level="WARNING"):
            self.assertFalse(await self.run_events([RuntimeError("reader failed")]))
        self.assertIn("Connection interrupted", self.m.transcript_text)

    async def test_hard_deadline_applies_even_with_continuous_events(self):
        self.svc.cfg.turn_timeout = -1
        self.assertFalse(await self.run_events([event("session.execution.started")]))
        self.client.interrupt.assert_awaited_once_with("s")
        self.assertIn("time limit", self.m.transcript_text)

    async def test_timeout_path_has_no_false_completion_summary(self):
        self.svc.cfg.turn_timeout = -1
        self.assertFalse(await self.run_events([asyncio.TimeoutError()]))
        self.assertIn("Timed out", self.m.transcript_text)
        self.assertNotIn("finished without", self.m.transcript_text)
        self.client.interrupt.assert_awaited_once()

    async def test_stall_does_not_abort_waiting_permission_or_form(self):
        self.svc.cfg.stall_timeout = -1
        for waiting in (event("permission.asked", id="p", permission="read"),
                        event("form.created", form={"id": "f", "sessionID": "s", "questions": []})):
            with self.subTest(kind=waiting["type"]):
                self.assertTrue(await self.run_events([
                    event("session.execution.started"), waiting, asyncio.TimeoutError(),
                    event("permission.replied", requestID="p"), event("form.replied", id="f"),
                    event("session.text.delta", assistantMessageID="a", ordinal=0, delta="done"),
                    event("session.execution.succeeded")]))
        self.client.interrupt.assert_not_awaited()

    async def test_stall_without_pending_input_stops_and_pauses(self):
        self.svc.cfg.stall_timeout = -1
        self.assertFalse(await self.run_events([event("session.execution.started"), asyncio.TimeoutError()]))
        self.client.interrupt.assert_awaited_once()

    async def test_overflow_never_reports_a_truncated_answer_as_complete(self):
        self.assertFalse(await self.run_events([event("session.execution.succeeded")], dropped=1))
        self.assertIn("events were lost", self.m.transcript_text)

    async def test_interrupted_event_stops_without_waiting_for_timeout(self):
        self.assertFalse(await self.run_events([
            event("session.execution.started"), event("session.interrupted")]))
        self.assertIn("interrupted", self.m.transcript_text)


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def make(self, capacity=2):
        self.client = SimpleNamespace(_subscribers=set(), events_lost=0, reconnects=0)
        sub = _Subscription(self.client, "s", maxsize=capacity)
        self.client._subscribers.add(sub)
        return sub

    async def test_full_queue_close_terminates_and_unregisters(self):
        sub = self.make()
        for _ in range(3):
            sub.push(event("test"))
        self.assertEqual(sub.dropped, 1)
        sub.close()
        sub.close()
        self.assertEqual(self.client._subscribers, set())
        await sub.__anext__()
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(sub.__anext__(), 0.1)
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(sub.__anext__(), 0.1)

    async def test_close_wakes_blocked_reader(self):
        sub = self.make()
        read = asyncio.create_task(sub.__anext__())
        await asyncio.sleep(0)
        sub.close()
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(read, 0.1)

    async def test_native_loss_and_reconnect_are_visible(self):
        sub = self.make()
        self.client.events_lost = 2
        self.client.reconnects = 1
        self.assertEqual(sub.dropped, 2)
        self.assertTrue(sub.disconnected)
        sub.close()


class LiveMessageTests(unittest.IsolatedAsyncioTestCase):
    def http_error(self, code=500):
        return discord.HTTPException(SimpleNamespace(status=code, reason="failure"), "failed")

    async def test_failed_edit_can_retry_identical_content(self):
        m = FakeMessenger()
        live = LiveMessage(m)
        await live.start("before")
        live.message.edit = AsyncMock(side_effect=[self.http_error(), None])
        live._last_edit = 0
        await live.update("after")
        self.assertEqual(live._content, "before")
        live._last_edit = 0
        await live.update("after")
        self.assertEqual(live._content, "after")
        self.assertEqual(live.message.edit.await_count, 2)

    async def test_failed_final_edit_reposts_complete_text(self):
        m = FakeMessenger()
        live = LiveMessage(m)
        await live.start("preview")
        live.message.edit = AsyncMock(side_effect=self.http_error())
        await live.finish("complete")
        self.assertEqual(m.all[-1].content, "complete")
        self.assertTrue(live._closed)

    async def test_late_failure_after_flushed_step_remains_visible(self):
        m = FakeMessenger()
        live = LiveMessage(m)
        await live.start("preview")
        await live.finish("partial answer")
        await live.finish("turn failed")
        await live.finish("turn failed")
        self.assertEqual([message.content for message in m.all], ["partial answer", "turn failed"])

    async def test_first_channel_reply_references_original_once(self):
        channel = SimpleNamespace(send=AsyncMock(return_value=object()))
        original = object()
        m = Messenger(channel=channel, reference=original)
        await m.send("first")
        self.assertIs(channel.send.call_args.kwargs["reference"], original)
        await m.send("second")
        self.assertNotIn("reference", channel.send.call_args.kwargs)

    async def test_followups_keep_ephemeral_flag(self):
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
        await Messenger.after_defer(interaction).send("private")
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])

    async def test_edits_sanitize_mentions(self):
        m = FakeMessenger()
        live = LiveMessage(m)
        await live.start("start")
        await live.finish("@everyone @here")
        self.assertNotIn("@everyone", m.all[-1].content)
        self.assertNotIn("@here", m.all[-1].content)


if __name__ == "__main__":
    unittest.main()
