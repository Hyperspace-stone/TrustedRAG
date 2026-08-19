from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from .auth_store import AuthConflictError, AuthStore, AuthValidationError, SESSION_TTL_SECONDS
from .corpus_catalog import CorpusCatalogError
from .original_viewer_service import (
    OriginalViewerError,
    OriginalViewerRangeError,
    original_viewer_service,
)
from .output_pdf_catalog import OutputPdfCatalogError
from .document_router import DocumentRoutingError
from .intent_router import intent_router
from .kb_visibility_store import (
    KnowledgeBaseVisibilityError,
    KnowledgeBaseVisibilityStore,
)
from .knowledge_bases import KnowledgeBaseError, catalog
from .kb_build_service import KbBuildError, kb_build_service
from .model_settings import EffectiveModelConfig, model_settings_store
from .retrieval_service import (
    RetrievalConfigurationError,
    RetrievalQueryError,
    retrieval_service,
)
from .schemas import ChatRequest
from .settings import settings
from .sse import answer_chunks, event
from .trusted_document import TrustedDocumentError


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
app = FastAPI(title="RAG-Anything UltraRAG Adapter", version="1.1.0")
chat_session_store: dict[str, dict[str, Any]] = {}
auth_store = AuthStore(settings.auth_db_path)
kb_visibility_store = KnowledgeBaseVisibilityStore(settings.auth_db_path)
AUTH_COOKIE_NAME = "trustedrag_session"


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": "raganything-adapter",
        "backend_connected": True,
        "knowledge_bases": len(catalog.list()),
        "retrieval": retrieval_service.readiness(),
        "kb_build": kb_build_service.readiness(),
    }


@app.on_event("startup")
async def start_kb_build_worker() -> None:
    kb_build_service.start()


@app.on_event("shutdown")
async def stop_kb_build_worker() -> None:
    kb_build_service.stop()


def logged_out_user() -> dict[str, Any]:
    return {
        "logged_in": False,
        "user_id": "default",
        "username": None,
        "nickname": None,
        "model_settings": None,
        "is_admin": False,
    }


def authenticated_user(user: dict[str, Any]) -> dict[str, Any]:
    username = str(user["username"])
    return {
        "logged_in": True,
        "user_id": username,
        "username": username,
        "nickname": user.get("nickname"),
        "model_settings": model_settings_store.public_settings() if username == "admin" else None,
        "is_admin": username == "admin",
    }


def request_user(request: Request) -> dict[str, Any] | None:
    return auth_store.session_user(request.cookies.get(AUTH_COOKIE_NAME))


def require_admin(request: Request) -> dict[str, Any]:
    user = request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if str(user["username"]) != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以修改模型设置")
    return user


async def probe_generation_model(config: EffectiveModelConfig) -> bool:
    import json

    import httpx

    endpoint = f"{config.base_url.rstrip('/')}/chat/completions"
    timeout = httpx.Timeout(25.0, connect=6.0, read=20.0)
    request_payload: dict[str, Any] = {
        "model": config.model_name,
        "messages": [{"role": "user", "content": "只回复 OK"}],
        "temperature": 0,
        "max_tokens": 8,
        "stream": True,
    }
    if "api.deepseek.com" in config.base_url.lower():
        request_payload["thinking"] = {"type": "disabled"}
    content_parts: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            endpoint,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            json=request_payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                clean = line.strip()
                if not clean or clean == "data: [DONE]":
                    continue
                if clean.startswith("data:"):
                    clean = clean[5:].strip()
                try:
                    payload = json.loads(clean)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("error"):
                    raise ModelProbePayloadError
                choices = payload.get("choices")
                if isinstance(choices, list) and choices:
                    choice = choices[0]
                    delta = choice.get("delta") if isinstance(choice, dict) else None
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(content, str) and content:
                        content_parts.append(content)
    return bool("".join(content_parts).strip())


class ModelProbePayloadError(RuntimeError):
    pass


def model_probe_error_message(exc: Exception) -> str:
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return "模型连接超时，请检查接口地址或网络状态。"
        if isinstance(exc, httpx.ConnectError):
            return "无法连接模型接口，请检查 Base URL。"
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code in {401, 403}:
                return f"模型鉴权失败（HTTP {status_code}），请检查 API Key。"
            if status_code == 404:
                return "模型接口或模型名称不存在（HTTP 404）。"
            if status_code == 429:
                return "模型接口限流或余额不足（HTTP 429）。"
            return f"模型接口返回 HTTP {status_code}。"
        if isinstance(exc, ModelProbePayloadError):
            return "模型接口拒绝了生成请求，请检查 API Key 和模型名称。"
    except Exception:
        pass
    return "模型连接失败，请检查 Base URL、API Key 和模型名称。"


def visibility_payload(record: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any]:
    username = str(user["username"]) if user else None
    is_admin = username == "admin"
    return {
        **record,
        "can_manage": kb_visibility_store.can_manage(record, username, is_admin),
        "can_view": kb_visibility_store.can_view(record, username, is_admin),
    }


def set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    user = request_user(request)
    return authenticated_user(user) if user else logged_out_user()


@app.post("/api/auth/login")
async def auth_login(response: Response, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        user = auth_store.verify_credentials(
            (payload or {}).get("username"),
            (payload or {}).get("password"),
        )
    except AuthValidationError as exc:
        raise HTTPException(status_code=401, detail="用户名或密码错误") from exc
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    set_auth_cookie(response, auth_store.create_session(str(user["username"])))
    return authenticated_user(user)


@app.post("/api/auth/register")
async def auth_register(response: Response, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        user = auth_store.create_user(
            (payload or {}).get("username"),
            (payload or {}).get("password"),
        )
    except AuthConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AuthValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_auth_cookie(response, auth_store.create_session(str(user["username"])))
    return authenticated_user(user)


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    auth_store.revoke_session(request.cookies.get(AUTH_COOKIE_NAME))
    response.delete_cookie(AUTH_COOKIE_NAME, path="/", samesite="lax")
    return {"status": "logged_out", **logged_out_user()}


@app.post("/api/auth/nickname")
async def auth_nickname(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    user = request_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    updated = auth_store.update_nickname(str(user["username"]), (payload or {}).get("nickname"))
    return {"status": "nickname_updated", "nickname": updated.get("nickname")}


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request, payload: dict[str, Any] | None = None) -> dict[str, str]:
    user = request_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    try:
        auth_store.change_password(
            str(user["username"]),
            (payload or {}).get("current_password"),
            (payload or {}).get("new_password"),
        )
    except AuthValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "password_changed"}


@app.post("/api/auth/model-settings")
async def auth_model_settings(
    request: Request,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_admin(request)
    try:
        public_settings = model_settings_store.save(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "model_settings": public_settings}


@app.get("/api/auth/model-settings")
async def get_auth_model_settings(request: Request) -> dict[str, Any]:
    require_admin(request)
    return {"status": "ok", "model_settings": model_settings_store.public_settings()}


@app.get("/api/models/config")
async def get_model_config(request: Request) -> dict[str, Any]:
    require_admin(request)
    return {
        "status": "ok",
        "generation": model_settings_store.effective_public(settings),
    }


@app.put("/api/models/config")
async def replace_model_config(
    request: Request,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_admin(request)
    try:
        saved = model_settings_store.save({"generation": payload or {}})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "ok",
        "message": "模型配置已保存，下一次请求立即生效。",
        "model_settings": saved,
        "generation": model_settings_store.effective_public(settings),
    }


@app.post("/api/models/test")
async def test_model_config(
    request: Request,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_admin(request)
    try:
        candidate = model_settings_store.resolve_generation_candidate(payload, settings)
        model_available = await probe_generation_model(candidate)
    except ValueError as exc:
        return {
            "status": "error",
            "ok": False,
            "error_type": "ValidationError",
            "message": str(exc),
        }
    except Exception as exc:
        logger.warning("Model connectivity test failed: %s", type(exc).__name__)
        return {
            "status": "error",
            "ok": False,
            "error_type": type(exc).__name__,
            "message": model_probe_error_message(exc),
        }
    if not model_available:
        return {
            "status": "error",
            "ok": False,
            "error_type": "GenerationEmpty",
            "message": "模型接口没有返回有效生成数据，请检查 API Key 和模型名称。",
        }
    return {
        "status": "ok",
        "ok": True,
        "message": "模型连接成功。",
        "response_received": True,
        "generation": {
            "base_url": candidate.base_url,
            "model_name": candidate.model_name,
            "source": candidate.source,
        },
    }


def pipeline_item() -> dict[str, Any]:
    return {
        "name": "raganything",
        "is_ready": True,
        "config": {
            "mode": "automatic-trusted-document",
            "read_only": True,
            "automatic_routing": True,
            "knowledge_bases": len(catalog.list()),
        },
    }


@app.get("/api/pipelines")
async def pipelines() -> list[dict[str, Any]]:
    return [pipeline_item()]


@app.get("/api/pipelines/raganything")
async def pipeline_config() -> dict[str, Any]:
    return pipeline_item()


@app.post("/api/pipelines/raganything/demo/start")
async def start_demo(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "ready",
        "pipeline": "raganything",
        "session_id": (payload or {}).get("session_id", ""),
    }


@app.get("/api/kb/files")
async def kb_files(request: Request) -> dict[str, Any]:
    knowledge_bases = catalog.list()
    records = kb_visibility_store.get_many(item.identifier for item in knowledge_bases)
    user = request_user(request)
    entries = []
    for item in knowledge_bases:
        entry = item.as_frontend_entry()
        record = records[item.identifier]
        entry.update(visibility_payload(record, user))
        entry["pdf_available"] = original_viewer_service.output_pdfs.resolve(item.identifier) is not None
        entries.append(entry)
    return {
        "raw": [],
        "corpus": [],
        "chunks": [],
        "index": entries,
        "db_status": "connected",
        "db_config": {
            "backend": "raganything",
            "read_only": False,
            "automatic_routing": True,
            "count": len(entries),
            "build": kb_build_service.readiness(),
        },
    }


@app.get("/api/kb/config")
async def kb_config() -> dict[str, Any]:
    return {
        "backend": "raganything",
        "read_only": False,
        "storage": "existing-light-rag-indexes",
        "automatic_routing": True,
        "embedding_model": retrieval_service.config.embedding_model,
        "embedding_dim": retrieval_service.config.embedding_dim,
        "build": kb_build_service.readiness(),
    }


def kb_build_http_error(exc: KbBuildError) -> HTTPException:
    if exc.code in {"TASK_NOT_FOUND", "KB_NOT_FOUND"}:
        status_code = 404
    elif exc.code == "TASK_FORBIDDEN":
        status_code = 403
    elif exc.code in {"DUPLICATE_DOCUMENT", "KB_ID_CONFLICT", "KB_BUILD_ACTIVE"}:
        status_code = 409
    elif exc.code == "BUILD_DISABLED":
        status_code = 503
    else:
        status_code = 400
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


@app.delete("/api/kb/files/collection/{collection_name}")
async def delete_kb_collection(collection_name: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    try:
        return kb_build_service.delete_collection(collection_name)
    except KbBuildError as exc:
        raise kb_build_http_error(exc) from exc


@app.post("/api/kb/builds", status_code=202)
async def create_kb_build(
    request: Request,
    file: UploadFile = File(...),
    visibility: str = Form(default="private"),
) -> dict[str, Any]:
    user = require_admin(request)
    try:
        return await kb_build_service.create_task(
            file,
            owner=str(user["username"]),
            visibility=visibility,
        )
    except KbBuildError as exc:
        raise kb_build_http_error(exc) from exc


@app.post("/api/kb/builds/batch", status_code=202)
async def create_kb_build_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    visibility: str = Form(default="private"),
) -> dict[str, Any]:
    user = require_admin(request)
    try:
        tasks, errors = await kb_build_service.create_tasks(
            files,
            owner=str(user["username"]),
            visibility=visibility,
        )
        return {"tasks": tasks, "errors": errors}
    except KbBuildError as exc:
        raise kb_build_http_error(exc) from exc


@app.get("/api/kb/builds")
async def list_kb_builds(request: Request) -> list[dict[str, Any]]:
    user = require_admin(request)
    return kb_build_service.list_tasks(str(user["username"]), is_admin=True)


@app.get("/api/kb/builds/{task_id}")
async def get_kb_build(task_id: str, request: Request) -> dict[str, Any]:
    user = require_admin(request)
    try:
        return kb_build_service.get_task(task_id, str(user["username"]), is_admin=True)
    except KbBuildError as exc:
        raise kb_build_http_error(exc) from exc


@app.post("/api/kb/builds/{task_id}/retry")
async def retry_kb_build(task_id: str, request: Request) -> dict[str, Any]:
    user = require_admin(request)
    try:
        return kb_build_service.retry(task_id, str(user["username"]), is_admin=True)
    except KbBuildError as exc:
        raise kb_build_http_error(exc) from exc


@app.get("/api/corpus/{collection_id}/metadata")
async def corpus_metadata(collection_id: str) -> dict[str, Any]:
    try:
        return original_viewer_service.metadata(collection_id)
    except (CorpusCatalogError, OutputPdfCatalogError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/corpus/{collection_id}/content")
async def corpus_content(
    collection_id: str,
    sheet: str = "",
    offset: int = Query(default=0, ge=0, le=1_000_000),
    limit: int = Query(default=120, ge=1, le=200),
) -> dict[str, Any]:
    try:
        return original_viewer_service.xlsx_content(
            collection_id,
            sheet=sheet,
            offset=offset,
            limit=limit,
        )
    except (CorpusCatalogError, OutputPdfCatalogError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OriginalViewerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/corpus/{collection_id}/preview")
async def corpus_preview(
    collection_id: str,
    request: Request,
):
    try:
        return original_viewer_service.preview_response(
            collection_id,
            range_header=request.headers.get("range", ""),
        )
    except (CorpusCatalogError, OutputPdfCatalogError, OriginalViewerError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OriginalViewerRangeError as exc:
        raise HTTPException(
            status_code=416,
            detail=str(exc),
            headers={"Content-Range": "bytes */*"},
        ) from exc


@app.get("/api/corpus/{collection_id}/download")
async def corpus_download(
    collection_id: str,
    request: Request,
    variant: str = "",
):
    try:
        return original_viewer_service.download_response(
            collection_id,
            variant_name=variant,
            range_header=request.headers.get("range", ""),
        )
    except (CorpusCatalogError, OutputPdfCatalogError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OriginalViewerRangeError as exc:
        raise HTTPException(status_code=416, detail=str(exc)) from exc


def protected_kb_pdf_context(
    collection_id: str,
    request: Request,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    user = request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录后查看 PDF")
    try:
        knowledge_base = catalog.resolve(collection_id)
    except KnowledgeBaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record = kb_visibility_store.get_or_create(knowledge_base.identifier)
    username = str(user["username"])
    if not kb_visibility_store.can_view(record, username, username == "admin"):
        raise HTTPException(status_code=403, detail="无权查看该知识库")
    return knowledge_base, user, record


@app.get("/api/kb/{collection_id}/pdf/metadata")
async def protected_kb_pdf_metadata(
    collection_id: str,
    request: Request,
) -> dict[str, Any]:
    knowledge_base, _, _ = protected_kb_pdf_context(collection_id, request)
    try:
        metadata = original_viewer_service.metadata(knowledge_base.identifier)
    except (CorpusCatalogError, OutputPdfCatalogError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if metadata.get("preview_kind") != "pdf":
        raise HTTPException(status_code=404, detail="该知识库没有可查看的 PDF")
    return {
        "collection_id": knowledge_base.identifier,
        "display_name": knowledge_base.display_name,
        "file_name": metadata["preview_file_name"],
        "size": metadata["preview_size"],
        "pdf_url": f"/api/kb/{knowledge_base.identifier}/pdf",
    }


@app.get("/api/kb/{collection_id}/pdf")
async def protected_kb_pdf(
    collection_id: str,
    request: Request,
):
    knowledge_base, _, _ = protected_kb_pdf_context(collection_id, request)
    try:
        return original_viewer_service.preview_response(
            knowledge_base.identifier,
            range_header=request.headers.get("range", ""),
        )
    except (CorpusCatalogError, OutputPdfCatalogError, OriginalViewerError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OriginalViewerRangeError as exc:
        raise HTTPException(
            status_code=416,
            detail=str(exc),
            headers={"Content-Range": "bytes */*"},
        ) from exc


@app.get("/api/kb/visibility/users")
async def kb_visibility_users(request: Request) -> dict[str, list[str]]:
    if request_user(request) is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return {"users": auth_store.list_users()}


@app.get("/api/kb/visibility/{collection_name}")
async def kb_visibility(collection_name: str, request: Request) -> dict[str, Any]:
    knowledge_base = catalog.resolve(collection_name)
    record = kb_visibility_store.get_or_create(knowledge_base.identifier)
    return visibility_payload(record, request_user(request))


@app.post("/api/kb/visibility/{collection_name}")
async def update_kb_visibility(
    collection_name: str,
    request: Request,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user = request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    knowledge_base = catalog.resolve(collection_name)
    record = kb_visibility_store.get_or_create(knowledge_base.identifier)
    username = str(user["username"])
    is_admin = username == "admin"
    if not kb_visibility_store.can_manage(record, username, is_admin):
        raise HTTPException(status_code=403, detail="无权修改该知识库")
    visible_users = (payload or {}).get("visible_users") or []
    known_users = set(auth_store.list_users())
    unknown_users = sorted({str(item) for item in visible_users} - known_users)
    if unknown_users:
        raise HTTPException(status_code=400, detail="包含不存在的共享用户")
    try:
        updated = kb_visibility_store.update(
            knowledge_base.identifier,
            visibility=(payload or {}).get("visibility"),
            visible_users=visible_users,
        )
    except KnowledgeBaseVisibilityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return visibility_payload(updated, user)


@app.get("/api/background-tasks")
async def background_tasks(limit: int = 50) -> list[Any]:
    del limit
    return []


@app.get("/api/tools")
async def tools() -> list[Any]:
    return []


@app.get("/api/chat/sessions")
async def chat_sessions(limit: int = 300) -> list[dict[str, Any]]:
    sessions = list(chat_session_store.values())
    sessions.sort(key=lambda item: int(item.get("timestamp") or 0), reverse=True)
    return sessions[: max(0, min(limit, 1000))]


@app.post("/api/chat/sessions")
async def upsert_chat_session(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = str(payload.get("id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="Chat session id is required")
    stored = {
        "id": session_id,
        "title": str(payload.get("title") or "New Chat"),
        "pipeline": payload.get("pipeline"),
        "messages": payload.get("messages") if isinstance(payload.get("messages"), list) else [],
        "timestamp": int(payload.get("timestamp") or 0),
    }
    chat_session_store[session_id] = stored
    return stored


@app.delete("/api/chat/sessions")
async def clear_chat_sessions() -> dict[str, Any]:
    count = len(chat_session_store)
    chat_session_store.clear()
    return {"status": "ok", "count": count}


@app.get("/api/chat/sessions/{session_id}")
async def get_chat_session(session_id: str) -> dict[str, Any]:
    stored = chat_session_store.get(session_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return stored


@app.put("/api/chat/sessions/{session_id}")
async def rename_chat_session(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    stored = chat_session_store.get(session_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    stored["title"] = str(payload.get("title") or stored.get("title") or "New Chat")
    return stored


@app.delete("/api/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str) -> dict[str, str]:
    chat_session_store.pop(session_id, None)
    return {"status": "ok"}


@app.get("/api/pipelines/chat/history")
async def chat_history(session_id: str = "") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "history": [],
        "is_first_turn": True,
        "message_count": 0,
    }


@app.post("/api/pipelines/chat/clear-history")
async def clear_history() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/pipelines/chat/stop")
async def stop_chat() -> dict[str, str]:
    return {"status": "ok"}


async def chat_events(request: ChatRequest) -> AsyncIterator[str]:
    try:
        decision = await intent_router.decide(
            request.question,
            manual_collection=request.collection_name,
            classifier=retrieval_service.classify_intent,
        )
        if decision.intent == "chat":
            yield event("step_start", name="Jarvis 对话")
            chat_result = await retrieval_service.generate_chat(
                request.question,
                request.history,
            )
            answer = chat_result.answer
            metadata = dict(chat_result.metadata)
            yield event(
                "step_end",
                name="Jarvis 对话",
                output={"intent": "chat", "intent_method": decision.method},
            )
            metadata.update(
                {
                    "intent": "chat",
                    "intent_method": decision.method,
                    "intent_confidence": decision.confidence,
                }
            )
            if chat_result.reasoning:
                yield event("step_start", name="模型分析")
                for chunk in answer_chunks(chat_result.reasoning):
                    yield event("token", content=chunk, is_final=False)
                yield event("step_end", name="模型分析")
            for chunk in answer_chunks(answer):
                yield event("token", content=chunk, is_final=True)
            yield event(
                "final",
                data={"status": "success", "answer": answer, **metadata},
            )
            return

        yield event("step_start", name="文档自动定位")
        result = await retrieval_service.answer(
            request.question,
            request.history,
            manual_collection=request.collection_name,
        )
        result.metadata.update(
            {
                "intent": "retrieval",
                "intent_method": decision.method,
                "intent_confidence": decision.confidence,
            }
        )
        yield event(
            "step_end",
            name="文档自动定位",
            output={
                "collection_id": result.metadata.get("collection_id"),
                "collection_name": result.metadata.get("collection_name"),
                "routing_method": result.metadata.get("routing_method"),
                "routing_confidence": result.metadata.get("routing_confidence"),
            },
        )
        yield event("step_start", name="可信文档检索")
        yield event("sources", data=[source.model_dump() for source in result.sources])
        yield event(
            "step_end",
            name="可信文档检索",
            output={"sources": len(result.sources), **result.metadata},
        )
        if result.reasoning:
            yield event("step_start", name="模型分析")
            for chunk in answer_chunks(result.reasoning):
                yield event("token", content=chunk, is_final=False)
            yield event("step_end", name="模型分析")
        for chunk in answer_chunks(result.answer):
            yield event("token", content=chunk, is_final=True)
        yield event(
            "final",
            data={"status": "success", "answer": result.answer, **result.metadata},
        )
    except DocumentRoutingError as exc:
        candidates = [
            f"{item.knowledge_base.identifier} {item.knowledge_base.display_name}"
            for item in exc.candidates
        ]
        suffix = f" 候选：{'；'.join(candidates)}" if candidates else ""
        yield event("error", message=f"文档定位失败：{exc}{suffix}")
    except KnowledgeBaseError as exc:
        yield event("error", message=str(exc))
    except TrustedDocumentError as exc:
        logger.warning("Trusted document retrieval failed: %s", exc)
        yield event("error", message=f"可信文档读取失败：{exc}")
    except RetrievalConfigurationError as exc:
        logger.warning("Retrieval configuration is incomplete: %s", exc)
        yield event("error", message=str(exc))
    except RetrievalQueryError as exc:
        logger.warning("Retrieval query failed: %s", exc)
        yield event("error", message=f"检索失败：{exc}")
    except Exception:
        logger.exception("Unhandled chat failure")
        yield event("error", message="检索服务发生内部错误，请查看服务器日志。")


@app.post("/api/pipelines/raganything/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        chat_events(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def unsupported_api(path: str) -> None:
    raise HTTPException(status_code=404, detail=f"Unsupported MVP endpoint: /api/{path}")
