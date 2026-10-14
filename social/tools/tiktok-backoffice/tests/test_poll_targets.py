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


# --- s23 (console ADR 054, D9): every scan attempt is dated, so failures no longer hog the head -----------------

import sqlite3

from app.models import Campaign, TikTokVideo
from app.store import TikTokBackofficeStore


def _linked_store(tmp_path, count):
    store = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")
    store.upsert_campaign(Campaign(slug="proxy", name="Proxy", keywords=("proxy",), reply_template="R"))
    urls = []
    for i in range(count):
        url = f"https://www.tiktok.com/@dupflodev/video/{1000 + i}"
        store.add_video(TikTokVideo(video_url=url, caption="Commente proxy"))
        store.assign_video_campaign(video_url=url, campaign_slug="proxy")
        urls.append(url)
    with sqlite3.connect(store.database) as connection:
        for i, url in enumerate(urls):
            connection.execute(
                "UPDATE tiktok_videos SET discovered_at = ? WHERE video_url = ?",
                (f"2026-09-01 00:{i:02d}:00", url),
            )
    return store, urls


def _last_scanned(store, url):
    with sqlite3.connect(store.database) as connection:
        return connection.execute("SELECT last_scanned_at FROM tiktok_videos WHERE video_url = ?", (url,)).fetchone()[0]


def test_mark_scan_attempt_dates_a_known_video_only(tmp_path):
    store, urls = _linked_store(tmp_path, 1)

    assert store.mark_scan_attempt("https://www.tiktok.com/@dupflodev/video/9") is False
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tiktok_videos").fetchone()[0] == 1

    assert _last_scanned(store, urls[0]) is None
    assert store.mark_scan_attempt(urls[0], at="2026-10-14 01:02:03") is True
    assert _last_scanned(store, urls[0]) == "2026-10-14 01:02:03"
    assert store.mark_scan_attempt(urls[0]) is True
    assert _last_scanned(store, urls[0]) not in (None, "2026-10-14 01:02:03")


def test_new_video_comes_first_and_attempts_rotate_the_head(tmp_path):
    store, urls = _linked_store(tmp_path, 10)
    for i, url in enumerate(urls[:8]):
        store.mark_scan_attempt(url, at=f"2026-10-01 00:{i:02d}:00")
    added = "https://www.tiktok.com/@dupflodev/video/2000"
    store.add_video(TikTokVideo(video_url=added, caption="Commente proxy"))
    store.assign_video_campaign(video_url=added, campaign_slug="proxy")
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_videos SET discovered_at = '2026-10-13 00:00:00' WHERE video_url = ?", (added,))

    first = [t["video_url"] for t in store.poll_targets(limit=8)]
    assert first[0] == added
    assert first[:3] == [added, urls[9], urls[8]]  # never scanned: newest first
    assert first[3:] == urls[:5]  # then the oldest attempts

    for i, url in enumerate(first):
        store.mark_scan_attempt(url, at=f"2026-10-14 00:{i:02d}:00")
    second = [t["video_url"] for t in store.poll_targets(limit=8)]
    assert second[:3] == urls[5:8]  # not attempted this round
    assert second[3:] == first[:5]  # then the oldest attempts of the round


def test_twenty_failing_videos_all_come_out_in_three_runs(tmp_path):
    store, urls = _linked_store(tmp_path, 20)
    seen = set()
    for run_number in range(3):
        targets = [t["video_url"] for t in store.poll_targets(limit=8)]
        assert len(targets) == 8
        for i, url in enumerate(targets):
            # the fetch failed: only the attempt is dated, nothing is ingested
            store.mark_scan_attempt(url, at=f"2026-10-14 0{run_number}:{i:02d}:00")
        seen.update(targets)
    assert seen == set(urls)
