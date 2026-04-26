#!/usr/bin/env bash
# Wrapper: sources .env then runs python with the project venv.
# Usage: ./run.sh benchmark.py [args]
#        ./run.sh gating.py
#        ./run.sh record.py --val-score 0.42 ...
set -euo pipefail
cd "$(dirname "$0")"
set -a
. ./.env
set +a
exec ./.venv/bin/python "$@"
