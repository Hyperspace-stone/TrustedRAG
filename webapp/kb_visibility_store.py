from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


VISIBILITY_MODES = {"private", "public", "shared"}


class KnowledgeBaseVisibilityError(ValueError):
    pass


class KnowledgeBaseVisibilityStore:
    def __init__(self, db_path: Path, default_owner: str = "admin") -> None:
        self._db_path = Path(db_path)
        self._default_owner = self._normalize_username(default_owner)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self._db_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_visibility (
                    collection_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    visibility TEXT NOT NULL,
                    visible_users_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    @staticmethod
    def _normalize_collection_id(raw_collection_id: Any) -> str:
        collection_id = str(raw_collection_id or "").strip()
        if not collection_id.isdigit():
            raise KnowledgeBaseVisibilityError("invalid knowledge-base identifier")
        return collection_id.zfill(3)

    @staticmethod
    def _normalize_username(raw_username: Any) -> str:
        username = str(raw_username or "").strip()
        if not username:
            raise KnowledgeBaseVisibilityError("username is required")
        return username

    @classmethod
    def _normalize_visible_users(cls, raw_users: Any) -> list[str]:
        if raw_users is None:
            return []
        if not isinstance(raw_users, (list, tuple, set)):
            raise KnowledgeBaseVisibilityError("visible_users must be a list")
        return sorted({cls._normalize_username(item) for item in raw_users})

    @staticmethod
    def _normalize_visibility(raw_visibility: Any) -> str:
        visibility = str(raw_visibility or "").strip().lower()
        if visibility not in VISIBILITY_MODES:
            raise KnowledgeBaseVisibilityError("invalid visibility mode")
        return visibility

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            raw_users = json.loads(str(row["visible_users_json"] or "[]"))
        except json.JSONDecodeError:
            raw_users = []
        visible_users = [str(item) for item in raw_users if str(item).strip()]
        return {
            "collection_name": str(row["collection_id"]),
            "owner_user_id": str(row["owner_user_id"]),
            "visibility": str(row["visibility"]),
            "visible_users": visible_users,
            "updated_at": str(row["updated_at"]),
        }

    def get_or_create(self, raw_collection_id: Any) -> dict[str, Any]:
        collection_id = self._normalize_collection_id(raw_collection_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kb_visibility WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            if row is None:
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    INSERT INTO kb_visibility (
                        collection_id, owner_user_id, visibility, visible_users_json, updated_at
                    ) VALUES (?, ?, 'public', '[]', ?)
                    """,
                    (collection_id, self._default_owner, now),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM kb_visibility WHERE collection_id = ?",
                    (collection_id,),
                ).fetchone()
        if row is None:
            raise KnowledgeBaseVisibilityError("failed to initialize visibility")
        return self._row_to_dict(row)

    def get_many(self, raw_collection_ids: Iterable[Any]) -> dict[str, dict[str, Any]]:
        collection_ids = sorted({self._normalize_collection_id(item) for item in raw_collection_ids})
        if not collection_ids:
            return {}
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO kb_visibility (
                    collection_id, owner_user_id, visibility, visible_users_json, updated_at
                ) VALUES (?, ?, 'public', '[]', ?)
                """,
                [(collection_id, self._default_owner, now) for collection_id in collection_ids],
            )
            placeholders = ",".join("?" for _ in collection_ids)
            rows = connection.execute(
                f"SELECT * FROM kb_visibility WHERE collection_id IN ({placeholders})",
                collection_ids,
            ).fetchall()
            connection.commit()
        return {
            str(row["collection_id"]): self._row_to_dict(row)
            for row in rows
        }

    def update(
        self,
        raw_collection_id: Any,
        *,
        visibility: Any,
        visible_users: Any = None,
    ) -> dict[str, Any]:
        current = self.get_or_create(raw_collection_id)
        collection_id = str(current["collection_name"])
        normalized_visibility = self._normalize_visibility(visibility)
        normalized_users = (
            self._normalize_visible_users(visible_users)
            if normalized_visibility == "shared"
            else []
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE kb_visibility
                SET visibility = ?, visible_users_json = ?, updated_at = ?
                WHERE collection_id = ?
                """,
                (
                    normalized_visibility,
                    json.dumps(normalized_users, ensure_ascii=False),
                    now,
                    collection_id,
                ),
            )
            connection.commit()
        return self.get_or_create(collection_id)

    def delete(self, raw_collection_id: Any) -> bool:
        collection_id = self._normalize_collection_id(raw_collection_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM kb_visibility WHERE collection_id = ?",
                (collection_id,),
            )
            connection.commit()
        return cursor.rowcount > 0

    @staticmethod
    def can_manage(record: dict[str, Any], username: str | None, is_admin: bool) -> bool:
        if not username:
            return False
        return is_admin or username == str(record.get("owner_user_id") or "")

    @classmethod
    def can_view(cls, record: dict[str, Any], username: str | None, is_admin: bool) -> bool:
        if cls.can_manage(record, username, is_admin):
            return True
        visibility = str(record.get("visibility") or "private")
        if visibility == "public":
            return True
        if visibility == "shared" and username:
            return username in {str(item) for item in record.get("visible_users") or []}
        return False
