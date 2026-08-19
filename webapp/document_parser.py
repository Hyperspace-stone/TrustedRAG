from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class DocumentParseError(ValueError):
    def __init__(self, message: str, *, code: str = "UNPARSEABLE_DOCUMENT") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ContentBlock:
    type: str
    text: str
    page_idx: int = 0
    sheet: str = ""
    cell_range: str = ""
    table_body: str = ""

    def as_content_item(self) -> dict[str, Any]:
        if self.type == "table":
            return {
                "type": "table",
                "table_body": self.table_body,
                "table_caption": [
                    part
                    for part in (
                        f"工作表：{self.sheet}" if self.sheet else "",
                        f"单元格：{self.cell_range}" if self.cell_range else "",
                    )
                    if part
                ],
                "table_footnote": [],
                "page_idx": self.page_idx,
            }
        return {
            "type": "text",
            "text": self.text,
            "text_level": 1 if self.type == "title" else 0,
            "page_idx": self.page_idx,
        }


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    source_name: str
    extension: str
    blocks: tuple[ContentBlock, ...]

    @property
    def searchable_text(self) -> str:
        values = [self.title]
        values.extend(block.text for block in self.blocks if block.text.strip())
        return "\n\n".join(values).strip()

    def content_list(self) -> list[dict[str, Any]]:
        result = [{"type": "text", "text": self.title, "text_level": 1, "page_idx": 0}]
        result.extend(block.as_content_item() for block in self.blocks)
        return result

    def as_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "title": self.title,
            "source_name": self.source_name,
            "extension": self.extension,
            "blocks": [asdict(block) for block in self.blocks],
        }


def _clean_title(path: Path) -> str:
    title = path.stem.strip()
    if len(title) > 4 and title[:3].isdigit() and title[3] in {"_", "-", " "}:
        title = title[4:].strip()
    return title or path.name


def _table_html(rows: list[list[Any]]) -> str:
    body = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(value or ''))}</td>" for value in row)
        body.append(f"<tr>{cells}</tr>")
    return "<table>" + "".join(body) + "</table>"


def _parse_text(path: Path) -> list[ContentBlock]:
    raw = path.read_bytes()
    if not raw:
        raise DocumentParseError("文件为空。", code="EMPTY_DOCUMENT")
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text.strip():
        raise DocumentParseError("无法识别文本编码或文件没有正文。")
    blocks = []
    for index, part in enumerate(text.replace("\r\n", "\n").split("\n\n")):
        clean = part.strip()
        if clean:
            blocks.append(ContentBlock("text", clean, page_idx=index))
    return blocks


def _parse_pdf(path: Path) -> list[ContentBlock]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise DocumentParseError("PDF 文件损坏或无法读取。") from exc
    blocks: list[ContentBlock] = []
    character_count = 0
    for page_idx, page in enumerate(reader.pages):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        character_count += len(text.replace(" ", ""))
        if text:
            blocks.append(ContentBlock("text", text, page_idx=page_idx))
    if not reader.pages or character_count < max(30, len(reader.pages) * 12):
        raise DocumentParseError(
            "该 PDF 主要是扫描图片，当前服务器需要配置 OCR/MinerU 服务后才能建库。",
            code="OCR_REQUIRED",
        )
    return blocks


def _parse_docx(path: Path) -> list[ContentBlock]:
    from docx import Document

    try:
        document = Document(str(path))
    except Exception as exc:
        raise DocumentParseError("DOCX 文件损坏或无法读取。") from exc
    blocks: list[ContentBlock] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            blocks.append(ContentBlock("text", text, page_idx=0))
    for table_index, table in enumerate(document.tables):
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        if rows:
            plain = "\n".join(" | ".join(row) for row in rows)
            blocks.append(
                ContentBlock(
                    "table",
                    f"表格 {table_index + 1}\n{plain}",
                    page_idx=table_index,
                    table_body=_table_html(rows),
                )
            )
    return blocks


def _parse_pptx(path: Path) -> list[ContentBlock]:
    from pptx import Presentation

    try:
        presentation = Presentation(str(path))
    except Exception as exc:
        raise DocumentParseError("PPTX 文件损坏或无法读取。") from exc
    blocks: list[ContentBlock] = []
    for page_idx, slide in enumerate(presentation.slides):
        texts: list[str] = []
        for shape in slide.shapes:
            text = str(getattr(shape, "text", "") or "").strip()
            if text:
                texts.append(text)
            if getattr(shape, "has_table", False):
                rows = [[cell.text.strip() for cell in row.cells] for row in shape.table.rows]
                if rows:
                    plain = "\n".join(" | ".join(row) for row in rows)
                    blocks.append(
                        ContentBlock(
                            "table",
                            f"幻灯片 {page_idx + 1} 表格\n{plain}",
                            page_idx=page_idx,
                            table_body=_table_html(rows),
                        )
                    )
        if texts:
            blocks.append(ContentBlock("text", "\n".join(texts), page_idx=page_idx))
    return blocks


def _xlsx_cell_text(sheet: str, coordinate: str, value: Any) -> str:
    return f"工作表《{sheet}》 单元格 {coordinate} = {value}"


def _parse_xlsx(path: Path) -> list[ContentBlock]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(str(path), data_only=True, read_only=True)
    except Exception as exc:
        raise DocumentParseError("XLSX 文件损坏或无法读取。") from exc
    blocks: list[ContentBlock] = []
    try:
        for page_idx, sheet in enumerate(workbook.worksheets):
            blocks.append(ContentBlock("title", f"工作表：{sheet.title}", page_idx=page_idx, sheet=sheet.title))
            rows: list[list[Any]] = []
            coordinate_lines: list[str] = []
            nonempty_cells = 0
            for row in sheet.iter_rows():
                values: list[Any] = []
                nonempty = False
                for cell in row:
                    value = cell.value
                    values.append("" if value is None else value)
                    if value not in (None, ""):
                        nonempty = True
                        nonempty_cells += 1
                        if nonempty_cells > 200_000:
                            raise DocumentParseError(
                                "Excel 非空单元格超过 200000 个，当前低配服务器无法安全建库。",
                                code="DOCUMENT_TOO_COMPLEX",
                            )
                        coordinate_lines.append(_xlsx_cell_text(sheet.title, cell.coordinate, value))
                if nonempty:
                    rows.append(values)
            if not rows:
                continue
            max_columns = max(len(row) for row in rows)
            normalized_rows = [row + [""] * (max_columns - len(row)) for row in rows]
            cell_range = f"A1:{sheet.cell(sheet.max_row, sheet.max_column).coordinate}"
            coordinate_text = "\n".join(coordinate_lines)
            blocks.append(
                ContentBlock(
                    "text",
                    f"工作表《{sheet.title}》单元格明细：\n{coordinate_text}",
                    page_idx=page_idx,
                    sheet=sheet.title,
                    cell_range=cell_range,
                )
            )
            blocks.append(
                ContentBlock(
                    "table",
                    f"工作表《{sheet.title}》范围 {cell_range}\n"
                    + "\n".join(" | ".join(map(str, row)) for row in normalized_rows),
                    page_idx=page_idx,
                    sheet=sheet.title,
                    cell_range=cell_range,
                    table_body=_table_html(normalized_rows),
                )
            )
    finally:
        workbook.close()
    return blocks


def _parse_xls(path: Path) -> list[ContentBlock]:
    try:
        import xlrd
    except ImportError as exc:
        raise DocumentParseError(
            "服务器尚未安装旧版 XLS 解析组件 xlrd。",
            code="PARSER_DEPENDENCY_MISSING",
        ) from exc
    try:
        workbook = xlrd.open_workbook(str(path))
    except Exception as exc:
        raise DocumentParseError("XLS 文件损坏或无法读取。") from exc
    blocks: list[ContentBlock] = []
    for page_idx, sheet in enumerate(workbook.sheets()):
        rows = [sheet.row_values(index) for index in range(sheet.nrows)]
        coordinate_lines: list[str] = []
        for row_index, row in enumerate(rows, start=1):
            for column_index, value in enumerate(row, start=1):
                if value not in (None, ""):
                    from openpyxl.utils import get_column_letter

                    coordinate = f"{get_column_letter(column_index)}{row_index}"
                    coordinate_lines.append(_xlsx_cell_text(sheet.name, coordinate, value))
        if rows:
            cell_range = f"A1:{__import__('openpyxl').utils.get_column_letter(sheet.ncols)}{sheet.nrows}"
            blocks.append(ContentBlock("title", f"工作表：{sheet.name}", page_idx=page_idx, sheet=sheet.name))
            blocks.append(
                ContentBlock(
                    "text",
                    f"工作表《{sheet.name}》单元格明细：\n" + "\n".join(coordinate_lines),
                    page_idx=page_idx,
                    sheet=sheet.name,
                    cell_range=cell_range,
                )
            )
            blocks.append(
                ContentBlock(
                    "table",
                    f"工作表《{sheet.name}》范围 {cell_range}\n"
                    + "\n".join(" | ".join(map(str, row)) for row in rows),
                    page_idx=page_idx,
                    sheet=sheet.name,
                    cell_range=cell_range,
                    table_body=_table_html(rows),
                )
            )
    return blocks


def parse_document(path: Path) -> ParsedDocument:
    resolved = path.resolve()
    extension = resolved.suffix.lower()
    parsers = {
        ".txt": _parse_text,
        ".md": _parse_text,
        ".pdf": _parse_pdf,
        ".docx": _parse_docx,
        ".pptx": _parse_pptx,
        ".xlsx": _parse_xlsx,
        ".xls": _parse_xls,
    }
    parser = parsers.get(extension)
    if parser is None:
        raise DocumentParseError("不支持该文件格式。", code="UNSUPPORTED_FILE_TYPE")
    blocks = parser(resolved)
    if not blocks or not any(block.text.strip() for block in blocks):
        raise DocumentParseError("文档没有提取到可检索内容。")
    return ParsedDocument(
        title=_clean_title(resolved),
        source_name=resolved.name,
        extension=extension,
        blocks=tuple(blocks),
    )


def write_structured_output(document: ParsedDocument, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document.as_json(), ensure_ascii=False, indent=2), encoding="utf-8")
