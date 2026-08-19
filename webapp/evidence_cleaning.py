from __future__ import annotations

import ast
import re
from typing import Any


_IMAGE_PATH = re.compile(r"(?:^|\s)Image\s+Path\s*:\s*\S+", flags=re.IGNORECASE)
_PARSER_PREFIX = re.compile(
    r"^\s*(?:Table|List\s+Content)\s+Analysis\s*:\s*",
    flags=re.IGNORECASE,
)
_LIST_PAYLOAD = re.compile(
    r"^\s*List\s+Content\s+Analysis\s*:\s*Content\s*:\s*(\{[\s\S]*?\})"
    r"(?:\s*Analysis\s*:[\s\S]*)?$",
    flags=re.IGNORECASE,
)
_TRAILING_ANALYSIS = re.compile(r"\s+Analysis\s*:[\s\S]*$", flags=re.IGNORECASE)


def _list_items(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("list_items")
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def clean_evidence_content(content: str) -> str:
    """Remove parser/debug metadata while retaining genuine document text."""
    normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    list_match = _LIST_PAYLOAD.match(normalized)
    if list_match:
        try:
            items = _list_items(ast.literal_eval(list_match.group(1)))
        except (SyntaxError, ValueError):
            items = []
        if items:
            return "\n\n".join(items)
        return ""

    cleaned = _PARSER_PREFIX.sub("", normalized)
    cleaned = _IMAGE_PATH.sub(" ", cleaned)
    cleaned = _TRAILING_ANALYSIS.sub("", cleaned)
    cleaned = re.sub(r"\b(?:Caption|Structure)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned.strip()
