from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROGRESS_STAGES = {
    "starting",
    "probing",
    "initializing",
    "extracting",
    "finalizing",
    "succeeded",
    "failed",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object")
    return payload


def safe_error_message(message: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    cleaned = str(message or "").strip()
    for secret in secrets:
        value = str(secret or "")
        if value:
            cleaned = cleaned.replace(value, "***")
    return cleaned[:500]


def classify_index_error(message: str) -> str:
    value = str(message or "").casefold()
    if "维度不匹配" in value or "dimension" in value and "mismatch" in value:
        return "EMBEDDING_DIMENSION_MISMATCH"
    if "401" in value or "unauthorized" in value or "invalid api key" in value:
        return "MODEL_AUTH_FAILED"
    if "429" in value or "rate limit" in value:
        return "MODEL_RATE_LIMITED"
    if "timeout" in value or "timed out" in value:
        return "MODEL_TIMEOUT"
    return "INDEX_BUILD_FAILED"


def index_progress_percent(stage: str, processed_chunks: int = 0, total_chunks: int = 0) -> int:
    if stage == "probing":
        return 38
    if stage == "initializing":
        return 42
    if stage == "extracting":
        if total_chunks > 0:
            return min(83, 45 + int(38 * max(0, processed_chunks) / total_chunks))
        return 45
    if stage in {"finalizing", "succeeded"}:
        return 84
    return 36


def write_progress(
    path: Path,
    *,
    stage: str,
    message: str,
    processed_chunks: int = 0,
    total_chunks: int = 0,
    error_code: str = "",
) -> None:
    normalized_stage = stage if stage in PROGRESS_STAGES else "starting"
    atomic_write_json(
        path,
        {
            "stage": normalized_stage,
            "message": str(message or ""),
            "processed_chunks": max(0, int(processed_chunks or 0)),
            "total_chunks": max(0, int(total_chunks or 0)),
            "error_code": str(error_code or ""),
            "heartbeat_at": utc_now(),
        },
    )
