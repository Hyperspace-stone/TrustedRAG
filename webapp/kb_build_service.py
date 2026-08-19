from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from .document_parser import DocumentParseError, parse_document, write_structured_output
from .kb_index_protocol import atomic_write_json, index_progress_percent, read_json
from .model_settings import model_settings_store
from .preview_renderer import PreviewRenderError, render_preview
from .settings import Settings, settings


logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {"succeeded", "failed", "interrupted", "deleted"}


class KbBuildError(RuntimeError):
    def __init__(self, message: str, *, code: str = "BUILD_FAILED") -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_file_name(raw: str) -> str:
    name = Path(str(raw or "")).name.strip().replace("\x00", "")
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise KbBuildError("文件名无效。", code="INVALID_FILE_NAME")
    if len(name) > 220:
        stem = Path(name).stem[:180]
        name = stem + Path(name).suffix[:20]
    return name


class KbBuildService:
    def __init__(self, config: Settings = settings) -> None:
        self.config = config
        self.root = config.build_root
        self.tasks_path = self.root / "tasks.json"
        self.staging_root = self.root / "staging"
        self._lock = threading.RLock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._queued_ids: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        if self.tasks_path.is_file():
            try:
                payload = json.loads(self.tasks_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self._tasks = {
                        str(key): value
                        for key, value in payload.items()
                        if isinstance(value, dict)
                    }
            except (OSError, json.JSONDecodeError):
                logger.exception("Unable to read knowledge-base build task store")
        changed = False
        for task in self._tasks.values():
            if task.get("status") not in TERMINAL_STATUSES and task.get("status") != "queued":
                task.update(
                    status="interrupted",
                    stage="interrupted",
                    message="服务重启导致任务中断，可以点击重试。",
                    error_code="BUILD_INTERRUPTED",
                    finished_at=_utc_now(),
                )
                changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=".tasks.", suffix=".json", dir=str(self.root), text=True
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self._tasks, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.tasks_path)
            os.chmod(self.tasks_path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker, name="trustedrag-kb-builder", daemon=True
            )
            self._thread.start()
            queued = sorted(
                self._tasks.items(),
                key=lambda item: str(item[1].get("created_at") or ""),
            )
            for task_id, task in queued:
                if task.get("status") == "queued":
                    self._enqueue(task_id)

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def _enqueue(self, task_id: str) -> None:
        with self._lock:
            if task_id in self._queued_ids:
                return
            self._queued_ids.add(task_id)
            self._queue.put(task_id)

    def readiness(self) -> dict[str, Any]:
        return {
            "enabled": self.config.build_enabled,
            "worker_running": bool(self._thread and self._thread.is_alive()),
            "embedding_configured": self.config.embedding_configured,
            "embedding_model": self.config.embedding_model,
            "embedding_dim": self.config.embedding_dim,
            "max_upload_bytes": self.config.build_max_upload_bytes,
            "max_batch_files": self.config.build_batch_limit,
            "single_active_build": True,
            "allowed_extensions": list(self.config.build_allowed_extensions),
        }

    def _public(self, task: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "task_id",
            "owner_user_id",
            "original_name",
            "display_name",
            "kb_id",
            "visibility",
            "size",
            "sha256",
            "status",
            "stage",
            "progress",
            "message",
            "error_code",
            "created_at",
            "started_at",
            "finished_at",
        }
        return {key: task.get(key) for key in allowed if key in task}

    def list_tasks(self, owner: str = "", *, is_admin: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            values = [
                self._public(task)
                for task in self._tasks.values()
                if is_admin or task.get("owner_user_id") == owner
            ]
        values.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return values[: self.config.build_history_limit]

    def get_task(self, task_id: str, owner: str = "", *, is_admin: bool = False) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                raise KbBuildError("建库任务不存在。", code="TASK_NOT_FOUND")
            if not is_admin and task.get("owner_user_id") != owner:
                raise KbBuildError("无权查看该建库任务。", code="TASK_FORBIDDEN")
            return self._public(task)

    def _used_ids(self) -> set[int]:
        used: set[int] = set()
        try:
            payload = json.loads(self.config.name_map_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                used.update(int(value) for value in payload.values() if str(value).isdigit())
        except (OSError, json.JSONDecodeError):
            pass
        for root in (self.config.storage_root, self.config.output_root):
            if root.is_dir():
                used.update(int(path.name) for path in root.iterdir() if path.name.isdigit())
        if self.config.corpus_root.is_dir():
            used.update(
                int(path.stem)
                for path in self.config.corpus_root.iterdir()
                if path.is_file() and path.stem.isdigit()
            )
        used.update(
            int(task["kb_id"])
            for task in self._tasks.values()
            if str(task.get("kb_id") or "").isdigit()
            and task.get("status") not in {"failed"}
        )
        return used

    def _allocate_id(self) -> str:
        used = self._used_ids()
        next_id = (max(used) + 1) if used else 1
        if next_id > 999:
            raise KbBuildError("三位知识库编号已经用尽。", code="KB_ID_EXHAUSTED")
        return str(next_id).zfill(3)

    def _find_duplicate(self, name: str, size: int, digest: str) -> str | None:
        normalized_name = name.casefold()
        try:
            mapping = json.loads(self.config.name_map_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            mapping = {}
        if isinstance(mapping, dict):
            for title, raw_id in mapping.items():
                clean_title = re.sub(r"^\d{3}_", "", str(title)).casefold()
                if clean_title == normalized_name:
                    return str(raw_id).zfill(3)
        for task in self._tasks.values():
            if task.get("sha256") == digest and task.get("status") != "failed":
                return str(task.get("kb_id") or "").zfill(3)
        if self.config.corpus_root.is_dir():
            for candidate in self.config.corpus_root.iterdir():
                if not candidate.is_file() or candidate.stat().st_size != size:
                    continue
                hasher = hashlib.sha256()
                with candidate.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                if hasher.hexdigest() == digest:
                    return candidate.stem.zfill(3) if candidate.stem.isdigit() else ""
        return None

    async def create_task(
        self,
        upload: UploadFile,
        *,
        owner: str,
        visibility: str = "private",
    ) -> dict[str, Any]:
        tasks, errors = await self.create_tasks(
            [upload],
            owner=owner,
            visibility=visibility,
        )
        if tasks:
            return tasks[0]
        error = errors[0] if errors else {"message": "文件上传失败。", "code": "UPLOAD_FAILED"}
        raise KbBuildError(str(error["message"]), code=str(error["code"]))

    async def create_tasks(
        self,
        uploads: list[UploadFile],
        *,
        owner: str,
        visibility: str = "private",
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        if not self.config.build_enabled:
            raise KbBuildError("单文件建库功能当前未启用。", code="BUILD_DISABLED")
        if visibility not in {"private", "public", "shared"}:
            raise KbBuildError("知识库可见性设置无效。", code="INVALID_VISIBILITY")
        if not uploads:
            raise KbBuildError("请选择要上传的文件。", code="EMPTY_UPLOAD_BATCH")
        if len(uploads) > self.config.build_batch_limit:
            raise KbBuildError(
                f"一次最多上传 {self.config.build_batch_limit} 个文件。",
                code="UPLOAD_BATCH_TOO_LARGE",
            )

        self.config.corpus_pending_root.mkdir(parents=True, exist_ok=True)
        staged: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for upload in uploads:
            raw_name = upload.filename or ""
            task_id = uuid.uuid4().hex
            pending_dir = self.config.corpus_pending_root / task_id
            try:
                name = _safe_file_name(raw_name)
                extension = Path(name).suffix.lower()
                if extension not in self.config.build_allowed_extensions:
                    raise KbBuildError("不支持该文件格式。", code="UNSUPPORTED_FILE_TYPE")
                pending_dir.mkdir(parents=True, exist_ok=False)
                source_path = pending_dir / name
                size = 0
                hasher = hashlib.sha256()
                with source_path.open("wb") as stream:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.config.build_max_upload_bytes:
                            raise KbBuildError("文件超过上传大小限制。", code="UPLOAD_TOO_LARGE")
                        hasher.update(chunk)
                        stream.write(chunk)
                os.chmod(source_path, 0o600)
                if size == 0:
                    raise KbBuildError("不能上传空文件。", code="EMPTY_DOCUMENT")
                staged.append(
                    {
                        "task_id": task_id,
                        "name": name,
                        "source_path": source_path,
                        "size": size,
                        "digest": hasher.hexdigest(),
                    }
                )
            except KbBuildError as exc:
                shutil.rmtree(pending_dir, ignore_errors=True)
                errors.append({"file_name": raw_name, "code": exc.code, "message": str(exc)})
            except Exception as exc:
                shutil.rmtree(pending_dir, ignore_errors=True)
                errors.append(
                    {
                        "file_name": raw_name,
                        "code": "UPLOAD_FAILED",
                        "message": str(exc) or "文件上传失败。",
                    }
                )
            finally:
                await upload.close()

        created: list[dict[str, Any]] = []
        with self._lock:
            added_ids: list[str] = []
            try:
                for item in staged:
                    duplicate = self._find_duplicate(item["name"], item["size"], item["digest"])
                    if duplicate:
                        shutil.rmtree(Path(item["source_path"]).parent, ignore_errors=True)
                        errors.append(
                            {
                                "file_name": item["name"],
                                "code": "DUPLICATE_DOCUMENT",
                                "message": f"该文件已经存在于知识库 {duplicate}。",
                            }
                        )
                        continue
                    kb_id = self._allocate_id()
                    task = {
                        "task_id": item["task_id"],
                        "owner_user_id": owner,
                        "original_name": item["name"],
                        "display_name": f"{kb_id}_{item['name']}",
                        "kb_id": kb_id,
                        "visibility": visibility,
                        "source_path": str(item["source_path"]),
                        "size": item["size"],
                        "sha256": item["digest"],
                        "status": "queued",
                        "stage": "queued",
                        "progress": 0,
                        "message": "文件已保存，等待后台建库。",
                        "error_code": "",
                        "created_at": _utc_now(),
                        "started_at": None,
                        "finished_at": None,
                    }
                    self._tasks[item["task_id"]] = task
                    added_ids.append(item["task_id"])
                    created.append(self._public(task))
                if added_ids:
                    self._save()
            except Exception:
                for task_id in added_ids:
                    task = self._tasks.pop(task_id, None)
                    if task:
                        shutil.rmtree(Path(str(task["source_path"])).parent, ignore_errors=True)
                raise
        for task in created:
            self._enqueue(str(task["task_id"]))
        return created, errors

    def retry(self, task_id: str, owner: str, *, is_admin: bool = False) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KbBuildError("建库任务不存在。", code="TASK_NOT_FOUND")
            if not is_admin and task.get("owner_user_id") != owner:
                raise KbBuildError("无权重试该任务。", code="TASK_FORBIDDEN")
            if task.get("status") not in {"failed", "interrupted"}:
                raise KbBuildError("该任务当前不能重试。", code="TASK_NOT_RETRYABLE")
            if not Path(str(task.get("source_path") or "")).is_file():
                raise KbBuildError("原上传文件已经不存在。", code="STAGED_SOURCE_MISSING")
            task.update(
                status="queued",
                stage="queued",
                progress=0,
                message="任务已重新排队。",
                error_code="",
                started_at=None,
                finished_at=None,
            )
            self._save()
            self._enqueue(task_id)
            return self._public(task)

    def delete_collection(self, raw_kb_id: str) -> dict[str, Any]:
        kb_id = str(raw_kb_id or "").strip()
        if not kb_id.isdigit() or len(kb_id) > 3:
            raise KbBuildError("知识库编号无效。", code="KB_ID_INVALID")
        kb_id = kb_id.zfill(3)

        with self._lock:
            active = [
                task
                for task in self._tasks.values()
                if str(task.get("kb_id") or "").zfill(3) == kb_id
                and task.get("status") not in TERMINAL_STATUSES
            ]
            if active:
                raise KbBuildError("该知识库仍在构建，暂时不能删除。", code="KB_BUILD_ACTIVE")

            try:
                old_map_bytes = self.config.name_map_path.read_bytes()
                mapping = json.loads(old_map_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise KbBuildError("name2id.json 无法读取。", code="NAME_MAP_INVALID") from exc
            if not isinstance(mapping, dict):
                raise KbBuildError("name2id.json 格式错误。", code="NAME_MAP_INVALID")

            matching_titles = [
                str(title)
                for title, value in mapping.items()
                if str(value).strip().isdigit() and str(value).strip().zfill(3) == kb_id
            ]
            storage_target = self._safe_collection_dir(self.config.storage_root, kb_id)
            if not matching_titles or not storage_target.is_dir():
                raise KbBuildError("知识库不存在。", code="KB_NOT_FOUND")

            output_target = self._safe_collection_dir(self.config.output_root, kb_id)
            corpus_targets = self._safe_corpus_targets(kb_id)
            targets = [path for path in [storage_target, output_target, *corpus_targets] if path.exists()]
            trash_root = self.root / "delete-staging" / f"{kb_id}-{uuid.uuid4().hex}"
            trash_root.mkdir(parents=True, exist_ok=False)
            moved: list[tuple[Path, Path]] = []
            old_task_states = {
                task_id: dict(task)
                for task_id, task in self._tasks.items()
                if str(task.get("kb_id") or "").zfill(3) == kb_id
            }
            try:
                for index, target in enumerate(targets):
                    quarantined = trash_root / f"{index}-{target.name}"
                    os.replace(target, quarantined)
                    moved.append((target, quarantined))

                updated_mapping = {
                    title: value for title, value in mapping.items() if str(title) not in matching_titles
                }
                atomic_write_json(self.config.name_map_path, updated_mapping)

                from .knowledge_bases import catalog
                from .kb_visibility_store import KnowledgeBaseVisibilityStore

                catalog.invalidate()
                for task in self._tasks.values():
                    if (
                        str(task.get("kb_id") or "").zfill(3) == kb_id
                        and task.get("status") == "succeeded"
                    ):
                        task.update(
                            status="deleted",
                            stage="deleted",
                            progress=100,
                            message=f"知识库 {kb_id} 已删除。",
                            error_code="",
                            finished_at=_utc_now(),
                        )
                self._save()
                KnowledgeBaseVisibilityStore(self.config.auth_db_path).delete(kb_id)
            except Exception:
                for task_id, old_task in old_task_states.items():
                    self._tasks[task_id] = old_task
                try:
                    self._save()
                except Exception:
                    logger.exception("Unable to restore build task history after delete failure")
                try:
                    self.config.name_map_path.write_bytes(old_map_bytes)
                    from .knowledge_bases import catalog

                    catalog.invalidate()
                except Exception:
                    logger.exception("Unable to restore name2id.json after delete failure")
                for target, quarantined in reversed(moved):
                    if quarantined.exists() and not target.exists():
                        os.replace(quarantined, target)
                shutil.rmtree(trash_root, ignore_errors=True)
                raise

            try:
                shutil.rmtree(trash_root)
            except OSError:
                logger.warning("Unable to purge deleted knowledge-base staging: %s", trash_root)
            return {
                "status": "deleted",
                "collection_name": kb_id,
                "display_names": matching_titles,
                "deleted_artifacts": len(targets),
            }

    def _safe_collection_dir(self, root: Path, kb_id: str) -> Path:
        resolved_root = root.resolve()
        target = (resolved_root / kb_id).resolve()
        if target.parent != resolved_root or target.name != kb_id:
            raise KbBuildError("知识库路径无效。", code="KB_PATH_INVALID")
        return target

    def _safe_corpus_targets(self, kb_id: str) -> list[Path]:
        root = self.config.corpus_root.resolve()
        if not root.is_dir():
            return []
        targets: list[Path] = []
        for candidate in root.iterdir():
            if not candidate.is_file() or candidate.is_symlink() or candidate.stem != kb_id:
                continue
            resolved = candidate.resolve()
            if resolved.parent == root:
                targets.append(resolved)
        return sorted(targets, key=lambda path: path.name)

    def _update(self, task_id: str, **values: Any) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.update(values)
            self._save()

    def _worker(self) -> None:
        while not self._stop.is_set():
            task_id = self._queue.get()
            if task_id is None:
                break
            with self._lock:
                self._queued_ids.discard(task_id)
            try:
                self._process(task_id)
            except Exception as exc:
                logger.exception("Knowledge-base build failed for task %s", task_id)
                code = getattr(exc, "code", "BUILD_FAILED")
                message = str(exc) or "建库失败。"
                self._update(
                    task_id,
                    status="failed",
                    stage="failed",
                    message=message,
                    error_code=code,
                    finished_at=_utc_now(),
                )

    def _process(self, task_id: str) -> None:
        with self._lock:
            task = dict(self._tasks[task_id])
        source = Path(task["source_path"])
        kb_id = str(task["kb_id"])
        task_root = self.staging_root / task_id
        self._update(
            task_id,
            status="parsing",
            stage="parsing",
            progress=10,
            message="正在解析文档内容。",
            started_at=_utc_now(),
        )
        try:
            document = parse_document(source)
        except DocumentParseError as exc:
            raise KbBuildError(str(exc), code=exc.code) from exc

        output_dir = task_root / "output" / kb_id
        output_dir.mkdir(parents=True, exist_ok=True)
        write_structured_output(document, output_dir / "structured_content.json")
        (output_dir / f"{kb_id}_content_list.json").write_text(
            json.dumps(document.content_list(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._update(
            task_id,
            status="rendering",
            stage="rendering",
            progress=25,
            message="正在生成 PDF 预览。",
        )
        try:
            render_preview(source, document, output_dir / f"{kb_id}.pdf")
        except PreviewRenderError as exc:
            raise KbBuildError(str(exc), code="PREVIEW_RENDER_FAILED") from exc

        self._update(
            task_id,
            status="indexing",
            stage="indexing",
            progress=35,
            message="正在构建图谱和向量索引。",
        )
        self._build_index(task, document.content_list(), task_root / "storage")
        self._update(
            task_id,
            status="validating",
            stage="validating",
            progress=85,
            message="正在验证索引完整性。",
        )
        self._validate_index(task_root / "storage" / kb_id, kb_id, source.suffix.lower())
        self._update(
            task_id,
            status="publishing",
            stage="publishing",
            progress=92,
            message="正在发布知识库。",
        )
        self._publish(task, task_root)
        self._update(
            task_id,
            status="succeeded",
            stage="succeeded",
            progress=100,
            message=f"知识库 {kb_id} 已创建完成。",
            error_code="",
            finished_at=_utc_now(),
        )

    def _build_index(self, task: dict[str, Any], content_list: list[dict[str, Any]], storage_root: Path) -> None:
        if not self.config.embedding_configured:
            raise KbBuildError(
                "建库 Embedding API 未配置。请在服务器设置 RAG_EMBEDDING_BASE_URL、RAG_EMBEDDING_API_KEY 和 RAG_EMBEDDING_MODEL。",
                code="EMBEDDING_NOT_CONFIGURED",
            )
        generation = model_settings_store.effective_generation(self.config)
        if not generation.base_url or not generation.api_key or not generation.model_name:
            raise KbBuildError("生成模型未正确配置。", code="GENERATION_NOT_CONFIGURED")
        del content_list
        task_root = storage_root.parent
        kb_id = str(task["kb_id"])
        content_path = task_root / "output" / kb_id / f"{kb_id}_content_list.json"
        request_path = task_root / "index-request.json"
        progress_path = task_root / "index-progress.json"
        storage_dir = storage_root / kb_id
        request_path.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)
        atomic_write_json(
            request_path,
            {
                "task_id": task["task_id"],
                "kb_id": kb_id,
                "content_list_path": str(content_path),
                "storage_dir": str(storage_dir),
                "source_file_name": f"{kb_id}{Path(task['original_name']).suffix.lower()}",
            },
        )
        command = [
            sys.executable,
            "-m",
            "webapp.kb_index_worker",
            "--request",
            str(request_path),
            "--progress",
            str(progress_path),
        ]
        popen_options: dict[str, Any] = {
            "cwd": str(self.config.project_root),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_options["start_new_session"] = True
        process = subprocess.Popen(command, **popen_options)
        started = time.monotonic()
        last_signature: tuple[Any, ...] | None = None

        def terminate_process() -> None:
            if process.poll() is not None:
                return
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except Exception:
                try:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    pass

        try:
            while process.poll() is None:
                if self._stop.is_set():
                    terminate_process()
                    raise KbBuildError("服务停止导致构图中断。", code="BUILD_INTERRUPTED")
                elapsed = time.monotonic() - started
                if elapsed > self.config.build_timeout_seconds:
                    terminate_process()
                    raise KbBuildError("构图超过允许的最长时间。", code="INDEX_TIMEOUT")
                if progress_path.is_file():
                    try:
                        progress = read_json(progress_path)
                        heartbeat = datetime.fromisoformat(str(progress.get("heartbeat_at") or ""))
                        if heartbeat.tzinfo is None:
                            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
                        heartbeat_age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
                        if heartbeat_age > self.config.build_heartbeat_timeout_seconds:
                            terminate_process()
                            raise KbBuildError("构图子进程长时间没有心跳。", code="INDEX_WORKER_LOST")
                        stage = str(progress.get("stage") or "starting")
                        processed = int(progress.get("processed_chunks") or 0)
                        total = int(progress.get("total_chunks") or 0)
                        percent = index_progress_percent(stage, processed, total)
                        signature = (stage, processed, total, progress.get("message"), percent)
                        if signature != last_signature:
                            self._update(
                                str(task["task_id"]),
                                status="indexing",
                                stage=f"indexing_{stage}",
                                progress=min(84, max(35, percent)),
                                message=str(progress.get("message") or "正在构建图谱和向量索引。"),
                            )
                            last_signature = signature
                    except KbBuildError:
                        raise
                    except (OSError, ValueError, json.JSONDecodeError):
                        pass
                time.sleep(max(0.2, self.config.build_progress_poll_seconds))
            final_progress = read_json(progress_path) if progress_path.is_file() else {}
            if process.returncode != 0:
                detail = str(final_progress.get("message") or "构图子进程异常退出。")
                detail = detail.replace(self.config.embedding_api_key, "***")
                if generation.api_key:
                    detail = detail.replace(generation.api_key, "***")
                raise KbBuildError(
                    f"LightRAG 构图失败：{detail[:300]}",
                    code=str(final_progress.get("error_code") or "INDEX_BUILD_FAILED"),
                )
        finally:
            if process.poll() is None:
                terminate_process()

    def _validate_index(self, storage: Path, kb_id: str, extension: str) -> None:
        required = {
            "kv_store_full_docs.json",
            "kv_store_text_chunks.json",
            "kv_store_doc_status.json",
            "vdb_chunks.json",
            "vdb_entities.json",
            "vdb_relationships.json",
        }
        missing = sorted(name for name in required if not (storage / name).is_file())
        if missing:
            raise KbBuildError("索引缺少必要文件：" + ", ".join(missing), code="INDEX_INCOMPLETE")
        try:
            chunks = json.loads((storage / "vdb_chunks.json").read_text(encoding="utf-8"))
            status = json.loads((storage / "kv_store_doc_status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KbBuildError("索引文件无法读取。", code="INDEX_INVALID") from exc
        if int(chunks.get("embedding_dim") or 0) != self.config.embedding_dim:
            raise KbBuildError("索引 Embedding 维度不正确。", code="INDEX_INVALID")
        expected_path = f"{kb_id}{extension}"
        paths = {
            Path(str(value.get("file_path") or "")).name
            for value in status.values()
            if isinstance(value, dict)
        }
        if expected_path not in paths:
            raise KbBuildError("索引来源文件映射不正确。", code="INDEX_SOURCE_MISMATCH")

    def _publish(self, task: dict[str, Any], task_root: Path) -> None:
        kb_id = str(task["kb_id"])
        extension = Path(task["original_name"]).suffix.lower()
        staged_source = Path(task["source_path"])
        staged_output = task_root / "output" / kb_id
        staged_storage = task_root / "storage" / kb_id
        corpus_target = self.config.corpus_root / f"{kb_id}{extension}"
        output_target = self.config.output_root / kb_id
        storage_target = self.config.storage_root / kb_id
        targets = [corpus_target, output_target, storage_target]
        if any(target.exists() for target in targets):
            raise KbBuildError("目标知识库编号已被占用。", code="KB_ID_CONFLICT")
        for root in (self.config.corpus_root, self.config.output_root, self.config.storage_root):
            root.mkdir(parents=True, exist_ok=True)
        old_map_bytes = self.config.name_map_path.read_bytes()
        moved: list[Path] = []
        try:
            shutil.copy2(staged_source, corpus_target)
            moved.append(corpus_target)
            os.replace(staged_output, output_target)
            moved.append(output_target)
            os.replace(staged_storage, storage_target)
            moved.append(storage_target)
            mapping = json.loads(old_map_bytes.decode("utf-8"))
            if not isinstance(mapping, dict):
                raise KbBuildError("name2id.json 格式错误。", code="NAME_MAP_INVALID")
            mapping[str(task["display_name"])] = int(kb_id)
            handle, temporary_name = tempfile.mkstemp(
                prefix=".name2id.", suffix=".json", dir=str(self.config.name_map_path.parent), text=True
            )
            temporary = Path(temporary_name)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(mapping, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config.name_map_path)
            from .knowledge_bases import catalog

            catalog.invalidate()
            from .kb_visibility_store import KnowledgeBaseVisibilityStore

            visibility_store = KnowledgeBaseVisibilityStore(self.config.auth_db_path)
            visibility_store.get_or_create(kb_id)
            visibility_store.update(kb_id, visibility=task.get("visibility") or "private", visible_users=[])
            try:
                shutil.rmtree(staged_source.parent)
            except OSError:
                logger.warning("Unable to remove published pending source directory: %s", staged_source.parent)
        except Exception:
            try:
                self.config.name_map_path.write_bytes(old_map_bytes)
                from .knowledge_bases import catalog

                catalog.invalidate()
            except Exception:
                logger.exception("Unable to restore name2id.json after publish failure")
            for target in reversed(moved):
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            raise


kb_build_service = KbBuildService()
