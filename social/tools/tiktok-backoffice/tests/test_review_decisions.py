import json

from app.cli import run


def _make_review(db):
    run(["--db", str(db), "add-comment", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--comment-id", "c1", "--author", "@alice", "--text", "proxy"] )
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "Je te mets le lien ici 👍"] )
    run(["--db", str(db), "match", "--campaign", "proxy"] )
    return json.loads(run(["--db", str(db), "next-review"]))["item"]["id"]


def test_approve_draft_moves_review_to_approved_for_draft(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    review_id = _make_review(db)

    result = json.loads(run(["--db", str(db), "approve-draft", "--review-id", str(review_id)]))

    assert result == {"ok": True, "review_id": review_id, "status": "approved_for_draft"}
    assert json.loads(run(["--db", str(db), "next-review"]))["item"] is None
    approved = json.loads(run(["--db", str(db), "review", "--review-id", str(review_id)]))["item"]
    assert approved["status"] == "approved_for_draft"
    assert approved["reply_text"] == "Je te mets le lien ici 👍"


def test_ignore_review_records_operator_reason(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    review_id = _make_review(db)

    result = json.loads(run(["--db", str(db), "ignore-review", "--review-id", str(review_id), "--reason", "hors sujet"]))

    assert result == {"ok": True, "review_id": review_id, "status": "ignored"}
    ignored = json.loads(run(["--db", str(db), "review", "--review-id", str(review_id)]))["item"]
    assert ignored["status"] == "ignored"
    assert ignored["failure_reason"] == "hors sujet"
