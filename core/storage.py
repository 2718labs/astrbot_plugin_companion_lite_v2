from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class Storage:
    """Small, V2-only store for relationship state and persona bonds."""

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._init_tables()

    def _init_tables(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS companion_state (
                user_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_buffer (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                interaction_key TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                completed_round INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                UNIQUE(interaction_key, role)
            );

            CREATE TABLE IF NOT EXISTS pending_interaction (
                interaction_key TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                user_message_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'failed')),
                created_at REAL NOT NULL,
                completed_at REAL NOT NULL DEFAULT 0,
                completed_round INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS companion_bond (
                persona_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                bound_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS companion_umo_settings (
                user_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_v2_messages_user_round
                ON message_buffer(user_id, completed_round, id);
            CREATE INDEX IF NOT EXISTS idx_v2_pending_user_round
                ON pending_interaction(user_id, completed_round, status);
            """
        )

    def get_state(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT state_json FROM companion_state WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row["state_json"]))
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def get_state_record(self, user_id: str) -> tuple[dict[str, Any] | None, float | None]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT state_json, updated_at
                FROM companion_state WHERE user_id=?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return None, None
        try:
            value = json.loads(str(row["state_json"]))
        except (TypeError, json.JSONDecodeError):
            value = None
        return (
            value if isinstance(value, dict) else None,
            float(row["updated_at"]),
        )

    def save_state(self, user_id: str, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO companion_state(user_id, state_json, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (user_id, payload, time.time()),
            )

    def has_state(self, user_id: str) -> bool:
        with self._lock:
            row = self._connection.execute("SELECT 1 FROM companion_state WHERE user_id=?", (user_id,)).fetchone()
        return row is not None

    def is_user_enabled(self, user_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT enabled FROM companion_umo_settings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return row is None or bool(row["enabled"])

    def set_user_enabled(self, user_id: str, enabled: bool) -> None:
        now = time.time()
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    """
                    INSERT INTO companion_umo_settings(user_id, enabled, updated_at)
                    VALUES(?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        enabled=excluded.enabled,
                        updated_at=excluded.updated_at
                    """,
                    (user_id, int(enabled), now),
                )
                if not enabled:
                    cursor.execute(
                        """
                        DELETE FROM message_buffer
                        WHERE interaction_key IN (
                            SELECT interaction_key FROM pending_interaction
                            WHERE user_id=? AND status='pending'
                        )
                        """,
                        (user_id,),
                    )
                    cursor.execute(
                        """
                        DELETE FROM pending_interaction
                        WHERE user_id=? AND status='pending'
                        """,
                        (user_id,),
                    )
                cursor.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                raise

    def get_bond(self, persona_id: str) -> dict[str, Any] | None:
        persona_key = str(persona_id or "").strip()
        if not persona_key:
            return None
        with self._lock:
            row = self._connection.execute(
                """
                SELECT persona_id, user_id, bound_at
                FROM companion_bond WHERE persona_id=?
                """,
                (persona_key,),
            ).fetchone()
        return dict(row) if row else None

    def list_bonds(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT persona_id, user_id, bound_at
                FROM companion_bond ORDER BY bound_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def bind_persona(self, persona_id: str, user_id: str) -> dict[str, Any]:
        persona_key = str(persona_id or "").strip()
        user_key = str(user_id or "").strip()
        if not persona_key or not user_key:
            return {"status": "invalid"}
        now = time.time()
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                row = cursor.execute(
                    """
                    SELECT persona_id, user_id, bound_at
                    FROM companion_bond WHERE persona_id=?
                    """,
                    (persona_key,),
                ).fetchone()
                if row is not None:
                    cursor.execute("ROLLBACK")
                    existing = dict(row)
                    existing["status"] = "already_bound" if str(row["user_id"]) == user_key else "occupied"
                    return existing
                cursor.execute(
                    """
                    INSERT INTO companion_bond(persona_id, user_id, bound_at)
                    VALUES(?,?,?)
                    """,
                    (persona_key, user_key, now),
                )
                cursor.execute("COMMIT")
                return {
                    "status": "bound",
                    "persona_id": persona_key,
                    "user_id": user_key,
                    "bound_at": now,
                }
            except Exception:
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                raise

    def unbind_persona(self, persona_id: str, user_id: str) -> bool:
        persona_key = str(persona_id or "").strip()
        user_key = str(user_id or "").strip()
        if not persona_key or not user_key:
            return False
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                result = cursor.execute(
                    """
                    DELETE FROM companion_bond
                    WHERE persona_id=? AND user_id=?
                    """,
                    (persona_key, user_key),
                )
                removed = result.rowcount == 1
                cursor.execute("COMMIT")
                return removed
            except Exception:
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                raise

    def replace_state_if_revision(
        self,
        user_id: str,
        expected_updated_at: float,
        state: dict[str, Any],
    ) -> bool:
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            result = self._connection.execute(
                """
                UPDATE companion_state
                SET state_json=?, updated_at=?
                WHERE user_id=? AND updated_at=?
                """,
                (payload, time.time(), user_id, expected_updated_at),
            )
            return result.rowcount == 1

    def claim_interaction(self, interaction_key: str, user_id: str, user_content: str) -> bool:
        now = time.time()
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                if cursor.execute(
                    "SELECT 1 FROM pending_interaction WHERE interaction_key=?",
                    (interaction_key,),
                ).fetchone():
                    cursor.execute("ROLLBACK")
                    return False
                cursor.execute(
                    """
                    INSERT INTO message_buffer(
                        user_id, interaction_key, role, content, created_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (user_id, interaction_key, "user", user_content, now),
                )
                message_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    INSERT INTO pending_interaction(
                        interaction_key, user_id, user_message_id, status, created_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (interaction_key, user_id, message_id, "pending", now),
                )
                cursor.execute("COMMIT")
                return True
            except Exception:
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                raise

    def complete_interaction(
        self,
        interaction_key: str,
        user_id: str,
        assistant_content: str,
        completed_round: int,
    ) -> bool:
        now = time.time()
        round_number = max(1, int(completed_round))
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                row = cursor.execute(
                    """
                    SELECT user_id, status FROM pending_interaction
                    WHERE interaction_key=?
                    """,
                    (interaction_key,),
                ).fetchone()
                if row is None or str(row["user_id"]) != user_id or str(row["status"]) != "pending":
                    cursor.execute("ROLLBACK")
                    return False
                cursor.execute(
                    """
                    INSERT INTO message_buffer(
                        user_id, interaction_key, role, content,
                        completed_round, created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        user_id,
                        interaction_key,
                        "assistant",
                        assistant_content,
                        round_number,
                        now,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE message_buffer SET completed_round=?
                    WHERE interaction_key=? AND role='user'
                    """,
                    (round_number, interaction_key),
                )
                cursor.execute(
                    """
                    UPDATE pending_interaction
                    SET status='completed', completed_at=?, completed_round=?
                    WHERE interaction_key=?
                    """,
                    (now, round_number, interaction_key),
                )
                cursor.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                return False
            except Exception:
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                raise

    def fail_interaction(self, interaction_key: str, user_id: str) -> bool:
        with self._lock:
            result = self._connection.execute(
                """
                UPDATE pending_interaction
                SET status='failed', completed_at=?
                WHERE interaction_key=? AND user_id=? AND status='pending'
                """,
                (time.time(), interaction_key, user_id),
            )
            return result.rowcount == 1

    def get_pending_interaction(self, interaction_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM pending_interaction WHERE interaction_key=?",
                (interaction_key,),
            ).fetchone()
        return dict(row) if row else None

    def get_completed_rounds(
        self,
        user_id: str,
        rounds: int,
        up_to_round: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, int(rounds))
        params: list[Any] = [user_id]
        bound = ""
        if up_to_round is not None:
            bound = " AND completed_round<=?"
            params.append(max(0, int(up_to_round)))
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT interaction_key, completed_round
                FROM pending_interaction
                WHERE user_id=? AND status='completed'{bound}
                ORDER BY completed_round DESC, completed_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            selected = [(str(row["interaction_key"]), int(row["completed_round"])) for row in reversed(rows)]
            result: list[dict[str, Any]] = []
            for interaction_key, _ in selected:
                messages = self._connection.execute(
                    """
                    SELECT id, user_id, interaction_key, role, content,
                           completed_round, created_at
                    FROM message_buffer
                    WHERE user_id=? AND interaction_key=?
                    ORDER BY CASE role WHEN 'user' THEN 0 ELSE 1 END, id
                    """,
                    (user_id, interaction_key),
                ).fetchall()
                if len(messages) == 2:
                    result.extend(dict(message) for message in messages)
        return result

    def get_recent_messages(
        self,
        user_id: str,
        limit: int = 20,
        up_to_round: int | None = None,
        completed_only: bool = False,
    ) -> list[dict[str, Any]]:
        conditions = ["user_id=?"]
        params: list[Any] = [user_id]
        if completed_only:
            conditions.append("completed_round>0")
        if up_to_round is not None:
            conditions.append("completed_round<=?")
            params.append(max(0, int(up_to_round)))
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT id, user_id, interaction_key, role, content,
                       completed_round, created_at
                FROM message_buffer
                WHERE {" AND ".join(conditions)}
                ORDER BY id DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_max_completed_round(self, user_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COALESCE(MAX(completed_round), 0) AS max_round
                FROM pending_interaction
                WHERE user_id=? AND status='completed'
                """,
                (user_id,),
            ).fetchone()
        return int(row["max_round"]) if row else 0

    def get_message_revision(self, user_id: str) -> tuple[int, int]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS message_count, COALESCE(MAX(id), 0) AS max_id
                FROM message_buffer WHERE user_id=?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return (0, 0)
        return (int(row["message_count"]), int(row["max_id"]))

    def list_states(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT user_id, state_json, updated_at
                FROM companion_state
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                state = json.loads(str(row["state_json"]))
            except (TypeError, json.JSONDecodeError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            result.append(
                {
                    "user_id": str(row["user_id"]),
                    "updated_at": float(row["updated_at"]),
                    "enabled": self.is_user_enabled(str(row["user_id"])),
                    "state": state,
                }
            )
        return result

    def trim_completed_rounds(self, user_id: str, keep_rounds: int) -> int:
        with self._lock:
            old = self._connection.execute(
                """
                SELECT interaction_key FROM pending_interaction
                WHERE user_id=? AND status='completed'
                ORDER BY completed_round DESC, completed_at DESC
                LIMIT -1 OFFSET ?
                """,
                (user_id, max(1, int(keep_rounds))),
            ).fetchall()
            keys = [str(row["interaction_key"]) for row in old]
            if not keys:
                return 0
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                for key in keys:
                    cursor.execute(
                        "DELETE FROM message_buffer WHERE interaction_key=?",
                        (key,),
                    )
                    cursor.execute(
                        "DELETE FROM pending_interaction WHERE interaction_key=?",
                        (key,),
                    )
                cursor.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                raise
        return len(keys)

    def reset_user(self, user_id: str) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute("DELETE FROM message_buffer WHERE user_id=?", (user_id,))
                cursor.execute("DELETE FROM pending_interaction WHERE user_id=?", (user_id,))
                cursor.execute("DELETE FROM companion_state WHERE user_id=?", (user_id,))
                cursor.execute("DELETE FROM companion_bond WHERE user_id=?", (user_id,))
                cursor.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()
