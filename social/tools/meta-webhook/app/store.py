from app.sqlite_store import SQLiteStore


class ProcessedCommentStore(SQLiteStore):

    def was_processed(self, platform: str, comment_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM processed_comments WHERE platform = ? AND comment_id = ?",
                (platform, comment_id),
            ).fetchone()
        return row is not None

    def comment_status(self, platform: str, comment_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM processed_comments WHERE platform = ? AND comment_id = ?",
                (platform, comment_id),
            ).fetchone()
        return row[0] if row else None

    def delivery_state(self, platform: str, comment_id: str) -> dict[str, bool | str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status, like_sent, public_reply_sent, dm_sent
                FROM processed_comments
                WHERE platform = ? AND comment_id = ?
                """,
                (platform, comment_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "status": row[0],
            "like_sent": bool(row[1]),
            "public_reply_sent": bool(row[2]),
            "dm_sent": bool(row[3]),
        }

    def is_fully_processed(self, platform: str, comment_id: str) -> bool:
        state = self.delivery_state(platform, comment_id)
        return bool(
            state
            and state["status"] == "processed"
            and state["public_reply_sent"]
            and state["dm_sent"]
        )

    def is_terminal(self, platform: str, comment_id: str) -> bool:
        state = self.delivery_state(platform, comment_id)
        return bool(state and state["status"] in {"private_reply_blocked", "wrong_resource_sent"})

    def try_claim(self, platform: str, comment_id: str, keyword: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO processed_comments (
                    platform, comment_id, keyword, status
                ) VALUES (?, ?, ?, 'claimed')
                ON CONFLICT(platform, comment_id) DO UPDATE SET
                    keyword = excluded.keyword,
                    status = 'claimed',
                    created_at = CURRENT_TIMESTAMP
                WHERE processed_comments.status = 'failed'
                   OR (
                        processed_comments.status = 'claimed'
                        AND processed_comments.created_at < datetime('now', '-5 minutes')
                   )
                   OR (
                        processed_comments.status = 'processed'
                        AND (
                            processed_comments.public_reply_sent = 0
                            OR processed_comments.dm_sent = 0
                        )
                   )
                """,
                (platform, comment_id, keyword),
            )
        return cursor.rowcount > 0

    def mark_processed(
        self,
        *,
        platform: str,
        comment_id: str,
        keyword: str,
        like_sent: bool,
        public_reply_sent: bool,
        dm_sent: bool,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO processed_comments (
                    platform, comment_id, keyword, status,
                    like_sent, public_reply_sent, dm_sent, processed_at
                ) VALUES (?, ?, ?, 'processed', ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(platform, comment_id) DO UPDATE SET
                    keyword = excluded.keyword,
                    status = 'processed',
                    like_sent = MAX(processed_comments.like_sent, excluded.like_sent),
                    public_reply_sent = MAX(processed_comments.public_reply_sent, excluded.public_reply_sent),
                    dm_sent = MAX(processed_comments.dm_sent, excluded.dm_sent),
                    processed_at = CURRENT_TIMESTAMP
                """,
                (
                    platform,
                    comment_id,
                    keyword,
                    int(like_sent),
                    int(public_reply_sent),
                    int(dm_sent),
                ),
            )

    def mark_failed(
        self,
        *,
        platform: str,
        comment_id: str,
        keyword: str,
        error_message: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO processed_comments (
                    platform, comment_id, keyword, status, error_message
                ) VALUES (?, ?, ?, 'failed', ?)
                ON CONFLICT(platform, comment_id) DO UPDATE SET
                    keyword = excluded.keyword,
                    status = 'failed',
                    error_message = excluded.error_message
                """,
                (platform, comment_id, keyword, error_message),
            )

    def mark_private_reply_blocked(
        self,
        *,
        platform: str,
        comment_id: str,
        keyword: str,
        like_sent: bool,
        public_reply_sent: bool,
        error_message: str = "private reply not allowed by Meta",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO processed_comments (
                    platform, comment_id, keyword, status,
                    like_sent, public_reply_sent, dm_sent, error_message, processed_at
                ) VALUES (?, ?, ?, 'private_reply_blocked', ?, ?, 0, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(platform, comment_id) DO UPDATE SET
                    keyword = excluded.keyword,
                    status = 'private_reply_blocked',
                    like_sent = MAX(processed_comments.like_sent, excluded.like_sent),
                    public_reply_sent = MAX(processed_comments.public_reply_sent, excluded.public_reply_sent),
                    dm_sent = 0,
                    error_message = excluded.error_message,
                    processed_at = CURRENT_TIMESTAMP
                """,
                (platform, comment_id, keyword, int(like_sent), int(public_reply_sent), error_message),
            )

    def recent_failures(self, limit: int = 10) -> list[dict[str, str | None]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT platform, comment_id, keyword, error_message
                FROM processed_comments
                WHERE status = 'failed'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "platform": row[0],
                "comment_id": row[1],
                "keyword": row[2],
                "error_message": row[3],
            }
            for row in rows
        ]

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_comments (
                    platform TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    status TEXT NOT NULL,
                    like_sent INTEGER NOT NULL DEFAULT 0,
                    public_reply_sent INTEGER NOT NULL DEFAULT 0,
                    dm_sent INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processed_at TEXT,
                    PRIMARY KEY (platform, comment_id)
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(processed_comments)")}
            migrations = {
                "like_sent": "ALTER TABLE processed_comments ADD COLUMN like_sent INTEGER NOT NULL DEFAULT 0",
                "public_reply_sent": "ALTER TABLE processed_comments ADD COLUMN public_reply_sent INTEGER NOT NULL DEFAULT 0",
                "dm_sent": "ALTER TABLE processed_comments ADD COLUMN dm_sent INTEGER NOT NULL DEFAULT 0",
                "error_message": "ALTER TABLE processed_comments ADD COLUMN error_message TEXT",
            }
            for column, sql in migrations.items():
                if column not in columns:
                    connection.execute(sql)
