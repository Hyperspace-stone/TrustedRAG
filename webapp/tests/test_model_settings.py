import json
import os
import tempfile
import unittest
from pathlib import Path

from webapp.model_settings import MASKED_API_KEY, ModelSettingsStore
from webapp.settings import Settings


class ModelSettingsStoreTest(unittest.TestCase):
    def test_candidate_preserves_saved_key_when_browser_submits_blank(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ModelSettingsStore(Path(directory) / "model-settings.json")
            store.save(
                {
                    "generation": {
                        "api_key": "test-value",
                        "base_url": "https://uni-api.cstcloud.cn/v1",
                        "model_name": "deepseek-v4-flash",
                    }
                }
            )
            candidate = store.resolve_generation_candidate(
                {
                    "api_key": "",
                    "base_url": "https://uni-api.cstcloud.cn/v1",
                    "model_name": "deepseek-v4-flash",
                }
            )
            self.assertEqual(candidate.api_key, "test-value")

    def test_candidate_requires_key_when_model_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ModelSettingsStore(Path(directory) / "model-settings.json")
            store.save(
                {
                    "generation": {
                        "api_key": "test-value",
                        "base_url": "https://uni-api.cstcloud.cn/v1",
                        "model_name": "deepseek-v4-flash",
                    }
                }
            )
            with self.assertRaisesRegex(ValueError, "API Key"):
                store.resolve_generation_candidate(
                    {
                        "api_key": "",
                        "base_url": "https://uni-api.cstcloud.cn/v1",
                        "model_name": "another-model",
                    }
                )

    def test_persists_secret_but_returns_only_mask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-settings.json"
            store = ModelSettingsStore(path)
            public = store.save(
                {
                    "generation": {
                        "api_key": "test-value",
                        "base_url": "https://api.deepseek.com/v1/",
                        "model_name": "deepseek-chat",
                    }
                }
            )
            self.assertEqual(public["generation"]["api_key"], MASKED_API_KEY)
            self.assertIs(public["generation"]["has_api_key"], True)
            self.assertNotIn("test-value", json.dumps(public))
            self.assertIn("test-value", path.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_masked_or_empty_key_preserves_existing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-settings.json"
            store = ModelSettingsStore(path)
            store.save(
                {
                    "generation": {
                        "api_key": "test-value",
                        "base_url": "https://api.deepseek.com/v1",
                        "model_name": "deepseek-chat",
                    }
                }
            )
            store.save(
                {
                    "generation": {
                        "api_key": MASKED_API_KEY,
                        "base_url": "https://api.deepseek.com/v1",
                        "model_name": "deepseek-reasoner",
                    }
                }
            )
            effective = store.effective_generation(Settings())
            self.assertEqual(effective.api_key, "test-value")
            self.assertEqual(effective.model_name, "deepseek-reasoner")

    def test_environment_is_fallback_only_without_persisted_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ModelSettingsStore(Path(directory) / "model-settings.json")
            config = Settings(
                llm_base_url="http://127.0.0.1:8317/v1",
                llm_api_key="test-value",
                llm_model="fallback-model",
            )
            fallback = store.effective_generation(config)
            self.assertEqual(fallback.source, "server_environment")
            store.save(
                {
                    "generation": {
                        "api_key": "test-value",
                        "base_url": "https://api.deepseek.com/v1",
                        "model_name": "deepseek-chat",
                    }
                }
            )
            persisted = store.effective_generation(config)
            self.assertEqual(persisted.source, "persisted_generation")
            self.assertEqual(persisted.api_key, "test-value")

    def test_empty_payload_clears_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-settings.json"
            store = ModelSettingsStore(path)
            store.save(
                {
                    "generation": {
                        "api_key": "test-value",
                        "base_url": "https://api.deepseek.com/v1",
                        "model_name": "deepseek-chat",
                    }
                }
            )
            self.assertIsNone(store.save({}))
            self.assertIs(path.exists(), False)

    def test_rejects_invalid_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ModelSettingsStore(Path(directory) / "model-settings.json")
            with self.assertRaises(ValueError):
                store.save(
                    {
                        "generation": {
                            "api_key": "test-value",
                            "base_url": "file:///tmp/model",
                            "model_name": "deepseek-chat",
                        }
                    }
                )
