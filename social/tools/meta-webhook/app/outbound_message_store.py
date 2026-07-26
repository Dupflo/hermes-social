from typing import Any

from app.sqlite_store import SQLiteStore


class OutboundMessageStore(SQLiteStore):
    def record_sent(
        self,
        *,
        platform: str,
        source_type: str,
        source_id: str,
        recipient_id: str | None,
        message_type: str,
        message_text: str,
        meta_response_id: str | None,
        status: str = "sent",
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO outbound_messages (
                    platform, source_type, source_id, recipient_id,
                    message_type, message_text, meta_response_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    source_type,
                    source_id,
                    recipient_id,
                    message_type,
                    message_text,
                    meta_response_id,
                    status,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Failed to record outbound message")
            return cursor.lastrowid

    def list_for_source(self, platform: str, source_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.row_factory = None
            rows = connection.execute(
                """
                SELECT id, platform, source_type, source_id, recipient_id,
                       message_type, message_text, meta_response_id, status, created_at
                FROM outbound_messages
                WHERE platform = ? AND source_id = ?
                ORDER BY id
                """,
                (platform, source_id),
            ).fetchall()
        keys = [
            "id",
            "platform",
            "source_type",
            "source_id",
            "recipient_id",
            "message_type",
            "message_text",
            "meta_response_id",
            "status",
            "created_at",
        ]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbound_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    recipient_id TEXT,
                    message_type TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    meta_response_id TEXT,
                    status TEXT NOT NULL DEFAULT 'sent',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbound_messages_source
                ON outbound_messages(platform, source_id)
                """
            )
