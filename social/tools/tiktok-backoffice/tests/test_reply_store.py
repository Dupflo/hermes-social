"""s24 — additive store for the `tiktok-reply` job (console ADR 055: D3, D6, D7, D8, P2, P7, P15, B3, S1, S3).

Temporary base created by the store itself. `tiktok_outbound_messages` must keep exactly the columns of
meta-webhook's `outbound_messages` (AC 3), compared through PRAGMA on a base created by meta-webhook's own store.
"""

import importlib.util
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from app.models import Campaign
from app.store import TikTokBackofficeStore, sqlite_utc

META_APP = Path(__file__).resolve().parents[2] / "meta-webhook" / "app"
VIDEO = "https://www.tiktok.com/@dupflodev/video/7000"
VIDEO2 = "https://www.tiktok.com/@dupflodev/video/7001"
SQLITE_UTC = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
# Console requeue SQL (console core/replay TIKTOK_REQUEUE_SQL, ADR 056), copied for the S3 test.
CONSOLE_REQUEUE_SQL = """UPDATE tiktok_review_items
SET status = 'pending_review', failure_reason = NULL, updated_at = CURRENT_TIMESTAMP
WHERE id = @id
  AND created_at >= @window
  AND (status IN ('failed', 'needs_manual_public_fallback')
       OR (status = 'sending' AND updated_at < @stale))"""


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def meta_outbound_store(monkeypatch, db):
    base = _load_by_path("meta_sqlite_store_s24", META_APP / "sqlite_store.py")
    monkeypatch.setitem(sys.modules, "app.sqlite_store", base)
    module = _load_by_path("meta_outbound_message_store_s24", META_APP / "outbound_message_store.py")
    return module.OutboundMessageStore(db)


def table_shape(db, table):
    with sqlite3.connect(db) as connection:
        cols = [tuple(row[1:]) for row in connection.execute(f"PRAGMA table_info({table})")]
        indexes = []
        for row in connection.execute(f"PRAGMA index_list({table})"):
            name = row[1]
            if name.startswith("sqlite_autoindex"):
                continue
            indexes.append([r[2] for r in connection.execute(f"PRAGMA index_info({name})")])
    return cols, indexes


def ago(seconds):
    return sqlite_utc(time.time() - seconds)


@pytest.fixture
def store(tmp_path):
    s = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")
    s.upsert_campaign(Campaign(slug="proxy", name="Campagne Proxy", keywords=("proxy",), reply_template="R"))
    s.upsert_campaign(Campaign(slug="guide", name="Guide", keywords=("guide",), reply_template="R"))
    with sqlite3.connect(s.database) as connection:
        for url, vid in ((VIDEO, "7000"), (VIDEO2, "7001")):
            connection.execute("INSERT INTO tiktok_videos (video_url, video_id) VALUES (?, ?)", (url, vid))
            connection.execute(
                "INSERT INTO tiktok_video_campaigns (video_url, campaign_slug, approved) VALUES (?, 'proxy', 1)", (url,)
            )
        connection.execute("UPDATE tiktok_campaigns SET dm_text = ? WHERE slug = 'proxy'", ("Ligne 1\nLigne 2",))
    return s


def add_item(store, n, *, created_ago=60, status="pending_review", author=None, slug="proxy", video=VIDEO, baseline_ago=86400 * 30):
    with sqlite3.connect(store.database) as connection:
        if baseline_ago is not None:
            connection.execute(
                "UPDATE tiktok_videos SET baseline_at = COALESCE(baseline_at, ?) WHERE video_url = ?", (ago(baseline_ago), video)
            )
        connection.execute(
            "INSERT INTO tiktok_comments (comment_id, video_url, author, text) VALUES (?, ?, ?, 'proxy')",
            (f"c{n}", video, author if author is not None else f"@user{n}"),
        )
        cursor = connection.execute(
            """INSERT INTO tiktok_review_items (comment_id, campaign_slug, matched_keyword, reply_text, status, created_at, updated_at)
               VALUES (?, ?, 'proxy', 'R', ?, ?, ?)""",
            (f"c{n}", slug, status, ago(created_ago), ago(created_ago)),
        )
        return cursor.lastrowid


def row(store, review_id):
    with sqlite3.connect(store.database) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute("SELECT * FROM tiktok_review_items WHERE id = ?", (review_id,)).fetchone())


def outbound(store):
    with sqlite3.connect(store.database) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(r) for r in connection.execute("SELECT * FROM tiktok_outbound_messages ORDER BY id")]


# --- schema ---------------------------------------------------------------------------------------------------------


def test_outbound_table_has_exactly_the_columns_of_meta_outbound_messages(tmp_path, monkeypatch, store):
    meta_db = tmp_path / "meta.sqlite3"
    meta_outbound_store(monkeypatch, meta_db)
    meta_cols, meta_idx = table_shape(meta_db, "outbound_messages")
    cols, idx = table_shape(store.database, "tiktok_outbound_messages")
    assert cols == meta_cols
    assert idx == meta_idx == [["platform", "source_id"]]


def test_schema_is_idempotent_and_adds_baseline_at_to_an_s23_base(tmp_path):
    db = tmp_path / "s23.sqlite3"
    TikTokBackofficeStore(db)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE tiktok_outbound_messages")
        connection.execute("ALTER TABLE tiktok_videos DROP COLUMN baseline_at")
        connection.execute("INSERT INTO tiktok_videos (video_url, video_id) VALUES ('u', '1')")
    TikTokBackofficeStore(db)
    TikTokBackofficeStore(db)
    with sqlite3.connect(db) as connection:
        cols = [r[1] for r in connection.execute("PRAGMA table_info(tiktok_videos)")]
        assert cols[-1] == "baseline_at"
        assert connection.execute("SELECT video_url, baseline_at FROM tiktok_videos").fetchall() == [("u", None)]
        assert connection.execute("SELECT COUNT(*) FROM tiktok_outbound_messages").fetchone() == (0,)


def test_sqlite_utc_format():
    assert sqlite_utc(0) == "1970-01-01 00:00:00"
    assert SQLITE_UTC.match(sqlite_utc(time.time()))


# --- queue ----------------------------------------------------------------------------------------------------------


def test_reply_queue_matches_pending_review_items_ids_and_order(store):
    add_item(store, 1, created_ago=300)
    add_item(store, 2, created_ago=500)
    add_item(store, 3, created_ago=100, status="posted")
    add_item(store, 4, created_ago=200, slug="guide")
    add_item(store, 5, created_ago=200, author="Display Name")
    add_item(store, 6, created_ago=400, video=VIDEO2)
    queue = store.reply_queue(10)
    assert [r["id"] for r in queue] == [r["id"] for r in store.pending_review_items(10)]
    assert len(queue) == 3
    first = queue[0]
    assert set(first) == {
        "id", "comment_id", "campaign_slug", "matched_keyword", "reply_text", "created_at", "author",
        "comment_text", "video_url", "campaign_found", "campaign_active", "dm_text",
    }
    assert first["campaign_found"] == 1 and first["campaign_active"] == 1
    assert first["dm_text"] == "Ligne 1\nLigne 2"


def test_reply_queue_unknown_campaign(store):
    review_id = add_item(store, 1)
    with sqlite3.connect(store.database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("UPDATE tiktok_review_items SET campaign_slug = 'systeme' WHERE id = ?", (review_id,))
    [item] = store.reply_queue(10)
    assert item["campaign_found"] == 0
    assert item["dm_text"] is None


def test_reply_queue_limit(store):
    for n in range(4):
        add_item(store, n, created_ago=100 + n)
    assert len(store.reply_queue(2)) == 2


# --- expiration (D3, P15) -------------------------------------------------------------------------------------------


def test_expire_stale_pending_strict_bound_and_queue_only(store):
    week = 7 * 86400
    old = add_item(store, 1, created_ago=week + 60)
    young = add_item(store, 2, created_ago=week - 60)
    manual = add_item(store, 3, created_ago=week + 60, status="needs_manual_public_fallback")
    guide = add_item(store, 4, created_ago=week + 60, slug="guide")
    noat = add_item(store, 5, created_ago=week + 60, author="Display")
    cutoff = ago(week)
    assert store.expire_stale_pending(cutoff) == 1
    r = row(store, old)
    assert (r["status"], r["failure_reason"]) == ("expired", "older_than_7d")
    assert SQLITE_UTC.match(r["updated_at"])
    assert row(store, young)["status"] == "pending_review"
    assert row(store, manual)["status"] == "needs_manual_public_fallback"
    assert row(store, guide)["status"] == "pending_review"
    assert row(store, noat)["status"] == "pending_review"


def test_expire_stale_pending_exact_second_bound(store):
    now = time.time()
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_videos SET baseline_at = '2000-01-01 00:00:00'")
    a = add_item(store, 1, baseline_ago=None)
    b = add_item(store, 2, baseline_ago=None)
    week = 7 * 86400
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_review_items SET created_at = ? WHERE id = ?", (sqlite_utc(now - week - 1), a))
        connection.execute("UPDATE tiktok_review_items SET created_at = ? WHERE id = ?", (sqlite_utc(now - week + 1), b))
    assert store.expire_stale_pending(sqlite_utc(now - week)) == 1
    assert row(store, a)["status"] == "expired"
    assert row(store, b)["status"] == "pending_review"


def test_legacy_backlog_items_are_not_eligible_and_get_closed(store):
    # B3: an item ingested by the legacy path (no console baseline for its video) never gets a DM.
    legacy = add_item(store, 1, created_ago=2 * 86400, video=VIDEO2, baseline_ago=None)
    before_baseline = add_item(store, 2, created_ago=40 * 86400, baseline_ago=None)
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_videos SET baseline_at = ? WHERE video_url = ?", (ago(86400), VIDEO))
    eligible = add_item(store, 3, created_ago=60, baseline_ago=None)
    assert [r["id"] for r in store.reply_queue(10)] == [eligible]
    assert store.claim_review_item(legacy, ago(7 * 86400)) is False
    assert store.expire_legacy_backlog() == 2
    for rid in (legacy, before_baseline):
        r = row(store, rid)
        assert (r["status"], r["failure_reason"]) == ("expired", "legacy_backlog")
    assert row(store, eligible)["status"] == "pending_review"


def test_console_read_after_baseline_is_eligible(store):
    store.ingest_comments(video_url=VIDEO2, comments=[{"id": "x1", "author": "@a", "text": "proxy"}], console_read=True)
    store.ingest_comments(video_url=VIDEO2, comments=[{"id": "x2", "author": "@b", "text": "proxy please"}], console_read=True)
    ids = [r["comment_id"] for r in store.reply_queue(10)]
    assert ids == ["x1", "x2"]


def test_legacy_ingest_is_never_eligible(store):
    store.ingest_comments(video_url=VIDEO2, comments=[{"id": "x1", "author": "@a", "text": "proxy"}])
    assert store.reply_queue(10) == []
    assert store.expire_legacy_backlog() == 1


# --- claim, release, finish (D8, S3) --------------------------------------------------------------------------------


def test_claim_is_conditional(store):
    rid = add_item(store, 1)
    cutoff = ago(7 * 86400)
    assert store.claim_review_item(rid, cutoff) is True
    r = row(store, rid)
    assert r["status"] == "sending" and r["failure_reason"] is None
    assert SQLITE_UTC.match(r["updated_at"])
    assert store.claim_review_item(rid, cutoff) is False


def test_claim_refuses_old_and_non_eligible_items(store):
    old = add_item(store, 1, created_ago=8 * 86400)
    assert store.claim_review_item(old, ago(7 * 86400)) is False
    early = add_item(store, 2, created_ago=40 * 86400, video=VIDEO2, baseline_ago=None)
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_videos SET baseline_at = ? WHERE video_url = ?", (ago(86400), VIDEO2))
    assert store.claim_review_item(early, ago(50 * 86400)) is False


def test_old_sending_item_is_never_claimed_again(store):
    rid = add_item(store, 1, status="sending", created_ago=3600)
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_review_items SET updated_at = ? WHERE id = ?", (ago(7200), rid))
    assert store.claim_review_item(rid, ago(7 * 86400)) is False
    assert store.reply_queue(10) == []


def test_release_only_from_sending(store):
    rid = add_item(store, 1)
    assert store.release_review_item(rid) is False
    store.claim_review_item(rid, ago(7 * 86400))
    assert store.release_review_item(rid) is True
    assert row(store, rid)["status"] == "pending_review"


def test_finish_writes_status_only_when_sending(store):
    rid = add_item(store, 1)
    store.claim_review_item(rid, ago(7 * 86400))
    assert store.finish_review_item(rid, "posted", reason=None) is True
    assert (row(store, rid)["status"], row(store, rid)["failure_reason"]) == ("posted", None)
    assert store.finish_review_item(rid, "failed", reason="not_verified") is False
    assert row(store, rid)["status"] == "posted"


def test_s3_requeued_between_send_and_finish_keeps_the_outbound_row(store):
    rid = add_item(store, 1)
    store.claim_review_item(rid, ago(7 * 86400))
    out_id = store.record_outbound(review_id=rid, recipient="@user1", message_type="private_reply", text="Ligne 1\nLigne 2", status="sent")
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_review_items SET updated_at = ? WHERE id = ?", (ago(31 * 60), rid))
        params = {"id": rid, "window": ago(7 * 86400), "stale": ago(30 * 60)}
        sql = CONSOLE_REQUEUE_SQL.replace("@id", ":id").replace("@window", ":window").replace("@stale", ":stale")
        assert connection.execute(sql, params).rowcount == 1
    assert store.finish_review_item(rid, "posted", reason=None) is False
    assert row(store, rid)["status"] == "pending_review"
    [line] = outbound(store)
    assert line["id"] == out_id
    assert store.sent_outbound_types(rid) == {"private_reply"}


def test_record_outbound_columns(store):
    rid = add_item(store, 1)
    store.record_outbound(review_id=rid, recipient="@user1", message_type="public_reply", text="R", status="failed")
    [line] = outbound(store)
    assert line["platform"] == "tiktok"
    assert line["source_type"] == "comment"
    assert line["source_id"] == str(rid)
    assert line["recipient_id"] == "@user1"
    assert line["message_type"] == "public_reply"
    assert line["message_text"] == "R"
    assert line["meta_response_id"] is None
    assert line["status"] == "failed"
    assert SQLITE_UTC.match(line["created_at"])
    assert store.sent_outbound_types(rid) == set()


def test_outbound_count_since_is_a_sliding_hour_with_failed_rows(store):
    rid = add_item(store, 1)
    for status, age in (("sent", 59 * 60), ("failed", 30 * 60), ("sent", 61 * 60)):
        store.record_outbound(review_id=rid, recipient="@u", message_type="private_reply", text="t", status=status)
        with sqlite3.connect(store.database) as connection:
            connection.execute(
                "UPDATE tiktok_outbound_messages SET created_at = ? WHERE id = (SELECT MAX(id) FROM tiktok_outbound_messages)",
                (ago(age),),
            )
    assert store.outbound_count_since(ago(3600)) == 2


# --- baseline (P7, S1) ----------------------------------------------------------------------------------------------


def test_needs_baseline(store):
    assert store.needs_baseline(VIDEO2) is True
    store.ingest_comments(video_url=VIDEO2, comments=[{"id": "z", "author": "@a", "text": "hello"}])
    assert store.needs_baseline(VIDEO2) is False  # legacy comments exist
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_videos SET baseline_at = '2026-01-01 00:00:00' WHERE video_url = ?", (VIDEO,))
    assert store.needs_baseline(VIDEO) is False


def baseline_of(store, url):
    with sqlite3.connect(store.database) as connection:
        return connection.execute("SELECT baseline_at FROM tiktok_videos WHERE video_url = ?", (url,)).fetchone()[0]


def test_console_read_sets_baseline_even_with_zero_comments(store):
    store.ingest_comments(video_url=VIDEO2, comments=[], console_read=True)
    assert SQLITE_UTC.match(baseline_of(store, VIDEO2))
    first = baseline_of(store, VIDEO2)
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_videos SET baseline_at = '2026-01-01 00:00:00' WHERE video_url = ?", (VIDEO2,))
    store.ingest_comments(video_url=VIDEO2, comments=[], console_read=True)
    assert baseline_of(store, VIDEO2) == "2026-01-01 00:00:00" != first


def test_s1_failed_ingestion_sets_no_baseline_and_creates_nothing(store):
    with pytest.raises(AttributeError):
        store.ingest_comments(
            video_url=VIDEO2, comments=[{"id": "ok", "author": "@a", "text": "proxy"}, None], console_read=True
        )
    assert baseline_of(store, VIDEO2) is None
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tiktok_review_items").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM tiktok_comments").fetchone() == (0,)


def test_legacy_ingest_never_sets_baseline(store):
    store.ingest_comments(video_url=VIDEO2, comments=[{"id": "x", "author": "@a", "text": "proxy"}])
    assert baseline_of(store, VIDEO2) is None


def test_baseline_ingest_creates_expired_items(store):
    result = store.ingest_comments(
        video_url=VIDEO2,
        comments=[{"id": "x1", "author": "@a", "text": "proxy"}, {"id": "x2", "author": "Display", "text": "proxy"}],
        console_read=True,
        baseline=True,
    )
    assert result == {"ingested": 2, "created_reviews": 2}
    with sqlite3.connect(store.database) as connection:
        rows = connection.execute("SELECT status, failure_reason FROM tiktok_review_items ORDER BY id").fetchall()
    assert rows == [("expired", "first_scan_baseline"), ("expired", "first_scan_baseline")]
    assert store.reply_queue(10) == []


def test_default_ingest_is_unchanged(store):
    store.ingest_comments(
        video_url=VIDEO2,
        comments=[{"id": "x1", "author": "@a", "text": "proxy"}, {"id": "x2", "author": "Display", "text": "proxy"}],
    )
    with sqlite3.connect(store.database) as connection:
        rows = connection.execute("SELECT status FROM tiktok_review_items ORDER BY id").fetchall()
    assert rows == [("pending_review",), ("needs_manual_handle",)]
