"""Test ensure-opencode.sh without touching the real OpenCode service.

Stopping the real service would kill the very session running this test, so the
script is exercised against a stub `opencode` on PATH that reports whatever the
test asks it to.

    .venv/bin/python -m tests.ensure_script
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ensure-opencode.sh"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


STUB = """#!/usr/bin/env bash
# Stub standing in for the opencode CLI.
STATE="$STUB_DIR/state"
case "$1 $2" in
  "service status")
    if [ -f "$STATE/running" ]; then
      echo "http://127.0.0.1:12345"
      exit 0
    fi
    echo "no service" >&2
    exit 1
    ;;
  "service start")
    touch "$STATE/start_attempted"
    [ -f "$STATE/start_fails" ] && { echo "boom" >&2; exit 1; }
    sleep "$STUB_START_DELAY"
    touch "$STATE/running"
    exit 0
    ;;
esac
exit 2
"""


def run(tmp: Path, *, running: bool, start_fails: bool = False, delay: str = "0",
        wait_only: bool = False) -> tuple[int, str]:
    state = tmp / "state"
    state.mkdir(exist_ok=True)
    if running:
        (state / "running").touch()
    if start_fails:
        (state / "start_fails").touch()
    stub = tmp / "opencode"
    stub.write_text(STUB)
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp}:{os.environ['PATH']}",
        "STUB_DIR": str(tmp),
        "STUB_START_DELAY": delay,
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), *(["--wait-only"] if wait_only else [])],
        capture_output=True, text=True, env=env, timeout=120
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    if not SCRIPT.exists():
        print(f"missing {SCRIPT}")
        return 1

    print("\nensure-opencode.sh")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        code, out = run(tmp, running=True, wait_only=True)
        check("wait-only accepts a ready server", code == 0, out)
        check("wait-only never starts a ready server", not (tmp / "state/start_attempted").exists())

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        code, out = run(tmp, running=False, wait_only=True)
        check("wait-only fails when the server is unavailable", code != 0, out)
        check("wait-only never spawns a daemon", not (tmp / "state/start_attempted").exists())

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        code, out = run(tmp, running=True)
        check("exits 0 when the service is already up", code == 0, f"code={code}")
        check("does not try to start it", "starting it" not in out and "trying to start" not in out, out)

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        code, out = run(tmp, running=False)
        check("exits 0 when it starts the service", code == 0, f"code={code}")
        check("reports that it started it", "not reachable" in out or "not running" in out, out)
        check("confirms the service came up", "is up" in out, out)

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        code, out = run(tmp, running=False, start_fails=True)
        check("still exits 0 when start fails", code == 0, f"code={code}")
        check("notes the failed start", "failed or timed out" in out, out)
        check("preserves the startup error for diagnosis", "boom" in out, out)
        check("warns instead of hanging", "not up" in out, out)

    # A hung `opencode` must not wedge the unit: the script bounds every call.
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        start = time_of_day()
        code, out = run(tmp, running=False, delay="45")
        elapsed = time_of_day() - start
        check("a hanging 'opencode start' is cut off", elapsed < 60, f"{elapsed:.0f}s")
        check("exits 0 even when the service never appears", code == 0, f"code={code}")

    if shutil.which("timeout") is None:
        print("  note: coreutils 'timeout' not found; the script relies on it")

    print("\n" + "=" * 40)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("the dependency check behaves")
    return 0


def time_of_day() -> float:
    import time

    return time.monotonic()


if __name__ == "__main__":
    sys.exit(main())
