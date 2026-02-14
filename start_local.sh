#!/usr/bin/env bash
set -euo pipefail

if [ -f .env ]; then
  # shellcheck disable=SC2046
  export $(grep -v '^\s*#' .env | grep -v '^\s*$' | xargs)
fi

python -c "from dev_core.storage import init_db; init_db()"

# Run dashboard in foreground
python dashboard_server.py
