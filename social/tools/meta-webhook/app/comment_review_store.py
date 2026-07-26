from dataclasses import dataclass

from app.sqlite_store import SQLiteStore


TERMINAL_STATUSES = {"replied", "manually_replied", "skipped", "ignored", "error"}


@dataclass(frozen=True)
class CommentReviewItem:
    platform: str
    comment_id: str
    media_id: str | None
    username: str | None
    text: str
    media_permalink: str | None
    media_caption: str | None
    reason: str
    score: int
    status: str = "pending"
    suggested_reply: str | None = None
    posted_reply_id: str | None = None
    comment_permalink: str | None = None
    owner_reply_id: str | None = None
    owner_replied_at: str | None = None
    parent_id: str | None = None


class CommentReviewStore(SQLiteStore):

    def upsert_pending(self, item: CommentReviewItem) -> bool:
        existing = self.get(item.platform, item.comment_id)
        if existing and existing.status in TERMINAL_STATUSES:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO comment_review_items (
                    platform, comment_id, media_id, username, text,
                    media_permalink, media_caption, reason, score, status,
                    suggested_reply, comment_permalink, parent_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(platform, comment_id) DO UPDATE SET
                    media_id = excluded.media_id,
                    username = excluded.username,
                    text = excluded.text,
                    media_permalink = excluded.media_permalink,
                    media_caption = excluded.media_caption,
                    reason = excluded.reason,
                    score = excluded.score,
                    suggested_reply = excluded.suggested_reply,
                    comment_permalink = excluded.comment_permalink,
                    parent_id = excluded.parent_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE comment_review_items.status NOT IN ('replied', 'manually_replied', 'skipped', 'ignored', 'error')
                """,
                (
                    item.platform,
                    item.comment_id,
                    item.media_id,
                    item.username,
                    item.text,
                    item.media_permalink,
                    item.media_caption,
                    item.reason,
                    item.score,
                    item.suggested_reply,
                    item.comment_permalink,
                    item.parent_id,
                ),
            )
        return existing is None and cursor.rowcount > 0

    def get(self, platform: str, comment_id: str) -> CommentReviewItem | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT platform, comment_id, media_id, username, text,
                       media_permalink, media_caption, reason, score, status,
                       suggested_reply, posted_reply_id, comment_permalink,
                       owner_reply_id, owner_replied_at, parent_id
                FROM comment_review_items
                WHERE platform = ? AND comment_id = ?
                """,
                (platform, comment_id),
            ).fetchone()
        return self._item_from_row(row) if row else None

    def next_pending(self) -> CommentReviewItem | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT platform, comment_id, media_id, username, text,
                       media_permalink, media_caption, reason, score, status,
                       suggested_reply, posted_reply_id, comment_permalink,
                       owner_reply_id, owner_replied_at, parent_id
                FROM comment_review_items
                WHERE status = 'pending'
                ORDER BY score DESC, created_at ASC
                LIMIT 1
                """
            ).fetchone()
        return self._item_from_row(row) if row else None

    def active_in_review(self) -> CommentReviewItem | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT platform, comment_id, media_id, username, text,
                       media_permalink, media_caption, reason, score, status,
                       suggested_reply, posted_reply_id, comment_permalink,
                       owner_reply_id, owner_replied_at, parent_id
                FROM comment_review_items
                WHERE status = 'in_review'
                ORDER BY updated_at ASC
                LIMIT 1
                """
            ).fetchone()
        return self._item_from_row(row) if row else None

    def mark_in_review(self, platform: str, comment_id: str) -> None:
        self._update_status(platform, comment_id, "in_review")

    def mark_replied(self, platform: str, comment_id: str, posted_reply_id: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE comment_review_items
                SET status = 'replied', posted_reply_id = ?, reviewed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE platform = ? AND comment_id = ?
                """,
                (posted_reply_id, platform, comment_id),
            )

    def mark_manually_replied(
        self,
        platform: str,
        comment_id: str,
        *,
        owner_reply_id: str | None = None,
        owner_replied_at: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE comment_review_items
                SET status = 'manually_replied', owner_reply_id = ?, owner_replied_at = ?,
                    reviewed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE platform = ? AND comment_id = ?
                """,
                (owner_reply_id, owner_replied_at, platform, comment_id),
            )

    def mark_thread_replies_manually_replied(
        self,
        platform: str,
        parent_id: str,
        *,
        owner_reply_id: str | None = None,
        owner_replied_at: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE comment_review_items
                SET status = 'manually_replied', owner_reply_id = ?, owner_replied_at = ?,
                    reviewed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE platform = ?
                  AND parent_id = ?
                  AND status IN ('pending', 'in_review')
                """,
                (owner_reply_id, owner_replied_at, platform, parent_id),
            )
        return cursor.rowcount

    def mark_skipped(self, platform: str, comment_id: str) -> None:
        self._update_status(platform, comment_id, "skipped", reviewed=True)

    def mark_error(self, platform: str, comment_id: str, error_message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE comment_review_items
                SET status = 'error', reason = ?, reviewed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE platform = ? AND comment_id = ?
                """,
                (error_message, platform, comment_id),
            )

    def counts_by_status(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*)
                FROM comment_review_items
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def link_context(self, platform: str, comment_id: str) -> dict[str, str | None]:
        item = self.get(platform, comment_id)
        if item is None:
            raise KeyError(f"Unknown review item: {platform}/{comment_id}")
        return {
            "platform": item.platform,
            "comment_id": item.comment_id,
            "username": item.username,
            "media_permalink": item.media_permalink,
            "comment_permalink": item.comment_permalink,
        }

    def _update_status(self, platform: str, comment_id: str, status: str, *, reviewed: bool = False) -> None:
        reviewed_sql = ", reviewed_at = CURRENT_TIMESTAMP" if reviewed else ""
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE comment_review_items
                SET status = ?, updated_at = CURRENT_TIMESTAMP{reviewed_sql}
                WHERE platform = ? AND comment_id = ?
                """,
                (status, platform, comment_id),
            )

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS comment_review_items (
                    platform TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    media_id TEXT,
                    username TEXT,
                    text TEXT NOT NULL,
                    media_permalink TEXT,
                    media_caption TEXT,
                    reason TEXT NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    suggested_reply TEXT,
                    posted_reply_id TEXT,
                    comment_permalink TEXT,
                    owner_reply_id TEXT,
                    owner_replied_at TEXT,
                    parent_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at TEXT,
                    PRIMARY KEY(platform, comment_id)
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(comment_review_items)")}
            migrations = {
                "comment_permalink": "ALTER TABLE comment_review_items ADD COLUMN comment_permalink TEXT",
                "owner_reply_id": "ALTER TABLE comment_review_items ADD COLUMN owner_reply_id TEXT",
                "owner_replied_at": "ALTER TABLE comment_review_items ADD COLUMN owner_replied_at TEXT",
                "parent_id": "ALTER TABLE comment_review_items ADD COLUMN parent_id TEXT",
            }
            for column, sql in migrations.items():
                if column not in columns:
                    connection.execute(sql)

    def _item_from_row(self, row: tuple) -> CommentReviewItem:
        return CommentReviewItem(
            platform=row[0],
            comment_id=row[1],
            media_id=row[2],
            username=row[3],
            text=row[4],
            media_permalink=row[5],
            media_caption=row[6],
            reason=row[7],
            score=int(row[8]),
            status=row[9],
            suggested_reply=row[10],
            posted_reply_id=row[11],
            comment_permalink=row[12],
            owner_reply_id=row[13],
            owner_replied_at=row[14],
            parent_id=row[15],
        )

