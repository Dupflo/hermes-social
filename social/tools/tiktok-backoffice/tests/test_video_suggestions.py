import json

from app.cli import run


def test_suggest_video_campaigns_from_caption_keywords_without_approving(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--caption", "Proxy propre pour VPS"] )
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy,proxies", "--reply", "Je viens de t’envoyer le lien en message privé"] )
    run(["--db", str(db), "campaign-upsert", "--slug", "sheet", "--name", "Sheet", "--keywords", "sheet", "--reply", "Je viens de t’envoyer le lien en message privé"] )

    result = json.loads(run(["--db", str(db), "suggest-video-campaigns"]))

    assert result == {"ok": True, "suggested": 1}
    videos = json.loads(run(["--db", str(db), "list-videos", "--with-campaigns"]))["items"]
    assert videos[0]["campaigns"] == [{"campaign_slug": "proxy", "source": "caption_keyword", "confidence": 0.8, "approved": 0}]


def test_approve_video_campaign_suggestion_is_idempotent(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--caption", "Proxy propre pour VPS"] )
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "Je viens de t’envoyer le lien en message privé"] )
    run(["--db", str(db), "suggest-video-campaigns"])

    approved = json.loads(run(["--db", str(db), "approve-video-campaign", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--campaign", "proxy"]))
    again = json.loads(run(["--db", str(db), "approve-video-campaign", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--campaign", "proxy"]))

    assert approved == {"ok": True, "changed": True}
    assert again == {"ok": True, "changed": False}
    videos = json.loads(run(["--db", str(db), "list-videos", "--with-campaigns"]))["items"]
    assert videos[0]["campaigns"] == [{"campaign_slug": "proxy", "source": "operator_approved", "confidence": 1.0, "approved": 1}]
