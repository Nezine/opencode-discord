#!/usr/bin/env bash
# Make sure the OpenCode background service is up before the bot starts.
#
# The bot needs it to answer at all, and that service does not survive a reboot on
# its own, so the bot unit runs this first. Every call is bounded: a hung
# `opencode` must never wedge the unit, and failing here is not fatal because the
# bot retries the connection and systemd restarts it.
set -uo pipefail

CHECK=(timeout 10 opencode service status)
START=(timeout 30 opencode service start)

if "${CHECK[@]}" >/dev/null 2>&1; then
  exit 0
fi

echo "opencode service is not reachable; trying to start it"
if ! "${START[@]}" >/dev/null 2>&1; then
  echo "note: 'opencode service start' failed or timed out" >&2
fi

for _ in $(seq 1 20); do
  if "${CHECK[@]}" >/dev/null 2>&1; then
    echo "opencode service is up"
    exit 0
  fi
  sleep 0.5
done

echo "warning: the opencode service is not up; the bot will keep retrying" >&2
exit 0
