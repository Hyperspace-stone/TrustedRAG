#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${TRUSTEDRAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

bash scripts/init-data-layout.sh
python scripts/validate-config.py

HOST=${TRUSTEDRAG_API_HOST:-127.0.0.1}
PORT=${TRUSTEDRAG_API_PORT:-5051}
exec .venv/bin/uvicorn webapp.app:app --host "$HOST" --port "$PORT" --reload
