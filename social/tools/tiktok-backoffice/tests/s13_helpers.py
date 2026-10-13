"""s13 — shared fixtures: the pre-s13 DDL of the campaign tables, and a campaign written exactly
as the console writes it (console ADR 037, plan P7)."""

import sqlite3

PRE_S13_CAMPAIGN_DDL = """
CREATE TABLE tiktok_campaigns (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    reply_template TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE tiktok_campaign_keywords (
    campaign_slug TEXT NOT NULL,
    keyword TEXT NOT NULL,
    PRIMARY KEY (campaign_slug, keyword),
    FOREIGN KEY(campaign_slug) REFERENCES tiktok_campaigns(slug)
);
"""

CONSOLE_SLUG = "console-0123456789ab"
CONSOLE_VIDEO = "https://www.tiktok.com/@dupflodev/video/7657941429938949399"


def write_console_campaign(db, *, slug=CONSOLE_SLUG, keywords=("voicebox", "voicebix"), active=1):
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            INSERT INTO tiktok_campaigns (slug, name, reply_template, active, source, dm_text)
            VALUES (?, 'Voicebox console', 'Je viens de t’envoyer le lien en message privé', ?, 'console', 'Le lien')
            ON CONFLICT(slug) DO UPDATE SET source = 'console', active = excluded.active,
                dm_text = excluded.dm_text, updated_at = CURRENT_TIMESTAMP
            """,
            (slug, active),
        )
        connection.execute("DELETE FROM tiktok_campaign_keywords WHERE campaign_slug = ?", (slug,))
        connection.executemany(
            "INSERT INTO tiktok_campaign_keywords (campaign_slug, keyword) VALUES (?, ?)",
            [(slug, keyword) for keyword in keywords],
        )
        connection.execute(
            "INSERT OR IGNORE INTO tiktok_videos (video_url, video_id, author, active) VALUES (?, ?, '@dupflodev', 1)",
            (CONSOLE_VIDEO, CONSOLE_VIDEO.rsplit("/", 1)[1]),
        )
        connection.execute(
            """
            INSERT INTO tiktok_video_campaigns (video_url, campaign_slug, source, confidence, approved)
            VALUES (?, ?, 'manual', 1.0, 1)
            ON CONFLICT(video_url, campaign_slug) DO UPDATE SET approved = 1, updated_at = CURRENT_TIMESTAMP
            """,
            (CONSOLE_VIDEO, slug),
        )


def dump_campaign(db, slug):
    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT * FROM tiktok_campaigns WHERE slug = ?", (slug,)).fetchone()
        keywords = connection.execute(
            "SELECT keyword FROM tiktok_campaign_keywords WHERE campaign_slug = ? ORDER BY keyword", (slug,)
        ).fetchall()
    return row, keywords
