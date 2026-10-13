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
                        -- mark_processed() also checkpoints mid-flow; only resume an
                        -- incomplete row once that run is clearly over, otherwise a
                        -- Meta retry would process the comment twice in parallel.
                        processed_comments.status = 'processed'
                        AND (
                            processed_comments.processed_at IS NULL
                            OR processed_comments.processed_at < datetime('now', '-5 minutes')
                        )
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


    def mark_follow_gate_pending(
        self,
        *,
        platform: str,
        comment_id: str,
        keyword: str,
        like_sent: bool,
        public_reply_sent: bool,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO processed_comments (
                    platform, comment_id, keyword, status,
                    like_sent, public_reply_sent, dm_sent, processed_at
                ) VALUES (?, ?, ?, 'follow_gate_pending', ?, ?, 0, CURRENT_TIMESTAMP)
                ON CONFLICT(platform, comment_id) DO UPDATE SET
                    keyword = excluded.keyword,
                    status = 'follow_gate_pending',
                    like_sent = MAX(processed_comments.like_sent, excluded.like_sent),
                    public_reply_sent = MAX(processed_comments.public_reply_sent, excluded.public_reply_sent),
                    dm_sent = 0,
                    processed_at = CURRENT_TIMESTAMP
                """,
                (platform, comment_id, keyword, int(like_sent), int(public_reply_sent)),
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

    # --- s09: replay from the console (methods added; the ones above are unchanged) ---------
    # Same clock and thresholds as try_claim: a row is replayable if it is `failed`, or
    # `claimed` for more than 5 minutes. created_at is rewritten by every claim, so it is only
    # a lower bound of the comment's age: "older than 7 days" by the database is a safe refusal.

    _REPLAYABLE_SQL = """
        (status = 'failed' OR (status = 'claimed' AND created_at < datetime('now', '-5 minutes')))
    """

    def replay_state(self, platform: str, comment_id: str) -> dict[str, bool | str] | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT status,
                       {self._REPLAYABLE_SQL} AS replayable,
                       created_at < datetime('now', '-7 days') AS window_expired_by_db
                FROM processed_comments
                WHERE platform = ? AND comment_id = ?
                """,
                (platform, comment_id),
            ).fetchone()
        if row is None:
            return None
        return {"status": row[0], "replayable": bool(row[1]), "window_expired_by_db": bool(row[2])}

    def close_manually(self, platform: str, comment_id: str, reason_text: str) -> bool:
        """Close a failed or stale claimed row as private_reply_blocked. Never inserts, never deletes."""
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE processed_comments
                SET status = 'private_reply_blocked',
                    error_message = ?,
                    processed_at = CURRENT_TIMESTAMP
                WHERE platform = ? AND comment_id = ?
                  AND {self._REPLAYABLE_SQL}
                """,
                (reason_text, platform, comment_id),
            )
        return cursor.rowcount > 0

    def sqlite_now(self) -> str:
        with self._connect() as connection:
            return connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]

    def _batch_where(self, after: tuple[str, str, str] | None) -> tuple[str, list[str]]:
        clause = f"""
            platform IN ('instagram', 'facebook')
            AND {self._REPLAYABLE_SQL}
            AND created_at >= datetime('now', '-7 days')
            AND created_at < ?
        """
        if after is None:
            return clause, []
        return clause + " AND (created_at, platform, comment_id) > (?, ?, ?)", list(after)

    def replayable_batch(
        self,
        sweep_started: str,
        after: tuple[str, str, str] | None,
        limit: int,
    ) -> list[dict[str, str]]:
        clause, params = self._batch_where(after)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT platform, comment_id, created_at
                FROM processed_comments
                WHERE {clause}
                ORDER BY created_at, platform, comment_id
                LIMIT ?
                """,
                [sweep_started, *params, limit],
            ).fetchall()
        return [{"platform": row[0], "comment_id": row[1], "created_at": row[2]} for row in rows]

    def count_replayable_after(self, sweep_started: str, after: tuple[str, str, str] | None) -> int:
        clause, params = self._batch_where(after)
        with self._connect() as connection:
            return connection.execute(
                f"SELECT COUNT(*) FROM processed_comments WHERE {clause}",
                [sweep_started, *params],
            ).fetchone()[0]

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
