from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .settings import Settings, settings


MASKED_API_KEY = "********"
ROLE_NAMES = ("retriever", "generation")


@dataclass(frozen=True)
class EffectiveModelConfig:
    base_url: str
    api_key: str
    model_name: str
    source: str


class ModelSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _validate_base_url(value: str) -> str:
        clean = value.strip().rstrip("/")
        if not clean:
            return ""
        parsed = urlparse(clean)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("模型 Base URL 必须是有效的 http/https 地址。")
        if parsed.username or parsed.password:
            raise ValueError("模型 Base URL 不能包含用户名或密码。")
        if len(clean) > 2048:
            raise ValueError("模型 Base URL 过长。")
        return clean

    @staticmethod
    def _validate_model_name(value: str) -> str:
        clean = value.strip()
        if len(clean) > 256:
            raise ValueError("模型名称过长。")
        if any(ord(char) < 32 for char in clean):
            raise ValueError("模型名称包含无效字符。")
        return clean

    @staticmethod
    def _validate_api_key(value: str) -> str:
        clean = value.strip()
        if len(clean) > 8192:
            raise ValueError("API Key 过长。")
        if any(char in clean for char in ("\r", "\n", "\x00")):
            raise ValueError("API Key 包含无效字符。")
        return clean

    def _normalized_role(
        self,
        raw: Any,
        previous: dict[str, Any],
    ) -> dict[str, str]:
        if raw is None:
            return dict(previous)
        if not isinstance(raw, dict):
            raise ValueError("模型角色设置必须是对象。")
        raw_key = self._validate_api_key(str(raw.get("api_key") or ""))
        previous_key = str(previous.get("api_key") or "")
        api_key = previous_key if raw_key in {"", MASKED_API_KEY} else raw_key
        return {
            "api_key": api_key,
            "base_url": self._validate_base_url(str(raw.get("base_url") or "")),
            "model_name": self._validate_model_name(str(raw.get("model_name") or "")),
        }

    def save(self, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        submitted = payload or {}
        with self._lock:
            if not submitted:
                self.clear()
                return None
            unknown = set(submitted) - set(ROLE_NAMES)
            if unknown:
                raise ValueError("模型设置包含不支持的字段。")
            previous = self._read()
            stored: dict[str, Any] = {"version": 1}
            for role_name in ROLE_NAMES:
                role = self._normalized_role(
                    submitted.get(role_name),
                    previous.get(role_name) if isinstance(previous.get(role_name), dict) else {},
                )
                if any(role.values()):
                    if not role["base_url"] or not role["model_name"]:
                        raise ValueError(f"{role_name} 必须同时填写 Base URL 和模型名称。")
                    stored[role_name] = role
            if len(stored) == 1:
                self.clear()
                return None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                dir=str(self.path.parent),
                text=True,
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(stored, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary_path, 0o600)
                os.replace(temporary_path, self.path)
                os.chmod(self.path, 0o600)
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
            return self.public_settings()

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def public_settings(self) -> dict[str, Any] | None:
        with self._lock:
            stored = self._read()
        public: dict[str, Any] = {}
        for role_name in ROLE_NAMES:
            raw = stored.get(role_name)
            if not isinstance(raw, dict):
                continue
            has_api_key = bool(str(raw.get("api_key") or ""))
            public[role_name] = {
                "api_key": MASKED_API_KEY if has_api_key else "",
                "has_api_key": has_api_key,
                "base_url": str(raw.get("base_url") or ""),
                "model_name": str(raw.get("model_name") or ""),
            }
        return public or None

    def effective_generation(self, config: Settings = settings) -> EffectiveModelConfig:
        with self._lock:
            stored = self._read()
        generation = stored.get("generation")
        if isinstance(generation, dict) and any(generation.values()):
            return EffectiveModelConfig(
                base_url=str(generation.get("base_url") or "").strip(),
                api_key=str(generation.get("api_key") or "").strip(),
                model_name=str(generation.get("model_name") or "").strip(),
                source="persisted_generation",
            )
        return EffectiveModelConfig(
            base_url=config.llm_base_url,
            api_key=config.resolved_llm_api_key(),
            model_name=config.llm_model,
            source="server_environment",
        )

    def effective_public(self, config: Settings = settings) -> dict[str, Any]:
        effective = self.effective_generation(config)
        return {
            "api_key": MASKED_API_KEY if effective.api_key else "",
            "has_api_key": bool(effective.api_key),
            "base_url": effective.base_url,
            "model_name": effective.model_name,
            "source": effective.source,
        }

    def resolve_generation_candidate(
        self,
        raw: dict[str, Any] | None,
        config: Settings = settings,
    ) -> EffectiveModelConfig:
        submitted = raw or {}
        current = self.effective_generation(config)
        raw_key = self._validate_api_key(str(submitted.get("api_key") or ""))
        base_url = self._validate_base_url(
            str(submitted.get("base_url") or current.base_url)
        )
        model_name = self._validate_model_name(
            str(submitted.get("model_name") or current.model_name)
        )
        same_persisted_config = (
            current.source == "persisted_generation"
            and base_url == current.base_url
            and model_name == current.model_name
        )
        preserve_saved_key = raw_key == MASKED_API_KEY or (
            not raw_key and same_persisted_config
        )
        api_key = current.api_key if preserve_saved_key else raw_key
        if not api_key:
            raise ValueError("请输入模型 API Key。")
        if not base_url:
            raise ValueError("请输入模型 Base URL。")
        if not model_name:
            raise ValueError("请输入模型名称。")
        return EffectiveModelConfig(
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            source="candidate",
        )


model_settings_store = ModelSettingsStore(settings.model_settings_path)
