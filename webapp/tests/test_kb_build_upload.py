import asyncio
import json
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile

from webapp.kb_build_service import KbBuildError, KbBuildService
from webapp.settings import Settings


def build_settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    for name in ("corpus", "output", "rag_storage"):
        (data / name).mkdir(parents=True)
    (data / "name2id.json").write_text(
        json.dumps({"507_现有文件.xlsx": 507}, ensure_ascii=False), encoding="utf-8"
    )
    return Settings(
        project_root=tmp_path,
        auth_db_path=data / "auth.sqlite3",
        name_map_path=data / "name2id.json",
        storage_root=data / "rag_storage",
        corpus_root=data / "corpus",
        corpus_pending_root=data / "corpus" / ".pending",
        output_root=data / "output",
        build_root=data / ".trustedrag-builds",
        build_enabled=True,
        build_max_upload_bytes=1024,
    )


def test_upload_allocates_next_production_id(tmp_path: Path) -> None:
    service = KbBuildService(build_settings(tmp_path))
    upload = UploadFile(filename="新文件.txt", file=BytesIO("测试正文".encode("utf-8")))

    task = asyncio.run(service.create_task(upload, owner="admin"))

    assert task["kb_id"] == "508"
    assert task["status"] == "queued"
    assert task["display_name"] == "508_新文件.txt"


def test_upload_rejects_more_than_limit(tmp_path: Path) -> None:
    config = build_settings(tmp_path)
    service = KbBuildService(config)
    upload = UploadFile(filename="too-large.txt", file=BytesIO(b"x" * 2048))

    with pytest.raises(KbBuildError) as caught:
        asyncio.run(service.create_task(upload, owner="admin"))

    assert caught.value.code == "UPLOAD_TOO_LARGE"


def test_batch_upload_preserves_order_and_queues_all(tmp_path: Path) -> None:
    service = KbBuildService(build_settings(tmp_path))
    uploads = [
        UploadFile(filename="一.txt", file=BytesIO("一".encode("utf-8"))),
        UploadFile(filename="二.txt", file=BytesIO("二".encode("utf-8"))),
    ]

    tasks, errors = asyncio.run(service.create_tasks(uploads, owner="admin"))

    assert errors == []
    assert [task["kb_id"] for task in tasks] == ["508", "509"]
    assert [task["original_name"] for task in tasks] == ["一.txt", "二.txt"]
    assert all(task["status"] == "queued" and task["progress"] == 0 for task in tasks)
    assert (service.config.corpus_pending_root / tasks[0]["task_id"] / "一.txt").is_file()


def test_batch_upload_keeps_valid_files_when_one_is_invalid(tmp_path: Path) -> None:
    service = KbBuildService(build_settings(tmp_path))
    uploads = [
        UploadFile(filename="bad.exe", file=BytesIO(b"bad")),
        UploadFile(filename="good.txt", file=BytesIO(b"good")),
    ]

    tasks, errors = asyncio.run(service.create_tasks(uploads, owner="admin"))

    assert [task["original_name"] for task in tasks] == ["good.txt"]
    assert errors[0]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_delete_collection_removes_all_artifacts_and_marks_task_deleted(tmp_path: Path) -> None:
    config = build_settings(tmp_path)
    service = KbBuildService(config)
    storage = config.storage_root / "507"
    output = config.output_root / "507"
    storage.mkdir()
    output.mkdir()
    (storage / "vdb_chunks.json").write_text("{}", encoding="utf-8")
    (output / "507.pdf").write_bytes(b"%PDF-test")
    (config.corpus_root / "507.xlsx").write_bytes(b"xlsx")
    service._tasks["completed-task"] = {
        "task_id": "completed-task",
        "kb_id": "507",
        "status": "succeeded",
        "stage": "succeeded",
        "progress": 100,
    }
    service._save()

    result = service.delete_collection("507")

    assert result["status"] == "deleted"
    assert result["collection_name"] == "507"
    assert not storage.exists()
    assert not output.exists()
    assert not (config.corpus_root / "507.xlsx").exists()
    assert json.loads(config.name_map_path.read_text(encoding="utf-8")) == {}
    assert service._tasks["completed-task"]["status"] == "deleted"


def test_delete_collection_rejects_active_build(tmp_path: Path) -> None:
    config = build_settings(tmp_path)
    service = KbBuildService(config)
    (config.storage_root / "507").mkdir()
    service._tasks["active-task"] = {
        "task_id": "active-task",
        "kb_id": "507",
        "status": "indexing",
    }

    with pytest.raises(KbBuildError) as caught:
        service.delete_collection("507")

    assert caught.value.code == "KB_BUILD_ACTIVE"
    assert (config.storage_root / "507").is_dir()
    assert "507_现有文件.xlsx" in json.loads(config.name_map_path.read_text(encoding="utf-8"))
