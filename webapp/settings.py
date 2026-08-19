from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    auth_db_path: Path = Path(
        _env(
            "TRUSTEDRAG_AUTH_DB_PATH",
            str(PROJECT_ROOT / "data" / "trustedrag_auth.sqlite3"),
        )
    )
    name_map_path: Path = Path(
        _env("TRUSTEDRAG_NAME_MAP_PATH", str(PROJECT_ROOT / "data" / "name2id.json"))
    )
    storage_root: Path = Path(
        _env("TRUSTEDRAG_STORAGE_ROOT", str(PROJECT_ROOT / "data" / "rag_storage"))
    )
    corpus_root: Path = Path(
        _env("TRUSTEDRAG_CORPUS_ROOT", str(PROJECT_ROOT / "data" / "corpus"))
    )
    corpus_pending_root: Path = Path(
        _env(
            "TRUSTEDRAG_CORPUS_PENDING_ROOT",
            str(PROJECT_ROOT / "data" / "corpus" / ".pending"),
        )
    )
    output_root: Path = Path(
        _env("TRUSTEDRAG_OUTPUT_ROOT", str(PROJECT_ROOT / "data" / "output"))
    )
    build_root: Path = Path(
        _env(
            "TRUSTEDRAG_BUILD_ROOT",
            str(PROJECT_ROOT / "data" / ".trustedrag-builds"),
        )
    )
    build_enabled: bool = _env("TRUSTEDRAG_KB_BUILD_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    build_max_upload_bytes: int = int(
        _env("TRUSTEDRAG_BUILD_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))
    )
    build_history_limit: int = int(_env("TRUSTEDRAG_BUILD_HISTORY_LIMIT", "100"))
    build_batch_limit: int = int(_env("TRUSTEDRAG_BUILD_BATCH_LIMIT", "20"))
    build_timeout_seconds: float = float(_env("TRUSTEDRAG_BUILD_TIMEOUT_SECONDS", "2700"))
    build_heartbeat_timeout_seconds: float = float(
        _env("TRUSTEDRAG_BUILD_HEARTBEAT_TIMEOUT_SECONDS", "180")
    )
    build_progress_poll_seconds: float = float(
        _env("TRUSTEDRAG_BUILD_PROGRESS_POLL_SECONDS", "1")
    )
    build_nice_level: int = int(_env("TRUSTEDRAG_BUILD_NICE_LEVEL", "5"))
    build_embedding_max_async: int = int(
        _env("TRUSTEDRAG_BUILD_EMBEDDING_MAX_ASYNC", "2")
    )
    build_embedding_batch_num: int = int(
        _env("TRUSTEDRAG_BUILD_EMBEDDING_BATCH_NUM", "10")
    )
    build_embedding_timeout_seconds: int = int(
        _env("TRUSTEDRAG_BUILD_EMBEDDING_TIMEOUT_SECONDS", "120")
    )
    build_allowed_extensions: tuple[str, ...] = (
        ".pdf",
        ".docx",
        ".xlsx",
        ".xls",
        ".pptx",
        ".txt",
        ".md",
    )
    proxy_config_path: Path = Path(
        _env("TRUSTEDRAG_PROXY_CONFIG_PATH", str(PROJECT_ROOT / "config" / "proxy.yaml"))
    )
    model_settings_path: Path = Path(
        _env(
            "TRUSTEDRAG_MODEL_SETTINGS_PATH",
            str(PROJECT_ROOT / "data" / "webapp_model_settings.json"),
        )
    )
    llm_base_url: str = _env("RAG_LLM_BASE_URL", "http://127.0.0.1:8317/v1")
    llm_api_key: str = _env("RAG_LLM_API_KEY")
    llm_model: str = _env("RAG_LLM_MODEL", "gpt-5.4-mini")
    embedding_base_url: str = (
        _env("RAG_EMBEDDING_BASE_URL")
        or _env("LLM_BINDING_HOST")
        or _env("OPENAI_BASE_URL")
    )
    embedding_api_key: str = (
        _env("RAG_EMBEDDING_API_KEY")
        or _env("LLM_BINDING_API_KEY")
        or _env("OPENAI_API_KEY")
    )
    embedding_model: str = _env("RAG_EMBEDDING_MODEL", "text-embedding-3-large")
    embedding_dim: int = int(_env("RAG_EMBEDDING_DIM", "3072"))
    query_mode: str = _env("RAG_QUERY_MODE", "hybrid")
    top_k: int = int(_env("RAG_TOP_K", "40"))
    chunk_top_k: int = int(_env("RAG_CHUNK_TOP_K", "20"))
    max_sources: int = int(_env("RAG_MAX_SOURCES", "8"))
    max_source_chars: int = int(_env("RAG_MAX_SOURCE_CHARS", "2400"))
    max_context_chars: int = int(_env("RAG_MAX_CONTEXT_CHARS", "36000"))
    route_shortlist_size: int = int(_env("RAG_ROUTE_SHORTLIST_SIZE", "8"))
    route_min_score: float = float(_env("RAG_ROUTE_MIN_SCORE", "0.20"))
    route_min_margin: float = float(_env("RAG_ROUTE_MIN_MARGIN", "0.025"))
    evidence_parse_max_bytes: int = int(_env("RAG_EVIDENCE_PARSE_MAX_BYTES", str(16 * 1024 * 1024)))
    evidence_max_candidates: int = int(_env("RAG_EVIDENCE_MAX_CANDIDATES", "8"))
    evidence_min_score: float = float(_env("RAG_EVIDENCE_MIN_SCORE", "8.0"))
    evidence_min_margin: float = float(_env("RAG_EVIDENCE_MIN_MARGIN", "1.5"))
    evidence_excerpt_chars: int = int(_env("RAG_EVIDENCE_EXCERPT_CHARS", "900"))
    evidence_cache_ttl_seconds: float = float(_env("RAG_EVIDENCE_CACHE_TTL_SECONDS", "300"))
    corpus_preview_rows: int = int(_env("RAG_CORPUS_PREVIEW_ROWS", "120"))
    corpus_max_cell_chars: int = int(_env("RAG_CORPUS_MAX_CELL_CHARS", "4000"))

    def resolved_llm_api_key(self) -> str:
        if self.llm_api_key:
            return self.llm_api_key
        if not self.proxy_config_path.is_file():
            return ""
        try:
            import yaml

            payload = yaml.safe_load(self.proxy_config_path.read_text(encoding="utf-8")) or {}
            keys = payload.get("api-keys") or []
            if isinstance(keys, list) and keys:
                return str(keys[0]).strip()
        except Exception:
            return ""
        return ""

    @property
    def embedding_configured(self) -> bool:
        return bool(self.embedding_base_url and self.embedding_api_key)


settings = Settings()
