#!/usr/bin/env bash
# Create a Python 3.12 venv and install dependencies.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3.12}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Need Python 3.12 (set \$PYTHON to its path). Try: brew install python@3.12" >&2
  exit 1
fi

"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Done. Start the server with:  ./run.sh"
