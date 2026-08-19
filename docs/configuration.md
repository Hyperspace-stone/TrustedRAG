# Configuration

Copy `.env.example` to `.env`. The application reads environment variables at process startup; restart the API after changing server-side values.

## Authentication

- `TRUSTEDRAG_ADMIN_PASSWORD`: required before the first startup. It is used only to create the initial `admin` account when no admin exists.
- `TRUSTEDRAG_AUTH_DB_PATH`: SQLite path for users, sessions and knowledge-base visibility.

## Data paths

- `TRUSTEDRAG_NAME_MAP_PATH`: title-to-ID JSON mapping.
- `TRUSTEDRAG_STORAGE_ROOT`: LightRAG index root.
- `TRUSTEDRAG_CORPUS_ROOT`: original documents.
- `TRUSTEDRAG_CORPUS_PENDING_ROOT`: durable upload queue area.
- `TRUSTEDRAG_OUTPUT_ROOT`: generated previews and structured parser output.
- `TRUSTEDRAG_BUILD_ROOT`: task history and isolated build staging.
- `TRUSTEDRAG_MODEL_SETTINGS_PATH`: encrypted-at-rest is not provided; protect this file with filesystem permissions.
- `TRUSTEDRAG_PROXY_CONFIG_PATH`: optional compatible proxy configuration fallback.

## Generation model

- `RAG_LLM_BASE_URL`
- `RAG_LLM_API_KEY`
- `RAG_LLM_MODEL`

The endpoint must implement an OpenAI-compatible chat completions API. The frontend may save a different generation model for each user.

## Embedding model

- `RAG_EMBEDDING_BASE_URL`
- `RAG_EMBEDDING_API_KEY`
- `RAG_EMBEDDING_MODEL`
- `RAG_EMBEDDING_DIM`

Embedding is used for both retrieval and new index construction. Existing indexes must use the same dimension as the configured model.

## Retrieval

- `RAG_QUERY_MODE`: normally `hybrid`.
- `RAG_TOP_K`, `RAG_CHUNK_TOP_K`: candidate limits.
- `RAG_MAX_SOURCES`: maximum returned source records.
- `RAG_MAX_SOURCE_CHARS`, `RAG_MAX_CONTEXT_CHARS`: context size limits.
- `RAG_ROUTE_SHORTLIST_SIZE`, `RAG_ROUTE_MIN_SCORE`, `RAG_ROUTE_MIN_MARGIN`: title routing thresholds.
- `RAG_EVIDENCE_*`: reverse evidence search limits and thresholds.
- `RAG_CORPUS_PREVIEW_ROWS`, `RAG_CORPUS_MAX_CELL_CHARS`: original document preview limits.

## Build queue

- `TRUSTEDRAG_KB_BUILD_ENABLED`
- `TRUSTEDRAG_BUILD_MAX_UPLOAD_BYTES`
- `TRUSTEDRAG_BUILD_HISTORY_LIMIT`
- `TRUSTEDRAG_BUILD_BATCH_LIMIT`
- `TRUSTEDRAG_BUILD_TIMEOUT_SECONDS`
- `TRUSTEDRAG_BUILD_HEARTBEAT_TIMEOUT_SECONDS`
- `TRUSTEDRAG_BUILD_PROGRESS_POLL_SECONDS`
- `TRUSTEDRAG_BUILD_NICE_LEVEL`
- `TRUSTEDRAG_BUILD_EMBEDDING_MAX_ASYNC`
- `TRUSTEDRAG_BUILD_EMBEDDING_BATCH_NUM`
- `TRUSTEDRAG_BUILD_EMBEDDING_TIMEOUT_SECONDS`

Low-resource servers should keep Embedding concurrency small. The validated production value is `2`.

## Standalone examples

The examples also understand compatibility variables such as `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `LLM_BINDING_API_KEY`, `LLM_BINDING_HOST`, `LLM_MODEL`, `VISION_MODEL`, `EMBEDDING_MODEL` and `EMBEDDING_DIM`.
