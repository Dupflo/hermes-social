"""s23 — AC 4: the video of a console campaign (s13, source='console') is scanned and its comments create review
items. Integration test on a COPY of the base (console ADR 054)."""

import hashlib
import shutil
import sqlite3

from app import tiktok_scan as ts
from app.captcha_solver import CaptchaReport
from app.models import Campaign, TikTokVideo
from app.store import TikTokBackofficeStore
from s13_helpers import CONSOLE_SLUG, CONSOLE_VIDEO, write_console_campaign

KANBAN_VIDEO = "https://www.tiktok.com/@dupflodev/video/7000000000000000001"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_console_campaign_video_is_scanned_on_a_copy_of_the_base(tmp_path):
    original = tmp_path / "tiktok_backoffice.sqlite3"
    store = TikTokBackofficeStore(original)
    store.upsert_campaign(Campaign(slug="proxy", name="Proxy", keywords=("proxy",), reply_template="R"))
    store.add_video(TikTokVideo(video_url=KANBAN_VIDEO, caption="Commente proxy"))
    store.assign_video_campaign(video_url=KANBAN_VIDEO, campaign_slug="proxy")
    write_console_campaign(original)
    before = digest(original)

    copy = tmp_path / "copy.sqlite3"
    shutil.copy2(original, copy)
    # s24 : épingle amendée (processeur retiré du scan, console ADR 055) — the console video was already read once
    # by the console (its baseline is set), so this read creates a pending item, not a first-scan baseline (P7).
    with sqlite3.connect(copy) as connection:
        connection.execute("UPDATE tiktok_videos SET baseline_at = '2000-01-01 00:00:00' WHERE video_url = ?", (CONSOLE_VIDEO,))

    def fetch(*, video_url, on_captcha, expired, sleep=None):
        if video_url == CONSOLE_VIDEO:
            return {"ok": True, "comments": [{"author": "@fan", "text": "voicebox stp"}]}
        return {"ok": True, "comments": []}

    result = ts.run_scan(
        store=TikTokBackofficeStore(copy),
        fetch=fetch,
        tabs_count=lambda: 1,
        legacy_running=lambda: False,
        legacy_imminent=lambda bound: False,
        solve=lambda tab_id, *, deadline: "unsolved",
        clock=lambda: 0.0,
        sleep=lambda s: None,
        report=CaptchaReport(),
    )

    console_id = CONSOLE_VIDEO.rsplit("/", 1)[1]
    assert console_id in [v["id"] for v in result.videos]
    assert result.counters["comments_new"] == 1
    with sqlite3.connect(copy) as connection:
        rows = connection.execute(
            "SELECT ri.campaign_slug, ri.status FROM tiktok_review_items ri JOIN tiktok_comments c ON c.comment_id = ri.comment_id"
            " WHERE c.video_url = ?",
            (CONSOLE_VIDEO,),
        ).fetchall()
        scanned = connection.execute(
            "SELECT last_scanned_at FROM tiktok_videos WHERE video_url = ?", (CONSOLE_VIDEO,)
        ).fetchone()[0]
    assert rows == [(CONSOLE_SLUG, "pending_review")]
    assert scanned is not None
    assert digest(original) == before
