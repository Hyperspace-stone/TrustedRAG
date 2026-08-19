#!/usr/bin/env bash
set -euo pipefail

failures=0
check_command() {
  local command_name=$1
  local required=$2
  if command -v "$command_name" >/dev/null 2>&1; then
    printf '[ok] %s: %s\n' "$command_name" "$(command -v "$command_name")"
  elif [[ "$required" == required ]]; then
    printf '[missing] %s\n' "$command_name" >&2
    failures=$((failures + 1))
  else
    printf '[optional missing] %s\n' "$command_name"
  fi
}

check_command python3 required
check_command node required
check_command npm required
check_command nginx optional
check_command libreoffice optional

python3 - <<'PY'
import sys
if not ((3, 11) <= sys.version_info[:2] < (3, 13)):
    raise SystemExit(f"Python 3.11 or 3.12 is required; found {sys.version.split()[0]}")
print(f"[ok] Python version: {sys.version.split()[0]}")
PY

node -e 'const major=Number(process.versions.node.split(".")[0]); if (major < 20) process.exit(1); console.log(`[ok] Node version: ${process.versions.node}`)'

if [[ "$failures" -ne 0 ]]; then
  exit 1
fi
