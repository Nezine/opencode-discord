# Architecture

The bot is one process with two halves:

```
opencode-server.service                     separate user service
  └─ opencode serve --service               API + agent tool execution

opencode-discord.service
  └─ .venv/bin/python -m bot.main            (cwd = the repo root)
       ├─ bot/                                Discord + asyncio        [Python]
       └─ bot/_engine.cpython-314-*.so        the OpenCode engine      [C++20]
            ├─ std::thread: curl_easy_perform(GET /api/event)
            │    → SseParser → ordered_json → bounded queue + condvar
            └─ blocking HTTP calls, GIL released
```

The bridge waits for the server with `ensure-opencode.sh --wait-only`. The
server has its own systemd cgroup and can write the OpenCode state registry
and user projects; the bridge keeps its read-only filesystem sandbox.
Restarting the bridge does not stop the server.

## Where the line falls

Free-text form answers are collected by Discord modals in `bot/ui.py`.
`cpp/forms.cpp` validates required values and converts numeric answers before
the existing native client sends them to OpenCode. Python handles the view
pages and interaction acknowledgements.

The split is not "make it faster" (it was not slow). It is that the two halves
have genuinely different natures, and each language gets the half it is good at:

- **C++ gets the OpenCode side.** Byte-level SSE framing with a length cap,
  a tagged-union event reducer, SQLite, UTF-8-aware text chunking, and an HTTP
  client on its own thread. This is protocol and state work with no framework
  helping either way.
- **Python gets the Discord side.** `discord.py` provides the gateway WebSocket,
  RESUME/IDENTIFY, heartbeats, zlib decompression, per-route rate-limit buckets,
  interaction ACKs, follow-up webhooks, and component JSON. Hand-rolling that is
  the single most expensive and most failure-prone thing in this repository, and
  it buys nothing.

| Concern | Owner | Why |
|---|---|---|
| SSE framing, per-line cap, drop accounting | C++ | byte-bound, and aiohttp's line reader caps a line at 512 KiB |
| JSON parse + `dict` conversion of events | C++ | avoids per-event Python object churn |
| Turn state machine (steps, tools, permissions) | C++ | the real logic, and pure enough to unit test |
| Turn text composition (`render_*`) | C++ | pure string work |
| OpenCode HTTP transport + reconnect/backoff | C++ | libcurl, own thread |
| SQLite persistence | C++ | direct SQLite C API |
| Text chunking / fence balancing / formatting | C++ | code-point-aware, replaces a regex |
| Config parsing + service discovery | C++ | env, PATH lookup, subprocess, JSON |
| Discord gateway, commands, views, embeds | Python | `discord.py` is the moat |
| asyncio orchestration loop, event fan-out | Python | I/O wiring around Discord — see below |

## How the halves talk

Three seams, all narrow:

1. **Requests** — `cpp/client.cpp` exposes one blocking method per endpoint. The
   binding releases the GIL around each (`py::gil_scoped_release`), and
   `bot/oc.py` drives them with `asyncio.to_thread`, so the async call sites never
   changed.

2. **Events** — the libcurl stream thread parses SSE and appends to one bounded
   `std::deque` (capacity 4000, drop-oldest) behind a mutex and condvar. Python
   drains it with `poll(max_n, timeout_ms)`, which also releases the GIL while it
   waits. One adapter task in `bot/oc.py` fans the flat stream out to per-session
   `asyncio.Queue`s.

3. **Errors** — `HttpError` carries status/message/payload out of C++; an
   exception translator raises the Python `OpenCodeError`, which is *defined* by
   the engine (via `py::exec`) so it keeps the original's constructor, attributes
   and `not_found`/`conflict` properties. Existing `except OpenCodeError` sites
   catch it unchanged.

### Threading rules

These are load-bearing; breaking them gives you a crash, not a warning:

1. **The stream thread never touches a Python object.** It only appends plain
   C++ values to the queue. All `json → py::object` conversion happens on the
   Python thread, in `poll()`, with the GIL held.
2. **Anything blocking releases the GIL.** Otherwise a 20 s timeout stalls the
   whole event loop.
3. **`close()` must be able to interrupt a blocked thread.** The stream thread's
   libcurl progress callback aborts the transfer when closing, and the condvar is
   notified, so neither `poll()` nor `shutdown` waits out the low-speed timeout.
4. **The `SseParser` is owned by the stream thread alone.** Its `largest`/`dropped`
   counters are mirrored into atomics for Python to read.

## The Python shims

`bot/turn.py`, `bot/store.py`, `bot/textutil.py` and `bot/config.py` are
re-export shims over the engine, and `bot/oc.py` keeps `OpenCodeError`,
`SSEParser` and the fan-out. This is deliberate: every test suite imports these
module names directly, so keeping them meant each phase of the migration could
land with the full suite still green.

If you add something to the engine, add it to the matching shim's `__all__` and
to the `required` list in `tests/engine_smoke.py` — that list is what catches a
missing binding before a suite fails somewhere less obvious.

## Build

CMake is authoritative (`CMakeLists.txt`). Two targets:

- `_engine` — the pybind11 module, written **into `bot/`**. Both the editable
  install (which maps `bot` at the source tree) and `python -m bot.main` from the
  repo root resolve `bot` to that directory, so the extension has to live there
  rather than in a build tree.
- `engine_tests` — the same C++ sources, no Python, so it can be built with
  `-fsanitize=address,undefined`. An instrumented `.so` in `bot/` would clobber
  the module the bot loads and need `LD_PRELOAD`, so sanitizers run here.

```sh
./build.sh              # incremental RelWithDebInfo
./build.sh --sanitize   # engine_tests with ASan + UBSan; leaves bot/ alone
./build.sh --clean
```

`pip install -e .` goes through `setup.py`, which shells out to CMake from
`build_ext`, so the pip path and the direct path build the same thing.

Dependencies are resolved from the venv: `find_package(pybind11)` is pointed at
`.venv/bin/python -m pybind11 --cmakedir`, so the module is always built against
the interpreter that will load it. `nlohmann/json` is vendored in `third_party/`
rather than taken from the system, so the build needs no root.

## Things to know before changing the engine

- **Python dicts preserve insertion order; `nlohmann::json` sorts keys.** The
  engine uses `nlohmann::ordered_json` throughout (`cpp/json.hpp`) because
  payloads cross straight over from Python dicts. Using the default type silently
  reordered `tool_detail()`'s output once.
- **Discord's 1900/2000 character budget is code points, not bytes.** Every
  length and slice in `cpp/text.cpp` goes through `cpp/utf8.hpp`. A byte-based
  `size()` splits emoji in half.
- **Python's `int()` tolerates whitespace and a sign**, `std::from_chars` does
  not, and `str.isdigit()` (used by the allowlist parser) rejects both. The env
  parsers in `cpp/config.cpp` mirror Python's behaviour on purpose.
- **Reflection has no C++ equivalent.** `Store.update(**fields)` was
  `hasattr`/`setattr`; it is now an explicit field switch, and unknown names are
  still ignored.
- **`rel_time` takes milliseconds** and renders a local date past 30 days, so it
  uses `localtime_r` (not thread-safe `localtime`).

## Deliberate deviations

Recorded so they are not mistaken for bugs later:

- An error payload with no `message` field renders as JSON where Python used
  `str(dict)`'s repr. Only reachable for a message-less error.
- `client.config()` returns `{}` for the bare-array response the service actually
  sends, where the original would have raised `AttributeError`. The method has no
  callers.
- `discover_service()` runs `opencode service status` through coreutils
  `timeout`, the same bound `ensure-opencode.sh` uses, instead of asyncio's
  subprocess plus `wait_for`.
- The XDG path helpers (`xdg_dir`, `state_dir`, `cache_dir`) build `pathlib.Path`
  through Python from the binding, rather than reimplementing `Path`
  normalisation in C++.
- `conversation_ids` orders by `last_seen DESC, rowid DESC`. The original ordered
  only by `last_seen`, so two remembers inside one clock tick could come back
  either way; the faster C++ path makes that tie more likely.
