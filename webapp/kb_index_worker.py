from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import threading
from functools import partial
from pathlib import Path
from typing import Any

from .kb_index_protocol import classify_index_error, read_json, safe_error_message, write_progress
from .model_settings import model_settings_store
from .settings import settings


CHUNK_PATTERNS = (
    re.compile(r"(?:Processing|Processed)\s+chunk\s+(\d+)\s+(?:of|/)\s+(\d+)", re.I),
    re.compile(r"chunk\s+(\d+)\s*/\s*(\d+)", re.I),
    re.compile(r"chunk\s+(\d+)\s+of\s+(\d+)", re.I),
)


class ProgressReporter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state: dict[str, Any] = {
            "stage": "starting",
            "message": "正在启动构图进程。",
            "processed_chunks": 0,
            "total_chunks": 0,
            "error_code": "",
        }
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)

    def start(self) -> None:
        self.publish()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def update(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)
        self.publish()

    def publish(self) -> None:
        with self._lock:
            state = dict(self._state)
        write_progress(self.path, **state)

    def _heartbeat(self) -> None:
        while not self._stop.wait(10):
            self.publish()


class ChunkProgressHandler(logging.Handler):
    def __init__(self, reporter: ProgressReporter) -> None:
        super().__init__()
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        for pattern in CHUNK_PATTERNS:
            match = pattern.search(message)
            if not match:
                continue
            processed = int(match.group(1))
            total = int(match.group(2))
            self.reporter.update(
                stage="extracting",
                message=f"正在构建图谱：文本块 {processed}/{total}。",
                processed_chunks=processed,
                total_chunks=total,
            )
            return


def document_text(content_list: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in content_list:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") == "table":
            captions = item.get("table_caption") or []
            if isinstance(captions, list):
                parts.extend(str(value) for value in captions if str(value).strip())
            table_body = str(item.get("table_body") or "").strip()
            if table_body:
                parts.append(table_body)
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


async def build_index(request: dict[str, Any], reporter: ProgressReporter) -> None:
    from lightrag import LightRAG
    from lightrag.llm.openai import openai_complete_if_cache, openai_embed
    from lightrag.utils import EmbeddingFunc

    if not settings.embedding_configured:
        raise RuntimeError("建库 Embedding API 未配置。")
    generation = model_settings_store.effective_generation(settings)
    if not generation.base_url or not generation.api_key or not generation.model_name:
        raise RuntimeError("生成模型未正确配置。")

    reporter.update(stage="probing", message="正在验证 Embedding 模型。")
    probe = await openai_embed.func(
        ["TrustedRAG embedding probe"],
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
    )
    actual_dim = len(probe[0]) if len(probe) else 0
    if len(probe) != 1 or actual_dim != settings.embedding_dim:
        raise RuntimeError(f"Embedding 维度不匹配：配置 {settings.embedding_dim}，接口返回 {actual_dim}。")

    content_path = Path(str(request["content_list_path"]))
    payload = json.loads(content_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("解析结果格式错误。")
    text = document_text(payload)
    if not text:
        raise RuntimeError("解析结果没有可插入 LightRAG 的文本。")

    storage_dir = Path(str(request["storage_dir"]))
    if storage_dir.exists():
        shutil.rmtree(storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=False)

    def llm_model_func(prompt: str, system_prompt: str | None = None, history_messages=None, **kwargs: Any):
        return openai_complete_if_cache(
            generation.model_name,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            api_key=generation.api_key,
            base_url=generation.base_url,
            **kwargs,
        )

    embedding_func = EmbeddingFunc(
        embedding_dim=settings.embedding_dim,
        max_token_size=8192,
        func=partial(
            openai_embed.func,
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        ),
    )
    reporter.update(stage="initializing", message="正在初始化图谱和向量存储。")
    rag = LightRAG(
        working_dir=str(storage_dir),
        llm_model_func=llm_model_func,
        embedding_func=embedding_func,
        embedding_batch_num=max(1, settings.build_embedding_batch_num),
        embedding_func_max_async=max(1, settings.build_embedding_max_async),
        default_embedding_timeout=max(30, settings.build_embedding_timeout_seconds),
        enable_llm_cache=True,
    )
    try:
        await rag.initialize_storages()
        reporter.update(stage="extracting", message="正在提取实体、关系和文本向量。")
        await rag.ainsert(
            input=text,
            file_paths=str(request["source_file_name"]),
        )
        reporter.update(stage="finalizing", message="正在合并图谱并写入索引。")
    finally:
        await rag.finalize_storages()


def run(request_path: Path, progress_path: Path) -> int:
    reporter = ProgressReporter(progress_path)
    reporter.start()
    handler = ChunkProgressHandler(reporter)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        try:
            os.nice(max(0, settings.build_nice_level))
        except (AttributeError, OSError):
            pass
        request = read_json(request_path)
        asyncio.run(build_index(request, reporter))
        reporter.update(stage="succeeded", message="图谱和向量索引构建完成。")
        return 0
    except Exception as exc:
        generation_key = model_settings_store.effective_generation(settings).api_key
        message = safe_error_message(
            str(exc) or exc.__class__.__name__,
            [settings.embedding_api_key, generation_key],
        )
        reporter.update(
            stage="failed",
            message=message or "LightRAG 构图失败。",
            error_code=classify_index_error(message),
        )
        return 1
    finally:
        root_logger.removeHandler(handler)
        reporter.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--progress", required=True)
    args = parser.parse_args()
    return run(Path(args.request), Path(args.progress))


if __name__ == "__main__":
    raise SystemExit(main())
