"""Read-only local diagnostics. No model calls, repairs, or credential output."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import sqlite3
import subprocess


def result(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def check_systemd(unit: str) -> dict:
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return result(unit, "warn", "Cannot inspect systemd; skip this check if you run the bot manually.")
    if completed.returncode:
        return result(unit, "warn", "User systemd bus is unavailable. Run as the bot's Linux user, outside a sandbox.")
    values = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    if values.get("LoadState") != "loaded":
        return result(unit, "warn", "Service not installed; see README if you want automatic startup.")
    active = values.get("ActiveState") == "active" and values.get("SubState") == "running"
    # Values come from systemd, but only output known labels and parsed counts.
    try:
        restarts = int(values.get("NRestarts", "0"))
    except ValueError:
        restarts = 0
    return result(unit, "ok" if active else "warn",
                  f"{'Running' if active else 'Not running'}; {restarts} automatic restarts. "
                  + ("" if active else f"Inspect: journalctl --user -u {unit} -n 40"))


def check_database(path: Path) -> dict:
    if not path.exists():
        return result("database", "warn", "No saved state yet; normal before the first start.")
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            healthy = db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    except sqlite3.Error:
        return result("database", "fail", "Cannot read state.db. Check file permissions and available disk space; do not delete it.")
    return result("database", "ok" if healthy else "fail",
                  "SQLite quick_check passed." if healthy else "SQLite reported corruption. Back up state.db before attempting recovery.")


async def inspect(*, offline: bool = False) -> list[dict]:
    checks = []
    # Import lazily: a missing/incompatible extension must produce guidance,
    # not stop the doctor with an unreadable ImportError.
    try:
        from .config import Config, discover_service, state_dir
        from .oc import OpenCodeClient, OpenCodeError
    except (ImportError, OSError):
        return [result("native engine", "fail", "Engine or Python dependencies missing/incompatible. Run .venv/bin/pip install -e . and ./build.sh.")]
    checks.append(result("native engine", "ok", "C++ extension loaded."))
    try:
        cfg = Config.from_env()
    except (Exception, SystemExit):
        checks.append(result("configuration", "fail", "Configuration invalid. Set DISCORD_TOKEN and DISCORD_USER_IDS in .env; run bash doctor.sh to load it."))
        cfg = None
    if cfg is not None:
        checks.append(result("configuration", "warn" if cfg.allow_any_user else "ok",
                             "ALLOW_ANY_USER is enabled: anyone able to DM the bot can request tools on this machine."
                             if cfg.allow_any_user else f"Token is present; {len(cfg.allowed_user_ids)} allowed user IDs. Token validity is not checked."))
        checks.append(result("working directory", "ok" if Path(cfg.opencode_directory).is_dir() else "fail",
                             "Directory exists." if Path(cfg.opencode_directory).is_dir() else "OPENCODE_DIRECTORY does not point to an existing directory."))
    state_path = Path(state_dir())
    checks.append(await asyncio.to_thread(check_database, state_path / "state.db"))
    checks.extend(await asyncio.gather(*(asyncio.to_thread(check_systemd, unit) for unit in
                                        ("opencode-discord.service", "opencode-server.service"))))
    if offline:
        checks.append(result("OpenCode API", "skip", "Offline mode: network check skipped."))
    else:
        client = None
        try:
            url, username, password = await discover_service()
            client = OpenCodeClient(url, username, password, timeout=8)
            active = await client.active_sessions()
            checks.append(result("OpenCode API", "ok", f"API reachable; {len(active)} active sessions. No prompt sent."))
        except OpenCodeError as exc:
            if exc.status in (401, 403):
                detail = "Authentication rejected. Check OPENCODE_USERNAME / OPENCODE_PASSWORD and service discovery."
            elif exc.status == 0:
                detail = "Connection failed. Check the OpenCode service, OPENCODE_URL and network access."
            else:
                detail = f"API returned HTTP {exc.status}; check server logs and API compatibility."
            checks.append(result("OpenCode API", "fail", detail))
        except Exception:
            checks.append(result("OpenCode API", "fail", "Discovery or API check failed. Check the OpenCode service and configured URL."))
        finally:
            if client is not None:
                await client.close()
    checks.append(result("Discord delivery", "skip", "No Discord login or test DM sent. Running services alone do not prove that DMs work."))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Skip the OpenCode network check")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable results")
    args = parser.parse_args()
    checks = asyncio.run(inspect(offline=args.offline))
    if args.json:
        print(json.dumps({"checks": checks}, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"[{check['status'].upper():4}] {check['check']}: {check['detail']}")
    return 1 if any(check["status"] == "fail" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
