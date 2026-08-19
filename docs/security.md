# Security

## Secrets

- Never commit `.env`, model settings JSON, proxy configuration, API keys or SSH credentials.
- Use separate generation and embedding keys with the minimum required provider permissions.
- Restrict `.env`, SQLite and model settings files to the service account.
- Rotate any key that appears in logs, screenshots or Git history.

## Authentication

`TRUSTEDRAG_ADMIN_PASSWORD` is required only when the initial admin account is created. Choose a unique strong value and remove it from shell history or provisioning output. Do not use example passwords in production.

## User isolation

Generation model settings are associated with the authenticated user. API responses must never return another user's key. Knowledge-base visibility records distinguish public, private and explicitly shared collections.

## Document access

PDF preview and original-file download endpoints require authentication and visibility checks. Nginx should not expose the `data/` directory directly.

## Uploads and paths

The backend normalizes file names, limits size and allowed extensions, and validates resolved paths before publication or deletion. Keep the service account's filesystem permissions limited to the project data directories.

## Logs and errors

Provider errors should be redacted before persistence. Avoid logging request headers, cookies, API keys, document contents or user passwords. Build task records contain status and metadata, not credentials.

## Repository release checklist

Before publishing:

```bash
python scripts/audit-release.py
```

Review every finding, every file over 5 MB and all third-party licenses. Prefer a private repository until business-code and document rights are confirmed.
