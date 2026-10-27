"""Diagnostic checks are read-only, bounded, and do not print credentials."""

import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot.doctor import check_database, check_systemd, inspect
from bot.oc import OpenCodeError


class LocalDoctorTests(unittest.TestCase):
    def test_database_check_does_not_create_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            self.assertEqual(check_database(path)["status"], "warn")
            self.assertFalse(path.exists())

    def test_database_integrity_check_keeps_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE test (value TEXT)")
                db.execute("INSERT INTO test VALUES ('private data')")
                db.commit()
                self.assertEqual(check_database(path)["status"], "ok")
                self.assertEqual(db.execute("SELECT * FROM test").fetchall(), [("private data",)])

    def test_systemd_only_returns_sanitized_fields(self):
        completed = SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=2\nSECRET=do-not-print")
        with patch("bot.doctor.subprocess.run", return_value=completed) as run:
            check = check_systemd("opencode-discord.service")
        self.assertEqual(check["status"], "ok")
        self.assertIn("2 automatic", check["detail"])
        self.assertNotIn("SECRET", json.dumps(check))
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_unavailable_systemd_is_not_a_false_health_pass(self):
        with patch("bot.doctor.subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 5)):
            self.assertEqual(check_systemd("opencode-discord.service")["status"], "warn")

    def test_console_entrypoint_awaits_main(self):
        from bot.main import cli
        with patch("bot.main.main", new=AsyncMock(return_value=7)) as main:
            self.assertEqual(cli(), 7)
            main.assert_awaited_once()


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_config_reports_failure_instead_of_system_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"DISCORD_TOKEN": "", "XDG_STATE_HOME": directory}), \
                 patch("bot.doctor.check_systemd", return_value={"status": "warn"}):
                checks = await inspect(offline=True)
        self.assertEqual(next(item for item in checks if item.get("check") == "configuration")["status"], "fail")

    async def run_probe(self, *, offline=False, failure=None):
        client = SimpleNamespace(active_sessions=AsyncMock(return_value={"private-session": "busy"}), close=AsyncMock())
        if failure:
            client.active_sessions.side_effect = failure
        with tempfile.TemporaryDirectory() as directory:
            env = {"DISCORD_TOKEN": "token-do-not-print", "DISCORD_USER_IDS": "12345", "ALLOW_ANY_USER": "0",
                   "OPENCODE_DIRECTORY": directory, "XDG_STATE_HOME": directory}
            with patch.dict(os.environ, env), \
                 patch("bot.doctor.check_systemd", return_value={"status": "ok", "check": "unit", "detail": "Running"}), \
                 patch("bot.config.discover_service", new=AsyncMock(return_value=("http://127.0.0.1:1", "u", "secret-password"))) as discover, \
                 patch("bot.oc.OpenCodeClient", return_value=client):
                checks = await inspect(offline=offline)
        serialized = json.dumps(checks)
        for secret in ("token-do-not-print", "secret-password", "private-session"):
            self.assertNotIn(secret, serialized)
        return checks, client, discover

    async def test_offline_skips_network(self):
        checks, client, discover = await self.run_probe(offline=True)
        client.active_sessions.assert_not_awaited()
        discover.assert_not_awaited()
        self.assertIn("skip", [item["status"] for item in checks])

    async def test_success_closes_client_without_prompt_or_login(self):
        checks, client, _ = await self.run_probe()
        api = next(item for item in checks if item["check"] == "OpenCode API")
        self.assertEqual(api["status"], "ok")
        client.close.assert_awaited_once()
        discord = next(item for item in checks if item["check"] == "Discord delivery")
        self.assertEqual(discord["status"], "skip")

    async def test_network_and_auth_failures_have_safe_guidance(self):
        for code in (0, 401, 403, 500):
            with self.subTest(code=code):
                checks, client, _ = await self.run_probe(failure=OpenCodeError(code, "secret-password"))
                api = next(item for item in checks if item["check"] == "OpenCode API")
                self.assertEqual(api["status"], "fail")
                client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
