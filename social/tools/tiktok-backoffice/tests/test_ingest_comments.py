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
