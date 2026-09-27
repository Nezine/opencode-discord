"""SQLite persistence for per-user chat state.

One row per Discord user holds the active conversation plus the model / effort /
agent preferences, and a join table remembers every conversation the user has
used through the bot.

The implementation now lives in the native engine (``cpp/store.cpp``); this
module re-exports it so existing imports keep working. The schema and the stored
value types are unchanged, so an existing ``state.db`` keeps working.
"""

from __future__ import annotations

from ._engine import Store, UserState

__all__ = ["Store", "UserState"]
