from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from .document_router import (
    DocumentRoutingError,
    RouteCandidate,
    RouteResult,
    document_router,
)
from .evidence_cleaning import clean_evidence_content
from .corpus_catalog import CorpusCatalogError
from .original_viewer_service import original_viewer_service
from .knowledge_bases import KnowledgeBase
from .model_settings import model_settings_store
from .prompts import (
    INTENT_CLASSIFIER_SYSTEM_PROMPT,
    JARVIS_SYSTEM_PROMPT,
    TRUSTED_RAG_SYSTEM_PROMPT,
)
from .schemas import HistoryMessage, RetrievalResult, SourceItem
from .settings import Settings, settings
from .trusted_document import TrustedDocumentError, trusted_document_loader


logger = logging.getLogger(__name__)


_RAW_SOURCE_BLOCK_START = re.compile(
    r"^\s*\[(?:来源\s*)?\d+\]\s*(?:Table\s+Analysis\s*:|Image\s+Path\s*:|Caption\s*:|Structure\s*:)",
    flags=re.IGNORECASE,
)
_RAW_SOURCE_METADATA_LINE = re.compile(
    r"^\s*(?:Image\s+Path|Caption|Structure)\s*:",
    flags=re.IGNORECASE,
)
_INTERNAL_PATH = re.compile(
    r"(?<!\w)(?:[A-Za-z]:[\\/]|/(?:root|home|data\d*|tmp|var|etc|usr|opt)/)[^\s，。；;]+",
    flags=re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|api[_ -]?key\s*[:=]\s*\S+)",
)
_INTERNAL_PROMPT_LINE = re.compile(
    r"^\s*(?:system|developer)\s*(?:prompt|message)\s*:|^\s*系统提示(?:词)?\s*[:：]",
    flags=re.IGNORECASE,
)
_MAX_REASONING_CHARS = 8_000


@dataclass(frozen=True)
class ModelCompletion:
    content: str
    reasoning: str = ""


def sanitize_answer_text(answer: str) -> str:
    """Remove leaked retrieval metadata while preserving normal prose and citations."""
    normalized = str(answer or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n\s*\n", normalized)
    kept: list[str] = []
    for paragraph in paragraphs:
        lines = paragraph.splitlines()
        if any(_RAW_SOURCE_BLOCK_START.match(line) for line in lines):
            continue
        cleaned_lines = [line for line in lines if not _RAW_SOURCE_METADATA_LINE.match(line)]
        cleaned = "\n".join(cleaned_lines).strip()
        if cleaned:
            kept.append(cleaned)
    cleaned_answer = "\n\n".join(kept).strip()
    return cleaned_answer or "当前文档证据不足，请点击下方来源查看检索到的原始证据。"


def sanitize_reasoning_text(reasoning: str) -> str:
    """Remove retrieval internals and likely secrets from model-provided reasoning."""
    normalized = str(reasoning or "").replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for line in normalized.splitlines():
        if _RAW_SOURCE_BLOCK_START.match(line) or _RAW_SOURCE_METADATA_LINE.match(line):
            continue
        if _INTERNAL_PROMPT_LINE.match(line):
            continue
        cleaned = _INTERNAL_PATH.sub("[内部路径已隐藏]", line)
        cleaned = _SENSITIVE_VALUE.sub("[敏感信息已隐藏]", cleaned)
        cleaned = re.sub(r"<\|[^>]+\|>", "", cleaned).rstrip()
        kept.append(cleaned)
    cleaned_reasoning = "\n".join(kept).strip()
    if len(cleaned_reasoning) > _MAX_REASONING_CHARS:
        cleaned_reasoning = (
            cleaned_reasoning[:_MAX_REASONING_CHARS].rstrip()
            + "\n\n[分析过程已截断]"
        )
    return cleaned_reasoning


def collapse_trusted_document_sources(
    answer: str,
    sources: list[SourceItem],
    *,
    max_content_chars: int,
) -> tuple[str, list[SourceItem]]:
    """Expose one file-level source while retaining all retrieved evidence text."""
    if not sources:
        return answer, sources
    unique_contents: list[str] = []
    seen_contents: set[str] = set()
    for source in sources:
        content = source.content.strip()
        if not content or content in seen_contents:
            continue
        seen_contents.add(content)
        unique_contents.append(content)
    merged_content = "\n\n".join(unique_contents)[:max_content_chars].strip()
    collapsed_source = sources[0].model_copy(
        update={
            "id": 1,
            "content": merged_content or sources[0].content,
        }
    )
    normalized_answer = re.sub(r"\[来源\s*\d+\]", "[来源 1]", answer)
    normalized_answer = re.sub(
        r"(?:\s*\[来源 1\]){2,}",
        " [来源 1]",
        normalized_answer,
    ).strip()
    return normalized_answer, [collapsed_source]


def normalize_source_citations(
    answer: str,
    sources: list[SourceItem],
) -> tuple[str, list[SourceItem]]:
    """Make final source IDs contiguous and keep answer citations aligned."""
    if not sources:
        return answer, sources
    old_to_new = {source.id: index for index, source in enumerate(sources, start=1)}
    normalized_sources = [
        source.model_copy(update={"id": index})
        for index, source in enumerate(sources, start=1)
    ]

    def replace(match: re.Match[str]) -> str:
        prefix = "来源 " if match.group(1) else ""
        original_id = int(match.group(2))
        mapped = old_to_new.get(original_id)
        if mapped is None and 1 <= original_id <= len(normalized_sources):
            mapped = original_id
        return f"[{prefix}{mapped}]" if mapped is not None else match.group(0)

    normalized_answer = re.sub(r"\[(来源\s*)?(\d+)\]", replace, answer)
    return normalized_answer, normalized_sources


class RetrievalConfigurationError(RuntimeError):
    pass


class RetrievalQueryError(RuntimeError):
    pass


class EmptyFinalContentError(RetrievalQueryError):
    """The model spent its response budget without producing a final answer."""

    pass


class RetrievalService:
    def __init__(self, config: Settings = settings) -> None:
        self.config = config
        self._query_lock = asyncio.Lock()
        self._active_identifier = ""
        self._active_rag: Any = None

    def readiness(self) -> dict[str, object]:
        effective_model = model_settings_store.effective_generation(self.config)
        return {
            "llm_configured": bool(
                effective_model.base_url
                and effective_model.api_key
                and effective_model.model_name
            ),
            "embedding_configured": self.config.embedding_configured,
            "llm_model": effective_model.model_name,
            "llm_source": effective_model.source,
            "embedding_model": self.config.embedding_model,
            "embedding_dim": self.config.embedding_dim,
            "query_mode": self.config.query_mode,
            "automatic_routing": True,
            "trusted_document_fallback": True,
        }

    async def _call_llm(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        history_messages: list[dict[str, str]] | None = None,
        **kwargs: Any,
    ) -> str:
        from lightrag.llm.openai import openai_complete_if_cache

        effective_model = model_settings_store.effective_generation(self.config)
        api_key = effective_model.api_key
        if not api_key:
            raise RetrievalConfigurationError("服务器没有可用的 LLM API key。")
        if not effective_model.base_url:
            raise RetrievalConfigurationError("服务器没有配置 LLM API 地址。")
        if not effective_model.model_name:
            raise RetrievalConfigurationError("服务器没有配置 LLM 模型名称。")
        result = await openai_complete_if_cache(
            effective_model.model_name,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            api_key=api_key,
            base_url=effective_model.base_url,
            **kwargs,
        )
        if not isinstance(result, str) or not result.strip():
            raise RetrievalQueryError("模型返回了空内容。")
        return result.strip()

    @staticmethod
    def _supports_deepseek_reasoning(base_url: str, model_name: str) -> bool:
        normalized_url = base_url.lower()
        return "api.deepseek.com" in normalized_url

    @staticmethod
    def _message_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""

    async def _request_generation_completion(
        self,
        *,
        messages: list[dict[str, str]],
        enable_reasoning: bool,
        **kwargs: Any,
    ) -> ModelCompletion:
        effective_model = model_settings_store.effective_generation(self.config)
        endpoint = f"{effective_model.base_url.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": effective_model.model_name,
            "messages": messages,
            "stream": False,
            **{key: value for key, value in kwargs.items() if key != "stream"},
        }
        if self._supports_deepseek_reasoning(
            effective_model.base_url,
            effective_model.model_name,
        ):
            payload["thinking"] = {
                "type": "enabled" if enable_reasoning else "disabled"
            }
            if enable_reasoning:
                payload["reasoning_effort"] = "low"
        timeout = httpx.Timeout(150.0, connect=10.0, read=140.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {effective_model.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices else None
        if not isinstance(message, dict):
            raise RetrievalQueryError("模型响应中缺少 message。")
        content = self._message_text(message.get("content")).strip()
        reasoning = sanitize_reasoning_text(
            self._message_text(message.get("reasoning_content"))
        )
        if not content:
            if reasoning:
                raise EmptyFinalContentError(
                    "模型只返回了分析过程，没有生成最终答案。"
                )
            raise RetrievalQueryError("模型返回了空内容。")
        return ModelCompletion(content=content, reasoning=reasoning)

    async def _call_generation_llm(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        history_messages: list[dict[str, str]] | None = None,
        reasoning_enabled: bool | None = None,
        **kwargs: Any,
    ) -> ModelCompletion:
        effective_model = model_settings_store.effective_generation(self.config)
        if not effective_model.api_key:
            raise RetrievalConfigurationError("服务器没有可用的 LLM API key。")
        if not effective_model.base_url:
            raise RetrievalConfigurationError("服务器没有配置 LLM API 地址。")
        if not effective_model.model_name:
            raise RetrievalConfigurationError("服务器没有配置 LLM 模型名称。")
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history_messages or [])
        messages.append({"role": "user", "content": prompt})
        enable_reasoning = (
            self._supports_deepseek_reasoning(
                effective_model.base_url,
                effective_model.model_name,
            )
            if reasoning_enabled is None
            else reasoning_enabled
        )
        if not enable_reasoning:
            return await self._request_generation_completion(
                messages=messages,
                enable_reasoning=False,
                **kwargs,
            )
        try:
            return await self._request_generation_completion(
                messages=messages,
                enable_reasoning=True,
                **kwargs,
            )
        except (httpx.HTTPStatusError, EmptyFinalContentError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {
                400,
                404,
                422,
            }:
                raise
            logger.warning(
                "Reasoning mode did not produce a usable final answer; retrying without it"
            )
            return await self._request_generation_completion(
                messages=messages,
                enable_reasoning=False,
                **kwargs,
            )

    async def classify_intent(self, question: str) -> str:
        completion = await self._call_generation_llm(
            f"用户消息：{question}",
            system_prompt=INTENT_CLASSIFIER_SYSTEM_PROMPT,
            reasoning_enabled=False,
            temperature=0,
            max_tokens=32,
            stream=False,
        )
        return completion.content

    async def generate_chat(
        self,
        question: str,
        history: list[HistoryMessage],
    ) -> RetrievalResult:
        recent_history = [
            {"role": item.role, "content": item.text}
            for item in history[-8:]
            if item.text.strip()
        ]
        generation_mode = "llm"
        generation_error = ""
        reasoning = ""
        try:
            completion = await self._call_generation_llm(
                question.strip(),
                system_prompt=JARVIS_SYSTEM_PROMPT,
                history_messages=recent_history,
                temperature=0.4,
                max_tokens=1000,
                stream=False,
            )
            answer = completion.content
            reasoning = completion.reasoning
        except Exception as exc:
            generation_mode = "safe_fallback"
            generation_error = type(exc).__name__
            logger.exception("Jarvis chat generation failed")
            answer = "我是Jarvis，当前对话模型暂时不可用，请稍后再试。"
        return RetrievalResult(
            answer=answer,
            reasoning=reasoning,
            sources=[],
            metadata={
                "generation_mode": generation_mode,
                "generation_error": generation_error,
            },
        )

    async def _model_select_candidate(
        self,
        question: str,
        candidates: list[RouteCandidate],
    ) -> str | None:
        candidate_lines = [
            f"{item.knowledge_base.identifier}\t{item.knowledge_base.display_name}"
            for item in candidates
        ]
        prompt = (
            "请从候选银行文档中选择最可能回答问题的一份。\n"
            "只能返回 JSON，例如 {\"id\":\"001\"}；无法判断则返回 {\"id\":null}。\n\n"
            f"问题：{question}\n\n候选：\n" + "\n".join(candidate_lines)
        )
        try:
            completion = await self._call_generation_llm(
                prompt,
                system_prompt="你是可信银行知识库的文档路由器，不回答问题，只选择候选文档 ID。",
                reasoning_enabled=False,
                temperature=0,
                max_tokens=160,
                stream=False,
            )
            response = completion.content
        except Exception:
            logger.exception("Model-assisted document routing failed")
            return None

        selected: str | None = None
        try:
            match = re.search(r"\{.*?\}", response, flags=re.DOTALL)
            payload = json.loads(match.group(0) if match else response)
            raw_id = payload.get("id") if isinstance(payload, dict) else None
            selected = str(raw_id).zfill(3) if raw_id is not None else None
        except Exception:
            match = re.search(r"(?<!\d)(\d{1,3})(?!\d)", response)
            selected = match.group(1).zfill(3) if match else None
        valid = {item.knowledge_base.identifier for item in candidates}
        return selected if selected in valid else None

    async def _create_rag(self, knowledge_base: KnowledgeBase) -> Any:
        from lightrag import LightRAG
        from lightrag.llm.openai import openai_embed
        from lightrag.utils import EmbeddingFunc

        if not self.config.embedding_configured:
            raise RetrievalConfigurationError("Embedding API 未配置。")

        async def llm_model_func(
            prompt: str,
            system_prompt: str | None = None,
            history_messages: list[dict[str, str]] | None = None,
            **kwargs: Any,
        ) -> Any:
            return await self._call_llm(
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages,
                **kwargs,
            )

        embedding_func = EmbeddingFunc(
            embedding_dim=self.config.embedding_dim,
            max_token_size=8192,
            func=partial(
                openai_embed.func,
                model=self.config.embedding_model,
                api_key=self.config.embedding_api_key,
                base_url=self.config.embedding_base_url,
            ),
        )
        rag = LightRAG(
            working_dir=str(knowledge_base.storage_dir),
            llm_model_func=llm_model_func,
            embedding_func=embedding_func,
            enable_llm_cache=True,
        )
        await rag.initialize_storages()
        return rag

    async def _get_rag(self, knowledge_base: KnowledgeBase) -> Any:
        if self._active_rag is not None and self._active_identifier == knowledge_base.identifier:
            return self._active_rag
        if self._active_rag is not None:
            try:
                await self._active_rag.finalize_storages()
            except Exception:
                logger.exception("Failed to finalize previous LightRAG instance")
        self._active_rag = await self._create_rag(knowledge_base)
        self._active_identifier = knowledge_base.identifier
        return self._active_rag

    def _hybrid_sources(
        self,
        payload: dict[str, Any],
        knowledge_base: KnowledgeBase,
    ) -> list[SourceItem]:
        data = payload.get("data") if isinstance(payload, dict) else {}
        chunks = data.get("chunks", []) if isinstance(data, dict) else []
        allowed_paths = self._knowledge_base_document_paths(knowledge_base)
        sources: list[SourceItem] = []
        seen: set[tuple[str, str]] = set()
        for chunk in chunks if isinstance(chunks, list) else []:
            if not isinstance(chunk, dict):
                continue
            file_path = str(chunk.get("file_path") or "").strip()
            file_name = Path(file_path).name if file_path else ""
            if not file_name or file_name not in allowed_paths:
                continue
            content = clean_evidence_content(str(chunk.get("content") or ""))
            if not content:
                continue
            title = knowledge_base.display_name
            dedupe_key = (title, content)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            sources.append(
                SourceItem(
                    id=len(sources) + 1,
                    title=title,
                    content=content[: self.config.max_source_chars],
                )
            )
            if len(sources) >= self.config.max_sources:
                break
        return sources

    @staticmethod
    def _knowledge_base_document_paths(knowledge_base: KnowledgeBase) -> set[str]:
        """Resolve the only file names allowed to appear in this collection's evidence."""
        status_path = knowledge_base.storage_dir / "kv_store_doc_status.json"
        document_paths: set[str] = set()
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                document_paths = {
                    Path(str(value.get("file_path"))).name
                    for value in payload.values()
                    if isinstance(value, dict) and value.get("file_path")
                }
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "Unable to read document ownership metadata for %s",
                knowledge_base.identifier,
            )

        expected_suffix = Path(knowledge_base.display_name).suffix.lower()
        expected_file = f"{knowledge_base.identifier}{expected_suffix}"
        if expected_file in document_paths:
            return {expected_file}
        identifier_matches = {
            file_name
            for file_name in document_paths
            if Path(file_name).stem == knowledge_base.identifier
        }
        if identifier_matches:
            return identifier_matches
        if len(document_paths) == 1:
            return document_paths
        return {expected_file}

    @staticmethod
    def _attach_original_files(
        sources: list[SourceItem],
        knowledge_base: KnowledgeBase,
    ) -> list[SourceItem]:
        try:
            metadata = original_viewer_service.source_metadata(knowledge_base.identifier)
        except CorpusCatalogError:
            logger.warning(
                "Original corpus file is unavailable for collection %s",
                knowledge_base.identifier,
            )
            return sources
        source_metadata = {
            **metadata,
            "title": knowledge_base.display_name,
        }
        return [source.model_copy(update=source_metadata) for source in sources]

    async def _query_hybrid(
        self,
        knowledge_base: KnowledgeBase,
        question: str,
        history: list[HistoryMessage],
    ) -> RetrievalResult:
        from lightrag import QueryParam

        conversation_history = [
            {"role": item.role, "content": item.text}
            for item in history[-6:]
            if item.text.strip()
        ]
        params = QueryParam(
            mode=self.config.query_mode,
            response_type="Multiple Paragraphs",
            stream=False,
            top_k=self.config.top_k,
            chunk_top_k=self.config.chunk_top_k,
            enable_rerank=False,
            include_references=True,
            conversation_history=conversation_history,
            history_turns=len(conversation_history),
        )
        async with self._query_lock:
            rag = await self._get_rag(knowledge_base)
            payload = await rag.aquery_llm(question.strip(), param=params)
        if not isinstance(payload, dict) or payload.get("status") != "success":
            message = payload.get("message") if isinstance(payload, dict) else "Unknown retrieval failure"
            raise RetrievalQueryError(str(message))
        answer = sanitize_answer_text(
            str((payload.get("llm_response") or {}).get("content") or "").strip()
        )
        if not answer:
            raise RetrievalQueryError("模型返回了空回答。")
        sources = self._hybrid_sources(payload, knowledge_base)
        if not sources:
            raise RetrievalQueryError("LightRAG 没有返回可展示的来源 chunks。")
        return RetrievalResult(
            answer=answer,
            sources=sources,
            metadata={"retrieval_mode": "hybrid", "query_mode": self.config.query_mode},
        )

    async def _query_trusted_document(
        self,
        route: RouteResult,
        question: str,
        history: list[HistoryMessage],
    ) -> RetrievalResult:
        document = trusted_document_loader.load(route.knowledge_base, question)
        recent_history = [
            {"role": item.role, "content": item.text}
            for item in history[-4:]
            if item.text.strip()
        ]
        prompt = (
            f"用户问题：{question}\n\n"
            "以下内容来自已定位的唯一可信银行文档。请仅依据这些证据回答。"
            "若证据不足，明确说‘当前文档证据不足’，不要使用常识补全。"
            "回答中可用 [来源 1]、[来源 2] 标注依据。\n\n"
            f"{document.context}"
        )
        generation_mode = "llm"
        generation_error = ""
        reasoning = ""
        try:
            completion = await self._call_generation_llm(
                prompt,
                system_prompt=TRUSTED_RAG_SYSTEM_PROMPT,
                history_messages=recent_history,
                temperature=0.1,
                max_tokens=4096,
                stream=False,
            )
            answer = sanitize_answer_text(completion.content)
            reasoning = completion.reasoning
        except Exception as exc:
            generation_mode = "extractive_fallback"
            generation_error = type(exc).__name__
            logger.exception("LLM generation failed; returning trusted extractive evidence")
            excerpts = []
            for source in document.sources[:3]:
                plain = html.unescape(re.sub(r"<[^>]+>", " ", source.content))
                plain = re.sub(r"\s+", " ", plain).strip()
                if plain:
                    excerpts.append(f"[来源 {source.id}] {plain[:900]}")
            answer = (
                f"已自动定位到《{route.knowledge_base.display_name}》。"
                "当前生成模型暂时不可用，下面直接展示可信索引中与问题最相关的原文证据：\n\n"
                + "\n\n".join(excerpts)
                + "\n\n请点击回答下方的来源编号，在右侧查看完整文档证据。"
            )
        return RetrievalResult(
            answer=answer,
            reasoning=reasoning,
            sources=document.sources,
            metadata={
                "retrieval_mode": "trusted_document",
                "target_file": document.target_file,
                "total_document_chunks": document.total_chunks,
                "selected_chunks": document.selected_chunks,
                "foreign_chunks_excluded": document.foreign_chunks,
                "generation_mode": generation_mode,
                "generation_error": generation_error,
            },
        )

    async def answer(
        self,
        question: str,
        history: list[HistoryMessage],
        manual_collection: str = "",
    ) -> RetrievalResult:
        route = await document_router.route(
            question,
            manual_collection=manual_collection,
            model_selector=self._model_select_candidate,
        )
        result: RetrievalResult
        hybrid_error = ""
        if self.config.embedding_configured:
            try:
                result = await self._query_hybrid(route.knowledge_base, question, history)
            except Exception as exc:
                hybrid_error = type(exc).__name__
                logger.exception("Hybrid retrieval failed; using trusted-document fallback")
                result = await self._query_trusted_document(route, question, history)
        else:
            result = await self._query_trusted_document(route, question, history)

        result.metadata.update(
            {
                "collection_id": route.knowledge_base.identifier,
                "collection_name": route.knowledge_base.display_name,
                "routing_method": route.method,
                "routing_confidence": round(route.confidence, 4),
            }
        )
        if route.method == "evidence_reverse":
            result.metadata.update(
                {
                    "routing_evidence_terms": list(route.evidence_terms),
                    "routing_evidence_values": list(route.evidence_values),
                    "routing_evidence_excerpts": list(route.evidence_excerpts),
                }
            )
            if re.search(r"(?:哪个|哪份|什么).*文档|文档.*(?:哪个|哪份|什么)", question):
                result.answer = (
                    f"对应的文档是《{route.knowledge_base.display_name}》。\n\n"
                    f"{result.answer}"
                )
        result.sources = self._attach_original_files(result.sources, route.knowledge_base)
        if result.metadata.get("retrieval_mode") == "trusted_document":
            result.answer, result.sources = collapse_trusted_document_sources(
                result.answer,
                result.sources,
                max_content_chars=self.config.max_source_chars,
            )
        result.answer, result.sources = normalize_source_citations(
            result.answer,
            result.sources,
        )
        if hybrid_error:
            result.metadata["hybrid_fallback_reason"] = hybrid_error
        return result


retrieval_service = RetrievalService()
