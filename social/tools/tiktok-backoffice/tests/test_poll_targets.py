import json

from app.cli import run


def test_poll_targets_returns_only_approved_active_video_campaigns(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/1", "--caption", "Commente proxy"])
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/2", "--caption", "Commente gstack"])
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "Je viens de t’envoyer le lien en message privé"])
    run(["--db", str(db), "campaign-upsert", "--slug", "gstack", "--name", "Gstack", "--keywords", "gstack", "--reply", "Je viens de t’envoyer le lien en message privé"])
    run(["--db", str(db), "assign-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/1", "--campaign", "proxy", "--source", "manual"])
    run(["--db", str(db), "suggest-video-campaigns"])  # creates unapproved gstack suggestion only

    result = json.loads(run(["--db", str(db), "poll-targets"]))

    assert result == {
        "ok": True,
        "items": [
            {
                "video_url": "https://www.tiktok.com/@dupflodev/video/1",
                "video_id": "1",
                "caption": "Commente proxy",
                "campaigns": [
                    {"slug": "proxy", "keywords": ["proxy"], "reply_template": "Je viens de t’envoyer le lien en message privé"}
                ],
            }
        ],
    }


def test_poll_targets_limit(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    for i in range(3):
        run(["--db", str(db), "add-video", "--video-url", f"https://www.tiktok.com/@dupflodev/video/{i}", "--caption", "Commente proxy"])
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "Je viens de t’envoyer le lien en message privé"])
    for i in range(3):
        run(["--db", str(db), "assign-video", "--video-url", f"https://www.tiktok.com/@dupflodev/video/{i}", "--campaign", "proxy"])

    result = json.loads(run(["--db", str(db), "poll-targets", "--limit", "2"]))

    assert len(result["items"]) == 2
