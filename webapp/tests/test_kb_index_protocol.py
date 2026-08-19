import json
import os

from webapp.kb_index_protocol import (
    atomic_write_json,
    classify_index_error,
    index_progress_percent,
    safe_error_message,
    write_progress,
)


def test_progress_file_is_valid_and_private(tmp_path) -> None:
    target = tmp_path / "progress.json"

    write_progress(
        target,
        stage="extracting",
        message="正在处理 2/5",
        processed_chunks=2,
        total_chunks=5,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["stage"] == "extracting"
    assert payload["processed_chunks"] == 2
    assert payload["total_chunks"] == 5
    if os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0


def test_atomic_write_replaces_existing_json(tmp_path) -> None:
    target = tmp_path / "request.json"
    atomic_write_json(target, {"value": 1})
    atomic_write_json(target, {"value": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 2}


def test_safe_error_message_masks_secrets() -> None:
    assert safe_error_message("request failed for test-value", ["test-value"]) == "request failed for ***"


def test_progress_maps_real_chunks_into_index_range() -> None:
    assert index_progress_percent("extracting", 0, 10) == 45
    assert index_progress_percent("extracting", 5, 10) == 64
    assert index_progress_percent("extracting", 10, 10) == 83
    assert index_progress_percent("finalizing") == 84


def test_error_classification_is_diagnostic() -> None:
    assert classify_index_error("HTTP 401 Unauthorized") == "MODEL_AUTH_FAILED"
    assert classify_index_error("429 rate limit") == "MODEL_RATE_LIMITED"
    assert classify_index_error("Embedding 维度不匹配") == "EMBEDDING_DIMENSION_MISMATCH"
