from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".venv", "node_modules", "dist", "__pycache__", ".pytest_cache"}
BANNED_NAMES = {
    ".env",
    "trustedrag_auth.sqlite3",
    "webapp_model_settings.json",
    "proxy.yaml",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".ttf",
}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "Bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}", re.I),
    "production IPv4": re.compile(r"\b120\.79\.138\.124\b"),
    "production root": re.compile(r"/root/RAG_competition/RAG-Anything"),
    "developer Windows path": re.compile(r"(?i)C:\\Users\\xzl0v"),
    "known server password": re.compile(r"XZL@aliyun123", re.I),
}


def included_files() -> list[Path]:
    return sorted(
        (
            path
            for path in ROOT.rglob("*")
            if path.is_file() and not any(part in EXCLUDED_PARTS for part in path.parts)
        ),
        key=lambda path: str(path.relative_to(ROOT)),
    )


def main() -> int:
    findings: list[str] = []
    large_files: list[str] = []
    files = included_files()
    for path in files:
        relative = path.relative_to(ROOT)
        if path.name in BANNED_NAMES or path.suffix.lower() in {".sqlite3", ".db", ".log"}:
            findings.append(f"banned file: {relative}")
        size = path.stat().st_size
        if size > 5 * 1024 * 1024:
            large_files.append(f"{size}\t{relative}")
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        # This file intentionally contains the signatures it searches for.
        if relative == Path("scripts/audit-release.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{label}: {relative}:{line_number}")

    print(f"files_scanned={len(files)}")
    print(f"large_files={len(large_files)}")
    for item in large_files:
        print(f"LARGE\t{item}")
    print(f"security_findings={len(findings)}")
    for item in findings:
        print(f"FINDING\t{item}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
