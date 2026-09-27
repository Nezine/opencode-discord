#!/usr/bin/env bash
# Run every test suite.
#   ./test.sh          offline suites only (fast)
#   ./test.sh --live   also hit the local OpenCode service
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python

# The native engine is a build artifact; make sure it exists before importing it.
shopt -s nullglob
engine=(bot/_engine*.so)
shopt -u nullglob
if (( ${#engine[@]} == 0 )); then
  echo "Native engine not built yet; running ./build.sh"
  ./build.sh || exit 1
fi

status=0
run() {
  echo
  echo "════ $* ════"
  "$@" || status=1
}

run "$PY" -m tests.engine_smoke
run "$PY" -m tests.parser_parity
run "$PY" -m tests.units
run "$PY" -m tests.discord_surface
run "$PY" -m tests.view_callbacks
run "$PY" -m tests.ensure_script
run "$PY" -m tests.messenger
run "$PY" -m tests.commands_flow

if [[ "${1:-}" == "--live" ]]; then
  run "$PY" -m tests.e2e
  run "$PY" -m tests.permissions
  run "$PY" -m tests.big_event
fi

echo
if [[ $status -eq 0 ]]; then
  echo "all suites passed"
else
  echo "some suites failed"
fi
exit $status
