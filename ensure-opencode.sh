#!/usr/bin/env bash
# Make sure the OpenCode background service is up before the bot starts.
#
# The systemd bridge uses --wait-only with a separately supervised server.
# Manual invocation can still start a background service. Every call is bounded.
set -uo pipefail

CHECK=(timeout 10 opencode service status)
START=(timeout 30 opencode service start)

if "${CHECK[@]}" >/dev/null 2>&1; then
  exit 0
fi

if [[ "${1:-}" == "--wait-only" ]]; then
  echo "waiting for the opencode server"
else
  echo "opencode service is not reachable; trying to start it"
  if ! "${START[@]}" >/dev/null; then
    echo "note: 'opencode service start' failed or timed out" >&2
  fi
fi

for _ in $(seq 1 20); do
  if "${CHECK[@]}" >/dev/null 2>&1; then
    echo "opencode service is up"
    exit 0
  fi
  sleep 0.5
done

echo "warning: the opencode service is not up; the bot will keep retrying" >&2
if [[ "${1:-}" == "--wait-only" ]]; then
  exit 1
fi
exit 0
