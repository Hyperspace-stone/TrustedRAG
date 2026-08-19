from __future__ import annotations

import json
from typing import Any


def event(event_type: str, **payload: Any) -> str:
    body = {"type": event_type, **payload}
    return f"data: {json.dumps(body, ensure_ascii=False, default=str)}\n\n"


def answer_chunks(answer: str, size: int = 48) -> list[str]:
    return [answer[index : index + size] for index in range(0, len(answer), size)]
