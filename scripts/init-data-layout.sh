#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${TRUSTEDRAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
DATA_ROOT="$PROJECT_ROOT/data"

mkdir -p \
  "$DATA_ROOT/corpus/.pending" \
  "$DATA_ROOT/output" \
  "$DATA_ROOT/rag_storage" \
  "$DATA_ROOT/.trustedrag-builds"

if [[ ! -f "$DATA_ROOT/name2id.json" ]]; then
  printf '{}\n' > "$DATA_ROOT/name2id.json"
fi

chmod 700 "$DATA_ROOT/corpus/.pending" "$DATA_ROOT/.trustedrag-builds"
chmod 600 "$DATA_ROOT/name2id.json"
printf 'Initialized TrustedRAG data layout at %s\n' "$DATA_ROOT"
