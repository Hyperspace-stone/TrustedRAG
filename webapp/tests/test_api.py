import tempfile
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("TRUSTEDRAG_ADMIN_PASSWORD", "test123")

from webapp.app import app
from webapp.model_settings import model_settings_store
from webapp.retrieval_service import retrieval_service
from webapp.schemas import RetrievalResult, SourceItem


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_health_and_knowledge_bases(self) -> None:
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertIs(health.json()["backend_connected"], True)
        files = self.client.get("/api/kb/files")
        self.assertEqual(files.status_code, 200)
        payload = files.json()
        self.assertEqual(payload["db_status"], "connected")
        self.assertGreater(len(payload["index"]), 0)
        self.assertTrue(all(item["name"].isdigit() for item in payload["index"]))

    def test_demo_start(self) -> None:
        response = self.client.post(
            "/api/pipelines/raganything/demo/start", json={"session_id": "test"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

    def test_cookie_auth_login_and_logout(self) -> None:
        self.client.post("/api/auth/logout")
        current = self.client.get("/api/auth/me")
        self.assertEqual(current.status_code, 200)
        self.assertIs(current.json()["logged_in"], False)
        self.assertEqual(current.json()["user_id"], "default")

        username = f"test{uuid.uuid4().hex[:12]}"
        registered = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": "password123"},
        )
        self.assertEqual(registered.status_code, 200)
        self.assertEqual(registered.json()["username"], username)
        self.assertIs(self.client.get("/api/auth/me").json()["logged_in"], True)

        self.client.post("/api/auth/logout")
        logged_in = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": "password123"},
        )
        self.assertEqual(logged_in.status_code, 200)
        self.assertEqual(logged_in.json()["username"], username)

        logged_out = self.client.post("/api/auth/logout")
        self.assertEqual(logged_out.status_code, 200)
        self.assertIs(logged_out.json()["logged_in"], False)
        self.assertIs(self.client.get("/api/auth/me").json()["logged_in"], False)

    def test_login_error_is_localized(self) -> None:
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "用户名或密码错误")

    def test_chat_session_upsert_prevents_frontend_failure_status(self) -> None:
        payload = {
            "id": "session-test",
            "title": "Question",
            "pipeline": "raganything",
            "messages": [],
            "timestamp": 1,
        }
        created = self.client.post("/api/chat/sessions", json=payload)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["id"], "session-test")
        listed = self.client.get("/api/chat/sessions?limit=300")
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(any(item["id"] == "session-test" for item in listed.json()))

    def test_knowledge_base_visibility(self) -> None:
        self.client.post("/api/auth/logout")
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test123"},
        )
        files = self.client.get("/api/kb/files").json()
        collection_name = files["index"][0]["name"]
        response = self.client.get(f"/api/kb/visibility/{collection_name}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["collection_name"], collection_name)
        self.assertIn(payload["visibility"], {"private", "public", "shared"})
        self.assertIs(payload["can_manage"], True)
        self.assertIs(payload["can_view"], True)

    def test_delete_knowledge_base_endpoint(self) -> None:
        self.client.post("/api/auth/logout")
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test123"},
        )
        expected = {
            "status": "deleted",
            "collection_name": "509",
            "display_names": ["509_example.pdf"],
            "deleted_artifacts": 3,
        }
        with patch("webapp.app.kb_build_service.delete_collection", return_value=expected) as deleted:
            response = self.client.delete("/api/kb/files/collection/509")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        deleted.assert_called_once_with("509")

    def test_kb_visibility_access(self) -> None:
        self.client.post("/api/auth/logout")
        guest_files = self.client.get("/api/kb/files")
        self.assertEqual(guest_files.status_code, 200)
        guest_entry = guest_files.json()["index"][0]
        self.assertIn("pdf_available", guest_entry)
        self.assertIs(guest_entry["can_manage"], False)
        self.assertEqual(
            self.client.get("/api/kb/visibility/users").status_code,
            401,
        )

        logged_in = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test123"},
        )
        self.assertEqual(logged_in.status_code, 200)
        admin_entry = self.client.get("/api/kb/files").json()["index"][0]
        self.assertIs(admin_entry["can_manage"], True)
        users = self.client.get("/api/kb/visibility/users")
        self.assertEqual(users.status_code, 200)
        self.assertIn("admin", users.json()["users"])

    def test_kb_visibility_update_permissions(self) -> None:
        collection_name = self.client.get("/api/kb/files").json()["index"][0]["name"]
        self.client.post("/api/auth/logout")
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test123"},
        )
        saved = self.client.post(
            f"/api/kb/visibility/{collection_name}",
            json={"visibility": "private", "visible_users": []},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["visibility"], "private")

        username = f"viewer{uuid.uuid4().hex[:10]}"
        self.client.post("/api/auth/logout")
        registered = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": "password123"},
        )
        self.assertEqual(registered.status_code, 200)
        forbidden = self.client.post(
            f"/api/kb/visibility/{collection_name}",
            json={"visibility": "public", "visible_users": []},
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_protected_kb_pdf_access(self) -> None:
        self.client.post("/api/auth/logout")
        entries = self.client.get("/api/kb/files").json()["index"]
        pdf_entry = next(item for item in entries if item["pdf_available"])
        collection_name = pdf_entry["name"]

        unauthorized = self.client.get(f"/api/kb/{collection_name}/pdf/metadata")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.json()["detail"], "请先登录后查看 PDF")

        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test123"},
        )
        metadata = self.client.get(f"/api/kb/{collection_name}/pdf/metadata")
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.json()["collection_id"], collection_name)
        self.assertTrue(metadata.json()["file_name"].lower().endswith(".pdf"))

        pdf = self.client.get(f"/api/kb/{collection_name}/pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.headers["content-type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_protected_kb_pdf_range(self) -> None:
        self.client.post("/api/auth/logout")
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test123"},
        )
        entries = self.client.get("/api/kb/files").json()["index"]
        collection_name = next(item for item in entries if item["pdf_available"])["name"]
        ranged = self.client.get(
            f"/api/kb/{collection_name}/pdf",
            headers={"Range": "bytes=0-99"},
        )
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(len(ranged.content), 100)
        self.assertTrue(ranged.headers["content-range"].startswith("bytes 0-99/"))

    def test_chat_automatically_routes_without_collection(self) -> None:
        mocked = RetrievalResult(
            answer="grounded answer",
            reasoning="先定位文档，再核对证据。",
            sources=[SourceItem(id=1, title="001.xls", content="evidence")],
            metadata={
                "collection_id": "001",
                "collection_name": "001_example.xls",
                "routing_method": "lexical",
                "routing_confidence": 0.9,
                "retrieval_mode": "trusted_document",
            },
        )
        with patch.object(retrieval_service, "answer", new=AsyncMock(return_value=mocked)) as answer:
            response = self.client.post(
                "/api/pipelines/raganything/chat",
                json={"question": "商业银行监管指标", "dynamic_params": {}},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('"type": "sources"', response.text)
        self.assertIn('"name": "模型分析"', response.text)
        self.assertIn('"is_final": false', response.text)
        self.assertIn("先定位文档", response.text)
        self.assertIn('"type": "final"', response.text)
        self.assertIn('"collection_id": "001"', response.text)
        answer.assert_awaited_once()

    def test_greeting_bypasses_retrieval_and_sources(self) -> None:
        chat_result = RetrievalResult(
            answer="模型生成的自然问候。",
            sources=[],
            metadata={"generation_mode": "llm"},
        )
        with patch.object(retrieval_service, "answer", new=AsyncMock()) as answer, patch.object(
            retrieval_service,
            "generate_chat",
            new=AsyncMock(return_value=chat_result),
        ) as generate_chat:
            response = self.client.post(
                "/api/pipelines/raganything/chat",
                json={"question": "你好", "dynamic_params": {}},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("模型生成的自然问候。", response.text)
        self.assertIn('"intent": "chat"', response.text)
        self.assertIn('"intent_method": "greeting_to_llm"', response.text)
        self.assertNotIn('"type": "sources"', response.text)
        self.assertNotIn("文档自动定位", response.text)
        answer.assert_not_awaited()
        generate_chat.assert_awaited_once()

    def test_manual_collection_forces_retrieval_even_for_greeting(self) -> None:
        mocked = RetrievalResult(
            answer="manual answer",
            sources=[SourceItem(id=1, title="012.xls", content="evidence")],
            metadata={
                "collection_id": "012",
                "collection_name": "012_example.xls",
                "routing_method": "manual",
                "routing_confidence": 1.0,
                "retrieval_mode": "trusted_document",
            },
        )
        with patch.object(retrieval_service, "answer", new=AsyncMock(return_value=mocked)) as answer:
            response = self.client.post(
                "/api/pipelines/raganything/chat",
                json={
                    "question": "你好",
                    "dynamic_params": {"collection_name": "012"},
                },
            )
        self.assertIn('"type": "sources"', response.text)
        self.assertIn('"intent": "retrieval"', response.text)
        answer.assert_awaited_once()

    def test_model_settings_are_masked(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            model_settings_store,
            "path",
            Path(directory) / "model-settings.json",
        ):
            self.client.post("/api/auth/logout")
            logged_in = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "test123"},
            )
            self.assertEqual(logged_in.status_code, 200)
            response = self.client.post(
                "/api/auth/model-settings",
                json={
                    "generation": {
                        "api_key": "do-not-return-this",
                        "base_url": "https://api.deepseek.com/v1",
                        "model_name": "deepseek-chat",
                    }
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("do-not-return-this", response.text)
            self.assertIn("********", response.text)
            current = self.client.get("/api/auth/me")
            self.assertNotIn("do-not-return-this", current.text)

    def test_model_config_api_requires_admin_and_switches_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            model_settings_store,
            "path",
            Path(directory) / "model-settings.json",
        ):
            self.client.post("/api/auth/logout")
            self.assertEqual(self.client.get("/api/models/config").status_code, 401)
            logged_in = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "test123"},
            )
            self.assertEqual(logged_in.status_code, 200)
            saved = self.client.put(
                "/api/models/config",
                json={
                    "api_key": "test-value",
                    "base_url": "https://api.deepseek.com/v1",
                    "model_name": "deepseek-chat",
                },
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["generation"]["source"], "persisted_generation")
            self.assertEqual(saved.json()["generation"]["model_name"], "deepseek-chat")
            self.assertNotIn("test-value", saved.text)

    def test_model_connection_endpoint_returns_safe_result(self) -> None:
        self.client.post("/api/auth/logout")
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test123"},
        )
        with patch("webapp.app.probe_generation_model", new=AsyncMock(return_value=True)):
            response = self.client.post(
                "/api/models/test",
                json={
                    "api_key": "test-value",
                    "base_url": "https://api.deepseek.com/v1",
                    "model_name": "deepseek-chat",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["ok"], True)
        self.assertNotIn("test-value", response.text)

    def test_model_connection_endpoint_validates_current_form(self) -> None:
        self.client.post("/api/auth/logout")
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test123"},
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            model_settings_store,
            "path",
            Path(directory) / "model-settings.json",
        ):
            response = self.client.post(
                "/api/models/test",
                json={
                    "base_url": "https://api.deepseek.com/v1",
                    "model_name": "deepseek-chat",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["ok"], False)
        self.assertEqual(response.json()["error_type"], "ValidationError")

    def test_model_connection_endpoint_rejects_empty_generation(self) -> None:
        self.client.post("/api/auth/logout")
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test123"},
        )
        with patch("webapp.app.probe_generation_model", new=AsyncMock(return_value=False)):
            response = self.client.post(
                "/api/models/test",
                json={
                    "api_key": "wrong-key",
                    "base_url": "https://api.example.com/v1",
                    "model_name": "bad-model",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["ok"], False)
        self.assertEqual(response.json()["error_type"], "GenerationEmpty")
