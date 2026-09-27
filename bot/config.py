"""Runtime configuration for the OpenCode Discord bridge."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SERVICE_URL = "http://127.0.0.1:4096"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Config:
    discord_token: str
    allowed_user_ids: set[int] = field(default_factory=set)
    allow_any_user: bool = False
    allowlist_hint: bool = True

    opencode_url: str = DEFAULT_SERVICE_URL
    opencode_username: str = "opencode"
    opencode_password: str = ""
    opencode_directory: str = ""

    default_model: str = ""
    default_effort: str = ""
    default_agent: str = "build"

    steer_when_busy: bool = True
    edit_interval: float = 1.5
    turn_timeout: float = 3600.0
    stall_timeout: float = 300.0
    show_tools: bool = True
    show_reasoning: bool = False
    session_list_limit: int = 100
    attachment_dir: str = ""

    def allowed(self, user_id: int) -> bool:
        if self.allow_any_user:
            return True
        return user_id in self.allowed_user_ids

    @classmethod
    def from_env(cls) -> "Config":
        users = {
            int(part)
            for part in os.environ.get("DISCORD_USER_IDS", "").replace(",", " ").split()
            if part.strip().isdigit()
        }
        allow_any = _env_bool("ALLOW_ANY_USER", False)
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
        if not users and not allow_any:
            raise SystemExit(
                "DISCORD_USER_IDS is empty. Set your numeric Discord user id, or set "
                "ALLOW_ANY_USER=1 to allow everyone (the agent can run tools on this machine, "
                "so a whitelist is strongly recommended)."
            )

        cfg = cls(
            discord_token=token,
            allowed_user_ids=users,
            allow_any_user=allow_any,
            allowlist_hint=_env_bool("ALLOWLIST_HINT", True),
            opencode_url=os.environ.get("OPENCODE_URL", "").strip() or DEFAULT_SERVICE_URL,
            opencode_username=os.environ.get("OPENCODE_USERNAME", "opencode").strip() or "opencode",
            opencode_password=os.environ.get("OPENCODE_PASSWORD", ""),
            opencode_directory=os.environ.get("OPENCODE_DIRECTORY", "").strip(),
            default_model=os.environ.get("DEFAULT_MODEL", "").strip(),
            default_effort=os.environ.get("DEFAULT_EFFORT", "").strip(),
            default_agent=os.environ.get("DEFAULT_AGENT", "build").strip() or "build",
            steer_when_busy=_env_bool("STEER_WHEN_BUSY", True),
            edit_interval=_env_float("EDIT_INTERVAL", 1.5),
            turn_timeout=_env_float("TURN_TIMEOUT", 3600.0),
            stall_timeout=_env_float("STALL_TIMEOUT", 300.0),
            show_tools=_env_bool("SHOW_TOOLS", True),
            show_reasoning=_env_bool("SHOW_REASONING", False),
            session_list_limit=_env_int("SESSION_LIST_LIMIT", 100),
            attachment_dir=os.environ.get("ATTACHMENT_DIR", "").strip(),
        )
        if not cfg.opencode_directory:
            cfg.opencode_directory = str(Path.home())
        if not cfg.attachment_dir:
            cfg.attachment_dir = str(cache_dir() / "attachments")
        return cfg


def xdg_dir(variable: str, default: str) -> Path:
    """Honour XDG_*_HOME, falling back to the spec's default."""
    value = os.environ.get(variable, "").strip()
    return Path(value) if value else Path.home() / default


def state_dir() -> Path:
    """Where the bot keeps its database and lock file.

    Systemd's StateDirectory= creates and owns this path, so using the XDG state
    directory keeps the unit working without hand-made directories.
    """
    return xdg_dir("XDG_STATE_HOME", ".local/state") / "opencode-discord"


def cache_dir() -> Path:
    """Scratch space for uploaded files (systemd CacheDirectory= owns this too)."""
    return xdg_dir("XDG_CACHE_HOME", ".cache") / "opencode-discord"


async def discover_service() -> tuple[str, str, str]:
    """Best-effort discovery of the local OpenCode service URL and password.

    Returns (url, username, password). Environment variables always win, so this
    only fills in what is missing.
    """
    url = os.environ.get("OPENCODE_URL", "").strip()
    username = os.environ.get("OPENCODE_USERNAME", "opencode").strip() or "opencode"
    password = os.environ.get("OPENCODE_PASSWORD", "")

    if not url:
        binary = shutil.which("opencode")
        if binary:
            import asyncio

            try:
                proc = await asyncio.create_subprocess_exec(
                    binary, "service", "status",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
                first = out.decode("utf-8", "replace").strip().splitlines()
                if first and first[0].startswith("http"):
                    url = first[0].strip()
            except (asyncio.TimeoutError, OSError):
                pass

    if not password:
        for candidate in (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opencode" / "service.json",
            Path.home() / ".config" / "opencode" / "service.json",
        ):
            try:
                password = json.loads(candidate.read_text()).get("password", "")
            except (OSError, ValueError):
                continue
            if password:
                break

    return (url or DEFAULT_SERVICE_URL, username, password)
