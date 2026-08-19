#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  printf 'Run this installer as root.\n' >&2
  exit 1
fi

PROJECT_ROOT=${TRUSTEDRAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
SERVICE_USER=${TRUSTEDRAG_SERVICE_USER:-trustedrag}
FRONTEND_ROOT=${TRUSTEDRAG_FRONTEND_ROOT:-/var/www/trustedrag}

[[ -f "$PROJECT_ROOT/.env" ]] || {
  printf 'Create %s/.env from .env.example before installation.\n' "$PROJECT_ROOT" >&2
  exit 1
}

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$PROJECT_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

python3.12 -m venv "$PROJECT_ROOT/.venv"
"$PROJECT_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$PROJECT_ROOT/.venv/bin/python" -m pip install -r "$PROJECT_ROOT/requirements.txt"

cd "$PROJECT_ROOT/frontend"
npm ci
npm run build
install -d -o root -g root -m 0755 "$FRONTEND_ROOT"
cp -a dist/. "$FRONTEND_ROOT/"

cd "$PROJECT_ROOT"
bash scripts/init-data-layout.sh
chown -R "$SERVICE_USER":"$SERVICE_USER" "$PROJECT_ROOT/data"

service_tmp=$(mktemp)
nginx_tmp=$(mktemp)
trap 'rm -f "$service_tmp" "$nginx_tmp"' EXIT
sed \
  -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
  -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
  deploy/systemd/trustedrag-api.service > "$service_tmp"
sed -e "s|__FRONTEND_ROOT__|$FRONTEND_ROOT|g" deploy/nginx/trustedrag.conf > "$nginx_tmp"

install -m 0644 "$service_tmp" /etc/systemd/system/trustedrag-api.service
install -m 0644 "$nginx_tmp" /etc/nginx/sites-available/trustedrag
ln -sfn /etc/nginx/sites-available/trustedrag /etc/nginx/sites-enabled/trustedrag

nginx -t
systemctl daemon-reload
systemctl enable --now trustedrag-api.service
systemctl reload nginx
printf 'TrustedRAG installed. Check: systemctl status trustedrag-api.service\n'
