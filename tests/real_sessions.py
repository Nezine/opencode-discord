"""Check /sessions against the real history in the bot's configured directory.

    .venv/bin/python -m tests.real_sessions
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config, discover_service  # noqa: E402
from bot.oc import OpenCodeClient  # noqa: E402
from bot.service import Service  # noqa: E402
from bot.store import Store  # noqa: E402
from bot.ui import PER_PAGE, SessionSelectView  # noqa: E402



class Stub:
    async def close(self) -> None:
        return None


async def main() -> int:
    url, username, password = await discover_service()
    os.environ.setdefault("DISCORD_TOKEN", "stub")
    cfg = Config.from_env()
    cfg.opencode_url = url
    cfg.opencode_username = username
    cfg.opencode_password = password

    svc = Service(cfg, OpenCodeClient(url, username, password), Store(":memory:"))
    await svc.start()
    if not cfg.allowed_user_ids:
        print("set DISCORD_USER_IDS to run this probe")
        return 1
    user = sorted(cfg.allowed_user_ids)[0]
    conv = svc.conversation(user)
    print(f"working directory: {conv.directory}")

    total, _ = await svc.client.list_sessions(limit=1, directory=conv.directory)
    everything, _ = await svc.client.list_sessions(limit=200, directory=conv.directory)
    print(f"sessions the service knows about here: {len(everything)}")

    mine = await svc.list_user_sessions(conv, scope="mine")
    print(f"bot-tracked for this user: {len(mine)}")
    if not mine:
        all_scope = await svc.list_user_sessions(conv, scope="all")
        print(f"fallback to scope=all: {len(all_scope)}")
        sessions = all_scope
        scope = "all"
    else:
        sessions = mine
        scope = "mine"

    if not sessions:
        print("no sessions to show — /sessions would render the empty state")
        await svc.close()
        return 0

    view = SessionSelectView(svc, conv, sessions, page=0, total=len(sessions), scope=scope)
    print()
    print(view.body()[:900])
    print()
    print(f"pages: {view.pages} ({PER_PAGE} per page), first page options: {len(view.children[0].options)}")
    dead = [
        str(getattr(c, "custom_id", ""))
        for c in view.children
        if not hasattr(getattr(c, "callback", None), "__call__")
    ]
    print(f"items with no callable handler: {dead or 'none'}")

    if len(sessions) > PER_PAGE:
        page1 = SessionSelectView(svc, conv, sessions, page=1, total=len(sessions), scope=scope)
        first_title = sessions[PER_PAGE].get("title", "")
        print(f"page 2 preview starts with: {first_title[:60]!r}")
        ok = first_title[:20] in page1.body()
        print(f"page 2 preview matches its slice: {ok}")
    await svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
