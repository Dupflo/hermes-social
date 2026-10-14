"""s23 — review queue and status counts of the store equal the cron's own helpers (console ADR 054, D2/P6).

The oracle is scripts/tiktok_cron_auto_dm.py of the repo, loaded by importlib (pattern of test_cron_auto_dm_db.py).
"""

import importlib.util
import sqlite3
from pathlib import Path

from app.models import Campaign, TikTokVideo
from app.store import TikTokBackofficeStore

CRON = Path(__file__).resolve().parents[1] / "scripts" / "tiktok_cron_auto_dm.py"


def _oracle(db):
    spec = importlib.util.spec_from_file_location("tiktok_cron_auto_dm_s23_queue", CRON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DB = db
    return module


def _fixture(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    store = TikTokBackofficeStore(db)
    for slug in ("proxy", "guide"):
        store.upsert_campaign(Campaign(slug=slug, name=slug, keywords=(slug,), reply_template="R"))
    video = "https://www.tiktok.com/@dupflodev/video/1"
    store.add_video(TikTokVideo(video_url=video))
    rows = [
        # (comment_id, author, campaign, status, created_at)
        ("c1", "@alice", "proxy", "pending_review", "2026-08-02 10:00:00"),
        ("c2", "@bob", "proxy", "pending_review", "2026-08-02 10:00:00"),  # same date: id decides
        ("c3", "Display Name", "proxy", "pending_review", "2026-08-01 10:00:00"),  # no @handle
        ("c4", "@carol", "guide", "pending_review", "2026-07-01 10:00:00"),  # guide excluded
        ("c5", "@dave", "proxy", "ignored", "2026-07-01 10:00:00"),
        ("c6", "Erin", "proxy", "needs_manual_handle", "2026-07-01 10:00:00"),
        ("c7", "@frank", "proxy", "pending_review", "2026-08-03 10:00:00"),
        ("c8", "@gina", "proxy", "pending_review", "2026-08-04 10:00:00"),
    ]
    with sqlite3.connect(db) as connection:
        for comment_id, author, slug, status, created in rows:
            connection.execute(
                "INSERT INTO tiktok_comments (comment_id, video_url, author, text) VALUES (?, ?, ?, 'proxy')",
                (comment_id, video, author),
            )
            connection.execute(
                """
                INSERT INTO tiktok_review_items (comment_id, campaign_slug, matched_keyword, reply_text, status, created_at)
                VALUES (?, ?, 'proxy', 'R', ?, ?)
                """,
                (comment_id, slug, status, created),
            )
    return store, db


def test_pending_review_items_equal_the_cron_pending_items(tmp_path):
    store, db = _fixture(tmp_path)
    oracle = _oracle(db)

    for limit in (1, 3, 10):
        assert store.pending_review_items(limit) == oracle.pending_items(limit)
    assert [row["author"] for row in store.pending_review_items(3)] == ["@alice", "@bob", "@frank"]


def test_review_status_counts_equal_the_cron_status_counts(tmp_path):
    store, db = _fixture(tmp_path)

    assert store.review_status_counts() == _oracle(db).status_counts()
    assert store.review_status_counts() == {"pending_review": 6, "ignored": 1, "needs_manual_handle": 1}
