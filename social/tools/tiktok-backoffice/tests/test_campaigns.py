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
        "--reply", "Je te mets le lien ici 👍",
    ]))
    assert campaign == {"ok": True, "slug": "proxy"}

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
    assert item["reply_text"] == "Je te mets le lien ici 👍"
    assert item["status"] == "pending_review"


def test_match_does_not_touch_non_matching_comments(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-comment", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--comment-id", "c1", "--author", "@bob", "--text", "Bonjour"])
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "réponse"] )

    matched = json.loads(run(["--db", str(db), "match", "--campaign", "proxy"]))

    assert matched["matched"] == 0
    assert TikTokBackofficeStore(db).next_review_item() is None
