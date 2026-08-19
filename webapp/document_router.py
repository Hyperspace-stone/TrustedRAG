from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .evidence_router import EvidenceRouter, evidence_router
from .knowledge_bases import KnowledgeBase, KnowledgeBaseCatalog, KnowledgeBaseError, catalog
from .settings import Settings, settings


class DocumentRoutingError(LookupError):
    def __init__(self, message: str, candidates: list["RouteCandidate"] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


@dataclass(frozen=True)
class RouteCandidate:
    knowledge_base: KnowledgeBase
    score: float


@dataclass(frozen=True)
class RouteResult:
    knowledge_base: KnowledgeBase
    method: str
    confidence: float
    candidates: tuple[RouteCandidate, ...] = ()
    evidence_terms: tuple[str, ...] = ()
    evidence_values: tuple[str, ...] = ()
    evidence_excerpts: tuple[str, ...] = ()


ModelSelector = Callable[[str, list[RouteCandidate]], Awaitable[str | None]]


GENERIC_TERMS = {
    "请问",
    "请",
    "根据",
    "回答",
    "问题",
    "数据",
    "情况",
    "情况表",
    "统计",
    "分析",
    "指标",
    "列出",
    "概括",
    "说明",
    "什么",
    "多少",
    "如何",
}


def normalize_text(value: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", str(value or "").lower()))


def corpus_title(display_name: str) -> str:
    stem = Path(display_name).stem
    stem = re.sub(r"^\d{3}_", "", stem)
    parts = [part.strip() for part in stem.split("_") if part.strip()]
    if len(parts) >= 2 and normalize_text(parts[0]) == normalize_text(parts[1]):
        return parts[0]
    return "_".join(parts) if parts else stem


def extract_book_title(question: str) -> str | None:
    match = re.search(r"《(.*?)》", question)
    return match.group(1).strip() if match else None


def extract_explicit_collection_id(question: str) -> str | None:
    """Accept an index number only when the user labels it as an identifier."""
    match = re.search(
        r"(?:知识库|索引|文档|collection)\s*(?:编号|序号|id)?\s*[:：#]?\s*(\d{3})(?!\d)",
        question,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def _terms(value: str) -> set[str]:
    normalized = normalize_text(value)
    result: set[str] = set()
    for token in re.findall(r"\d{4}年|\d{1,2}月|\d+(?:\.\d+)?|[A-Za-z]+|[\u4e00-\u9fff]{2,}", value):
        clean = normalize_text(token)
        if clean and clean not in GENERIC_TERMS:
            result.add(clean)
    for size in (2, 3, 4):
        if len(normalized) < size:
            continue
        result.update(normalized[index : index + size] for index in range(len(normalized) - size + 1))
    return result


def _score(question: str, knowledge_base: KnowledgeBase) -> float:
    query = normalize_text(question)
    title = normalize_text(corpus_title(knowledge_base.display_name))
    if not query or not title:
        return 0.0
    if title in query:
        return 1.0
    if len(query) >= 6 and query in title:
        return 0.92

    query_terms = _terms(question)
    title_terms = _terms(corpus_title(knowledge_base.display_name))
    if not query_terms or not title_terms:
        return 0.0
    overlap = query_terms & title_terms
    weighted_overlap = sum(1.0 + min(len(term), 8) / 8 for term in overlap)
    title_weight = sum(1.0 + min(len(term), 8) / 8 for term in title_terms)
    query_weight = sum(1.0 + min(len(term), 8) / 8 for term in query_terms)
    containment = weighted_overlap / max(title_weight, 1.0)
    dice = 2 * weighted_overlap / max(title_weight + query_weight, 1.0)

    years = set(re.findall(r"20\d{2}年", question))
    title_years = set(re.findall(r"20\d{2}年", corpus_title(knowledge_base.display_name)))
    months = set(re.findall(r"\d{1,2}月", question))
    title_months = set(re.findall(r"\d{1,2}月", corpus_title(knowledge_base.display_name)))
    date_bonus = 0.0
    if years:
        date_bonus += 0.12 if years & title_years else -0.10
    if months:
        date_bonus += 0.08 if months & title_months else -0.06

    return max(0.0, min(0.89, 0.62 * containment + 0.38 * dice + date_bonus))


class DocumentRouter:
    def __init__(
        self,
        kb_catalog: KnowledgeBaseCatalog = catalog,
        config: Settings = settings,
        reverse_router: EvidenceRouter | None = None,
    ) -> None:
        self.catalog = kb_catalog
        self.config = config
        self.reverse_router = reverse_router or (
            evidence_router
            if kb_catalog is catalog and config is settings
            else EvidenceRouter(kb_catalog, config)
        )

    def shortlist(self, question: str) -> list[RouteCandidate]:
        candidates = [
            RouteCandidate(knowledge_base=item, score=_score(question, item))
            for item in self.catalog.list()
        ]
        candidates.sort(key=lambda item: (-item.score, int(item.knowledge_base.identifier)))
        return candidates[: self.config.route_shortlist_size]

    def _match_target(self, target: str) -> KnowledgeBase | None:
        normalized_target = normalize_text(target)
        if not normalized_target:
            return None
        candidates = []
        for knowledge_base in self.catalog.list():
            title = normalize_text(corpus_title(knowledge_base.display_name))
            if normalized_target in title or title in normalized_target:
                candidates.append(knowledge_base)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (len(corpus_title(item.display_name)), int(item.identifier)))
        return candidates[0]

    async def route(
        self,
        question: str,
        manual_collection: str = "",
        model_selector: ModelSelector | None = None,
    ) -> RouteResult:
        # Routing rules: semantic titles are authoritative; numeric identifiers
        # are only internal locators and must never be inferred from ordinary data.
        book_title = extract_book_title(question)
        if book_title:
            matched = self._match_target(book_title)
            if matched:
                return RouteResult(matched, "book_title", 1.0)
            raise DocumentRoutingError(f"未找到标题为《{book_title}》的知识库文档。")

        direct = self._match_target(question)
        if direct:
            return RouteResult(direct, "contained_title", 0.98)

        if manual_collection:
            return RouteResult(
                knowledge_base=self.catalog.resolve(manual_collection),
                method="manual",
                confidence=1.0,
            )

        explicit_id = extract_explicit_collection_id(question)
        if explicit_id:
            try:
                knowledge_base = self.catalog.resolve(explicit_id)
                return RouteResult(knowledge_base, "explicit_id", 1.0)
            except KnowledgeBaseError:
                raise DocumentRoutingError(f"知识库编号 {explicit_id} 不存在。") from None

        shortlist = self.shortlist(question)
        top = shortlist[0] if shortlist else None
        margin = (
            top.score - (shortlist[1].score if len(shortlist) > 1 else 0.0)
            if top is not None
            else 0.0
        )
        if (
            top is not None
            and top.score >= self.config.route_min_score
            and margin >= self.config.route_min_margin
        ):
            return RouteResult(
                top.knowledge_base,
                "lexical",
                min(0.95, top.score),
                tuple(shortlist),
            )

        reverse = self.reverse_router.route(question)
        reverse_candidates = self.reverse_router.candidates(question)
        if reverse is not None:
            return RouteResult(
                reverse.knowledge_base,
                "evidence_reverse",
                min(0.99, reverse.score / 14.0),
                tuple(
                    RouteCandidate(item.knowledge_base, item.score)
                    for item in reverse_candidates
                ),
                evidence_terms=reverse.matched_terms + reverse.graph_support,
                evidence_values=reverse.matched_numbers,
                evidence_excerpts=reverse.excerpts,
            )

        if model_selector is not None and shortlist and shortlist[0].score > 0:
            selected_id = await model_selector(question, shortlist)
            if selected_id:
                for candidate in shortlist:
                    if candidate.knowledge_base.identifier == selected_id:
                        confidence = max(0.55, min(0.90, candidate.score + 0.25))
                        return RouteResult(
                            candidate.knowledge_base,
                            "model_shortlist",
                            confidence,
                            tuple(shortlist),
                        )

        if reverse_candidates:
            raise DocumentRoutingError(
                "检索到了多份含相关数值和指标的文档，但当前证据不足以可靠区分。",
                [
                    RouteCandidate(item.knowledge_base, item.score)
                    for item in reverse_candidates[:3]
                ],
            )
        if not shortlist or shortlist[0].score <= 0:
            raise DocumentRoutingError("未在可信索引中找到与问题证据相符的银行文档。")
        raise DocumentRoutingError(
            "无法可靠定位唯一文档，且反向证据检索未得到唯一结果。",
            shortlist[:3],
        )


document_router = DocumentRouter()
