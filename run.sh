#!/usr/bin/env bash
# Launch the extracted AIO python-server locally.
# Mirrors how the container's supervisord starts it, minus the container-only backends.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="${VENV:-$ROOT/.venv}"
if [[ ! -d "$VENV" ]]; then
  echo "No venv at $VENV — run ./setup.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export WORKSPACE="${WORKSPACE:-$ROOT/workspace}"
mkdir -p "$WORKSPACE"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8091}"

exec python -m app.cli \
  --host "$HOST" \
  --port "$PORT" \
  --ws-ping-interval 30 \
  --mcp-config "$ROOT/config/mcp-hub.json" \
  --filter-mcp-servers sandbox \
  --workspace "$WORKSPACE"
