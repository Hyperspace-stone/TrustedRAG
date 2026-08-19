# Runtime data directory

This directory is intentionally empty in source control.

- `corpus/`: original uploaded documents.
- `output/`: generated PDF previews and structured parser output.
- `rag_storage/`: LightRAG graph and vector indexes.
- `.trustedrag-builds/`: persisted build queue and staging data.
- `name2id.json`: document title to numeric knowledge-base identifier mapping.
- `trustedrag_auth.sqlite3`: users, sessions and knowledge-base visibility records.
- `webapp_model_settings.json`: per-user generation model settings.

Run `bash scripts/init-data-layout.sh` before the first startup. Never commit
real documents, indexes, databases or API settings.
