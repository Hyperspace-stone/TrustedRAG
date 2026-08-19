from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import threading

from .settings import Settings, settings


class KnowledgeBaseError(ValueError):
    pass


@dataclass(frozen=True)
class KnowledgeBase:
    identifier: str
    display_name: str
    storage_dir: Path

    def as_frontend_entry(self) -> dict[str, object]:
        return {
            "name": self.identifier,
            "display_name": self.display_name,
            "path": f"rag_storage/{self.identifier}",
            "category": "collection",
            "type": "collection",
        }


class KnowledgeBaseCatalog:
    def __init__(self, config: Settings = settings) -> None:
        self.config = config
        self._by_identifier: dict[str, KnowledgeBase] | None = None
        self._lock = threading.RLock()

    def _load(self) -> dict[str, KnowledgeBase]:
        payload = json.loads(self.config.name_map_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise KnowledgeBaseError("name2id.json must contain an object")

        storage_root = self.config.storage_root.resolve()
        result: dict[str, KnowledgeBase] = {}
        for raw_name, raw_identifier in payload.items():
            unpadded_identifier = str(raw_identifier).strip()
            display_name = str(raw_name).strip()
            if not unpadded_identifier.isdigit() or not display_name:
                continue
            identifier = unpadded_identifier.zfill(3)
            storage_dir = (storage_root / identifier).resolve()
            if storage_dir.parent != storage_root or not storage_dir.is_dir():
                continue
            result[identifier] = KnowledgeBase(
                identifier=identifier,
                display_name=display_name,
                storage_dir=storage_dir,
            )
        return result

    @property
    def by_identifier(self) -> dict[str, KnowledgeBase]:
        with self._lock:
            if self._by_identifier is None:
                self._by_identifier = self._load()
            return self._by_identifier

    def invalidate(self) -> None:
        with self._lock:
            self._by_identifier = None

    def list(self) -> list[KnowledgeBase]:
        return sorted(self.by_identifier.values(), key=lambda item: int(item.identifier))

    def resolve(self, identifier: str) -> KnowledgeBase:
        normalized = str(identifier or "").strip()
        if not normalized.isdigit():
            raise KnowledgeBaseError("Invalid knowledge-base identifier")
        knowledge_base = self.by_identifier.get(normalized)
        if knowledge_base is None:
            raise KnowledgeBaseError("Knowledge base is unavailable")
        return knowledge_base


catalog = KnowledgeBaseCatalog()
