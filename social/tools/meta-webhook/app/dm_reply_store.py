"""s17 (console ADR 045 §2) — idempotency keys of the console DM replies.

`console_dm_replies` lives in the Meta database (schema owned by this `_ensure_schema`, additive).
A key is claimed (`INSERT OR IGNORE` in `BEGIN IMMEDIATE`) before any Graph call; the row is the
lock and is never deleted nor reused, its status is informative. No text, recipient or
conversation id is stored (minimisation). Every connection is closed explicitly.
"""

import sqlite3

from app.sqlite_store import SQLiteStore

STATUSES = ("sending", "sent", "failed", "uncertain")


class DirectReplyStore(SQLiteStore):
    def claim(self, key: str, platform: str) -> bool:
        connection = self._immediate()
        try:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO console_dm_replies (idempotency_key, platform, status) VALUES (?, ?, 'sending')",
                (key, platform),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def finish(self, key: str, status: str, result: str, meta_response_id: str | None = None) -> bool:
        if status not in STATUSES[1:]:
            raise ValueError("invalid status")
        connection = self._immediate()
        try:
            cursor = connection.execute(
                """
                UPDATE console_dm_replies
                SET status = ?, result = ?, meta_response_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE idempotency_key = ? AND status = 'sending'
                """,
                (status, result, meta_response_id, key),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def status_of(self, key: str) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT status FROM console_dm_replies WHERE idempotency_key = ?", (key,)
            ).fetchone()
        finally:
            connection.close()
        return str(row[0]) if row else None

    def _immediate(self) -> sqlite3.Connection:
        connection = self._connect()
        connection.isolation_level = None
        connection.execute("BEGIN IMMEDIATE")
        return connection

    def _ensure_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS console_dm_replies (
                    idempotency_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    meta_response_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
