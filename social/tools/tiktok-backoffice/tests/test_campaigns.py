import json

from app.cli import run
from app.store import TikTokBackofficeStore


def test_campaign_match_creates_single_review_draft(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-comment", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--comment-id", "c-proxy", "--author", "@alice", "--text", "Tu peux envoyer le proxy ?"])

    campaign = json.loads(run([
        "--db", str(db),
        "campaign-upsert",
        "--slug", "proxy",
        "--name", "Proxy",
        "--keywords", "proxy,proxies",
        "--reply", "Je viens de t’envoyer le lien en message privé",
    ]))
    assert campaign == {"ok": True, "slug": "proxy", "result": "created"}

    matched = json.loads(run(["--db", str(db), "match", "--campaign", "proxy"]))

    assert matched["ok"] is True
    assert matched["matched"] == 1
    assert matched["created_drafts"] == 1

    # Idempotent: running the matcher again must not duplicate draft rows.
    again = json.loads(run(["--db", str(db), "match", "--campaign", "proxy"]))
    assert again["matched"] == 1
    assert again["created_drafts"] == 0

    item = json.loads(run(["--db", str(db), "next-review"]))["item"]
    assert item["comment_id"] == "c-proxy"
    assert item["campaign_slug"] == "proxy"
    assert item["reply_text"] == "Je viens de t’envoyer le lien en message privé"
    assert item["status"] == "pending_review"


def test_match_does_not_touch_non_matching_comments(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-comment", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--comment-id", "c1", "--author", "@bob", "--text", "Bonjour"])
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "réponse"] )

    matched = json.loads(run(["--db", str(db), "match", "--campaign", "proxy"]))

    assert matched["matched"] == 0
    assert TikTokBackofficeStore(db).next_review_item() is None


# --- s13: upsert_campaign outcome and console guard ---------------------------------------

import sqlite3

from app.models import Campaign
from s13_helpers import CONSOLE_SLUG, dump_campaign, write_console_campaign


def test_upsert_campaign_reports_created_then_updated(tmp_path):
    store = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")

    assert store.upsert_campaign(Campaign(slug="proxy", name="Proxy", keywords=("proxy",), reply_template="R")) == "created"
    assert store.upsert_campaign(Campaign(slug="proxy", name="Proxy 2", keywords=("proxy",), reply_template="R")) == "updated"


def test_upsert_campaign_skips_a_console_slug(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    store = TikTokBackofficeStore(db)
    write_console_campaign(db)
    before = dump_campaign(db, CONSOLE_SLUG)

    result = store.upsert_campaign(
        Campaign(slug=CONSOLE_SLUG, name="From kanban", keywords=("other",), reply_template="X", active=False)
    )

    assert result == "skipped_console"
    assert dump_campaign(db, CONSOLE_SLUG) == before


def test_upsert_campaign_sees_a_conversion_made_by_another_connection(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    store = TikTokBackofficeStore(db)
    kanban = Campaign(slug="voicebox", name="Voicebox", keywords=("voicebox",), reply_template="R")
    assert store.upsert_campaign(kanban) == "created"
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE tiktok_campaigns SET source = 'console', name = 'Converted' WHERE slug = 'voicebox'")
    before = dump_campaign(db, "voicebox")

    assert store.upsert_campaign(kanban) == "skipped_console"
    assert dump_campaign(db, "voicebox") == before
