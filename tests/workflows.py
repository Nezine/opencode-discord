"""Native workflows, durable queue scheduling, and real slash callbacks (offline)."""

import asyncio
import contextlib
import json
import random
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot._engine import PromptQueue, export_transcript, project_name, transcript_entries
from bot.oc import OpenCodeError
from bot.runner import BusyError, Conversation
from bot.store import Store
from tests.commands_flow import FakeInteraction, StubClient, build_tree
from tests.harness import FakeMessenger


def prompt(text="hello", session="session"):
    return {"session_id": session, "text": text, "files": []}


class NativeWorkflowTests(unittest.TestCase):
    def test_randomized_queue_matches_fifo_reference(self):
        rng = random.Random(42)
        queue, reference, next_id = PromptQueue(), [], 1
        for _ in range(1000):
            action = rng.choice(("push", "pop", "remove", "restore", "clear"))
            if action == "push" and len(reference) < 20:
                text = str(rng.randrange(10000))
                self.assertEqual(queue.push(prompt(text)), next_id)
                reference.append({**prompt(text), "id": next_id})
                next_id += 1
            elif action == "pop":
                self.assertEqual(queue.pop(), reference.pop(0) if reference else None)
            elif action == "remove":
                item_id = rng.randrange(1, next_id + 1)
                expected = any(item["id"] == item_id for item in reference)
                self.assertEqual(queue.remove(item_id), expected)
                reference = [item for item in reference if item["id"] != item_id]
            elif action == "restore":
                replacement = PromptQueue()
                replacement.restore(queue.snapshot())
                queue = replacement
            elif action == "clear":
                queue.clear()
                reference.clear()
            self.assertEqual(queue.entries(), reference)

    def test_fifo_remove_and_monotonic_ids(self):
        queue = PromptQueue()
        self.assertIsNone(queue.pop())
        self.assertEqual([queue.push(prompt(str(i))) for i in range(3)], [1, 2, 3])
        self.assertTrue(queue.remove(2))
        self.assertFalse(queue.remove(99))
        self.assertEqual(queue.pop()["text"], "0")
        saved = queue.snapshot()
        recovered = PromptQueue()
        recovered.restore(saved)
        self.assertEqual(recovered.pop()["id"], 3)
        recovered.clear()
        self.assertEqual(recovered.push(prompt()), 4)

    def test_validation_and_capacity(self):
        queue = PromptQueue()
        for data in ({}, prompt(" \n\u3000"), prompt("x" * 128001),
                     {**prompt(), "files": [1] * 11}, {**prompt(), "files": {}},
                     {**prompt(), "text": 7}, prompt(session="")):
            with self.subTest(data=str(data)[:60]), self.assertRaises(ValueError):
                queue.push(data)
        queue.push({"session_id": "s", "files": [{"uri": "file:///tmp/a"}]})
        self.assertEqual(queue.pop()["text"], "")
        for _ in range(20):
            queue.push(prompt())
        with self.assertRaises(ValueError):
            queue.push(prompt())
        self.assertEqual(queue.size, 20)

    def test_restore_is_atomic_and_rejects_noninteger_ids(self):
        queue = PromptQueue()
        queue.push(prompt())
        before = queue.snapshot()
        invalid = [[], {}, {"next_id": 0, "items": []},
                   {"next_id": 2.5, "items": []}, {"next_id": True, "items": []},
                   {"next_id": 2**64 - 1, "items": []},
                   {"next_id": 2, "items": [{**prompt(), "id": 1.5}]},
                   {"next_id": 2, "items": [{**prompt(), "id": 2}]},
                   {"next_id": 3, "items": [{**prompt(), "id": 1}] * 2}]
        for saved in invalid:
            with self.subTest(saved=saved), self.assertRaises((ValueError, RuntimeError, IndexError)):
                queue.restore(saved)
            self.assertEqual(queue.snapshot(), before)
        entries = queue.entries()
        entries[0]["text"] = "changed"
        self.assertEqual(queue.entries()[0]["text"], "hello")

    def test_project_names(self):
        self.assertEqual(project_name("My_Project-2"), "my_project-2")
        for name in ("", "..", "../secret", "a b", "a" * 41, "dự-án", "nul\0name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                project_name(name)

    def test_export_preserves_long_unicode_text_without_private_parts(self):
        text = "  tiếng Việt 🦀\n```cpp\nint x;\n```\n" * 300
        messages = [{"id": "u", "type": "user", "text": "ask"},
                    {"id": "a", "type": "assistant", "content": [
                        {"type": "text", "text": text}, {"type": "reasoning", "text": "secret"},
                        {"type": "tool", "text": "secret"}, {"type": "text", "text": "tail"}]},
                    None, {"type": "tool", "text": "secret"}]
        entries = transcript_entries(messages)
        self.assertEqual(entries[1]["text"], text + "tail")
        session = {"id": "s", "title": "name\nheading"}
        exported = json.loads(export_transcript(session, messages, "json"))
        self.assertEqual(exported["messages"], entries)
        markdown = export_transcript(session, messages, "markdown")
        self.assertTrue(markdown.startswith("# name heading\n"))
        self.assertIn(text + "tail", markdown)
        self.assertNotIn("secret", markdown)
        with self.assertRaises(ValueError):
            export_transcript(session, messages, "html")

    def test_existing_database_upgrade_reopen_and_user_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.db")
            store = Store(path)
            state = store.get_user(1)
            state.session_id = "old-session"
            store.save_user(state)
            store.close()
            # Mimic the old schema: additive migration must preserve users.
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.executescript("DROP TABLE projects; DROP TABLE queues;")
            store = Store(path)
            self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
            self.assertEqual(store.get_user(1).session_id, "old-session")
            queue = PromptQueue()
            queue.push(prompt())
            store.save_queue(1, queue.snapshot())
            store.save_project(1, "Work", {"directory": "/tmp/a", "session_id": "s"})
            store.save_project(2, "work", {"directory": "/tmp/b"})
            store.close()
            store = Store(path)
            self.assertEqual(store.projects(1)["work"]["directory"], "/tmp/a")
            self.assertEqual(store.projects(2)["work"]["directory"], "/tmp/b")
            self.assertEqual(store.load_queue(1), queue.snapshot())
            self.assertIsNone(store.load_queue(2))
            self.assertTrue(store.delete_project(1, "WORK"))
            self.assertFalse(store.delete_project(1, "work"))
            self.assertIn("work", store.projects(2))
            with self.assertRaises((ValueError, RuntimeError, IndexError)):
                store.save_queue(1, {"items": [], "next_id": -1})
            self.assertEqual(store.load_queue(1), queue.snapshot())
            store.close()


class QueueRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        state = self.store.get_user(1)
        state.session_id = "session"
        self.store.save_user(state)
        self.client = SimpleNamespace(get_session=AsyncMock(return_value={"id": "session"}),
                                      active_sessions=AsyncMock(return_value={}),
                                      prompt=AsyncMock(), interrupt=AsyncMock(),
                                      set_model=AsyncMock(), set_agent=AsyncMock())
        self.svc = SimpleNamespace(store=self.store, client=self.client,
                                   cfg=SimpleNamespace(steer_when_busy=False, opencode_directory="/tmp", default_agent="build"))
        self.conv = Conversation(self.svc, 1)
        self.conv._maybe_title = AsyncMock()
        self.started = asyncio.Queue()
        self.finish = asyncio.Queue()

        async def run(session, text, messenger, files):
            await self.started.put(text)
            return await self.finish.get()

        self.conv._run_turn = run
        self.m = FakeMessenger()

    async def asyncTearDown(self):
        if self.conv.turn_task:
            self.conv.turn_task.cancel()
            await self.conv.turn_task
        self.store.close()

    async def start(self):
        task = asyncio.create_task(self.conv.send_text("first", self.m))
        self.assertEqual(await asyncio.wait_for(self.started.get(), 1), "first")
        return task

    async def test_fifo_drain_and_steer_are_distinct(self):
        task = await self.start()
        await self.conv.send_text("second", self.m)
        await self.conv.send_text("third", self.m)
        await self.conv.send_text("correction", self.m, delivery="steer")
        self.client.prompt.assert_awaited_once_with("session", "correction", files=None, delivery="steer")
        self.assertEqual(self.conv.queue.size, 2)
        for expected in ("second", "third"):
            await self.finish.put(True)
            self.assertEqual(await asyncio.wait_for(self.started.get(), 1), expected)
        await self.finish.put(True)
        await task
        self.assertFalse(self.conv.busy)
        self.assertIsNone(self.conv.turn_task)
        self.assertEqual(self.store.load_queue(1)["items"], [])

    async def test_failed_turn_pauses_surviving_queue_and_recovers_after_restart(self):
        task = await self.start()
        await self.conv.send_text("survives", self.m)
        await self.finish.put(False)
        await task
        restored = Conversation(self.svc, 1)
        self.assertFalse(restored.busy)
        self.assertEqual(restored.queue.entries()[0]["text"], "survives")
        restored._run_turn = AsyncMock(return_value=True)
        restored._maybe_title = AsyncMock()
        await restored.resume_queue(self.m)
        restored._run_turn.assert_awaited_once()
        self.assertEqual(restored.queue.size, 0)

    async def test_stop_keeps_waiting_messages(self):
        task = await self.start()
        await self.conv.send_text("later", self.m)
        self.assertTrue(await self.conv.stop(self.m))
        await task
        self.assertFalse(self.conv.busy)
        self.assertEqual(self.conv.queue.size, 1)
        self.assertTrue(self.started.empty())
        self.assertEqual(await self.conv.remove_queued(), 1)

    async def test_stop_does_not_cancel_newly_admitted_work(self):
        task = await self.start()
        interrupt_started, release = asyncio.Event(), asyncio.Event()

        async def interrupt(session):
            interrupt_started.set()
            await release.wait()

        self.client.interrupt.side_effect = interrupt
        stopping = asyncio.create_task(self.conv.stop(self.m))
        await interrupt_started.wait()
        await self.finish.put(True)
        await task
        with self.assertRaises(BusyError):
            await self.conv.send_text("new", self.m)
        release.set()
        await stopping
        self.assertFalse(self.conv._stopping)

    async def test_settings_blocked_with_running_or_paused_queue(self):
        task = await self.start()
        with self.assertRaises(BusyError):
            await self.conv.set_directory("/tmp/other")
        await self.conv.send_text("later", self.m)
        await self.finish.put(False)
        await task
        with self.assertRaises(BusyError):
            await self.conv.use_session("other")
        item = self.conv.queue.entries()[0]
        self.assertEqual(await self.conv.remove_queued(item["id"]), 1)
        await self.conv.set_directory("/tmp/other")
        self.assertEqual(self.conv.directory, "/tmp/other")

    async def test_resume_rejects_different_or_remotely_busy_session(self):
        self.conv._mutate_queue(self.conv.queue.push, prompt())
        self.conv.state.session_id = "other"
        with self.assertRaises(BusyError):
            await self.conv.resume_queue(self.m)
        self.conv.state.session_id = "session"
        self.client.active_sessions.return_value = {"session": "busy"}
        with self.assertRaises(BusyError):
            await self.conv.resume_queue(self.m)
        self.assertEqual(self.conv.queue.size, 1)

    async def test_database_failure_rolls_back_queue_and_ids(self):
        self.conv._mutate_queue(self.conv.queue.push, prompt())
        original = self.conv.queue.snapshot()
        self.svc.store = SimpleNamespace(save_queue=Mock(side_effect=RuntimeError("disk full")))
        for action in (lambda: self.conv._mutate_queue(self.conv.queue.push, prompt("other")),
                       lambda: self.conv._mutate_queue(self.conv.queue.pop)):
            with self.assertRaises(RuntimeError):
                action()
            self.assertEqual(self.conv.queue.snapshot(), original)
        with self.assertRaises(RuntimeError):
            await self.conv.remove_queued()
        self.assertEqual(self.conv.queue.snapshot(), original)

    async def test_announcement_failure_does_not_drop_queued_prompt(self):
        task = await self.start()
        bad = FakeMessenger()
        await self.conv.send_text("still here", bad)
        bad.send = AsyncMock(side_effect=RuntimeError("Discord unavailable"))
        await self.finish.put(True)
        with self.assertLogs("runner", level="ERROR"):
            await task
        self.assertEqual(self.conv.queue.entries()[0]["text"], "still here")

    async def test_concurrent_first_messages_create_only_one_session(self):
        self.conv.state.session_id = None
        self.client.create_session = AsyncMock(return_value={"id": "session"})
        tasks = [asyncio.create_task(self.conv.send_text(text, self.m)) for text in ("one", "two")]
        self.assertEqual(await asyncio.wait_for(self.started.get(), 1), "one")
        await self.finish.put(True)
        self.assertEqual(await asyncio.wait_for(self.started.get(), 1), "two")
        await self.finish.put(True)
        await asyncio.gather(*tasks)
        self.client.create_session.assert_awaited_once()

    async def test_remote_model_failure_does_not_save_false_preferences(self):
        self.conv.state.provider_id, self.conv.state.model_id = "old", "model"
        self.client.set_model.side_effect = OpenCodeError(500, "broken")
        with self.assertRaises(OpenCodeError):
            await self.conv.set_model("new", "other", None)
        self.assertEqual(self.conv.state.provider_id, "old")
        self.client.set_agent.side_effect = OpenCodeError(500, "broken")
        with self.assertRaises(OpenCodeError):
            await self.conv.set_agent("new")
        self.assertIsNone(self.conv.state.agent)


class WorkflowCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = StubClient()
        self.tree, self.svc = build_tree(self.client)
        self.conv = self.svc.conversation(12345)

    async def asyncTearDown(self):
        await self.svc.close()

    async def call(self, command, **kwargs):
        interaction = FakeInteraction()
        await self.tree.get_command(command).callback(interaction, **kwargs)
        self.assertTrue(interaction.response.is_done())
        valid, error = interaction.validate_all()
        self.assertTrue(valid, error)
        self.assertTrue(all(len(p.get("content") or "") <= 2000 for p in interaction.sends))
        return interaction

    def text(self, interaction):
        return "\n".join(p.get("content") or "" for p in interaction.sends)

    async def test_project_save_use_remove_and_overwrite_guard(self):
        self.conv.state.directory = "/tmp/first"
        self.conv.state.session_id = "saved"
        await self.call("project", action="save", name="Test")
        self.conv.state.directory = "/tmp/second"
        result = await self.call("project", action="save", name="test")
        self.assertIn("overwrite:true", self.text(result))
        self.assertEqual(self.svc.store.projects(12345)["test"]["directory"], "/tmp/first")
        self.client.get_session = AsyncMock(return_value={"id": "saved", "location": {"directory": "/remote"}})
        await self.call("project", action="use", name="TEST")
        self.assertEqual(self.conv.session_id, "saved")
        self.assertEqual(self.conv.directory, "/remote")
        await self.call("project", action="remove", name="test")
        self.assertEqual(self.svc.store.projects(12345), {})
        self.assertEqual(self.conv.session_id, "saved")

    async def test_missing_project_session_starts_fresh_on_next_message(self):
        self.svc.store.save_project(12345, "gone", {"session_id": "gone", "directory": "/tmp"})
        self.client.get_session = AsyncMock(side_effect=OpenCodeError(404, "gone"))
        result = await self.call("project", action="use", name="gone")
        self.assertIsNone(self.conv.session_id)
        self.assertIn("next message", self.text(result))

    async def test_project_switch_rechecks_busy_after_network_await(self):
        self.conv.state.session_id = "original"
        self.svc.store.save_project(12345, "work", {"session_id": "saved"})

        async def get_session(session):
            self.conv.busy = True
            return {"id": session}

        self.client.get_session = get_session
        result = await self.call("project", action="use", name="work")
        self.assertIn("Finish or stop", self.text(result))
        self.assertEqual(self.conv.session_id, "original")

    async def test_search_finds_late_unicode_match_and_caps_results(self):
        self.conv.state.session_id = "s"
        self.client.messages = AsyncMock(return_value=[
            {"type": "user", "text": "ß" * 800 + " tiếng Việt END"} for _ in range(13)])
        result = await self.call("search", query="TIẾNG việt")
        self.assertIn("Found 13", self.text(result))
        self.assertEqual(sum("END" in (p.get("content") or "") for p in result.sends), 10)

    async def test_export_captures_one_session_even_if_active_selection_changes(self):
        self.conv.state.session_id = "original"

        async def get_session(session):
            self.conv.state.session_id = "switched"
            return {"id": session}

        self.client.get_session = get_session
        self.client.messages = AsyncMock(return_value=[{"type": "user", "text": "long" * 1000}])
        interaction = FakeInteraction()
        captured = []

        async def send(content=None, **kwargs):
            captured.append(kwargs["files"][0].fp.read())
            self.assertTrue(kwargs["ephemeral"])

        interaction.followup.send = send
        await self.tree.get_command("export").callback(interaction, format="json")
        self.client.messages.assert_awaited_once_with("original")
        self.assertEqual(json.loads(captured[0])["session_id"], "original")
        self.assertEqual(json.loads(captured[0])["messages"][0]["text"], "long" * 1000)

    async def test_pending_restores_controls_with_original_session(self):
        self.conv.state.session_id = "s"
        self.client.permissions = AsyncMock(return_value=[{"id": "p"}])
        self.client.forms = AsyncMock(return_value=[{"id": "f"}])
        self.svc.show_permission_request = AsyncMock()
        self.svc.show_form = AsyncMock()
        await self.call("pending")
        self.assertEqual(self.svc.show_permission_request.call_args.args[1]["sessionID"], "s")
        self.assertEqual(self.svc.show_form.call_args.args[1]["sessionID"], "s")

    async def test_queue_commands_remove_without_stopping_current_work(self):
        self.conv._mutate_queue(self.conv.queue.push, prompt())
        self.conv.busy = True
        result = await self.call("queue")
        self.assertIn("#1", self.text(result))
        await self.call("queue", action="remove", item_id=1)
        self.assertTrue(self.conv.busy)
        self.assertEqual(self.conv.queue.size, 0)

    async def test_mutating_commands_reject_paused_queue(self):
        self.conv.state.session_id = "session"
        self.conv._mutate_queue(self.conv.queue.push, prompt())
        for name in ("delete", "fork", "compact", "redo", "undo"):
            result = await self.call(name)
            self.assertIn("Finish or stop", self.text(result))
        self.assertEqual(self.conv.session_id, "session")

    async def test_ask_acknowledges_before_long_running_work(self):
        self.conv.send_text = AsyncMock()
        result = await self.call("ask", text="hello", delivery="steer")
        self.assertEqual(result.log[0][0], "defer")
        self.assertIn("received", self.text(result))
        messenger = self.conv.send_text.call_args.args[1]
        self.assertIsNone(messenger.interaction)
        self.assertEqual(self.conv.send_text.call_args.kwargs["delivery"], "steer")


if __name__ == "__main__":
    unittest.main()
