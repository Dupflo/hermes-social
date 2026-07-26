import json

from app.cli import run


def test_suggest_video_campaigns_from_caption_keywords_without_approving(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--caption", "Commente proxy et je t envoie le repo"] )
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


def test_suggest_video_campaigns_uses_comment_cta_not_generic_caption_mentions(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/789", "--caption", "Un skill très populaire. Dis-moi en commentaire le prochain outil à tester."])
    run(["--db", str(db), "campaign-upsert", "--slug", "skill", "--name", "Skill", "--keywords", "skill", "--reply", "Je viens de t’envoyer le lien en message privé"])

    result = json.loads(run(["--db", str(db), "suggest-video-campaigns"]))

    assert result == {"ok": True, "suggested": 0}
    assert json.loads(run(["--db", str(db), "list-videos", "--with-campaigns"]))["items"][0]["campaigns"] == []


def test_suggest_video_campaigns_replaces_stale_unapproved_caption_suggestions(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/789", "--caption", "Un skill populaire. Dis-moi en commentaire le prochain outil."])
    run(["--db", str(db), "campaign-upsert", "--slug", "skill", "--name", "Skill", "--keywords", "skill", "--reply", "Je viens de t’envoyer le lien en message privé"])
    import sqlite3
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO tiktok_video_campaigns (video_url, campaign_slug, source, confidence, approved) VALUES (?, ?, ?, ?, ?)", ("https://www.tiktok.com/@dupflodev/video/789", "skill", "caption_keyword", 0.8, 0))

    result = json.loads(run(["--db", str(db), "suggest-video-campaigns"]))

    assert result == {"ok": True, "suggested": 0}
    assert json.loads(run(["--db", str(db), "list-videos", "--with-campaigns"]))["items"][0]["campaigns"] == []
