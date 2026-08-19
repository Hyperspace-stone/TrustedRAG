from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")
MIN_PASSWORD_LENGTH = 6
PBKDF2_ITERATIONS = 260_000
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = ""
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


class AuthValidationError(ValueError):
    pass


class AuthConflictError(ValueError):
    pass


class AuthStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    nickname TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
                """
            )
            existing = connection.execute(
                "SELECT 1 FROM users WHERE username = ?",
                (DEFAULT_ADMIN_USERNAME,),
            ).fetchone()
            if not existing:
                admin_password = os.getenv(
                    "TRUSTEDRAG_ADMIN_PASSWORD",
                    DEFAULT_ADMIN_PASSWORD,
                ).strip()
                if not admin_password:
                    raise RuntimeError(
                        "TRUSTEDRAG_ADMIN_PASSWORD must be set before the first startup"
                    )
                salt, password_hash = self._hash_password(admin_password)
                connection.execute(
                    """
                    INSERT INTO users (username, password_salt, password_hash, nickname, created_at)
                    VALUES (?, ?, ?, NULL, ?)
                    """,
                    (
                        DEFAULT_ADMIN_USERNAME,
                        salt,
                        password_hash,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            connection.commit()

    @staticmethod
    def _normalize_username(raw_username: Any) -> str:
        username = str(raw_username or "").strip()
        if not USERNAME_PATTERN.fullmatch(username):
            raise AuthValidationError(
                "username must match ^[A-Za-z][A-Za-z0-9_]{2,31}$"
            )
        return username

    @staticmethod
    def _validate_password(raw_password: Any) -> str:
        password = raw_password if isinstance(raw_password, str) else ""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthValidationError(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        return password

    @staticmethod
    def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
        password_salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(password_salt),
            PBKDF2_ITERATIONS,
        ).hex()
        return password_salt, digest

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "username": str(row["username"]),
            "nickname": str(row["nickname"]).strip() if row["nickname"] else None,
        }

    def create_user(self, raw_username: Any, raw_password: Any) -> dict[str, Any]:
        username = self._normalize_username(raw_username)
        password = self._validate_password(raw_password)
        salt, password_hash = self._hash_password(password)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO users (username, password_salt, password_hash, nickname, created_at)
                    VALUES (?, ?, ?, NULL, ?)
                    """,
                    (
                        username,
                        salt,
                        password_hash,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise AuthConflictError("username already exists") from exc
        return {"username": username, "nickname": None}

    def verify_credentials(self, raw_username: Any, raw_password: Any) -> dict[str, Any] | None:
        username = self._normalize_username(raw_username)
        password = raw_password if isinstance(raw_password, str) else ""
        if not password:
            raise AuthValidationError("password is required")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username, password_salt, password_hash, nickname FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if not row:
            return None
        _, candidate_hash = self._hash_password(password, str(row["password_salt"]))
        if not hmac.compare_digest(candidate_hash, str(row["password_hash"])):
            return None
        return self._public_user(row)

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO sessions (token_hash, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (self._token_hash(token), username, now, now + SESSION_TTL_SECONDS),
            )
            connection.commit()
        return token

    def session_user(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT users.username, users.nickname
                FROM sessions
                JOIN users ON users.username = sessions.username
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (self._token_hash(token), now),
            ).fetchone()
        return self._public_user(row) if row else None

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?",
                (self._token_hash(token),),
            )
            connection.commit()

    def update_nickname(self, username: str, raw_nickname: Any) -> dict[str, Any]:
        nickname = str(raw_nickname or "").strip() or None
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET nickname = ? WHERE username = ?",
                (nickname, username),
            )
            connection.commit()
        return {"username": username, "nickname": nickname}

    def list_users(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username FROM users ORDER BY username COLLATE NOCASE ASC"
            ).fetchall()
        return [str(row["username"]) for row in rows]

    def change_password(
        self,
        username: str,
        raw_current_password: Any,
        raw_new_password: Any,
    ) -> None:
        current = self.verify_credentials(username, raw_current_password)
        if not current:
            raise AuthValidationError("invalid current password")
        new_password = self._validate_password(raw_new_password)
        salt, password_hash = self._hash_password(new_password)
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET password_salt = ?, password_hash = ? WHERE username = ?",
                (salt, password_hash, username),
            )
            connection.commit()
