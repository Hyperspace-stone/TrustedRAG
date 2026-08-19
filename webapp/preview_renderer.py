from __future__ import annotations

import shutil
from pathlib import Path

from .document_parser import ParsedDocument


class PreviewRenderError(RuntimeError):
    pass


def _plain_preview_text(document: ParsedDocument) -> list[str]:
    lines = [document.title, ""]
    for block in document.blocks:
        if block.sheet:
            lines.append(f"工作表：{block.sheet}")
        lines.extend(block.text.splitlines())
        lines.append("")
    return lines


def _render_text_pdf(document: ParsedDocument, target: Path) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen.canvas import Canvas

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        canvas = Canvas(str(target), pagesize=A4)
        width, height = A4
        left = 42
        top = height - 46
        line_height = 15
        canvas.setFont("STSong-Light", 10)
        y = top
        for raw_line in _plain_preview_text(document):
            line = raw_line.replace("\t", "    ")
            chunks = [line[index : index + 72] for index in range(0, len(line), 72)] or [""]
            for chunk in chunks:
                if y < 46:
                    canvas.showPage()
                    canvas.setFont("STSong-Light", 10)
                    y = top
                canvas.drawString(left, y, chunk)
                y -= line_height
        canvas.save()
    except Exception as exc:
        raise PreviewRenderError("无法生成 PDF 预览。") from exc


def render_preview(source: Path, document: ParsedDocument, target: Path) -> None:
    if source.suffix.lower() == ".pdf":
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    else:
        _render_text_pdf(document, target)
    if not target.is_file() or target.stat().st_size < 100:
        raise PreviewRenderError("PDF 预览为空。")

