"""s17 (console ADR 045 §2) — idempotency keys of the console DM replies.

`console_dm_replies` lives in the Meta database (schema owned by this `_ensure_schema`, additive).
A key is claimed (`INSERT OR IGNORE` in `BEGIN IMMEDIATE`) before any Graph call; the row is the
lock and is never deleted nor reused, its status is informative. No text nor recipient is stored
(minimisation). Every connection is closed explicitly.

Console review n°3 (ADR 045, amendment): each key also records its conversation (`platform` +
the Graph conversation id decoded from the ref). In the same claim transaction, a *fresh* key is
refused without being claimed while another key of the same conversation is `sending` or
`uncertain` and younger than `HOLD_WINDOW`: a doubtful send can no longer be followed by a second
DM from a fresh key (reloaded page, second tab, lost client hold). A replayed key is checked first
and stays a `duplicate`. After the window the conversation is free again (documented residual).
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal, NamedTuple

from app.sqlite_store import SQLiteStore

STATUSES = ("sending", "sent", "failed", "uncertain")
HOLDING_STATUSES = ("sending", "uncertain")
HOLD_WINDOW = timedelta(minutes=15)
# Same text format as SQLite's CURRENT_TIMESTAMP (UTC, second precision): string order = time order.
_STAMP = "%Y-%m-%d %H:%M:%S"


class Claim(NamedTuple):
    """`claimed`; `duplicate` (the key exists); `held` (another doubtful key holds the conversation)."""

    kind: Literal["claimed", "duplicate", "held"]
    status: str | None = None
    retry_after: datetime | None = None


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_STAMP)


class DirectReplyStore(SQLiteStore):
    def claim(self, key: str, platform: str, conversation_id: str, now: datetime) -> Claim:
        """Duplicate first, then the conversation hold, then the insert — one `BEGIN IMMEDIATE`."""
        connection = self._immediate()
        try:
            if connection.execute(
                "SELECT 1 FROM console_dm_replies WHERE idempotency_key = ?", (key,)
            ).fetchone():
                connection.execute("COMMIT")
                return Claim("duplicate")
            holder = connection.execute(
                """
                SELECT status, created_at FROM console_dm_replies
                WHERE platform = ? AND conversation_id = ? AND status IN (?, ?) AND created_at > ?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (platform, conversation_id, *HOLDING_STATUSES, _stamp(now - HOLD_WINDOW)),
            ).fetchone()
            if holder is not None:
                connection.execute("COMMIT")
                created = datetime.strptime(str(holder[1]), _STAMP).replace(tzinfo=timezone.utc)
                return Claim("held", str(holder[0]), created + HOLD_WINDOW)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO console_dm_replies
                    (idempotency_key, platform, conversation_id, status, created_at, updated_at)
                VALUES (?, ?, ?, 'sending', ?, ?)
                """,
                (key, platform, conversation_id, _stamp(now), _stamp(now)),
            )
            connection.execute("COMMIT")
            return Claim("claimed" if cursor.rowcount == 1 else "duplicate")
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
                    conversation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    meta_response_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS console_dm_replies_conversation
                ON console_dm_replies (platform, conversation_id, created_at)
                """
            )
            connection.commit()
        finally:
            connection.close()
