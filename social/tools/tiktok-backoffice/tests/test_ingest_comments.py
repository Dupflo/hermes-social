import json

from app.cli import run


def _setup_video_campaign(db):
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--caption", "Commente proxy"])
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy,proxi", "--reply", "Je viens de t’envoyer le lien en message privé"])
    run(["--db", str(db), "assign-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--campaign", "proxy", "--source", "manual"])


def test_ingest_comments_json_creates_review_items_for_approved_video_campaign(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    _setup_video_campaign(db)
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps({
        "comments": [
            {"id": "c1", "author": "@alice", "text": "proxy stp"},
            {"id": "c2", "author": "@bob", "text": "bonjour"},
        ]
    }))

    result = json.loads(run(["--db", str(db), "ingest-comments", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--json-file", str(comments)]))

    assert result == {"ok": True, "ingested": 2, "created_reviews": 1}
    item = json.loads(run(["--db", str(db), "next-review"]))["item"]
    assert item["comment_id"] == "c1"
    assert item["campaign_slug"] == "proxy"
    assert item["matched_keyword"] == "proxy"
    assert item["reply_text"] == "Je viens de t’envoyer le lien en message privé"


def test_ingest_comments_is_idempotent_and_uses_fingerprint_without_id(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    _setup_video_campaign(db)
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([{"author": "@alice", "text": "proxi", "created_time": 1234}]))

    first = json.loads(run(["--db", str(db), "ingest-comments", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--json-file", str(comments)]))
    second = json.loads(run(["--db", str(db), "ingest-comments", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--json-file", str(comments)]))

    assert first == {"ok": True, "ingested": 1, "created_reviews": 1}
    assert second == {"ok": True, "ingested": 0, "created_reviews": 0}
    items = json.loads(run(["--db", str(db), "list"]))["items"]
    assert len(items) == 1
    assert items[0]["comment_id"].startswith("fp:")


def test_ingest_comments_dedupes_when_extractor_id_changes(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    _setup_video_campaign(db)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps([{"id": "@alice:100:proxy", "author": "@alice", "text": "proxy"}]))
    second.write_text(json.dumps([{"id": "@alice:stablehash", "author": "@alice", "text": "proxy"}]))

    created = json.loads(run(["--db", str(db), "ingest-comments", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--json-file", str(first)]))
    repeated = json.loads(run(["--db", str(db), "ingest-comments", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--json-file", str(second)]))

    assert created == {"ok": True, "ingested": 1, "created_reviews": 1}
    assert repeated == {"ok": True, "ingested": 0, "created_reviews": 0}
    assert len(json.loads(run(["--db", str(db), "list"]))["items"]) == 1


def test_ingest_comments_strips_tiktok_visual_date_suffix(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    _setup_video_campaign(db)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps([{"id": "c1", "author": "@alice", "text": "proxy 7-12"}]))
    second.write_text(json.dumps([{"id": "c2", "author": "@alice", "text": "proxy"}]))

    created = json.loads(run(["--db", str(db), "ingest-comments", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--json-file", str(first)]))
    repeated = json.loads(run(["--db", str(db), "ingest-comments", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--json-file", str(second)]))
    item = json.loads(run(["--db", str(db), "list"]))["items"][0]

    assert created == {"ok": True, "ingested": 1, "created_reviews": 1}
    assert repeated == {"ok": True, "ingested": 0, "created_reviews": 0}
    assert item["text"] == "proxy"


def test_ingest_comments_ignores_unapproved_video_campaign(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--caption", "Commente proxy"])
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "Je viens de t’envoyer le lien en message privé"])
    run(["--db", str(db), "suggest-video-campaigns"])  # suggestion, not approved
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([{"id": "c1", "author": "@alice", "text": "proxy"}]))

    result = json.loads(run(["--db", str(db), "ingest-comments", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--json-file", str(comments)]))

    assert result == {"ok": True, "ingested": 1, "created_reviews": 0}
    assert json.loads(run(["--db", str(db), "next-review"]))["item"] is None


def test_ingest_comments_routes_display_name_only_to_manual_handle(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    _setup_video_campaign(db)
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([{"id": "display-only", "author": "Alexandra Crabé", "text": "proxy"}]))

    result = json.loads(run(["--db", str(db), "ingest-comments", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--json-file", str(comments)]))

    assert result == {"ok": True, "ingested": 1, "created_reviews": 1}
    items = json.loads(run(["--db", str(db), "list"]))["items"]
    assert items[0]["author"] == "Alexandra Crabé"
    assert json.loads(run(["--db", str(db), "next-review"]))["item"] is None

    import sqlite3
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT status, failure_reason FROM tiktok_review_items WHERE comment_id='display-only'").fetchone()
    assert row["status"] == "needs_manual_handle"
    assert "exact @handle" in row["failure_reason"]
