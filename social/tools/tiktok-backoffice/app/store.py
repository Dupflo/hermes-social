from __future__ import annotations

import sqlite3
from pathlib import Path
import re

from app.keyword import contains_keyword
from app.models import Campaign, ReplyDraft, ReviewItemStatus, ReviewStatus, TikTokComment, TikTokVideo


def _video_id_from_url(video_url: str) -> str | None:
    match = re.search(r"/video/(\d+)", video_url)
    return match.group(1) if match else None


def _caption_has_comment_cta_keyword(caption: str, keyword: str) -> bool:
    # Avoid false mappings from generic words in the description. We only trust
    # the short requested term immediately after a CTA like "Commente proxy".
    normalized = caption or ""
    cta_markers = ["commente", "commentes", "commenter"]
    lowered = normalized.lower()
    for marker in cta_markers:
        start = 0
        while True:
            idx = lowered.find(marker, start)
            if idx == -1:
                break
            after = normalized[idx + len(marker) : idx + len(marker) + 80]
            # Stop before explanatory text ("→ je t'envoie...", hashtags, next sentence).
            requested = re.split(r"(?:→|👉|👇|#|\.|\?|!|\n|\r)", after, maxsplit=1)[0]
            # Drop common continuation after the requested keyword: "proxy et je...".
            requested = re.split(r"\b(?:et|puis|pour|afin|si|tu|je|j[’'])\b", requested, maxsplit=1, flags=re.IGNORECASE)[0]
            if contains_keyword(requested, keyword):
                return True
            start = idx + len(marker)
    return False


class TikTokBackofficeStore:
    def __init__(self, database: str | Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection


    def add_video(self, video: TikTokVideo) -> bool:
        video_id = video.video_id or _video_id_from_url(video.video_url)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tiktok_videos (video_url, video_id, author, caption, active)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(video_url) DO UPDATE SET
                    video_id = COALESCE(excluded.video_id, tiktok_videos.video_id),
                    author = COALESCE(excluded.author, tiktok_videos.author),
                    caption = COALESCE(excluded.caption, tiktok_videos.caption),
                    active = excluded.active,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    COALESCE(tiktok_videos.video_id, '') != COALESCE(excluded.video_id, '') OR
                    COALESCE(tiktok_videos.author, '') != COALESCE(excluded.author, '') OR
                    COALESCE(tiktok_videos.caption, '') != COALESCE(excluded.caption, '') OR
                    tiktok_videos.active != excluded.active
                """,
                (video.video_url, video_id, video.author, video.caption, int(video.active)),
            )
        return cursor.rowcount > 0

    def list_videos(self, *, with_campaigns: bool = False, limit: int = 50) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tiktok_videos ORDER BY discovered_at DESC, video_url DESC LIMIT ?",
                (limit,),
            ).fetchall()
            items = [dict(row) for row in rows]
            if with_campaigns:
                for item in items:
                    campaign_rows = connection.execute(
                        """
                        SELECT campaign_slug, source, confidence, approved
                        FROM tiktok_video_campaigns
                        WHERE video_url = ?
                        ORDER BY campaign_slug
                        """,
                        (item["video_url"],),
                    ).fetchall()
                    item["campaigns"] = [dict(row) for row in campaign_rows]
        return items


    def suggest_video_campaigns(self) -> int:
        suggested = 0
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM tiktok_video_campaigns WHERE source = 'caption_keyword' AND approved = 0"
            )
            videos = connection.execute(
                "SELECT video_url, caption FROM tiktok_videos WHERE active = 1 ORDER BY discovered_at DESC"
            ).fetchall()
            campaigns = connection.execute("SELECT slug FROM tiktok_campaigns WHERE active = 1 ORDER BY slug").fetchall()
            for video in videos:
                caption = video["caption"] or ""
                if not caption.strip():
                    continue
                for campaign_row in campaigns:
                    campaign = self.get_campaign(campaign_row["slug"])
                    if campaign is None:
                        continue
                    if any(_caption_has_comment_cta_keyword(caption, keyword) for keyword in campaign["keywords"]):
                        cursor = connection.execute(
                            """
                            INSERT INTO tiktok_video_campaigns (video_url, campaign_slug, source, confidence, approved)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(video_url, campaign_slug) DO NOTHING
                            """,
                            (video["video_url"], campaign["slug"], "caption_keyword", 0.8, 0),
                        )
                        suggested += cursor.rowcount
        return suggested

    def approve_video_campaign(self, *, video_url: str, campaign_slug: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tiktok_video_campaigns
                SET approved = 1,
                    source = 'operator_approved',
                    confidence = 1.0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE video_url = ? AND campaign_slug = ?
                  AND (approved != 1 OR source != 'operator_approved' OR confidence != 1.0)
                """,
                (video_url, campaign_slug),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    "SELECT 1 FROM tiktok_video_campaigns WHERE video_url = ? AND campaign_slug = ?",
                    (video_url, campaign_slug),
                ).fetchone()
                if existing is None:
                    video = connection.execute("SELECT 1 FROM tiktok_videos WHERE video_url = ?", (video_url,)).fetchone()
                    if video is None:
                        raise KeyError(f"Unknown video_url: {video_url}")
                    campaign = connection.execute("SELECT 1 FROM tiktok_campaigns WHERE slug = ?", (campaign_slug,)).fetchone()
                    if campaign is None:
                        raise KeyError(f"Unknown campaign: {campaign_slug}")
                    insert_cursor = connection.execute(
                        """
                        INSERT INTO tiktok_video_campaigns (video_url, campaign_slug, source, confidence, approved)
                        VALUES (?, ?, 'operator_approved', 1.0, 1)
                        """,
                        (video_url, campaign_slug),
                    )
                    return insert_cursor.rowcount > 0
        return cursor.rowcount > 0

    def assign_video_campaign(self, *, video_url: str, campaign_slug: str, source: str = "manual", confidence: float = 1.0, approved: bool = True) -> bool:
        with self._connect() as connection:
            video = connection.execute("SELECT 1 FROM tiktok_videos WHERE video_url = ?", (video_url,)).fetchone()
            if video is None:
                raise KeyError(f"Unknown video_url: {video_url}")
            campaign = connection.execute("SELECT 1 FROM tiktok_campaigns WHERE slug = ?", (campaign_slug,)).fetchone()
            if campaign is None:
                raise KeyError(f"Unknown campaign: {campaign_slug}")
            cursor = connection.execute(
                """
                INSERT INTO tiktok_video_campaigns (video_url, campaign_slug, source, confidence, approved)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(video_url, campaign_slug) DO NOTHING
                """,
                (video_url, campaign_slug, source, confidence, int(approved)),
            )
        return cursor.rowcount > 0

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


    def upsert_campaign(self, campaign: Campaign) -> None:
        if not campaign.slug.strip():
            raise ValueError("campaign slug must be non-empty")
        keywords = [keyword.strip() for keyword in campaign.keywords if keyword.strip()]
        if not keywords:
            raise ValueError("campaign must have at least one keyword")
        if not campaign.reply_template.strip():
            raise ValueError("reply_template must be non-empty")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tiktok_campaigns (slug, name, reply_template, active)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name = excluded.name,
                    reply_template = excluded.reply_template,
                    active = excluded.active,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (campaign.slug, campaign.name, campaign.reply_template, int(campaign.active)),
            )
            connection.execute("DELETE FROM tiktok_campaign_keywords WHERE campaign_slug = ?", (campaign.slug,))
            connection.executemany(
                "INSERT INTO tiktok_campaign_keywords (campaign_slug, keyword) VALUES (?, ?)",
                [(campaign.slug, keyword) for keyword in keywords],
            )


    def list_campaigns(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT slug, name, reply_template, active FROM tiktok_campaigns ORDER BY slug").fetchall()
            campaigns = [dict(row) for row in rows]
            for campaign in campaigns:
                keyword_rows = connection.execute(
                    "SELECT keyword FROM tiktok_campaign_keywords WHERE campaign_slug = ? ORDER BY keyword",
                    (campaign["slug"],),
                ).fetchall()
                campaign["keywords"] = [row["keyword"] for row in keyword_rows]
        return campaigns

    def get_campaign(self, slug: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tiktok_campaigns WHERE slug = ?", (slug,)).fetchone()
            if row is None:
                return None
            keywords = connection.execute(
                "SELECT keyword FROM tiktok_campaign_keywords WHERE campaign_slug = ? ORDER BY keyword",
                (slug,),
            ).fetchall()
        data = dict(row)
        data["keywords"] = [keyword["keyword"] for keyword in keywords]
        return data

    def match_campaign(self, slug: str) -> dict:
        campaign = self.get_campaign(slug)
        if campaign is None:
            raise KeyError(f"Unknown campaign: {slug}")
        if not campaign["active"]:
            return {"matched": 0, "created_drafts": 0}
        keywords = campaign["keywords"]
        matched_comment_ids: set[str] = set()
        created = 0
        with self._connect() as connection:
            comments = connection.execute(
                """
                SELECT * FROM tiktok_comments
                WHERE status IN (?, ?)
                ORDER BY created_at ASC
                """,
                (ReviewStatus.PENDING_REVIEW, ReviewStatus.NEEDS_REVIEW),
            ).fetchall()
            for row in comments:
                comment = dict(row)
                matched_keyword = next((keyword for keyword in keywords if contains_keyword(comment["text"], keyword)), None)
                if matched_keyword is None:
                    continue
                matched_comment_ids.add(comment["comment_id"])
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO tiktok_review_items (
                        comment_id, campaign_slug, matched_keyword, reply_text, status
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        comment["comment_id"],
                        slug,
                        matched_keyword,
                        campaign["reply_template"],
                        ReviewItemStatus.PENDING_REVIEW,
                    ),
                )
                created += cursor.rowcount
        return {"matched": len(matched_comment_ids), "created_drafts": created}



    def next_browser_draft_item(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    ri.id,
                    ri.comment_id,
                    ri.campaign_slug,
                    ri.matched_keyword,
                    ri.reply_text,
                    ri.status,
                    ri.screenshot_path,
                    ri.failure_reason,
                    ri.created_at,
                    c.video_url,
                    c.video_id,
                    c.author,
                    c.text AS comment_text
                FROM tiktok_review_items ri
                JOIN tiktok_comments c ON c.comment_id = ri.comment_id
                WHERE ri.status = ?
                ORDER BY ri.updated_at ASC, ri.id ASC
                LIMIT 1
                """,
                (ReviewItemStatus.APPROVED_FOR_DRAFT,),
            ).fetchone()
        return dict(row) if row else None

    def mark_review_browser_drafted(self, review_id: int, *, screenshot_path: str | None = None) -> dict:
        item = self.get_review_item(review_id)
        if item is None:
            raise KeyError(f"Unknown review_id: {review_id}")
        if item["status"] != ReviewItemStatus.APPROVED_FOR_DRAFT:
            raise ValueError(f"review_id {review_id} is not approved_for_draft")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tiktok_review_items
                SET status = ?, screenshot_path = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (ReviewItemStatus.DRAFTED_IN_BROWSER, screenshot_path, review_id),
            )
            connection.execute(
                """
                INSERT INTO tiktok_browser_events (video_url, event_type, screenshot_path)
                VALUES (?, ?, ?)
                """,
                (item["video_url"], "browser_draft_filled_not_posted", screenshot_path),
            )
        updated = self.get_review_item(review_id)
        assert updated is not None
        return updated

    def get_review_item(self, review_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    ri.id,
                    ri.comment_id,
                    ri.campaign_slug,
                    ri.matched_keyword,
                    ri.reply_text,
                    ri.status,
                    ri.screenshot_path,
                    ri.failure_reason,
                    ri.created_at,
                    c.video_url,
                    c.video_id,
                    c.author,
                    c.text AS comment_text
                FROM tiktok_review_items ri
                JOIN tiktok_comments c ON c.comment_id = ri.comment_id
                WHERE ri.id = ?
                """,
                (review_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_review_status(self, review_id: int, status: ReviewItemStatus, *, reason: str | None = None) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tiktok_review_items
                SET status = ?, failure_reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, reason, review_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown review_id: {review_id}")

    def next_review_item(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    ri.id,
                    ri.comment_id,
                    ri.campaign_slug,
                    ri.matched_keyword,
                    ri.reply_text,
                    ri.status,
                    ri.created_at,
                    c.video_url,
                    c.video_id,
                    c.author,
                    c.text AS comment_text
                FROM tiktok_review_items ri
                JOIN tiktok_comments c ON c.comment_id = ri.comment_id
                WHERE ri.status = ?
                ORDER BY ri.created_at ASC, ri.id ASC
                LIMIT 1
                """,
                (ReviewItemStatus.PENDING_REVIEW,),
            ).fetchone()
        return dict(row) if row else None

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
                CREATE TABLE IF NOT EXISTS tiktok_videos (
                    video_url TEXT PRIMARY KEY,
                    video_id TEXT,
                    author TEXT,
                    caption TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_scanned_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tiktok_video_campaigns (
                    video_url TEXT NOT NULL,
                    campaign_slug TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    approved INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (video_url, campaign_slug),
                    FOREIGN KEY(video_url) REFERENCES tiktok_videos(video_url),
                    FOREIGN KEY(campaign_slug) REFERENCES tiktok_campaigns(slug)
                )
                """
            )
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
                CREATE TABLE IF NOT EXISTS tiktok_campaigns (
                    slug TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    reply_template TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tiktok_campaign_keywords (
                    campaign_slug TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    PRIMARY KEY (campaign_slug, keyword),
                    FOREIGN KEY(campaign_slug) REFERENCES tiktok_campaigns(slug)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tiktok_review_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    comment_id TEXT NOT NULL,
                    campaign_slug TEXT NOT NULL,
                    matched_keyword TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    screenshot_path TEXT,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(comment_id, campaign_slug),
                    FOREIGN KEY(comment_id) REFERENCES tiktok_comments(comment_id),
                    FOREIGN KEY(campaign_slug) REFERENCES tiktok_campaigns(slug)
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
