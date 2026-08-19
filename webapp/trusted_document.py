from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .document_router import normalize_text
from .evidence_cleaning import clean_evidence_content
from .knowledge_bases import KnowledgeBase
from .schemas import SourceItem
from .settings import Settings, settings


class TrustedDocumentError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrustedDocumentContext:
    context: str
    sources: list[SourceItem]
    target_file: str
    total_chunks: int
    selected_chunks: int
    foreign_chunks: int


def _load_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TrustedDocumentError(f"Invalid index object: {path.name}")
    return payload


def _query_terms(question: str) -> set[str]:
    normalized = normalize_text(question)
    terms = set(re.findall(r"20\d{2}年|\d{1,2}月|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}", question))
    for size in (2, 3, 4):
        terms.update(normalized[index : index + size] for index in range(max(0, len(normalized) - size + 1)))
    return {normalize_text(term) for term in terms if normalize_text(term)}


def _chunk_score(question_terms: set[str], content: str) -> float:
    normalized = normalize_text(content)
    if not normalized:
        return 0.0
    score = 0.0
    for term in question_terms:
        if term in normalized:
            score += 1.0 + min(len(term), 12) / 6
    return score


class TrustedDocumentLoader:
    def __init__(self, config: Settings = settings) -> None:
        self.config = config

    def load(self, knowledge_base: KnowledgeBase, question: str) -> TrustedDocumentContext:
        chunks_path = knowledge_base.storage_dir / "kv_store_text_chunks.json"
        status_path = knowledge_base.storage_dir / "kv_store_doc_status.json"
        if not chunks_path.is_file() or not status_path.is_file():
            raise TrustedDocumentError("目标索引缺少文档状态或文本 chunks。")

        chunks = _load_object(chunks_path)
        status = _load_object(status_path)
        expected_suffix = Path(knowledge_base.display_name).suffix.lower()
        expected_file = f"{knowledge_base.identifier}{expected_suffix}"
        document_paths = {
            str(value.get("file_path"))
            for value in status.values()
            if isinstance(value, dict) and value.get("file_path")
        }
        if expected_file in document_paths:
            target_file = expected_file
        elif len(document_paths) == 1:
            target_file = next(iter(document_paths))
        else:
            raise TrustedDocumentError("无法在目标索引中唯一确认源文档。")

        target_chunks: list[tuple[str, dict[str, object]]] = []
        foreign_chunks = 0
        for chunk_id, raw_value in chunks.items():
            if not isinstance(raw_value, dict):
                continue
            file_path = str(raw_value.get("file_path") or "")
            if file_path == target_file:
                target_chunks.append((str(chunk_id), raw_value))
            elif file_path:
                foreign_chunks += 1
        if not target_chunks:
            raise TrustedDocumentError("目标文档没有可用文本 chunks。")

        question_terms = _query_terms(question)
        ranked = sorted(
            enumerate(target_chunks),
            key=lambda item: (
                -_chunk_score(question_terms, str(item[1][1].get("content") or "")),
                item[0],
            ),
        )
        selected: list[tuple[str, dict[str, object]]] = []
        context_chars = 0
        for _, chunk in ranked:
            chunk_id, value = chunk
            content = clean_evidence_content(str(value.get("content") or ""))
            if not content:
                continue
            value = {**value, "content": content}
            if selected and context_chars + len(content) > self.config.max_context_chars:
                continue
            selected.append((chunk_id, value))
            context_chars += len(content)
            if len(selected) >= self.config.max_sources:
                break
        if not selected:
            raise TrustedDocumentError("目标文档 chunks 超出上下文限制。")

        sources = [
            SourceItem(
                id=index,
                title=knowledge_base.display_name,
                content=clean_evidence_content(str(value.get("content") or ""))[: self.config.max_source_chars],
            )
            for index, (_, value) in enumerate(selected, start=1)
        ]
        context_blocks = [
            f"[来源 {index}] 文档：{knowledge_base.display_name}\n{clean_evidence_content(str(value.get('content') or ''))}"
            for index, (_, value) in enumerate(selected, start=1)
        ]
        return TrustedDocumentContext(
            context="\n\n".join(context_blocks),
            sources=sources,
            target_file=target_file,
            total_chunks=len(target_chunks),
            selected_chunks=len(selected),
            foreign_chunks=foreign_chunks,
        )


trusted_document_loader = TrustedDocumentLoader()
