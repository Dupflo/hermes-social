import json

from app import cli
from app.cli import run


def _setup_video_campaign(db):
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--caption", "Commente proxy"])
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "Je viens de t’envoyer le lien en message privé"])
    run(["--db", str(db), "assign-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--campaign", "proxy", "--source", "manual"])


def test_extract_dom_comments_filters_recommendations_and_navigation():
    from app.camofox_reader import extract_comments_from_dom_result

    result = {
        "url": "https://www.tiktok.com/@dupflodev/video/123",
        "logged_in": True,
        "comment_nodes": [
            {"author": "@alice", "text": "proxy stp", "comment_id": "c1"},
            {"author": "TikTok", "text": "Search"},
            {"author": "@suggested", "text": "You may like"},
            {"author": "@bob", "text": "bonjour", "comment_id": "c2"},
            {"author": "@alice", "text": "proxy stp", "comment_id": "c1"},
        ],
    }

    comments = extract_comments_from_dom_result(result)

    assert comments == [
        {"id": "c1", "author": "@alice", "text": "proxy stp"},
        {"id": "c2", "author": "@bob", "text": "bonjour"},
    ]



def test_extract_dom_comments_filters_related_tab_recommendation_cards():
    from app.camofox_reader import extract_comments_from_dom_result

    result = {
        "url": "https://www.tiktok.com/@dupflodev/video/123",
        "logged_in": False,
        "related_tab_active": True,
        "comment_nodes": [
            {
                "author": "@dupflodev",
                "text": "Un repo GitHub compile les system prompts leakés de Claude, ChatGPT, Cursor, v0. Et partout le même commentaire : plus besoin de payer.",
                "comment_id": "css-649dsf-5e6d46e3--DivInfoContainer e9pwkrg3",
            },
            {"author": "@alice", "text": "proxy stp", "comment_id": "real-c1"},
        ],
    }

    comments = extract_comments_from_dom_result(result)

    assert comments == [{"id": "real-c1", "author": "@alice", "text": "proxy stp"}]


def test_fetch_comments_camofox_cli_can_ingest_without_publishing(tmp_path, monkeypatch):
    db = tmp_path / "tiktok.sqlite3"
    _setup_video_campaign(db)

    def fake_fetch(**kwargs):
        assert kwargs["video_url"] == "https://www.tiktok.com/@dupflodev/video/123"
        return {
            "ok": True,
            "video_url": kwargs["video_url"],
            "logged_in": True,
            "comments": [
                {"id": "c1", "author": "@alice", "text": "proxy"},
                {"id": "c2", "author": "@bob", "text": "hors sujet"},
            ],
            "diagnostics": {"comment_nodes": 2},
        }

    monkeypatch.setattr(cli, "fetch_comments_from_camofox", fake_fetch)

    result = json.loads(run([
        "--db", str(db),
        "fetch-comments-camofox",
        "--video-url", "https://www.tiktok.com/@dupflodev/video/123",
        "--ingest",
    ]))

    assert result["ok"] is True
    assert result["fetched"] == 2
    assert result["ingest"] == {"ingested": 2, "created_reviews": 1}
    item = json.loads(run(["--db", str(db), "next-review"]))["item"]
    assert item["comment_id"] == "c1"
    assert item["reply_text"] == "Je viens de t’envoyer le lien en message privé"


def test_fetch_comments_camofox_cli_reports_fetch_errors(tmp_path, monkeypatch):
    db = tmp_path / "tiktok.sqlite3"

    def fake_fetch(**kwargs):
        return {"ok": False, "error": "camofox_unreachable", "comments": []}

    monkeypatch.setattr(cli, "fetch_comments_from_camofox", fake_fetch)

    result = json.loads(run([
        "--db", str(db),
        "fetch-comments-camofox",
        "--video-url", "https://www.tiktok.com/@dupflodev/video/123",
    ]))

    assert result == {"ok": False, "error": "camofox_unreachable", "comments": [], "fetched": 0}
