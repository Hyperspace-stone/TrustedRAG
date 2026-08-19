# Architecture

## Components

```text
Browser (React/Vite)
        |
        v
Nginx /api reverse proxy
        |
        v
FastAPI webapp
  |-- authentication and user model settings
  |-- intent routing
  |-- title-first document routing
  |-- reverse evidence routing
  |-- LightRAG retrieval
  |-- answer generation and citation alignment
  |-- protected corpus/PDF viewer
  `-- serial knowledge-base build queue
        |
        v
data/corpus + data/output + data/rag_storage + name2id.json
```

## Query flow

1. The frontend sends a question to the FastAPI chat endpoint.
2. The intent router decides whether the message needs document retrieval. Greetings and ordinary conversation go directly to the generation model.
3. For document questions, the router scores document titles. Explicit titles, dates, worksheet names and file names receive the highest priority.
4. When the title is absent, the evidence router can search parsed corpus content and use distinctive values or phrases to locate candidate documents.
5. The retrieval service queries the selected LightRAG storage in hybrid mode and filters evidence that does not belong to the selected document.
6. The generation model receives the question, options when present, and bounded evidence context.
7. Sources are normalized and deduplicated by document. The frontend renders one citation entry per original file.
8. Clicking a citation opens a resizable evidence dialog with the generated PDF preview and an authenticated original-file download link.

## Trust boundaries

- Document title matching is more important than historical source sequence numbers.
- Evidence from another collection is rejected before answer generation.
- The application never exposes model API keys in API responses or build task records.
- Corpus and PDF viewing is protected by login and knowledge-base visibility rules.
- Per-user model settings are stored separately; one user's key is not returned to another user.

## Knowledge-base build architecture

Uploads are persisted before processing. A single worker consumes tasks in order. Parsing and preview generation run in the API worker; LightRAG indexing runs in an isolated child process with heartbeat, timeout and lower scheduling priority. Publication is performed only after index validation succeeds.

See [knowledge-base-build.md](knowledge-base-build.md) for details.
