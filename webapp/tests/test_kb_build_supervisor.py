from webapp.kb_index_protocol import index_progress_percent
def test_chunk_progress_never_exceeds_indexing_boundary() -> None:
    assert index_progress_percent("extracting", 99, 10) == 83
    assert index_progress_percent("succeeded", 10, 10) == 84
