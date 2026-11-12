#!/usr/bin/env bash
# Polybench: create a clean virtual environment from a source checkout (editable install).
# Needs internet once (pip). Usage:  scripts/install.sh [extra ...]      e.g.  scripts/install.sh camera
#   PYTHON=/usr/local/bin/python3 scripts/install.sh      (default python3, must be 3.12 or newer)
#   VENV=/some/path scripts/install.sh                    (default .venv in the repo)
# Result: a venv with polybench, python-can and cantools. Ends with `polybench selftest`.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
VENV="${VENV:-.venv}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "need Python 3.12 or newer (set PYTHON=...)"; exit 1; }
EXTRAS=""
for e in "$@"; do EXTRAS="${EXTRAS:+$EXTRAS,}$e"; done
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
if [ -n "$EXTRAS" ]; then "$VENV/bin/python" -m pip install --quiet -e ".[$EXTRAS]"; else "$VENV/bin/python" -m pip install --quiet -e .; fi
echo "installed: $("$VENV/bin/python" -m pip list 2>/dev/null | grep -i -E '^(polybench|python-can|cantools|fastapi|uvicorn|PyYAML|opencv)' | tr -s ' ' | tr '\n' ';')"
"$VENV/bin/polybench" selftest
echo
echo "next: $VENV/bin/polybench doctor"
