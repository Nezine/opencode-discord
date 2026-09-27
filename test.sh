#!/usr/bin/env bash
# Run every test suite.
#   ./test.sh          offline suites only (fast)
#   ./test.sh --live   also hit the local OpenCode service
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python

status=0
run() {
  echo
  echo "════ $* ════"
  "$@" || status=1
}

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
