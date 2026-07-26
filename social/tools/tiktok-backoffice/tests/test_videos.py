import json

from app.cli import run


def test_add_and_list_video(tmp_path):
    db = tmp_path / "tiktok.sqlite3"

    added = json.loads(run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--caption", "Proxy propre pour VPS"]))

    assert added == {"ok": True, "changed": True}
    videos = json.loads(run(["--db", str(db), "list-videos"]))["items"]
    assert videos[0]["video_id"] == "123"
    assert videos[0]["video_url"] == "https://www.tiktok.com/@dupflodev/video/123"
    assert videos[0]["caption"] == "Proxy propre pour VPS"
    assert videos[0]["active"] == 1


def test_assign_video_to_campaign_is_idempotent(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--caption", "Proxy propre"] )
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "Je viens de t’envoyer le lien en message privé"] )

    assigned = json.loads(run(["--db", str(db), "assign-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--campaign", "proxy", "--source", "manual"]))
    again = json.loads(run(["--db", str(db), "assign-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--campaign", "proxy", "--source", "manual"]))

    assert assigned == {"ok": True, "changed": True}
    assert again == {"ok": True, "changed": False}
    videos = json.loads(run(["--db", str(db), "list-videos", "--with-campaigns"]))["items"]
    assert videos[0]["campaigns"] == [{"campaign_slug": "proxy", "source": "manual", "confidence": 1.0, "approved": 1}]
