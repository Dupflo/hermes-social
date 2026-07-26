import json

from app.cli import run


def _approved_review(db):
    run(["--db", str(db), "add-comment", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--comment-id", "c-browser", "--author", "@alice", "--text", "proxy"] )
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "Je viens de t’envoyer le lien en message privé"] )
    run(["--db", str(db), "match", "--campaign", "proxy"] )
    review_id = json.loads(run(["--db", str(db), "next-review"]))["item"]["id"]
    run(["--db", str(db), "approve-draft", "--review-id", str(review_id)])
    return review_id


def test_next_browser_draft_returns_approved_item(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    review_id = _approved_review(db)

    item = json.loads(run(["--db", str(db), "next-browser-draft"]))["item"]

    assert item["id"] == review_id
    assert item["status"] == "approved_for_draft"
    assert item["video_url"] == "https://www.tiktok.com/@dupflodev/video/123"
    assert item["reply_text"] == "Je viens de t’envoyer le lien en message privé"


def test_browser_drafted_marks_review_and_records_event(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    review_id = _approved_review(db)

    result = json.loads(run(["--db", str(db), "browser-drafted", "--review-id", str(review_id), "--screenshot-path", "/opt/data/browser_screenshots/draft.png"]))

    assert result == {"ok": True, "review_id": review_id, "status": "drafted_in_browser"}
    review = json.loads(run(["--db", str(db), "review", "--review-id", str(review_id)]))["item"]
    assert review["status"] == "drafted_in_browser"
    assert review["screenshot_path"] == "/opt/data/browser_screenshots/draft.png"
    assert json.loads(run(["--db", str(db), "next-browser-draft"]))["item"] is None
    events = json.loads(run(["--db", str(db), "browser-events"]))["items"]
    assert events[0]["event_type"] == "browser_draft_filled_not_posted"
