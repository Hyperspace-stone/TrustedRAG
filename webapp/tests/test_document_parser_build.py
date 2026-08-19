from pathlib import Path

from openpyxl import Workbook

from webapp.document_parser import parse_document
from webapp.preview_renderer import render_preview


def test_excel_parser_preserves_sheet_and_j8(tmp_path: Path) -> None:
    source = tmp_path / "2024年9月人身险公司经营情况表.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "人身保险公司（月度）"
    sheet["A1"] = "项目"
    sheet["J7"] = "本年累计/截至当期"
    sheet["A8"] = "原保险保费收入"
    sheet["J8"] = 34878.78
    workbook.save(source)
    workbook.close()

    document = parse_document(source)

    assert "工作表《人身保险公司（月度）》" in document.searchable_text
    assert "单元格 J8 = 34878.78" in document.searchable_text
    assert any(item.get("type") == "table" for item in document.content_list())


def test_office_document_gets_pdf_preview(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("可信 RAG 测试内容", encoding="utf-8")
    document = parse_document(source)
    target = tmp_path / "001.pdf"

    render_preview(source, document, target)

    assert target.read_bytes().startswith(b"%PDF")
    assert target.stat().st_size > 100

