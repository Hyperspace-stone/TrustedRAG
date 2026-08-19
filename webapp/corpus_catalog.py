from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

from .knowledge_bases import KnowledgeBaseCatalog, KnowledgeBaseError, catalog
from .settings import Settings, settings


class CorpusCatalogError(ValueError):
    pass


PREVIEW_PRIORITY = {
    ".xlsx": 0,
    ".xls": 1,
    ".pdf": 2,
    ".docx": 3,
    ".doc": 4,
    ".pptx": 5,
    ".txt": 6,
    ".md": 7,
}

MIME_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


@dataclass(frozen=True)
class CorpusVariant:
    name: str
    extension: str
    mime_type: str
    size: int
    path: Path

    def as_public_dict(self, *, preferred: bool = False) -> dict[str, object]:
        return {
            "name": self.name,
            "extension": self.extension,
            "mime_type": self.mime_type,
            "size": self.size,
            "preferred": preferred,
        }


@dataclass(frozen=True)
class CorpusDocument:
    collection_id: str
    display_name: str
    variants: tuple[CorpusVariant, ...]

    @property
    def preferred(self) -> CorpusVariant:
        return self.variants[0]

    def resolve_variant(self, name: str = "") -> CorpusVariant:
        requested = str(name or "").strip()
        if not requested:
            return self.preferred
        for variant in self.variants:
            if variant.name == requested:
                return variant
        raise CorpusCatalogError("请求的原始文件版本不存在。")

    def as_public_dict(self) -> dict[str, object]:
        preferred = self.preferred
        return {
            "collection_id": self.collection_id,
            "display_name": self.display_name,
            "preferred_file_name": preferred.name,
            "file_type": preferred.extension.lstrip("."),
            "mime_type": preferred.mime_type,
            "size": preferred.size,
            "preview_supported": preferred.extension in PREVIEW_PRIORITY,
            "variants": [
                item.as_public_dict(preferred=item.name == preferred.name)
                for item in self.variants
            ],
        }


class CorpusCatalog:
    def __init__(
        self,
        config: Settings = settings,
        kb_catalog: KnowledgeBaseCatalog = catalog,
    ) -> None:
        self.config = config
        self.kb_catalog = kb_catalog

    def _normalize_identifier(self, identifier: str) -> str:
        raw = str(identifier or "").strip()
        if not raw.isdigit() or len(raw) > 3:
            raise CorpusCatalogError("无效的原始文件编号。")
        normalized = raw.zfill(3)
        try:
            self.kb_catalog.resolve(normalized)
        except KnowledgeBaseError as exc:
            raise CorpusCatalogError("原始文件编号不存在。") from exc
        return normalized

    def resolve(self, identifier: str) -> CorpusDocument:
        normalized = self._normalize_identifier(identifier)
        root = self.config.corpus_root.resolve()
        if not root.is_dir():
            raise CorpusCatalogError("原始文件目录不可用。")
        variants: list[CorpusVariant] = []
        for candidate in root.iterdir():
            if candidate.stem != normalized or not candidate.is_file() or candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            if resolved.parent != root:
                continue
            extension = resolved.suffix.lower()
            if extension not in PREVIEW_PRIORITY:
                continue
            variants.append(
                CorpusVariant(
                    name=resolved.name,
                    extension=extension,
                    mime_type=MIME_TYPES.get(extension)
                    or mimetypes.guess_type(resolved.name)[0]
                    or "application/octet-stream",
                    size=resolved.stat().st_size,
                    path=resolved,
                )
            )
        if not variants:
            raise CorpusCatalogError("该知识库没有对应的原始文件。")
        variants.sort(key=lambda item: (PREVIEW_PRIORITY[item.extension], item.name))
        knowledge_base = self.kb_catalog.resolve(normalized)
        return CorpusDocument(
            collection_id=normalized,
            display_name=knowledge_base.display_name,
            variants=tuple(variants),
        )

    def list(self) -> list[CorpusDocument]:
        return [self.resolve(item.identifier) for item in self.kb_catalog.list()]


corpus_catalog = CorpusCatalog()
