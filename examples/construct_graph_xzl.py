#!/usr/bin/env python
"""
Example script demonstrating parser integration with RAGAnything

This example shows how to:
1. Process documents with RAGAnything using configurable parsers
2. Perform pure text queries using aquery() method
3. Perform multimodal queries with specific multimodal content using aquery_with_multimodal() method
4. Handle different types of multimodal content (tables, equations) in queries
"""

import os
import argparse
import asyncio
import fcntl
import json
import logging
import logging.config
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from importlib.metadata import version as package_version
from pathlib import Path
import xml.etree.ElementTree as ET

# Add project root directory to Python path
import sys

sys.path.append(str(Path(__file__).parent.parent))

from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc, logger, set_verbose_debug
from raganything import RAGAnything, RAGAnythingConfig
from raganything.parser import MineruParser

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = Path(__file__).resolve().parent
CONSTRUCT_ROOT = EXAMPLES_DIR / "new_construct"
DEFAULT_STORAGE_ROOT = CONSTRUCT_ROOT / "rag_storage"
DEFAULT_OUTPUT_ROOT = CONSTRUCT_ROOT / "output"
DEFAULT_CORPUS_DIR = PROJECT_ROOT / "data" / "corpus"
OFFICE_EXTENSIONS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
SUPPORTED_CORPUS_EXTENSIONS = OFFICE_EXTENSIONS | {".pdf"}
REQUIRED_MINERU_VERSION = "3.1.1"
REQUIRED_WORKSPACE_JSON_FILES = (
    "kv_store_doc_status.json",
    "kv_store_text_chunks.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
    "vdb_entities.json",
    "vdb_relationships.json",
    "vdb_chunks.json",
)

load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)


def _read_optional_setting(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


DEFAULT_API_KEY = (
    os.getenv("LLM_BINDING_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or _read_optional_setting(PROJECT_ROOT / "openai_api_key.txt")
)
DEFAULT_BASE_URL = (
    os.getenv("LLM_BINDING_HOST")
    or os.getenv("OPENAI_BASE_URL")
    or _read_optional_setting(PROJECT_ROOT / "openai_base_url.txt")
)


def get_document_locations(
    file_name: str, storage_root: str | Path, output_root: str | Path
) -> tuple[str, Path, Path]:
    """Return the isolated workspace, storage root, and parser output directory."""

    workspace = Path(file_name).name[:3]
    if len(workspace) != 3 or not workspace.isdigit():
        raise ValueError(
            f"Corpus file name must start with a three-digit id: {file_name}"
        )

    return workspace, Path(storage_root), Path(output_root) / workspace


def configure_logging(
    log_dir: str | Path | None = None,
    log_file_name: str = "raganything_example.log",
):
    """Configure logging for the application"""
    # Get log directory path from environment variable or use current directory
    resolved_log_dir = Path(
        log_dir or os.getenv("LOG_DIR", str(DEFAULT_OUTPUT_ROOT))
    ).expanduser()
    log_file_path = os.path.abspath(resolved_log_dir / log_file_name)

    print(f"\nRAGAnything example log file: {log_file_path}\n")
    os.makedirs(os.path.dirname(log_file_path) or ".", exist_ok=True)

    # Get log file max size and backup count from environment variables
    log_max_bytes = int(os.getenv("LOG_MAX_BYTES", 10485760))  # Default 10MB
    log_backup_count = int(os.getenv("LOG_BACKUP_COUNT", 5))  # Default 5 backups

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(levelname)s: %(message)s",
                },
                "detailed": {
                    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                },
            },
            "handlers": {
                "console": {
                    "formatter": "default",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                },
                "file": {
                    "formatter": "detailed",
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": log_file_path,
                    "maxBytes": log_max_bytes,
                    "backupCount": log_backup_count,
                    "encoding": "utf-8",
                },
            },
            "loggers": {
                "lightrag": {
                    "handlers": ["console", "file"],
                    "level": "INFO",
                    "propagate": False,
                },
            },
        }
    )

    # Set the logger level to INFO
    logger.setLevel(logging.INFO)
    # Enable verbose debug if needed
    set_verbose_debug(os.getenv("VERBOSE", "false").lower() == "true")


def parse_gpu_ids(raw_gpu_ids: str) -> list[str]:
    """Parse exactly three ordered, duplicate-free CUDA device ids."""

    gpu_ids = [item.strip() for item in raw_gpu_ids.split(",") if item.strip()]
    if not gpu_ids:
        raise ValueError("At least one GPU id is required")
    if len(gpu_ids) != len(set(gpu_ids)):
        raise ValueError(f"GPU ids must be unique: {raw_gpu_ids}")
    if len(gpu_ids) != 3:
        raise ValueError(
            f"Exactly three GPU ids are required, received {len(gpu_ids)}: "
            f"{raw_gpu_ids}"
        )
    return gpu_ids


def build_mineru_client_command(
    mineru_executable: str,
    pdf_path: str | Path,
    output_dir: str | Path,
    api_url: str,
) -> list[str]:
    """Build a MinerU client command that reuses a persistent API service."""

    return [
        mineru_executable,
        "-p",
        str(pdf_path),
        "-o",
        str(output_dir),
        "--api-url",
        api_url,
        "-m",
        "auto",
    ]


def convert_office_to_pdf_with_lock(
    parser: MineruParser,
    file_path: str | Path,
    output_dir: str | Path,
) -> Path:
    """Serialize the short LibreOffice stage while keeping GPU parsing parallel."""

    lock_path = CONSTRUCT_ROOT / "libreoffice_conversion.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            return Path(parser.convert_office_to_pdf(file_path, output_dir)).resolve()
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def is_workspace_complete(file_path: str | Path, storage_root: str | Path) -> bool:
    """Return True only when this file has a complete local workspace."""

    file_path = Path(file_path)
    workspace = file_path.name[:3]
    workspace_dir = Path(storage_root) / workspace
    graph_path = workspace_dir / "graph_chunk_entity_relation.graphml"
    json_paths = [workspace_dir / name for name in REQUIRED_WORKSPACE_JSON_FILES]
    if not graph_path.is_file() or any(not path.is_file() for path in json_paths):
        return False

    try:
        parsed_json = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in json_paths
        }
        graph_root = ET.parse(graph_path).getroot()
    except (OSError, json.JSONDecodeError, ET.ParseError):
        return False

    status_records = parsed_json["kv_store_doc_status.json"]
    text_chunks = parsed_json["kv_store_text_chunks.json"]
    if not isinstance(status_records, dict) or not isinstance(text_chunks, dict):
        return False
    if not any(element.tag.endswith("node") for element in graph_root.iter()):
        return False

    for record in status_records.values():
        if not isinstance(record, dict):
            continue
        if Path(str(record.get("file_path", ""))).name != file_path.name:
            continue
        # "handling" is accepted only for legacy 001-003 records that were
        # fully persisted before the status normalization fix.
        if record.get("status") not in {"processed", "handling"}:
            continue
        if record.get("multimodal_processed") is not True:
            continue
        try:
            chunks_count = int(record.get("chunks_count", 0) or 0)
        except (TypeError, ValueError):
            continue
        chunks_list = record.get("chunks_list", [])
        if not isinstance(chunks_list, list) or chunks_count <= 0:
            continue
        if chunks_count != len(chunks_list):
            continue
        if any(chunk_id not in text_chunks for chunk_id in chunks_list):
            continue
        return True

    return False


def archive_incomplete_workspace(
    file_path: str | Path,
    storage_root: str | Path,
) -> Path | None:
    """Move a partial workspace aside so a retry starts from clean storage."""

    file_path = Path(file_path)
    storage_root = Path(storage_root)
    workspace_dir = storage_root / file_path.name[:3]
    if not workspace_dir.exists():
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_root = storage_root.parent / "workspace_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / (
        f"{workspace_dir.name}_{timestamp}_pid{os.getpid()}"
    )
    shutil.move(str(workspace_dir), str(backup_path))
    return backup_path


def _ancestor_process_ids(pid: int) -> set[int]:
    ancestors = set()
    current = pid
    while current > 1:
        try:
            stat_fields = Path(f"/proc/{current}/stat").read_text().split()
            parent = int(stat_fields[3])
        except (OSError, ValueError, IndexError):
            break
        if parent <= 1 or parent in ancestors:
            break
        ancestors.add(parent)
        current = parent
    return ancestors


def find_other_orchestrator_processes() -> list[int]:
    """Find legacy/current top-level builders, excluding this process tree."""

    excluded = _ancestor_process_ids(os.getpid()) | {os.getpid()}
    matches = []
    for cmdline_path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            pid = int(cmdline_path.parent.name)
            if pid in excluded:
                continue
            args = [
                item.decode("utf-8", errors="ignore")
                for item in cmdline_path.read_bytes().split(b"\0")
                if item
            ]
        except (OSError, ValueError):
            continue
        if "--worker-file" in args:
            continue
        if any(Path(arg).name == Path(__file__).name for arg in args):
            matches.append(pid)
    return sorted(matches)


@contextmanager
def acquire_orchestrator_lock():
    """Prevent a legacy or second orchestrator from touching active workspaces."""

    other_processes = find_other_orchestrator_processes()
    if other_processes:
        raise RuntimeError(
            "Another construct_graph_xzl.py process is still running: "
            + ", ".join(str(pid) for pid in other_processes)
        )

    lock_path = CONSTRUCT_ROOT / "construct_graph.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_path, "a", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another multi-GPU orchestrator holds the lock") from exc
        yield
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class MineruServer:
    gpu_id: str
    api_url: str
    process: subprocess.Popen
    log_path: Path
    log_handle: object


def start_mineru_server(gpu_id: str) -> MineruServer:
    """Start one preloaded MinerU API process bound to one physical GPU."""

    mineru_api = shutil.which("mineru-api")
    if not mineru_api:
        raise RuntimeError("mineru-api was not found in the active environment")

    port = _allocate_local_port()
    api_url = f"http://127.0.0.1:{port}"
    safe_gpu_id = gpu_id.replace("/", "_")
    log_path = CONSTRUCT_ROOT / f"mineru_gpu_{safe_gpu_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "a", encoding="utf-8")

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = gpu_id
    env["MINERU_API_MAX_CONCURRENT_REQUESTS"] = "1"
    env["MINERU_API_ENABLE_FASTAPI_DOCS"] = "0"
    env["MINERU_API_OUTPUT_ROOT"] = str(
        (CONSTRUCT_ROOT / "mineru_api_cache" / safe_gpu_id).resolve()
    )

    command = [
        mineru_api,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--enable-vlm-preload",
        "true",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )
    except Exception:
        log_handle.close()
        raise

    return MineruServer(
        gpu_id=gpu_id,
        api_url=api_url,
        process=process,
        log_path=log_path,
        log_handle=log_handle,
    )


def validate_mineru_version() -> None:
    installed_version = package_version("mineru")
    if installed_version != REQUIRED_MINERU_VERSION:
        raise RuntimeError(
            f"MinerU {REQUIRED_MINERU_VERSION} is required, found "
            f"{installed_version}"
        )


def _read_server_health(api_url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{api_url}/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if response.status == 200 else None
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


async def wait_for_mineru_server(
    server: MineruServer, startup_timeout: float
) -> None:
    """Wait until a preloaded MinerU API is healthy or fail with its log path."""

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        return_code = server.process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"MinerU API for GPU {server.gpu_id} exited with code "
                f"{return_code}; see {server.log_path}"
            )
        health = await asyncio.to_thread(_read_server_health, server.api_url)
        if health is not None:
            server_version = str(health.get("version", ""))
            if server_version != REQUIRED_MINERU_VERSION:
                raise RuntimeError(
                    f"MinerU API on GPU {server.gpu_id} reported version "
                    f"{server_version!r}; required {REQUIRED_MINERU_VERSION}"
                )
        if health is not None and health.get("status") == "healthy":
            print(
                f"MinerU API ready: GPU {server.gpu_id} -> {server.api_url} "
                f"(log: {server.log_path})",
                flush=True,
            )
            return
        await asyncio.sleep(2)

    raise TimeoutError(
        f"MinerU API for GPU {server.gpu_id} did not become healthy within "
        f"{startup_timeout:.0f}s; see {server.log_path}"
    )


def stop_process(process: subprocess.Popen, timeout: float = 20) -> None:
    """Stop a child process, escalating to kill only after a grace period."""

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def stop_mineru_server(server: MineruServer) -> None:
    try:
        stop_process(server.process)
    finally:
        server.log_handle.close()


def parse_document_with_persistent_mineru(
    file_path: str | Path,
    output_dir: str | Path,
    api_url: str,
) -> list[dict]:
    """Parse one document through an already-running MinerU API service."""

    file_path = Path(file_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    parser = MineruParser()

    if file_path.suffix.lower() in OFFICE_EXTENSIONS:
        pdf_path = convert_office_to_pdf_with_lock(parser, file_path, output_dir)
    elif file_path.suffix.lower() == ".pdf":
        pdf_path = file_path
    else:
        raise ValueError(
            f"Persistent MinerU mode currently supports Office and PDF files, got: "
            f"{file_path.name}"
        )

    mineru_output_dir = parser._unique_output_dir(output_dir, pdf_path)
    if mineru_output_dir.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        previous_output_root = output_dir / "previous_parser_outputs"
        previous_output_root.mkdir(parents=True, exist_ok=True)
        previous_output = previous_output_root / (
            f"{mineru_output_dir.name}_{timestamp}_pid{os.getpid()}"
        )
        shutil.move(str(mineru_output_dir), str(previous_output))
    mineru_output_dir.mkdir(parents=True, exist_ok=True)
    mineru_executable = shutil.which("mineru")
    if not mineru_executable:
        raise RuntimeError("mineru was not found in the active environment")

    command = build_mineru_client_command(
        mineru_executable=mineru_executable,
        pdf_path=pdf_path,
        output_dir=mineru_output_dir,
        api_url=api_url,
    )
    print(
        f"Using persistent MinerU API {api_url} for {file_path.name}",
        flush=True,
    )
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"MinerU client failed for {file_path.name} with exit code "
            f"{result.returncode}"
        )

    content_list, _ = parser._read_output_files(
        mineru_output_dir,
        pdf_path.stem,
        method="auto",
    )
    if not content_list:
        raise ValueError(f"MinerU extracted no content from {file_path.name}")
    return content_list


async def process_with_rag(
    file_path: str,
    output_dir: str,
    api_key: str,
    base_url: str = None,
    working_dir: str = None,
    parser: str = None,
    workspace: str = None,
    content_list: list[dict] | None = None,
):
    """
    Process document with RAGAnything

    Args:
        file_path: Path to the document
        output_dir: Output directory for RAG results
        api_key: OpenAI API key
        base_url: Optional base URL for API
        working_dir: Shared root directory for all RAG workspaces
        parser: Parser implementation name
        workspace: Unique LightRAG workspace for this document
        content_list: Optional MinerU output parsed by a persistent API service
    """
    rag = None
    try:
        if not workspace:
            raise ValueError("A unique workspace is required for each document")

        # Create RAGAnything configuration
        config = RAGAnythingConfig(
            working_dir=working_dir or str(DEFAULT_STORAGE_ROOT),
            parser=parser,  # Parser selection: mineru, docling, or paddleocr
            parse_method="auto",  # Parse method: auto, ocr, or txt
            enable_image_processing=True,
            enable_table_processing=True,
            enable_equation_processing=True,
        )

        # Define LLM model function
        llm_model = os.getenv("LLM_MODEL", "gpt-4o-mini")
        vision_model = os.getenv("VISION_MODEL", "gpt-4o")

        def llm_model_func(prompt, system_prompt=None, history_messages=[], **kwargs):
            return openai_complete_if_cache(
                llm_model,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages,
                api_key=api_key,
                base_url=base_url,
                **kwargs,
            )

        # Define vision model function for image processing
        def vision_model_func(
            prompt,
            system_prompt=None,
            history_messages=[],
            image_data=None,
            messages=None,
            **kwargs,
        ):
            # If messages format is provided (for multimodal VLM enhanced query), use it directly
            if messages:
                return openai_complete_if_cache(
                    vision_model,
                    "",
                    system_prompt=None,
                    history_messages=[],
                    messages=messages,
                    api_key=api_key,
                    base_url=base_url,
                    **kwargs,
                )
            # Traditional single image format
            elif image_data:
                return openai_complete_if_cache(
                    vision_model,
                    "",
                    system_prompt=None,
                    history_messages=[],
                    messages=[
                        {"role": "system", "content": system_prompt}
                        if system_prompt
                        else None,
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{image_data}"
                                    },
                                },
                            ],
                        }
                        if image_data
                        else {"role": "user", "content": prompt},
                    ],
                    api_key=api_key,
                    base_url=base_url,
                    **kwargs,
                )
            # Pure text format
            else:
                return llm_model_func(prompt, system_prompt, history_messages, **kwargs)

        # Define embedding function - using environment variables for configuration
        embedding_dim = int(os.getenv("EMBEDDING_DIM", "3072"))
        embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")

        embedding_func = EmbeddingFunc(
            embedding_dim=embedding_dim,
            max_token_size=8192,
            func=partial(
                openai_embed.func,
                model=embedding_model,
                api_key=api_key,
                base_url=base_url,
            ),
        )

        # Initialize RAGAnything with new dataclass structure
        rag = RAGAnything(
            config=config,
            llm_model_func=llm_model_func,
            vision_model_func=vision_model_func,
            embedding_func=embedding_func,
            lightrag_kwargs={"workspace": workspace},
        )

        # Process document. Persistent MinerU workers insert their already-parsed
        # content directly so the model-bearing API stays alive across documents.
        if content_list is None:
            await rag.process_document_complete(
                file_path=file_path, output_dir=output_dir, parse_method="auto"
            )
        else:
            await rag.insert_content_list(
                content_list=content_list,
                file_path=Path(file_path).name,
            )

        # Example queries - demonstrating different query approaches
        # logger.info("\nQuerying processed document:")

        # 1. Pure text queries using aquery()
        # text_queries = [
        #     "What is the main content of the document?",
        #     "What are the key topics discussed?",
        # ]
        
        # text_queries = ["关于《意外伤害保险业务监管办法》，下列哪一组选项中的两项表述均属于该材料内容？\nA. 保险期限一年及以下的个人意外险平均附加费用率上限为35%。；基础利率曲线为即期曲线，由750日移动平均国债收益率曲线、终极利率过渡曲线和终极利率三段组成。 \nB. 保险期限一年及以下的个人意外险平均附加费用率上限为35%。；750日移动平均国债收益率曲线适用于0年到20年的区间。 \nC. 保险期限一年及以下的个人意外险平均附加费用率上限为35%。；保险期限一年及以下的团体意外险平均附加费用率上限为25%。 \nD. 保险期限一年及以下的个人意外险平均附加费用率上限为35%。；寿险合同负债评估中计算现金流现值所采用的折现率曲线由基础利率曲线加综合溢价形成。\n单选题，只需回答选项即可。"]
        
        
        # text_queries = ["需要对同一 Excel 附件做两处取数并计算。根据《2023年银行业金融机构普惠型小微企业贷款情况（季度）》，“农村金融机构”从“年-季度”到“季度”的数值变化约为多少？\n A. 6698.26\nB. -6089.33\nC. 5480.4\nD. 6089.33\n单选题，只需回答选项即可。]"]
        		


        # for query in text_queries:
        #     logger.info(f"\n[Text Query]: {query}")
        #     result = await rag.aquery(query, mode="hybrid")
        #     logger.info(f"Answer: {result}")

        # 2. Multimodal query with specific multimodal content using aquery_with_multimodal()
        # logger.info(
        #     "\n[Multimodal Query]: Analyzing performance data in context of document"
        # )
        # multimodal_result = await rag.aquery_with_multimodal(
        #     "Compare this performance data with any similar results mentioned in the document",
        #     multimodal_content=[
        #         {
        #             "type": "table",
        #             "table_data": """Method,Accuracy,Processing_Time
        #                         RAGAnything,95.2%,120ms
        #                         Traditional_RAG,87.3%,180ms
        #                         Baseline,82.1%,200ms""",
        #             "table_caption": "Performance comparison results",
        #         }
        #     ],
        #     mode="hybrid",
        # )
        # logger.info(f"Answer: {multimodal_result}")

        # 3. Another multimodal query with equation content
        # logger.info("\n[Multimodal Query]: Mathematical formula analysis")
        # equation_result = await rag.aquery_with_multimodal(
        #     "Explain this formula and relate it to any mathematical concepts in the document",
        #     multimodal_content=[
        #         {
        #             "type": "equation",
        #             "latex": "F1 = 2 \\cdot \\frac{precision \\cdot recall}{precision + recall}",
        #             "equation_caption": "F1-score calculation formula",
        #         }
        #     ],
        #     mode="hybrid",
        # )
        # logger.info(f"Answer: {equation_result}")

    except Exception as e:
        logger.error(f"Error processing with RAG: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        raise
    finally:
        if rag is not None:
            await rag.finalize_storages()


def build_argument_parser() -> argparse.ArgumentParser:
    """Build CLI arguments for the orchestrator and its private workers."""

    parser = argparse.ArgumentParser(description="MinerU RAG Example")
    parser.add_argument(
        "--corpus-dir",
        default=str(DEFAULT_CORPUS_DIR),
        help="Directory containing three-digit corpus files",
    )
    parser.add_argument(
        "--working_dir",
        "-w",
        default=str(DEFAULT_STORAGE_ROOT),
        help="Root directory containing one LightRAG workspace per document",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Root directory containing parser output for each document",
    )
    parser.add_argument(
        "--api-key",
        default=DEFAULT_API_KEY,
        help=(
            "OpenAI API key; defaults to LLM_BINDING_API_KEY, OPENAI_API_KEY, "
            "or openai_api_key.txt"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=(
            "Optional API base URL; defaults to LLM_BINDING_HOST, "
            "OPENAI_BASE_URL, or openai_base_url.txt"
        ),
    )
    parser.add_argument(
        "--parser",
        default="mineru",
        # default=os.getenv("PARSER", "mineru"),
        help=(
            "Parser selection. Built-ins: mineru, docling, paddleocr. "
            "Custom parsers that you register via register_parser() in the "
            "same Python process are also accepted when using RAGAnything as "
            "a library. This example script does not perform any automatic "
            "plugin discovery."
        ),
    )
    parser.add_argument(
        "--gpus",
        default=os.getenv("CUDA_VISIBLE_DEVICES", "1,2,3"),
        help="Comma-separated physical GPU ids; one persistent MinerU API per GPU",
    )
    parser.add_argument(
        "--mineru-startup-timeout",
        type=float,
        default=900,
        help="Seconds to wait for all preloaded MinerU API services",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess completed workspaces instead of resuming",
    )
    parser.add_argument("--worker-file", help=argparse.SUPPRESS)
    parser.add_argument("--mineru-api-url", help=argparse.SUPPRESS)
    parser.add_argument("--worker-gpu", help=argparse.SUPPRESS)
    return parser


def build_worker_command(
    args: argparse.Namespace,
    file_path: Path,
    server: MineruServer,
) -> list[str]:
    """Build an isolated one-document worker command."""

    command = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        "--worker-file",
        str(file_path),
        "--mineru-api-url",
        server.api_url,
        "--worker-gpu",
        server.gpu_id,
        "--working_dir",
        str(args.working_dir),
        "--output",
        str(args.output),
        "--parser",
        str(args.parser),
    ]
    if args.force:
        command.append("--force")
    return command


async def run_document_worker(args: argparse.Namespace) -> None:
    """Parse and index exactly one document in an isolated process."""

    if not args.mineru_api_url:
        raise ValueError("--mineru-api-url is required in worker mode")

    file_path = Path(args.worker_file).expanduser().resolve()
    storage_root = Path(args.working_dir).expanduser().resolve()
    output_root = Path(args.output).expanduser().resolve()
    workspace, working_dir, document_output_dir = get_document_locations(
        file_path.name,
        storage_root,
        output_root,
    )
    document_output_dir.mkdir(parents=True, exist_ok=True)

    # Recheck after dispatch in case another process completed the workspace.
    if not args.force and is_workspace_complete(file_path, storage_root):
        print(
            f"[GPU {args.worker_gpu}] Skipping newly completed {file_path.name}",
            flush=True,
        )
        return

    backup_path = archive_incomplete_workspace(file_path, storage_root)
    if backup_path is not None:
        print(
            f"Archived existing workspace {workspace} to {backup_path}",
            flush=True,
        )

    print(
        f"[GPU {args.worker_gpu}] Processing {file_path.name} "
        f"[workspace={workspace}]",
        flush=True,
    )
    print(f"  RAG storage: {working_dir / workspace}", flush=True)
    print(f"  Parser output: {document_output_dir}", flush=True)

    content_list = await asyncio.to_thread(
        parse_document_with_persistent_mineru,
        file_path,
        document_output_dir,
        args.mineru_api_url,
    )
    await process_with_rag(
        str(file_path),
        str(document_output_dir),
        args.api_key,
        args.base_url,
        str(working_dir),
        args.parser,
        workspace,
        content_list=content_list,
    )
    print(
        f"[GPU {args.worker_gpu}] Completed {file_path.name} "
        f"[workspace={workspace}]",
        flush=True,
    )


async def run_parallel_orchestrator(args: argparse.Namespace) -> None:
    """Run three persistent MinerU services and dynamically schedule documents."""

    if args.parser != "mineru":
        raise ValueError("Persistent multi-GPU mode currently requires --parser mineru")
    validate_mineru_version()

    corpus_dir = Path(args.corpus_dir).expanduser().resolve()
    storage_root = Path(args.working_dir).expanduser().resolve()
    output_root = Path(args.output).expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)

    all_files = sorted(path for path in corpus_dir.iterdir() if path.is_file())
    corpus_files = [
        path
        for path in all_files
        if path.suffix.lower() in SUPPORTED_CORPUS_EXTENSIONS
        and len(path.name) >= 3
        and path.name[:3].isdigit()
    ]
    ignored_files = [path for path in all_files if path not in corpus_files]
    if ignored_files:
        print(
            "Ignored unsupported corpus files: "
            + ", ".join(path.name for path in ignored_files),
            flush=True,
        )
    workspaces = [
        get_document_locations(path.name, storage_root, output_root)[0]
        for path in corpus_files
    ]
    if len(workspaces) != len(set(workspaces)):
        raise ValueError(
            "Corpus file ids are not unique; workspace isolation is impossible"
        )

    skipped_files = []
    pending_files = []
    for file_path in corpus_files:
        if not args.force and is_workspace_complete(file_path, storage_root):
            skipped_files.append(file_path)
        else:
            pending_files.append(file_path)

    print(
        f"Corpus files: {len(corpus_files)}; completed/skipped: "
        f"{len(skipped_files)}; pending: {len(pending_files)}",
        flush=True,
    )
    if skipped_files:
        print(
            f"Resume starts from pending workspace "
            f"{pending_files[0].name[:3] if pending_files else 'none'}",
            flush=True,
        )
    if not pending_files:
        print("All workspaces are already complete.", flush=True)
        return

    gpu_ids = parse_gpu_ids(args.gpus)
    print(
        f"Starting {len(gpu_ids)} persistent MinerU services on GPUs: "
        f"{','.join(gpu_ids)}",
        flush=True,
    )

    servers: list[MineruServer] = []
    active_workers: dict[str, tuple[subprocess.Popen, Path, MineruServer]] = {}
    failures: list[tuple[Path, int]] = []
    attempts: dict[Path, int] = {}
    try:
        for gpu_id in gpu_ids:
            servers.append(start_mineru_server(gpu_id))

        await asyncio.gather(
            *(
                wait_for_mineru_server(server, args.mineru_startup_timeout)
                for server in servers
            )
        )

        pending_queue = deque(pending_files)
        free_servers = deque(servers)

        while pending_queue or active_workers:
            while pending_queue and free_servers:
                server = free_servers.popleft()
                file_path = pending_queue.popleft()
                worker_env = os.environ.copy()
                worker_env["CUDA_VISIBLE_DEVICES"] = server.gpu_id
                worker_env["_RAG_WORKER_API_KEY"] = args.api_key
                worker_env["_RAG_WORKER_BASE_URL"] = args.base_url or ""
                command = build_worker_command(args, file_path, server)
                worker = subprocess.Popen(command, env=worker_env)
                attempts[file_path] = attempts.get(file_path, 0) + 1
                active_workers[server.gpu_id] = (worker, file_path, server)
                print(
                    f"Dispatched {file_path.name} to GPU {server.gpu_id} "
                    f"(attempt={attempts[file_path]}, pid={worker.pid})",
                    flush=True,
                )

            finished_gpu_ids = []
            for gpu_id, (worker, file_path, server) in active_workers.items():
                return_code = worker.poll()
                if return_code is None:
                    continue
                if server.process.poll() is not None:
                    raise RuntimeError(
                        f"MinerU API for GPU {gpu_id} exited while processing "
                        f"{file_path.name}; see {server.log_path}"
                    )
                finished_gpu_ids.append(gpu_id)
                free_servers.append(server)
                if return_code == 0:
                    print(
                        f"Worker finished: {file_path.name} on GPU {gpu_id}",
                        flush=True,
                    )
                else:
                    if attempts[file_path] < 2:
                        pending_queue.append(file_path)
                        print(
                            f"Worker failed: {file_path.name} on GPU {gpu_id}, "
                            f"exit={return_code}; scheduling one clean retry",
                            flush=True,
                        )
                    else:
                        failures.append((file_path, return_code))
                        print(
                            f"Worker failed twice: {file_path.name} on GPU "
                            f"{gpu_id}, exit={return_code}; it remains pending",
                            flush=True,
                        )

            for gpu_id in finished_gpu_ids:
                del active_workers[gpu_id]

            if active_workers:
                await asyncio.sleep(1)

        if failures:
            failed_names = ", ".join(path.name for path, _ in failures)
            raise RuntimeError(
                f"{len(failures)} document worker(s) failed: {failed_names}"
            )
    finally:
        for worker, _, _ in active_workers.values():
            stop_process(worker)
        for server in servers:
            stop_mineru_server(server)


async def main(args: argparse.Namespace) -> None:
    """Run either the multi-GPU orchestrator or one private worker."""

    if not args.api_key:
        raise ValueError("OpenAI API key is required")

    if args.worker_file:
        await run_document_worker(args)
    else:
        await run_parallel_orchestrator(args)


def cli() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()

    if args.worker_file:
        args.api_key = os.getenv("_RAG_WORKER_API_KEY", args.api_key)
        args.base_url = os.getenv("_RAG_WORKER_BASE_URL", args.base_url) or None
        worker_file = Path(args.worker_file).expanduser().resolve()
        workspace = worker_file.name[:3]
        configure_logging(
            Path(args.output).expanduser().resolve() / workspace,
            "raganything_worker.log",
        )
    else:
        signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(143))
        with acquire_orchestrator_lock():
            configure_logging(CONSTRUCT_ROOT, "construct_graph_orchestrator.log")
            print("RAGAnything persistent multi-GPU graph builder", flush=True)
            print("=" * 46, flush=True)
            asyncio.run(main(args))
        return

    print("RAGAnything persistent multi-GPU graph builder", flush=True)
    print("=" * 46, flush=True)
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
