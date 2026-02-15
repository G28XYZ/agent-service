from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx


class SessionStore:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.storage_dir = self.project_root / ".agent-service"
        self.config_path = self.storage_dir / "config.yaml"
        self.auth_path = self.storage_dir / "auth.json"
        self.cookies_path = self.storage_dir / "cookies.json"
        self.chats_path = self.storage_dir / "chats.json"
        self.chat_context_db_path = self.storage_dir / "chat_context.db"
        self.log_path = self.storage_dir / "service.log"

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_chat_context_db()

    def load_auth(self) -> dict[str, Any]:
        return self._load_json(self.auth_path, default={})

    def save_auth(self, metadata: dict[str, Any]) -> None:
        payload = dict(metadata)
        payload["updated_at"] = _utc_now_iso()
        self._atomic_write_json(self.auth_path, payload)

    def load_cookies(self) -> httpx.Cookies:
        raw = self._load_json(self.cookies_path, default={})
        cookie_items = raw.get("cookies", []) if isinstance(raw, dict) else []

        cookies = httpx.Cookies()
        for item in cookie_items:
            if not isinstance(item, dict):
                continue

            name = item.get("name")
            value = item.get("value")
            if not name or value is None:
                continue

            cookies.set(
                name,
                str(value),
                domain=item.get("domain"),
                path=item.get("path") or "/",
            )

        return cookies

    def save_cookies(self, cookies: httpx.Cookies) -> None:
        serialized: list[dict[str, Any]] = []
        for cookie in cookies.jar:
            serialized.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": cookie.expires,
                    "secure": cookie.secure,
                    "discard": cookie.discard,
                }
            )

        self._atomic_write_json(
            self.cookies_path,
            {
                "updated_at": _utc_now_iso(),
                "cookies": serialized,
            },
        )

    def record_chat(self, chat_record: dict[str, Any], autobind: bool = True) -> None:
        base = {
            "project_path": str(self.project_root),
            "latest_chat_id": None,
            "project_bindings": {},
            "chats": [],
        }
        data = self._load_json(self.chats_path, default=base)
        if not isinstance(data, dict):
            data = base

        chats = data.get("chats")
        if not isinstance(chats, list):
            chats = []
        chats.append(chat_record)
        data["chats"] = chats

        chat_id = chat_record.get("chat_id")
        if chat_id:
            data["latest_chat_id"] = chat_id
            if autobind:
                bindings = data.get("project_bindings")
                if not isinstance(bindings, dict):
                    bindings = {}
                bindings[str(self.project_root)] = chat_id
                data["project_bindings"] = bindings

        self._atomic_write_json(self.chats_path, data)

    def list_chats(self) -> list[dict[str, Any]]:
        base = {
            "project_path": str(self.project_root),
            "latest_chat_id": None,
            "project_bindings": {},
            "chats": [],
        }
        data = self._load_json(self.chats_path, default=base)
        if not isinstance(data, dict):
            return []

        chats = data.get("chats")
        if not isinstance(chats, list):
            return []

        result: list[dict[str, Any]] = []
        for item in chats:
            if not isinstance(item, dict):
                continue
            chat_id = item.get("chat_id")
            if not chat_id:
                continue
            result.append(dict(item))

        return result

    def set_latest_chat(self, chat_id: str, autobind: bool = True) -> None:
        if not chat_id:
            return

        base = {
            "project_path": str(self.project_root),
            "latest_chat_id": None,
            "project_bindings": {},
            "chats": [],
        }
        data = self._load_json(self.chats_path, default=base)
        if not isinstance(data, dict):
            data = base

        data["latest_chat_id"] = chat_id
        if autobind:
            bindings = data.get("project_bindings")
            if not isinstance(bindings, dict):
                bindings = {}
            bindings[str(self.project_root)] = chat_id
            data["project_bindings"] = bindings

        self._atomic_write_json(self.chats_path, data)

    def delete_chat(self, chat_id: str, autobind: bool = True) -> None:
        clean_chat_id = (chat_id or "").strip()
        if not clean_chat_id:
            return

        base = {
            "project_path": str(self.project_root),
            "latest_chat_id": None,
            "project_bindings": {},
            "chats": [],
        }
        data = self._load_json(self.chats_path, default=base)
        if not isinstance(data, dict):
            data = base

        chats = data.get("chats")
        if not isinstance(chats, list):
            chats = []

        kept_chats: list[dict[str, Any]] = []
        for item in chats:
            if not isinstance(item, dict):
                continue
            if str(item.get("chat_id") or "") == clean_chat_id:
                continue
            kept_chats.append(item)
        data["chats"] = kept_chats

        latest_chat_id = data.get("latest_chat_id")
        if str(latest_chat_id or "") == clean_chat_id:
            fallback_chat_id = None
            for item in reversed(kept_chats):
                value = item.get("chat_id")
                if value:
                    fallback_chat_id = str(value)
                    break
            data["latest_chat_id"] = fallback_chat_id

        bindings = data.get("project_bindings")
        if not isinstance(bindings, dict):
            bindings = {}
        bound_chat = bindings.get(str(self.project_root))
        if str(bound_chat or "") == clean_chat_id:
            if autobind and data.get("latest_chat_id"):
                bindings[str(self.project_root)] = data.get("latest_chat_id")
            else:
                bindings.pop(str(self.project_root), None)
        data["project_bindings"] = bindings

        self._atomic_write_json(self.chats_path, data)
        self.delete_chat_messages(clean_chat_id)

    def rename_chat_title(self, chat_id: str, title: str) -> None:
        clean_chat_id = (chat_id or "").strip()
        clean_title = (title or "").strip()
        if not clean_chat_id or not clean_title:
            return

        base = {
            "project_path": str(self.project_root),
            "latest_chat_id": None,
            "project_bindings": {},
            "chats": [],
        }
        data = self._load_json(self.chats_path, default=base)
        if not isinstance(data, dict):
            data = base

        chats = data.get("chats")
        if not isinstance(chats, list):
            chats = []

        updated_at = _utc_now_iso()
        updated = False
        for item in chats:
            if not isinstance(item, dict):
                continue
            if str(item.get("chat_id") or "") != clean_chat_id:
                continue
            item["title"] = clean_title
            item["updated_at"] = updated_at
            updated = True

        if not updated:
            chats.append(
                {
                    "chat_id": clean_chat_id,
                    "title": clean_title,
                    "updated_at": updated_at,
                }
            )
        data["chats"] = chats
        self._atomic_write_json(self.chats_path, data)

    def append_chat_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        *,
        created_at: str | None = None,
    ) -> None:
        clean_chat_id = (chat_id or "").strip()
        clean_role = (role or "").strip().lower()
        clean_content = (content or "").strip()
        if not clean_chat_id or clean_role not in {"user", "assistant"} or not clean_content:
            return

        with self._open_chat_context_db() as conn:
            conn.execute(
                """
                INSERT INTO chat_messages (chat_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    clean_chat_id,
                    clean_role,
                    clean_content,
                    created_at or _utc_now_iso(),
                ),
            )

    def append_chat_turn(
        self,
        chat_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        clean_chat_id = (chat_id or "").strip()
        if not clean_chat_id:
            return

        clean_user = (user_text or "").strip()
        clean_assistant = (assistant_text or "").strip()
        if clean_user:
            self.append_chat_message(clean_chat_id, "user", clean_user)
        if clean_assistant:
            self.append_chat_message(clean_chat_id, "assistant", clean_assistant)

    def replace_chat_messages(self, chat_id: str, messages: list[dict[str, Any]]) -> None:
        clean_chat_id = (chat_id or "").strip()
        if not clean_chat_id:
            return

        normalized: list[tuple[str, str, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            stamp = str(item.get("created_at") or "").strip() or _utc_now_iso()
            normalized.append((role, content, stamp))

        with self._open_chat_context_db() as conn:
            conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (clean_chat_id,))
            if normalized:
                conn.executemany(
                    """
                    INSERT INTO chat_messages (chat_id, role, content, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [(clean_chat_id, role, content, stamp) for role, content, stamp in normalized],
                )

    def list_chat_messages(self, chat_id: str, limit: int | None = None) -> list[dict[str, str]]:
        clean_chat_id = (chat_id or "").strip()
        if not clean_chat_id:
            return []

        query = (
            "SELECT role, content, created_at FROM chat_messages "
            "WHERE chat_id = ? ORDER BY id DESC"
        )
        params: list[Any] = [clean_chat_id]
        if isinstance(limit, int) and limit > 0:
            query = f"{query} LIMIT ?"
            params.append(limit)

        with self._open_chat_context_db() as conn:
            rows = conn.execute(query, params).fetchall()

        rows.reverse()
        return [
            {
                "role": str(row["role"]),
                "content": str(row["content"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def delete_chat_messages(self, chat_id: str) -> None:
        clean_chat_id = (chat_id or "").strip()
        if not clean_chat_id:
            return

        with self._open_chat_context_db() as conn:
            conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (clean_chat_id,))

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _atomic_write_json(self, path: Path, payload: Any) -> None:
        tmp_path = path.with_name(f".{path.name}.tmp")
        content = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)

        # Best-effort local hardening for auth artifacts and logs.
        try:
            os.chmod(path, 0o600)
        except PermissionError:
            pass

    def _ensure_chat_context_db(self) -> None:
        with self._open_chat_context_db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_id_id
                ON chat_messages (chat_id, id)
                """
            )

        try:
            os.chmod(self.chat_context_db_path, 0o600)
        except PermissionError:
            pass

    @contextmanager
    def _open_chat_context_db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.chat_context_db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
