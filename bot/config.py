"""Runtime configuration for the OpenCode Discord bridge.

The env parsing, defaults and fail-closed startup checks now live in the native
engine (``cpp/config.cpp``); this module re-exports them so existing imports keep
working.
"""

from __future__ import annotations

import asyncio

from ._engine import (
    DEFAULT_SERVICE_URL,
    Config,
    cache_dir,
    state_dir,
    xdg_dir,
)
from ._engine import discover_service as _discover_service

__all__ = [
    "DEFAULT_SERVICE_URL",
    "Config",
    "cache_dir",
    "discover_service",
    "state_dir",
    "xdg_dir",
]


async def discover_service() -> tuple[str, str, str]:
    """Best-effort discovery of the local OpenCode service URL and password.

    Returns (url, username, password). Environment variables always win, so this
    only fills in what is missing.

    The engine does the work synchronously (PATH lookup, a bounded subprocess,
    service.json), so it runs on a worker thread to keep the loop free while
    keeping the original async signature.
    """
    return await asyncio.to_thread(_discover_service)
