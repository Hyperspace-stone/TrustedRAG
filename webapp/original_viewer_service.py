from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi.responses import FileResponse, StreamingResponse

from .corpus_catalog import CorpusCatalog, CorpusCatalogError, CorpusVariant, corpus_catalog
from .output_pdf_catalog import OutputPdfCatalog, OutputPdfCatalogError, output_pdf_catalog
from .settings import Settings, settings


class OriginalViewerError(RuntimeError):
    pass


class OriginalViewerRangeError(ValueError):
    pass


class OriginalViewerService:
    def __init__(
        self,
        config: Settings = settings,
        corpus: CorpusCatalog = corpus_catalog,
        output_pdfs: OutputPdfCatalog = output_pdf_catalog,
    ) -> None:
        self.config = config
        self.corpus = corpus
        self.output_pdfs = output_pdfs

    def metadata(self, collection_id: str) -> dict[str, Any]:
        document = self.corpus.resolve(collection_id)
        preview_pdf = self.output_pdfs.resolve(document.collection_id)
        preview_kind = "pdf" if preview_pdf else (
            "xlsx" if document.preferred.extension == ".xlsx" else "unavailable"
        )
        payload = document.as_public_dict()
        payload.update(
            {
                "preview_kind": preview_kind,
                "preview_available": preview_kind != "unavailable",
                "preview_file_name": preview_pdf.file_name if preview_pdf else document.preferred.name,
                "preview_size": preview_pdf.size if preview_pdf else document.preferred.size,
                "preview_selection_method": preview_pdf.selection_method if preview_pdf else "xlsx_fallback",
                "preview_url": (
                    f"/api/corpus/{document.collection_id}/preview"
                    if preview_pdf
                    else f"/api/corpus/{document.collection_id}/content"
                ),
                "metadata_url": f"/api/corpus/{document.collection_id}/metadata",
                "content_url": f"/api/corpus/{document.collection_id}/content",
                "download_url": f"/api/corpus/{document.collection_id}/download",
            }
        )
        return payload

    def source_metadata(self, collection_id: str) -> dict[str, str | None]:
        metadata = self.metadata(collection_id)
        return {
            "collection_id": str(metadata["collection_id"]),
            "preview_kind": str(metadata["preview_kind"]),
            "preview_file_name": str(metadata["preview_file_name"]),
            "preview_metadata_url": str(metadata["metadata_url"]),
            "preview_content_url": str(metadata["content_url"]),
            "preview_file_url": (
                str(metadata["preview_url"])
                if metadata["preview_kind"] == "pdf"
                else None
            ),
            "original_file_name": str(metadata["preferred_file_name"]),
            "original_file_type": str(metadata["file_type"]),
            "original_metadata_url": str(metadata["metadata_url"]),
            "original_content_url": str(metadata["content_url"]),
            "original_file_url": None,
            "original_download_url": str(metadata["download_url"]),
        }

    def xlsx_content(
        self,
        collection_id: str,
        *,
        sheet: str = "",
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        document = self.corpus.resolve(collection_id)
        if self.output_pdfs.resolve(document.collection_id) is not None:
            return {
                "status": "ok",
                "kind": "pdf",
                "collection_id": document.collection_id,
                "preview_url": f"/api/corpus/{document.collection_id}/preview",
                "download_url": f"/api/corpus/{document.collection_id}/download",
            }
        variant = document.preferred
        if variant.extension != ".xlsx":
            return {
                "status": "preview_unavailable",
                "kind": "unavailable",
                "message": "该知识库没有生成 PDF，原始文件也不是可回退预览的 XLSX。",
                "collection_id": document.collection_id,
                "download_url": f"/api/corpus/{document.collection_id}/download",
            }
        return self._preview_xlsx(
            document.collection_id,
            variant,
            sheet=sheet,
            offset=max(0, int(offset)),
            limit=min(max(int(limit or self.config.corpus_preview_rows), 1), 200),
        )

    def preview_response(
        self,
        collection_id: str,
        *,
        range_header: str = "",
    ) -> FileResponse | StreamingResponse:
        preview_pdf = self.output_pdfs.resolve(collection_id)
        if preview_pdf is None:
            raise OriginalViewerError("该知识库没有生成 PDF 预览。")
        return self._file_response(
            preview_pdf.path,
            file_name=preview_pdf.file_name,
            media_type="application/pdf",
            range_header=range_header,
            disposition_type="inline",
        )

    def download_response(
        self,
        collection_id: str,
        *,
        variant_name: str = "",
        range_header: str = "",
    ) -> FileResponse | StreamingResponse:
        document = self.corpus.resolve(collection_id)
        variant = document.resolve_variant(variant_name)
        return self._file_response(
            variant.path,
            file_name=variant.name,
            media_type=variant.mime_type,
            range_header=range_header,
            disposition_type="attachment",
        )

    def _preview_xlsx(
        self,
        collection_id: str,
        variant: CorpusVariant,
        *,
        sheet: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        from openpyxl import load_workbook

        workbook = load_workbook(variant.path, read_only=True, data_only=True, keep_links=False)
        try:
            sheet_names = list(workbook.sheetnames)
            if not sheet_names:
                raise OriginalViewerError("XLSX 文件没有可用工作表。")
            if sheet and sheet not in sheet_names:
                raise OriginalViewerError("请求的工作表不存在。")
            selected_name = sheet or sheet_names[0]
            worksheet = workbook[selected_name]
            rows: list[list[str]] = []
            for row_index, values in enumerate(worksheet.iter_rows(values_only=True)):
                if row_index < offset:
                    continue
                if len(rows) >= limit:
                    break
                rows.append([self._cell_text(value) for value in values])
            return {
                "status": "ok",
                "kind": "xlsx",
                "collection_id": collection_id,
                "file_name": variant.name,
                "sheet": selected_name,
                "sheets": sheet_names,
                "offset": offset,
                "limit": limit,
                "total_rows": int(worksheet.max_row or 0),
                "total_columns": int(worksheet.max_column or 0),
                "rows": rows,
                "download_url": f"/api/corpus/{collection_id}/download",
            }
        finally:
            workbook.close()

    def _cell_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dt.date, dt.datetime, dt.time)):
            return value.isoformat()
        return str(value).replace("\x00", "")[: self.config.corpus_max_cell_chars]

    def _file_response(
        self,
        path: Path,
        *,
        file_name: str,
        media_type: str,
        range_header: str,
        disposition_type: str,
    ) -> FileResponse | StreamingResponse:
        size = path.stat().st_size
        headers = {
            "Accept-Ranges": "bytes",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        }
        if not range_header:
            return FileResponse(
                path,
                media_type=media_type,
                filename=file_name,
                content_disposition_type=disposition_type,
                headers=headers,
            )
        start, end = self._parse_range(range_header, size)
        length = end - start + 1
        return StreamingResponse(
            self._iter_range(path, start, length),
            status_code=206,
            media_type=media_type,
            headers={
                **headers,
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
                "Content-Disposition": (
                    f"{disposition_type}; filename*=UTF-8''{quote(file_name)}"
                ),
            },
        )

    @staticmethod
    def _parse_range(value: str, size: int) -> tuple[int, int]:
        raw = str(value or "").strip()
        if not raw.startswith("bytes=") or "," in raw:
            raise OriginalViewerRangeError("无效的 Range 请求。")
        start_text, separator, end_text = raw[6:].partition("-")
        if not separator:
            raise OriginalViewerRangeError("无效的 Range 请求。")
        try:
            if not start_text:
                suffix = int(end_text)
                if suffix <= 0:
                    raise ValueError
                start = max(0, size - suffix)
                end = size - 1
            else:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
        except ValueError as exc:
            raise OriginalViewerRangeError("无效的 Range 请求。") from exc
        if start < 0 or start >= size or end < start:
            raise OriginalViewerRangeError("Range 超出文件范围。")
        return start, min(end, size - 1)

    @staticmethod
    def _iter_range(path: Path, start: int, length: int) -> Iterator[bytes]:
        remaining = length
        with path.open("rb") as stream:
            stream.seek(start)
            while remaining > 0:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk


original_viewer_service = OriginalViewerService()
