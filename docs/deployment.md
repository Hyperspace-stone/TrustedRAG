# Deployment

## Recommended layout

```text
/opt/trustedrag/             repository and Python virtual environment
/opt/trustedrag/data/        runtime documents, indexes and SQLite state
/var/www/trustedrag/         built frontend
/etc/systemd/system/         API service
/etc/nginx/sites-available/  reverse proxy configuration
```

## Installation

1. Clone or extract the repository into the chosen project root.
2. Copy `.env.example` to `.env` and replace all placeholders.
3. Install Python 3.12, Node.js 20+, npm and Nginx.
4. Run `bash scripts/check-environment.sh`.
5. Run the installer:

```bash
sudo TRUSTEDRAG_PROJECT_ROOT=/opt/trustedrag \
  TRUSTEDRAG_SERVICE_USER=trustedrag \
  bash deploy/install.sh
```

The installer does not create or overwrite `.env`, and `init-data-layout.sh` does not overwrite an existing `name2id.json`.

## Verification

```bash
systemctl status trustedrag-api.service
curl http://127.0.0.1:5051/api/health
nginx -t
```

## Upgrade

1. Back up `data/` and `.env` using a protected location outside the repository.
2. Stop the API only for the shortest necessary window.
3. Update source, run `pip install -r requirements.txt`, rebuild the frontend and run tests.
4. Restart the API and verify `/api/health`.

Never include backups inside the Git repository or the public web root.

## Rollback

Keep a release archive of source and a separately protected runtime-data backup. Restore source first, reinstall the matching dependencies, restore data only when schema/content changes require it, then restart and verify health.

## TLS

The included Nginx file is an HTTP template. Production deployments should add a real domain, TLS certificates, HSTS after validation, and an appropriate firewall policy.
