#!/usr/bin/env bash
# Diagnose without starting/stopping either service or sending model prompts.
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
if [[ ! -x .venv/bin/python ]]; then
  echo 'Missing virtualenv: python3 -m venv .venv && .venv/bin/pip install -e .'
  exit 1
fi
exec .venv/bin/python -m bot.doctor "$@"
