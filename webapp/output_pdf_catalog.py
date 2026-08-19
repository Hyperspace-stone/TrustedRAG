from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .knowledge_bases import KnowledgeBaseCatalog, KnowledgeBaseError, catalog
from .settings import Settings, settings


class OutputPdfCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class OutputPdf:
    collection_id: str
    file_name: str
    size: int
    selection_method: str
    path: Path

    def as_public_dict(self) -> dict[str, object]:
        return {
            "file_name": self.file_name,
            "size": self.size,
            "selection_method": self.selection_method,
        }


class OutputPdfCatalog:
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
            raise OutputPdfCatalogError("无效的 PDF 预览编号。")
        normalized = raw.zfill(3)
        try:
            self.kb_catalog.resolve(normalized)
        except KnowledgeBaseError as exc:
            raise OutputPdfCatalogError("PDF 预览编号不存在。") from exc
        return normalized

    def resolve(self, identifier: str) -> OutputPdf | None:
        normalized = self._normalize_identifier(identifier)
        root = self.config.output_root.resolve()
        collection_root = (root / normalized).resolve()
        if collection_root.parent != root or not collection_root.is_dir() or collection_root.is_symlink():
            return None

        direct = collection_root / f"{normalized}.pdf"
        if self._is_safe_pdf(direct, collection_root):
            return self._result(normalized, direct, "direct")

        origin = self._candidates(collection_root, f"{normalized}_origin.pdf")
        if origin:
            return self._result(normalized, origin[0], "origin")

        layout = self._candidates(collection_root, f"{normalized}_layout.pdf")
        if layout:
            return self._result(normalized, layout[0], "layout")
        return None

    @staticmethod
    def _is_safe_pdf(path: Path, collection_root: Path) -> bool:
        if not path.is_file() or path.is_symlink() or path.suffix.lower() != ".pdf":
            return False
        resolved = path.resolve()
        try:
            resolved.relative_to(collection_root)
        except ValueError:
            return False
        return True

    def _candidates(self, collection_root: Path, file_name: str) -> list[Path]:
        candidates = [
            path.resolve()
            for path in collection_root.rglob(file_name)
            if self._is_safe_pdf(path, collection_root)
        ]
        candidates.sort(
            key=lambda path: (
                len(path.relative_to(collection_root).parts),
                str(path.relative_to(collection_root)),
            )
        )
        return candidates

    @staticmethod
    def _result(identifier: str, path: Path, method: str) -> OutputPdf:
        return OutputPdf(
            collection_id=identifier,
            file_name=path.name,
            size=path.stat().st_size,
            selection_method=method,
            path=path,
        )


output_pdf_catalog = OutputPdfCatalog()
