from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str = ""


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    history: list[HistoryMessage] = Field(default_factory=list)
    session_id: str = ""
    chat_session_id: str = ""
    dynamic_params: dict[str, Any] = Field(default_factory=dict)

    @property
    def collection_name(self) -> str:
        return str(self.dynamic_params.get("collection_name") or "").strip()


class SourceItem(BaseModel):
    id: int
    title: str
    content: str
    collection_id: str | None = None
    original_file_name: str | None = None
    original_file_type: str | None = None
    original_metadata_url: str | None = None
    original_content_url: str | None = None
    original_file_url: str | None = None
    original_download_url: str | None = None
    preview_kind: str | None = None
    preview_file_name: str | None = None
    preview_metadata_url: str | None = None
    preview_content_url: str | None = None
    preview_file_url: str | None = None


class RetrievalResult(BaseModel):
    answer: str
    reasoning: str = ""
    sources: list[SourceItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
