# OpenCode Discord bridge

Chat with OpenCode from Discord DMs. Pick a model, set the reasoning effort,
browse old conversations, approve tool permissions, and steer a running turn —
all from a DM.

```
you ▸ what changed in config.py?
bot ▸ ⏳ thinking · anthropic/claude-sonnet-4 · high
     …streams in, edits in place…
     ✅ read  ✅ grep
     Three things changed: the port default, the retry count, and the timeout.
     -#anthropic/claude-sonnet-4 · high · $0.0041 · 18.2k tokens · 6.2s · 2 tool calls
```

## Requirements

- An OpenCode service reachable over HTTP (the local background service is ideal)
- Python 3.11+, and its development headers
- A C++20 toolchain: GCC 13+ or Clang 16+, CMake 3.20+, `ninja` or `make`
- Development headers for **libcurl** and **SQLite 3**
- A Discord application with the **Message Content Intent** enabled

On Arch (what this was developed against):

```sh
sudo pacman -S base-devel cmake ninja curl sqlite
```

On Debian/Ubuntu: `sudo apt install build-essential cmake ninja-build libcurl4-openssl-dev libsqlite3-dev python3-dev`.

The bot is half Python and half C++ — the OpenCode-facing engine is a native
extension. See [ARCHITECTURE.md](ARCHITECTURE.md) for why the split falls there.

## Setup

**1. Create the Discord bot**

At <https://discord.com/developers/applications>: *New Application* → *Bot* →
*Reset Token* → copy it. Under **Bot → Privileged Gateway Intents**, turn on
**Message Content Intent** (the bot needs to read your DMs). *Install App* → *DM
the bot*.

**2. Invite it once**

Discord will not let you DM a bot until you share a server with it, so open this
link (it asks for **zero** server permissions — the bot only needs to exist in
one of your guilds) and authorise it for any server you have, even a throwaway
one:

```
https://discord.com/api/oauth2/authorize?client_id=<your application id>&permissions=0&scope=bot%20applications.commands
```

The application id is in the developer portal URL. The bot logs its own invite
link on start-up, so `journalctl --user -u opencode-discord | grep invite` will
print it for you.

**3. Install**

`pip install -e .` builds the native engine (`bot/_engine*.so`) through CMake:

```sh
cd ~/opencode-discord
python3 -m venv .venv
.venv/bin/pip install -e .
```

To build just the extension, or to rebuild after editing anything in `cpp/`:

```sh
./build.sh
```

`run.sh` and `test.sh` run that for you if the extension is missing, so the
steps below work either way.

**4. Configure**

```sh
cp .env.example .env
$EDITOR .env          # DISCORD_TOKEN and DISCORD_USER_IDS at minimum
```

Your numeric user id: enable Discord's *Developer Mode*, right-click yourself →
*Copy User ID*.

The OpenCode service is discovered automatically (`opencode service status` for
the URL, `~/.config/opencode/service.json` for the password). Set
`OPENCODE_URL` / `OPENCODE_PASSWORD` if you run it somewhere unusual.

**5. Run**

```sh
./run.sh
```

DM the bot. The commands are registered on start-up, but Discord can take up to
an hour to publish them to your DMs; until then typing one as plain text gets a
short explanation instead of reaching the model. Plain typing works immediately.

### As a service (always on)

```sh
mkdir -p ~/.config/systemd/user
cp opencode-server.service opencode-discord.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now opencode-discord
loginctl enable-linger "$USER"     # start at boot without logging in
```

Then: `journalctl --user -u opencode-discord -f` to follow it, and
`systemctl --user {start,stop,restart,status} opencode-discord` to manage it.

What "always on" actually means here:

| Behaviour | How |
| --- | --- |
| Starts at boot, no login needed | `loginctl enable-linger` |
| Recovers from a crash | `Restart=always`, every 10s |
| Starts OpenCode at boot | `Wants=opencode-server.service` starts a separate server unit |
| Waits for OpenCode to be ready | `ensure-opencode.sh --wait-only` checks it before starting the bot |
| Recovers from an OpenCode crash | The server unit restarts after 5s |
| Never gives up | `StartLimitIntervalSec=0` — no start-rate limit |
| Clean shutdown mid-reply | `KillSignal=SIGINT`, 20s grace |
| Refuses to run twice | `flock` on `bot.lock`, so you never get duplicate replies |
| Runs as you, not root | systemd **user** unit |

Because it is a *user* unit, it only starts once you have logged in at least
once (`enable-linger` covers reboots after that), and it stops when you log out
of your last session on some setups.

**Do not run `opencode service stop` to test this.** That service hosts any
interactive OpenCode session you have open — including the one you would use to
read this file. Stop it and your session dies mid-answer. The bot will start it
again, but the conversation does not come back.

State lives in `~/.local/state/opencode-discord/` (XDG state dir, created and
owned by systemd via `StateDirectory=`) and uploads in
`~/.cache/opencode-discord/`.

The local OpenCode server runs in `opencode-server.service`, outside the bot's
read-only filesystem sandbox. It can write its registry under
`~/.local/state/opencode/` and work on user projects. Restarting the bot leaves
the server and its conversations running. Do not launch a background server
from `ExecStartPre`: systemd kills processes left by that step before starting
the bot.

When upgrading an existing installation, copy **both** unit files above, run
`systemctl --user daemon-reload`, then `systemctl --user restart opencode-discord`.
If an OpenCode server is already running outside systemd, migrate it during an
idle period so that only one process owns the background service.

## Commands

| Command | What it does |
| --- | --- |
| `/new [title] [dir]` | Start a new conversation |
| `/sessions [query] [scope]` | Browse past conversations in a paged menu |
| `/use <conversation>` | Jump to one by id or title (autocomplete) |
| `/history [n]` | Replay recent messages |
| `/model [provider/model]` | Switch model (autocomplete across every enabled model) |
| `/effort [level]` | Reasoning effort for the current model (`low`…`max`) |
| `/agent [name]` | Switch agent (`build`, `plan`, …) |
| `/dir [path]` | Working directory for new conversations |
| `/status` | Session, model, effort, tokens, cost |
| `/stop` | Interrupt the running turn |
| `/undo` / `/redo` | Roll back the last exchange / cancel that |
| `/compact` | Summarise the context |
| `/rename <title>` | Rename the conversation |
| `/fork` | Branch into a new conversation |
| `/forget` | Drop it from your list (data kept) |
| `/delete` | Delete it on the server |
| `/refresh` | Reload models and agents |
| `/sync` | Re-register the commands with Discord |
| `/help` | The same list |

## How it behaves

**Streaming.** The bot subscribes to OpenCode's event stream and edits one
Discord message in place as the answer arrives (throttled to stay under
rate limits). Each model step gets its own message, so a tool-using turn reads
as a short sequence: `✅ read`, then the answer.
The final answer replaces the streaming preview and is split into messages
when needed, preserving the full text and keeping long code blocks balanced.
Tool summaries appear once after each step's answer.

**Effort.** OpenCode models expose *variants* — `low`, `medium`, `high`,
`xhigh`, `max` depending on the model. `/effort` maps onto those, and switching
model keeps your effort if the new model supports it.

**Steering.** Message the bot while a turn is running and the text is injected
into the live turn (`delivery: "steer"`) instead of queueing. `/stop` cancels.

**Permissions.** When the agent asks to do something (read a `.env`, touch a
path outside the project), the bot posts the request with *Allow once* /
*Always allow* / *Deny* buttons and the turn pauses until you answer.

**Questions.** When the agent needs you to choose between options (its
`question` tool), the bot renders the choices as a Discord select menu or
Yes/No buttons. Text and numeric questions have an **Enter** button that opens
an input window; numbers are checked and converted by the C++ engine before
submission. Forms with more than four fields have Previous/Next buttons.
Multi-field forms submit once every field is answered, and keep your answers
if the server request fails so you can retry. Input windows accept up to 4,000
characters per answer. Unsupported field types must be answered in OpenCode.

**Files.** Attachments are written to `ATTACHMENT_DIR` and passed to the model as
file URIs, so images work with multimodal models.

**Attachments to Discord output.** Answers longer than 1900 characters are split
on paragraph boundaries with code fences kept balanced; each turn ends with a dim
line showing model, effort, cost, tokens, duration and tool count.

## Security

This bot drives an agent that can run tools on this machine, so:

- `DISCORD_USER_IDS` is a hard allow list. DMs from anyone else are ignored and
  their interactions are dropped without a reply.
- The commands are registered with `allowed_contexts` set to DMs only, so they do
  not appear in guilds at all.
- Bot output is sent with `allowed_mentions=none`; `@everyone` cannot ping.
- Every permission request surfaces in the DM instead of being auto-approved.

`ALLOW_ANY_USER=1` exists for throwaway setups — don't use it on a machine you
care about.

## Layout

The OpenCode-facing half lives in C++ (`cpp/`, built into `bot/_engine*.so`); the
Discord-facing half is Python (`bot/`). [ARCHITECTURE.md](ARCHITECTURE.md) explains
the boundary and how the two halves talk to each other.

```
cpp/               native engine (C++20)
  client.*         OpenCode HTTP + SSE transport, stream thread
  sse.*            byte-level SSE framing
  turn.*           event -> renderable state (text, tools, reasoning)
  store.*          SQLite: active session, model/effort prefs, history
  text.*           Discord-safe chunking and formatting
  config.*         env knobs, XDG paths, service discovery
  jsonvalue.hpp    shared Python-truthiness / str() helpers
  bindings.cpp     the surface Python sees
  engine_tests.cpp standalone tests, so sanitizers can run off-Python

bot/               Python: Discord, and the asyncio wiring
  main.py          entry point, DM routing, DM-only command gate
  runner.py        per-user conversation state machine, turn orchestration
  service.py       shared state: client, store, per-user conversations
  messaging.py     send/edit helpers, throttled live messages
  ui.py            select menus, buttons, embeds
  commands.py      slash commands
  oc.py            async facade over the native transport + event fan-out
  turn.py          re-export shims over the native engine
  store.py
  textutil.py
  config.py
tests/             see below
```

State lives in `~/.local/state/opencode-discord/state.db`. Nothing is stored
beyond session ids, your model/effort preferences and conversation titles —
the transcripts themselves stay in OpenCode's own database.

## Tests

```sh
./test.sh          # offline: logic, command tree, views, embeds, gating
./test.sh --live   # plus real turns against the local OpenCode service
```

The native engine also builds as a plain executable, so it can run under
sanitizers without instrumenting the extension the bot loads:

```sh
./build.sh --sanitize && ./build-sanitize/engine_tests
```

- `tests/engine_smoke.py` — the extension built, imports, and exposes every name
  the Python modules re-export
- `tests/parser_parity.py` — the native SSE parser against a verbatim copy of the
  original Python one, over an adversarial corpus in several chunk splittings
- `tests/units.py` — chunking (including code-fence balance), the turn state
  machine, the SSE parser (including payloads past aiohttp's 512 KiB line
  limit), the store
- `tests/discord_surface.py` — command registration, select pagination, embeds,
  and the DM/allow-list gate
- `tests/e2e.py` — a real conversation: streaming, tool calls, transcripts,
  model/effort switching, fork/rename/revert
- `tests/permissions.py` — a real permission request, answered through the API,
  and the turn resuming afterwards
- `tests/big_event.py` — a tool reading a ~1.2 MB file, checking the event
  stream never has to reconnect mid-turn
- `tests/view_callbacks.py` — every button and select has a real handler, and
  simulated clicks actually re-render
- `tests/ensure_script.py` — the systemd dependency check, against a stub
  `opencode` (the real one must never be stopped: it hosts your own session)
- `tests/messenger.py` — message kwargs run through discord.py's own validation
- `tests/commands_flow.py` — every slash command's callback executed against a
  fake interaction, asserting each one actually replies

The live suites create sessions in a temp directory and delete them afterwards.

## Troubleshooting

**Slash commands don't appear.** The bot registers them *globally* on start-up,
because a global registration is the only one that reaches DM channels —
Discord rejects registration against the "DM guild" (the bot's own user id) with
`403 Missing Access`. The catch is propagation: Discord can take **up to an
hour** to publish a brand new command to DMs. `/sync` re-registers but cannot
shorten that. Until they appear, typing one as plain text is intercepted with an
explanation rather than being sent to the model, so the agent never tries to
"run" a slash command with its shell tools.

Verify what Discord has on file:

```sh
curl -s -H "Authorization: Bot $DISCORD_TOKEN" \
  https://discord.com/api/v10/applications/$APP_ID/commands | python3 -c \
  "import json,sys; print(len(json.load(sys.stdin)), 'commands')"
```

**"cannot reach the OpenCode service".** Check `opencode service status`; if it
prints a different port than `OPENCODE_URL`, either set the variable or unset it
to let the bot discover it.

**"Cannot send messages to this user" / no DM channel.** Discord blocks DMs
between users who share no server. Invite the bot once (see step 2) — the bot
prints its own invite link at start-up.

**Unauthorised / silence.** The sender is not in `DISCORD_USER_IDS`. The bot
replies with the sender's own user id once every 15 minutes; set
`ALLOWLIST_HINT=0` to suppress that.

**Bot ignores messages.** The Message Content Intent is off, or the bot lacks
`dm_messages`. Both are required.

**A turn hangs.** `/stop`, then check `opencode service status` and
`journalctl --user -u opencode-discord`.
