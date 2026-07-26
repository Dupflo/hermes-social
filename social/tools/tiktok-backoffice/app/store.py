from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models import ReplyDraft, ReviewStatus, TikTokComment


class TikTokBackofficeStore:
    def __init__(self, database: str | Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def add_comment(self, comment: TikTokComment) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tiktok_comments (
                    video_url, video_id, comment_id, author, text, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(comment_id) DO UPDATE SET
                    video_url = excluded.video_url,
                    video_id = excluded.video_id,
                    author = excluded.author,
                    text = excluded.text,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tiktok_comments.status IN (?, ?)
                """,
                (
                    comment.video_url,
                    comment.video_id,
                    comment.comment_id,
                    comment.author,
                    comment.text,
                    ReviewStatus.PENDING_REVIEW,
                    ReviewStatus.PENDING_REVIEW,
                    ReviewStatus.NEEDS_REVIEW,
                ),
            )
        return cursor.rowcount > 0

    def get_comment(self, comment_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tiktok_comments WHERE comment_id = ?", (comment_id,)).fetchone()
        return dict(row) if row else None

    def next_pending(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM tiktok_comments
                WHERE status IN (?, ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (ReviewStatus.PENDING_REVIEW, ReviewStatus.NEEDS_REVIEW),
            ).fetchone()
        return dict(row) if row else None

    def list_comments(self, limit: int = 20, status: str | None = None) -> list[dict]:
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM tiktok_comments WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM tiktok_comments ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def save_draft(self, draft: ReplyDraft) -> None:
        if not draft.reply_text.strip():
            raise ValueError("reply_text must be non-empty")
        with self._connect() as connection:
            existing = connection.execute("SELECT 1 FROM tiktok_comments WHERE comment_id = ?", (draft.comment_id,)).fetchone()
            if existing is None:
                raise KeyError(f"Unknown comment_id: {draft.comment_id}")
            connection.execute(
                """
                INSERT INTO tiktok_reply_drafts (comment_id, keyword, reply_text)
                VALUES (?, ?, ?)
                ON CONFLICT(comment_id) DO UPDATE SET
                    keyword = excluded.keyword,
                    reply_text = excluded.reply_text,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (draft.comment_id, draft.keyword, draft.reply_text),
            )
            connection.execute(
                "UPDATE tiktok_comments SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE comment_id = ?",
                (ReviewStatus.DRAFTED, draft.comment_id),
            )

    def get_draft(self, comment_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tiktok_reply_drafts WHERE comment_id = ?", (comment_id,)).fetchone()
        return dict(row) if row else None

    def mark_needs_manual_captcha(self, *, video_url: str, screenshot_path: str | None = None) -> None:
        self.add_browser_event(
            video_url=video_url,
            event_type="needs_manual_captcha",
            screenshot_path=screenshot_path,
        )

    def mark_browser_draft_filled(self, *, video_url: str, screenshot_path: str | None = None) -> None:
        self.add_browser_event(
            video_url=video_url,
            event_type="browser_draft_filled_not_posted",
            screenshot_path=screenshot_path,
        )

    def add_browser_event(self, *, video_url: str, event_type: str, screenshot_path: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tiktok_browser_events (video_url, event_type, screenshot_path)
                VALUES (?, ?, ?)
                """,
                (video_url, event_type, screenshot_path),
            )

    def recent_browser_events(self, limit: int = 10) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tiktok_browser_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tiktok_comments (
                    comment_id TEXT PRIMARY KEY,
                    video_url TEXT NOT NULL,
                    video_id TEXT,
                    author TEXT,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tiktok_reply_drafts (
                    comment_id TEXT PRIMARY KEY,
                    keyword TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(comment_id) REFERENCES tiktok_comments(comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tiktok_browser_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_url TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    screenshot_path TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
