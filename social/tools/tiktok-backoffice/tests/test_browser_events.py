import json

from app.cli import run
from app.store import TikTokBackofficeStore


def test_captcha_needed_records_browser_event(tmp_path):
    db = tmp_path / "tiktok.sqlite3"

    result = json.loads(
        run([
            "--db", str(db),
            "captcha-needed",
            "--video-url", "https://www.tiktok.com/@dupflodev",
            "--screenshot-path", "/opt/data/browser_screenshots/example.png",
        ])
    )

    assert result["ok"] is True
    assert result["status"] == "needs_manual_captcha"
    events = TikTokBackofficeStore(db).recent_browser_events()
    assert events[0]["event_type"] == "needs_manual_captcha"
    assert events[0]["screenshot_path"] == "/opt/data/browser_screenshots/example.png"


def test_browser_draft_filled_records_event(tmp_path):
    db = tmp_path / "tiktok.sqlite3"

    result = json.loads(
        run([
            "--db", str(db),
            "browser-draft-filled",
            "--video-url", "https://www.tiktok.com/@dupflodev/video/1",
            "--screenshot-path", "/opt/data/browser_screenshots/draft.png",
        ])
    )

    assert result["ok"] is True
    assert result["status"] == "browser_draft_filled_not_posted"
    event = TikTokBackofficeStore(db).recent_browser_events()[0]
    assert event["event_type"] == "browser_draft_filled_not_posted"
