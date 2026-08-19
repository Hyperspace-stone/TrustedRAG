from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .knowledge_bases import KnowledgeBase, KnowledgeBaseCatalog, catalog
from .settings import Settings, settings


logger = logging.getLogger(__name__)


REPORTING_BASIS_TERMS = (
    "本年累计",
    "截至当期",
    "截止当期",
    "当期",
    "本期",
    "期末",
    "月末",
    "年末",
    "季度末",
)
QUESTION_STOP_TERMS = {
    "哪个文档",
    "哪份文档",
    "是什么文档",
    "对应文档",
    "数值",
    "口径",
    "的是",
    "多少",
    "什么",
}
TEXT_INDEX_NAME = "kv_store_text_chunks.json"
GRAPH_INDEX_NAMES = (
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
)
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])")
QUOTED_PATTERN = re.compile(r"[《“\"]([^》”\"]+)[》”\"]")
DOMAIN_PATTERN = re.compile(
    r"[\u4e00-\u9fff]{2,16}(?:保费收入|保险金额|赔付支出|贷款余额|贷款|总资产|总负债|"
    r"资产|负债|余额|收入|支出|金额|利润|资本充足率|不良贷款率|比例|增长率)"
)


def _normalized_text(value: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9.%-]+", str(value or "").lower()))


def _unique(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = value.strip(" ：:，,。？?；;()（）[]【】")
        normalized = _normalized_text(clean)
        if not clean or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(clean)
    return tuple(result)


def _number_variants(raw: str) -> tuple[str, ...]:
    compact = raw.replace(",", "").strip()
    variants = [compact]
    try:
        value = Decimal(compact)
    except InvalidOperation:
        return _unique(variants)
    normalized = format(value, "f")
    variants.append(normalized)
    if "." in normalized:
        variants.append(normalized.rstrip("0").rstrip("."))
    if value == value.to_integral_value():
        integer = str(int(value))
        variants.extend((integer, f"{integer}.0"))
    return _unique(variants)


@dataclass(frozen=True)
class EvidenceConstraints:
    numbers: tuple[str, ...]
    phrases: tuple[str, ...]
    reporting_basis: tuple[str, ...]
    years: tuple[str, ...]
    months: tuple[str, ...]

    @property
    def search_needles(self) -> tuple[str, ...]:
        if self.numbers:
            return self.numbers
        return tuple(sorted(self.phrases, key=len, reverse=True)[:2])


@dataclass(frozen=True)
class EvidenceCandidate:
    knowledge_base: KnowledgeBase
    score: float
    matched_numbers: tuple[str, ...]
    matched_terms: tuple[str, ...]
    excerpts: tuple[str, ...]
    graph_support: tuple[str, ...] = ()


def extract_evidence_constraints(question: str) -> EvidenceConstraints:
    numbers: list[str] = []
    for match in NUMBER_PATTERN.finditer(question):
        raw = match.group(0)
        # Years and source display indexes are contextual metadata, not value anchors.
        if re.fullmatch(r"20\d{2}", raw) and f"{raw}年" in question:
            continue
        prefix = question[max(0, match.start() - 4) : match.start()]
        if re.search(r"(?:来源|索引)\s*$", prefix):
            continue
        numbers.extend(_number_variants(raw))

    quoted: list[str] = []
    for match in QUOTED_PATTERN.finditer(question):
        value = match.group(1).strip()
        if value.startswith("20") and ("情况表" in value or "表" in value):
            continue
        quoted.extend(part.strip() for part in re.split(r"[/／、]", value) if part.strip())

    basis = [term for term in REPORTING_BASIS_TERMS if term in question]
    domains = [match.group(0) for match in DOMAIN_PATTERN.finditer(question)]
    phrases = [
        item
        for item in quoted + domains
        if item not in QUESTION_STOP_TERMS and item not in basis and len(_normalized_text(item)) >= 2
    ]
    years = re.findall(r"20\d{2}年", question)
    months = re.findall(r"(?<!\d)(?:1[0-2]|[1-9])月", question)
    return EvidenceConstraints(
        numbers=_unique(numbers),
        phrases=_unique(phrases),
        reporting_basis=_unique(basis),
        years=_unique(years),
        months=_unique(months),
    )


def _iter_records(payload: object):
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            yield from (item for item in data if isinstance(item, dict))
            return
        yield from (item for item in payload.values() if isinstance(item, dict))
    elif isinstance(payload, list):
        yield from (item for item in payload if isinstance(item, dict))


def _record_text(record: dict) -> str:
    values: list[str] = []
    for key in (
        "content",
        "text",
        "entity_name",
        "src_id",
        "tgt_id",
        "description",
        "keywords",
        "file_path",
    ):
        value = record.get(key)
        if isinstance(value, (str, int, float)):
            values.append(str(value))
        elif isinstance(value, list):
            values.extend(str(item) for item in value if isinstance(item, (str, int, float)))
    return "\n".join(values)


def _contains_number(text: str, variants: tuple[str, ...]) -> tuple[str, ...]:
    available: set[str] = set()
    for match in NUMBER_PATTERN.finditer(text):
        available.update(_number_variants(match.group(0)))
    return tuple(value for value in variants if value and value in available)


def _excerpt(text: str, needles: tuple[str, ...], limit: int) -> str:
    compact = " ".join(text.split())
    positions = [compact.find(needle) for needle in needles if needle and compact.find(needle) >= 0]
    position = min(positions) if positions else 0
    start = max(0, position - limit // 3)
    return compact[start : start + limit]


class EvidenceRouter:
    def __init__(
        self,
        kb_catalog: KnowledgeBaseCatalog = catalog,
        config: Settings = settings,
    ) -> None:
        self.catalog = kb_catalog
        self.config = config
        self._query_cache: dict[
            tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]],
            tuple[float, tuple[EvidenceCandidate, ...]],
        ] = {}

    def _read_records(self, path: Path) -> list[str]:
        if not path.is_file() or path.stat().st_size > self.config.evidence_parse_max_bytes:
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to read reverse evidence index %s", path)
            return []
        return [text for record in _iter_records(payload) if (text := _record_text(record)).strip()]

    @staticmethod
    def _stream_contains(path: Path, needles: tuple[str, ...]) -> bool:
        encoded = tuple(needle.encode("utf-8") for needle in needles if needle)
        if not encoded:
            return False
        overlap = max(len(item) for item in encoded) - 1
        previous = b""
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    block = previous + chunk
                    if any(needle in block for needle in encoded):
                        return True
                    previous = block[-overlap:] if overlap > 0 else b""
        except OSError:
            return False
        return False

    def _stream_excerpt(self, path: Path, needles: tuple[str, ...]) -> str:
        encoded = tuple(needle.encode("utf-8") for needle in needles if needle)
        if not encoded:
            return ""
        context = max(self.config.evidence_excerpt_chars * 3, 4096)
        overlap = context
        previous = b""
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    block = previous + chunk
                    positions = [block.find(needle) for needle in encoded if block.find(needle) >= 0]
                    if positions:
                        position = min(positions)
                        start = max(0, position - context // 2)
                        return block[start : position + context].decode("utf-8", errors="ignore")
                    previous = block[-overlap:]
        except OSError:
            return ""
        return ""

    def _graph_support(self, knowledge_base: KnowledgeBase, constraints: EvidenceConstraints) -> tuple[str, ...]:
        support: list[str] = []
        needles = constraints.phrases + constraints.reporting_basis
        for filename in GRAPH_INDEX_NAMES:
            for text in self._read_records(knowledge_base.storage_dir / filename):
                normalized = _normalized_text(text)
                for term in needles:
                    if _normalized_text(term) in normalized:
                        support.append(term)
        return _unique(support)

    def _score_record(
        self,
        text: str,
        knowledge_base: KnowledgeBase,
        constraints: EvidenceConstraints,
    ) -> EvidenceCandidate | None:
        normalized = _normalized_text(text)
        matched_numbers = _contains_number(text, constraints.numbers)
        matched_phrases = tuple(
            term for term in constraints.phrases if _normalized_text(term) in normalized
        )
        matched_basis = tuple(
            term for term in constraints.reporting_basis if _normalized_text(term) in normalized
        )
        matched_terms = _unique(list(matched_phrases + matched_basis))

        if constraints.numbers and not matched_numbers:
            return None
        if constraints.numbers and not matched_terms:
            return None
        if not constraints.numbers and not matched_phrases:
            return None

        score = 0.0
        if matched_numbers:
            score += 5.0
        score += min(6.0, 2.5 * len(matched_phrases))
        score += min(3.0, 1.5 * len(matched_basis))

        title = knowledge_base.display_name
        for year in constraints.years:
            if year in title or year in text:
                score += 1.0
            elif re.search(r"20\d{2}年", title):
                score -= 1.5
        for month in constraints.months:
            if month in title or month in text:
                score += 0.75
            elif re.search(r"(?:1[0-2]|[1-9])月", title):
                score -= 1.0

        graph_support = self._graph_support(knowledge_base, constraints)
        score += min(1.0, 0.25 * len(graph_support))
        needles = constraints.numbers + matched_terms
        return EvidenceCandidate(
            knowledge_base=knowledge_base,
            score=max(0.0, score),
            matched_numbers=matched_numbers,
            matched_terms=matched_terms,
            excerpts=(_excerpt(text, needles, self.config.evidence_excerpt_chars),),
            graph_support=graph_support,
        )

    def _scan_tier(
        self,
        constraints: EvidenceConstraints,
        *,
        oversized: bool,
    ) -> list[EvidenceCandidate]:
        needles = constraints.search_needles
        candidates: list[EvidenceCandidate] = []
        for knowledge_base in self.catalog.list():
            path = knowledge_base.storage_dir / TEXT_INDEX_NAME
            if not path.is_file():
                continue
            size = path.stat().st_size
            is_oversized = size > self.config.evidence_parse_max_bytes
            if is_oversized != oversized:
                continue
            if not self._stream_contains(path, needles):
                continue
            if is_oversized:
                excerpt = self._stream_excerpt(path, needles)
                scored = self._score_record(excerpt, knowledge_base, constraints) if excerpt else None
                if scored is not None:
                    candidates.append(scored)
                continue
            best: EvidenceCandidate | None = None
            for text in self._read_records(path):
                scored = self._score_record(text, knowledge_base, constraints)
                if scored is not None and (best is None or scored.score > best.score):
                    best = scored
            if best is not None:
                candidates.append(best)
        return candidates

    def candidates(self, question: str) -> tuple[EvidenceCandidate, ...]:
        constraints = extract_evidence_constraints(question)
        if not constraints.search_needles or not (constraints.phrases or constraints.reporting_basis):
            return ()
        cache_key = (constraints.numbers, constraints.phrases, constraints.reporting_basis)
        cached = self._query_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] <= self.config.evidence_cache_ttl_seconds:
            return cached[1]

        started = time.perf_counter()
        result = self._scan_tier(constraints, oversized=False)
        if not result:
            result = self._scan_tier(constraints, oversized=True)
        result.sort(key=lambda item: (-item.score, int(item.knowledge_base.identifier)))
        bounded = tuple(result[: self.config.evidence_max_candidates])
        self._query_cache[cache_key] = (now, bounded)
        logger.info(
            "Reverse evidence search candidates=%s elapsed_ms=%.1f numbers=%s terms=%s",
            len(bounded),
            (time.perf_counter() - started) * 1000,
            constraints.numbers,
            constraints.phrases + constraints.reporting_basis,
        )
        return bounded

    def route(self, question: str) -> EvidenceCandidate | None:
        candidates = self.candidates(question)
        if not candidates:
            return None
        top = candidates[0]
        second_score = candidates[1].score if len(candidates) > 1 else 0.0
        if (
            top.score >= self.config.evidence_min_score
            and top.score - second_score >= self.config.evidence_min_margin
        ):
            return top
        return None


evidence_router = EvidenceRouter()
