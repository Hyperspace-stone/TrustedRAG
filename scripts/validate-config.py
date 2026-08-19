from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

REQUIRED = (
    "TRUSTEDRAG_ADMIN_PASSWORD",
    "RAG_LLM_BASE_URL",
    "RAG_LLM_API_KEY",
    "RAG_LLM_MODEL",
    "RAG_EMBEDDING_BASE_URL",
    "RAG_EMBEDDING_API_KEY",
    "RAG_EMBEDDING_MODEL",
    "RAG_EMBEDDING_DIM",
)


def main() -> int:
    missing = [name for name in REQUIRED if not os.getenv(name, "").strip()]
    placeholders = [
        name
        for name in REQUIRED
        if os.getenv(name, "").strip().lower().startswith("replace-me")
    ]
    if missing or placeholders:
        if missing:
            print("Missing configuration: " + ", ".join(missing))
        if placeholders:
            print("Replace placeholder values: " + ", ".join(placeholders))
        return 1
    print("TrustedRAG configuration is present. Secret values were not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
