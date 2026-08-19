# TrustedRAG

TrustedRAG is a trusted-document RAG application built around RAG-Anything and LightRAG. It adds title-first document routing, reverse evidence lookup, aligned citations, protected PDF evidence viewing, user-isolated model settings, and a strictly serial multi-file knowledge-base build queue.

This repository contains source code and configuration templates only. It does not include documents, indexes, user databases, credentials, or model API keys.

## Quick start

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
bash scripts/init-data-layout.sh
python scripts/validate-config.py
bash scripts/run-dev.sh
```

In another terminal:

```bash
cd frontend
npm ci
npm run dev
```

See [README.md](README.md) for the full Chinese documentation and `docs/` for architecture, configuration, deployment, and security guidance.
