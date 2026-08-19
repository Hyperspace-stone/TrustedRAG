# Knowledge-base build queue

## Upload behavior

- A request may contain multiple files, up to `TRUSTEDRAG_BUILD_BATCH_LIMIT`.
- Every file is written to `data/corpus/.pending/<task_id>/` before the API returns.
- Each task starts as `queued` at 0%.
- Only one task is active at a time; other tasks remain queued.
- Successful and deleted tasks are hidden from the frontend queue, while failed and interrupted tasks remain available for retry.

## Stages

| Progress | Stage | Meaning |
|---:|---|---|
| 0% | queued | upload saved, waiting for worker |
| 10% | parsing | document content extraction |
| 25% | rendering | PDF preview generation |
| 35–84% | indexing | isolated LightRAG subprocess |
| 85% | validating | required index files and source mapping |
| 92% | publishing | atomic publication to production data roots |
| 100% | succeeded | knowledge base is visible |

## Publication

The worker validates the staged index before publishing. It then moves/copies the original file, preview tree and LightRAG storage, updates `name2id.json` atomically, invalidates catalogs and creates visibility metadata. Failed publication restores the previous mapping and removes partially moved targets.

## Retry and recovery

- A failed or interrupted task can be requeued if its pending source still exists.
- A service restart marks an in-progress task as interrupted rather than silently treating it as successful.
- Task JSON never stores API keys.
- A 45-minute default timeout and heartbeat supervision terminate lost index processes.

## Common failures

- `OCR_REQUIRED`: the PDF is image-only; configure OCR/MinerU.
- `EMBEDDING_NOT_CONFIGURED`: missing embedding endpoint or key.
- `EMBEDDING_DIMENSION_MISMATCH`: actual embedding dimension differs from configuration.
- `MODEL_AUTH_FAILED`: invalid API credentials.
- `MODEL_TIMEOUT`: provider or network timeout.
- `INDEX_INCOMPLETE`: index validation found missing files. Inspect `kv_store_doc_status.json`; a nested HTTP 402 `Insufficient Balance` means the provider account has insufficient balance.
- `INDEX_TIMEOUT` / `INDEX_WORKER_LOST`: the subprocess exceeded its time or stopped heartbeating.

Do not publish staging output manually. Fix the model/parser configuration and retry the task through the API or frontend.
