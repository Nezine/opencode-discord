"""Smoke test for the native engine extension.

    .venv/bin/python -m tests.engine_smoke
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def eq(name: str, actual, expected) -> None:
    check(name, actual == expected, f"\n       got:      {actual!r}\n       expected: {expected!r}")


def test_engine_module() -> None:
    print("\nbot._engine")
    try:
        from bot import _engine
    except ImportError as exc:
        check("imports", False, f"\n       {exc}\n       run ./build.sh")
        return

    check("imports", True)
    check("has a docstring", bool(_engine.__doc__))
    eq("version", _engine.__version__, "0.1.0")
    eq("hello()", _engine.hello(), "engine online")

    # The extension must sit beside the Python sources, because both the editable
    # install and `python -m bot.main` resolve `bot` to this directory.
    module_dir = Path(_engine.__file__).resolve().parent
    eq("built into the bot package", module_dir, Path(__file__).resolve().parent.parent / "bot")

    # Everything bot/*.py re-exports must exist here, or the shims break at import
    # time in whichever suite touches them first.
    required = [
        "DEFAULT_SERVICE_URL",
        "LIMIT",
        "Config",
        "SSEParser",
        "Step",
        "Store",
        "ToolActivity",
        "TurnState",
        "UserState",
        "apply_event",
        "cache_dir",
        "clip",
        "discover_service",
        "duration",
        "human_cost",
        "human_tokens",
        "hydrate_from_message",
        "join_nonempty",
        "rel_time",
        "render_footer",
        "render_reasoning",
        "render_status",
        "render_tools",
        "sanitize_mentions",
        "split_text",
        "state_dir",
        "tool_detail",
        "xdg_dir",
    ]
    missing = [name for name in required if not hasattr(_engine, name)]
    check("exposes the expected API", not missing, f"\n       missing: {missing}")


def main() -> int:
    test_engine_module()
    print("\n" + ("=" * 40))
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("engine smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
