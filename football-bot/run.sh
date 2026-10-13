#!/usr/bin/env bash
# Linux / macOS launcher: creates .venv, installs pinned dependencies, starts the site.
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for v in python3.13 python3.12 python3.14 python3; do
  if command -v "$v" >/dev/null 2>&1 && "$v" -c 'import sys; sys.exit(0 if (3,12) <= sys.version_info[:2] <= (3,14) else 1)'; then
    PY="$v"; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.12–3.14 is required"; exit 1; }

[ -x .venv/bin/python ] || "$PY" -m venv .venv
if ! cmp -s requirements.txt .venv/requirements.installed; then
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi
exec .venv/bin/python -m fbot "$@"
