import logging

from webapp.kb_index_protocol import read_json
from webapp.kb_index_worker import ChunkProgressHandler, ProgressReporter, document_text


def test_document_text_keeps_tables_and_text() -> None:
    value = document_text(
        [
            {"type": "text", "text": "正文"},
            {"type": "table", "table_caption": ["标题"], "table_body": "A | B"},
        ]
    )
    assert "正文" in value
    assert "标题" in value
    assert "A | B" in value


def test_log_handler_extracts_chunk_progress(tmp_path) -> None:
    target = tmp_path / "progress.json"
    reporter = ProgressReporter(target)
    reporter.publish()
    handler = ChunkProgressHandler(reporter)

    handler.emit(logging.LogRecord("test", logging.INFO, "", 0, "Processing chunk 3 of 8", (), None))

    payload = read_json(target)
    assert payload["stage"] == "extracting"
    assert payload["processed_chunks"] == 3
    assert payload["total_chunks"] == 8
