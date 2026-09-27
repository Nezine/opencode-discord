"""SQLite persistence for per-user chat state.

One row per Discord user holds the active conversation plus the model / effort /
agent preferences, and a join table remembers every conversation the user has
used through the bot.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UserState:
    user_id: int
    session_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    variant: str | None = None
    agent: str | None = None
    directory: str | None = None
    title: str | None = None

    @property
    def has_model(self) -> bool:
        return bool(self.provider_id and self.model_id)

    @property
    def model_label(self) -> str:
        if not self.model_id:
            return "default"
        label = f"{self.provider_id}/{self.model_id}"
        if self.variant and self.variant != "default":
            label += f" · {self.variant}"
        return label


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                session_id  TEXT,
                provider_id TEXT,
                model_id    TEXT,
                variant     TEXT,
                agent       TEXT,
                directory   TEXT,
                title       TEXT,
                updated_at  REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS conversations (
                user_id    INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                title      TEXT,
                last_seen  REAL NOT NULL,
                PRIMARY KEY (user_id, session_id)
            );

            CREATE INDEX IF NOT EXISTS conversations_user
                ON conversations (user_id, last_seen DESC);
            """
        )
        self._db.commit()

    # ------------------------------------------------------------------ users

    def get_user(self, user_id: int) -> UserState:
        row = self._db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return UserState(user_id=user_id)
        return UserState(
            user_id=row["user_id"],
            session_id=row["session_id"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            variant=row["variant"],
            agent=row["agent"],
            directory=row["directory"],
            title=row["title"],
        )

    def save_user(self, state: UserState) -> None:
        self._db.execute(
            """
            INSERT INTO users
                (user_id, session_id, provider_id, model_id, variant, agent, directory, title, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                session_id = excluded.session_id,
                provider_id = excluded.provider_id,
                model_id = excluded.model_id,
                variant = excluded.variant,
                agent = excluded.agent,
                directory = excluded.directory,
                title = excluded.title,
                updated_at = excluded.updated_at
            """,
            (
                state.user_id,
                state.session_id,
                state.provider_id,
                state.model_id,
                state.variant,
                state.agent,
                state.directory,
                state.title,
                time.time(),
            ),
        )
        self._db.commit()

    def update(self, user_id: int, **fields: object) -> UserState:
        state = self.get_user(user_id)
        for key, value in fields.items():
            if hasattr(state, key) and key != "user_id":
                setattr(state, key, value)
        self.save_user(state)
        return state

    # ---------------------------------------------------------- conversations

    def remember(self, user_id: int, session_id: str, title: str | None = None) -> None:
        self._db.execute(
            """
            INSERT INTO conversations (user_id, session_id, title, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, session_id) DO UPDATE SET
                title = excluded.title,
                last_seen = excluded.last_seen
            """,
            (user_id, session_id, title, time.time()),
        )
        self._db.commit()

    def conversation_ids(self, user_id: int, limit: int = 200) -> list[str]:
        rows = self._db.execute(
            "SELECT session_id FROM conversations WHERE user_id = ? ORDER BY last_seen DESC LIMIT ?",
            (user_id, limit),
        )
        return [r["session_id"] for r in rows]

    def forget(self, user_id: int, session_id: str) -> None:
        self._db.execute(
            "DELETE FROM conversations WHERE user_id = ? AND session_id = ?", (user_id, session_id)
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
