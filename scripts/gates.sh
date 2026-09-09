#!/usr/bin/env bash
# The merge gates. CI runs exactly this script; run it locally before pushing.
# Requires: python3 with requirements-dev.txt installed (pytest, ruff, bandit).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "== ruff (lint: pyflakes, bugbear, bandit-style S rules)"
ruff check .

echo "== bandit"
bandit -q -c bandit.yaml -r .

echo "== pytest"
python3 -m pytest -q

echo "== all gates passed"
