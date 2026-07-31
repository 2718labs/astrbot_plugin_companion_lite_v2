from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class Storage:
    """V2 关系状态与人格绑定的 SQLite 存储层：单连接 + RLock 保证线程安全，默认开启 WAL。"""

    def __init__(self, db_path: str) -> None:
        """打开（必要时创建）数据库，启用 WAL 与外键约束并建表。"""
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 同一连接被多线程共享（check_same_thread=False），访问统一走 RLock 串行化
        self._connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._init_tables()

    def _init_tables(self) -> None:
        """建全量表与索引（IF NOT EXISTS，可重复执行）。"""
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
        """读取用户状态字典；无记录或 JSON 损坏时返回 None（不抛错）。"""
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
        """读取状态及其更新时间；无记录返回 (None, None)，JSON 损坏时状态为 None。"""
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
        """以 upsert 方式保存用户状态并刷新 updated_at；失败时抛异常。"""
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
        """该用户是否已存在状态记录。"""
        with self._lock:
            row = self._connection.execute("SELECT 1 FROM companion_state WHERE user_id=?", (user_id,)).fetchone()
        return row is not None

    def is_user_enabled(self, user_id: str) -> bool:
        """查询 UMO 开关；无设置记录时视为启用（默认开启）。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT enabled FROM companion_umo_settings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return row is None or bool(row["enabled"])

    def set_user_enabled(self, user_id: str, enabled: bool) -> None:
        """在单个事务中更新 UMO 开关；关闭时同步清除该用户全部 pending 交互，失败回滚并 re-raise。"""
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
                # 关闭 UMO 时把未完成的交互一并清掉，避免残留消息在恢复后继续消费
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
                # 先回滚再 re-raise，保证事务一致性
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                raise

    def get_bond(self, persona_id: str) -> dict[str, Any] | None:
        """查询人格绑定；persona_id 为空或未绑定返回 None。"""
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
        """列出全部人格绑定，按绑定时间倒序。"""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT persona_id, user_id, bound_at
                FROM companion_bond ORDER BY bound_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def bind_persona(self, persona_id: str, user_id: str) -> dict[str, Any]:
        """在单个事务中绑定人格：已绑给同一用户返回 already_bound、被他方占用返回 occupied、成功返回 bound 记录；输入为空返回 invalid，异常回滚并 re-raise。"""
        persona_key = str(persona_id or "").strip()
        user_key = str(user_id or "").strip()
        if not persona_key or not user_key:
            return {"status": "invalid"}
        now = time.time()
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                # 占用检查与插入在同一个写事务里，避免并发下同一人格被重复绑定
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
        """在单个事务中解除绑定；返回是否真的删除了记录（rowcount==1），异常回滚并 re-raise。"""
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
        """乐观锁写入：仅当当前 updated_at 与期望值一致时才覆盖状态，返回是否写入成功（CAS 防并发覆盖）。"""
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
        """在单个事务中原子认领交互：写入 user 消息与 pending 记录，已存在则失败；返回是否认领成功，异常回滚并 re-raise。"""
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
        """在单个事务中完成交互：校验归属与 pending 状态后写入 assistant 消息、补写轮次号并标记完成；归属不符/非 pending/重复完成返回 False，其余异常回滚并 re-raise。"""
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
                # UNIQUE(interaction_key, role) 冲突（assistant 消息已存在）视为重复完成，返回 False 而非抛错
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                return False
            except Exception:
                if self._connection.in_transaction:
                    cursor.execute("ROLLBACK")
                raise

    def fail_interaction(self, interaction_key: str, user_id: str) -> bool:
        """把 pending 交互标记为 failed 并记录时间；返回是否真的更新（rowcount==1）。"""
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
        """按 interaction_key 读取 pending 记录，不存在返回 None。"""
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
        """取该用户最近 N 个已完成轮次的消息（每轮必须恰好 user+assistant 两条才返回），按轮次升序。"""
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
        """取最近 limit 条消息（升序返回）；可限定轮次上限，或仅取已完成轮次的消息。"""
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
        """该用户已完成交互的最大轮次号，无记录返回 0。"""
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
        """返回 (消息条数, 最大自增 id)，用于判断该用户消息是否有新变更。"""
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
        """按更新时间倒序列出用户状态快照（含 UMO 开关），limit 限制在 1..1000。"""
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
        """在单个事务中删除超出 keep_rounds 的旧已完成轮次，返回删除的轮次数；失败回滚并 re-raise。"""
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
                # 先删消息再删 pending 记录，保证两张表对同一轮次同时消失
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
        """在单个事务中清除该用户的全部数据（消息、pending、状态、绑定），失败回滚并 re-raise。"""
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
        """关闭数据库连接。"""
        with self._lock:
            self._connection.close()
