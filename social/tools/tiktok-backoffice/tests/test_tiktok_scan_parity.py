"""s23 — sequence parity of the job `tiktok-scan` against the legacy cron (console ADR 054, AC 6, D10).

Oracle: scripts/tiktok_cron_auto_dm.py of the repo, loaded by importlib (pattern of test_cron_auto_dm_db.py), its
`run` replaced by a fake that records and answers. Job: app.tiktok_scan.run_scan with equivalent fakes. Both sides
work on their own copy of the same fixture base, and are compared on LOGICAL steps:
("counts",) ("sync",) ("poll", 8) ("scan", url) ("pending", 3) ("process", id).

Nominal sequence: a single named exception, `sync-kanban-campaigns`, which belongs to the job `campaign-sync`
(console ADR 040). Every deliberate divergence of the job is a row of DOCUMENTED_DEVIATIONS, tested oracle against
job, and listed under the same id in console ADR 054 § Écarts. Nothing here runs the versioned processor.
"""

import importlib.util
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app import tiktok_scan as ts
from app.captcha_solver import CaptchaReport
from app.models import Campaign, TikTokVideo
from app.store import TikTokBackofficeStore

CRON = Path(__file__).resolve().parents[1] / "scripts" / "tiktok_cron_auto_dm.py"
NAMED_EXCEPTION = "sync-kanban-campaigns"
SYNC = ("sync",)


def video(i):
    return f"https://www.tiktok.com/@dupflodev/video/{8100 + i}"


def build_fixture(tmp_path):
    db = tmp_path / "fixture.sqlite3"
    store = TikTokBackofficeStore(db)
    store.upsert_campaign(Campaign(slug="proxy", name="Proxy", keywords=("proxy",), reply_template="R"))
    for i in range(3):
        store.add_video(TikTokVideo(video_url=video(i), caption="Commente proxy"))
        store.assign_video_campaign(video_url=video(i), campaign_slug="proxy")
    with sqlite3.connect(db) as connection:
        for i in range(3):
            connection.execute("UPDATE tiktok_videos SET discovered_at = ? WHERE video_url = ?",
                               (f"2026-09-0{i + 1} 00:00:00", video(i)))
        for n in range(4):
            connection.execute(
                "INSERT INTO tiktok_comments (comment_id, video_url, author, text) VALUES (?, ?, ?, 'proxy')",
                (f"old{n}", video(0), f"@old{n}"),
            )
            connection.execute(
                """
                INSERT INTO tiktok_review_items (comment_id, campaign_slug, matched_keyword, reply_text, status, created_at)
                VALUES (?, 'proxy', 'proxy', 'R', 'pending_review', ?)
                """,
                (f"old{n}", f"2026-08-0{n + 1} 00:00:00"),
            )
    a, b = tmp_path / "a.sqlite3", tmp_path / "b.sqlite3"
    shutil.copy2(db, a)
    shutil.copy2(db, b)
    return a, b


def comments_for(url):
    return [{"author": "@fan" + url[-1], "text": "proxy please " + url[-4:]}]


# --- scenarios: what the page answers, seen by each side --------------------------------------------------------------


def nominal(url):
    return {"ok": True, "comments": comments_for(url)}


def session_lost_first(url):
    if url == video(2):  # first in line: newest discovered
        return {"ok": False, "error": "tiktok_page_not_actionable", "comments": []}
    return nominal(url)


def captcha_first_regex(url):
    if url == video(2):
        return {"ok": False, "error": "captcha_required", "captcha": True, "comments": []}
    return nominal(url)


# --- oracle -----------------------------------------------------------------------------------------------------------


def run_oracle(db, tmp_path, monkeypatch, page=nominal):
    spec = importlib.util.spec_from_file_location("tiktok_cron_auto_dm_s23_parity", CRON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dummy = tmp_path / "dummy_processor.py"
    dummy.write_text("# never run\n", encoding="utf-8")
    module.DB = db
    module.PROCESS_ONE = dummy
    monkeypatch.setenv("TIKTOK_CAMOFOX_BASE_URL", "http://127.0.0.1:9")
    store = TikTokBackofficeStore(db)
    trace = []

    def fake_run(cmd, *, cwd=None, timeout=600):
        if cmd[:3] == ["uv", "run", "tiktok-backoffice"]:
            sub = cmd[3]
            if sub == NAMED_EXCEPTION:
                trace.append(SYNC)
                out = {"ok": True}
            elif sub == "poll-targets":
                limit = int(cmd[cmd.index("--limit") + 1])
                trace.append(("poll", limit))
                out = {"ok": True, "items": store.poll_targets(limit)}
            elif sub == "fetch-comments-camofox":
                url = cmd[cmd.index("--video-url") + 1]
                trace.append(("scan", url))
                result = page(url)
                out = dict(result, fetched=len(result.get("comments") or []))
                if result.get("ok") and "--ingest" in cmd:
                    out["ingest"] = store.ingest_comments(video_url=url, comments=result["comments"])
            else:
                raise AssertionError(f"unexpected subcommand {sub}")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")
        if cmd[0] == "python3" and cmd[1] == str(dummy):
            trace.append(("process", int(cmd[2])))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"ok": True, "stage": "processed"}), "")
        raise AssertionError(f"unexpected command {cmd[0]}")

    real_counts, real_pending = module.status_counts, module.pending_items

    def counts():
        trace.append(("counts",))
        return real_counts()

    def pending(limit):
        trace.append(("pending", limit))
        return real_pending(limit)

    module.run = fake_run
    module.status_counts = counts
    module.pending_items = pending
    assert module.main(["--apply"]) == 0
    return trace


# --- job --------------------------------------------------------------------------------------------------------------


class TracingStore:
    def __init__(self, db, trace):
        self.store = TikTokBackofficeStore(db)
        self.trace = trace

    def review_status_counts(self):
        self.trace.append(("counts",))
        return self.store.review_status_counts()

    def poll_targets(self, limit=50):
        self.trace.append(("poll", limit))
        return self.store.poll_targets(limit)

    def pending_review_items(self, limit):
        self.trace.append(("pending", limit))
        return self.store.pending_review_items(limit)

    def ingest_comments(self, **kwargs):
        return self.store.ingest_comments(**kwargs)

    def mark_scan_attempt(self, url, **kwargs):
        return self.store.mark_scan_attempt(url, **kwargs)


class Clock:
    def __init__(self):
        self.t = 5_000_000.0

    def __call__(self):
        return self.t


def run_job(db, *, page=nominal, captcha_on=None, tabs=None, legacy_running=False, legacy_due=False,
            fetch_seconds=0.0, item_seconds=0.0):
    trace = []
    clock = Clock()
    report = CaptchaReport()
    tab_counts = iter(tabs or [])

    def fetch(*, video_url, on_captcha, expired, sleep=None):
        trace.append(("scan", video_url))
        clock.t += fetch_seconds
        if video_url == captcha_on and not on_captcha("tab-1"):
            return {"ok": False, "error": "captcha_unsolved", "comments": []}
        return page(video_url)

    def solve(tab_id, *, deadline):
        report.captchas.append({"seq": len(report.captchas) + 1, "outcome": "unsolved", "at": 0, "attempts": []})
        return "unsolved"

    def process_one(review_id):
        trace.append(("process", review_id))
        clock.t += item_seconds
        return {"id": review_id, "rc": 0, "stage": "processed", "ok": True}

    result = ts.run_scan(
        store=TracingStore(db, trace),
        fetch=fetch,
        tabs_count=lambda: next(tab_counts, 1),
        process_one=process_one,
        legacy_running=lambda: legacy_running,
        legacy_imminent=lambda bound: legacy_due,
        solve=solve,
        clock=clock,
        sleep=lambda s: None,
        report=report,
    )
    return trace, result


# --- nominal sequence -------------------------------------------------------------------------------------------------


def test_nominal_sequence_matches_the_cron_except_the_named_sync(tmp_path, monkeypatch):
    a, b = build_fixture(tmp_path)

    oracle = run_oracle(a, tmp_path, monkeypatch)
    job, result = run_job(b)

    assert oracle.count(SYNC) == 1, f"the oracle runs {NAMED_EXCEPTION} exactly once"
    assert SYNC not in job, f"{NAMED_EXCEPTION} belongs to campaign-sync (console ADR 040), never to tiktok-scan"
    assert job == [step for step in oracle if step != SYNC]
    assert result.status == "ok" and result.exit_code == 0
    # same steps on the same data: the processed ids are the 3 oldest pending items on both sides
    assert [s for s in job if s[0] == "process"] == [("process", 1), ("process", 2), ("process", 3)]


# --- documented deviations (console ADR 054 § Écarts) ------------------------------------------------------------------

DOCUMENTED_DEVIATIONS = [
    # console ADR 054 § Écarts : captcha_unsolved
    {"id": "captcha_unsolved", "scenario": "captcha on the 1st video, not solved",
     "oracle": "captcha_required, goes on, processes the queue", "job": "stops, queue untouched, exit 18",
     "ref": "ADR 054 § Écarts"},
    # console ADR 054 § Écarts : session_lost
    {"id": "session_lost", "scenario": "tiktok_page_not_actionable on the 1st video",
     "oracle": "goes on, processes the queue", "job": "stops, queue untouched, exit 20", "ref": "ADR 054 § Écarts"},
    # console ADR 054 § Écarts : tabs_full
    {"id": "tabs_full", "scenario": "9 Camofox tabs before the 2nd video",
     "oracle": "(no check) goes on", "job": "stops, queue untouched", "ref": "ADR 054 § Écarts"},
    # console ADR 054 § Écarts : legacy_guard
    {"id": "legacy_guard", "scenario": "legacy cron running / due",
     "oracle": "(runs)", "job": "skipped legacy_cron_running / legacy_cron_due", "ref": "ADR 054 § Écarts"},
    # console ADR 054 § Écarts : no_session
    {"id": "no_session", "scenario": "last verdict is not connected", "side": "console",
     "oracle": "(runs)", "job": "no exec (console side: worker/executors/hermes-exec.test.ts, C2)",
     "ref": "ADR 054 § Écarts"},
    # console ADR 054 § Écarts : budget
    {"id": "budget", "scenario": "clock beyond SCAN_BUDGET_S / window < 720 s",
     "oracle": "processes everything", "job": "videos or items skipped", "ref": "ADR 054 § Écarts"},
]


def full_oracle_run(trace):
    return [s[0] for s in trace].count("scan") == 3 and [s[0] for s in trace].count("process") == 3


@pytest.mark.parametrize("row", DOCUMENTED_DEVIATIONS, ids=lambda row: row["id"])
def test_documented_deviation(row, tmp_path, monkeypatch):
    a, b = build_fixture(tmp_path)

    if row["id"] == "captcha_unsolved":
        oracle = run_oracle(a, tmp_path, monkeypatch, page=captcha_first_regex)
        job, result = run_job(b, captcha_on=video(2))
        assert full_oracle_run(oracle)
        assert result.exit_code == 18 and job.count(("scan", video(2))) == 1
        assert not any(s[0] in ("pending", "process") for s in job)

    elif row["id"] == "session_lost":
        oracle = run_oracle(a, tmp_path, monkeypatch, page=session_lost_first)
        job, result = run_job(b, page=session_lost_first)
        assert full_oracle_run(oracle)
        assert result.exit_code == 20 and [s for s in job if s[0] == "scan"] == [("scan", video(2))]
        assert not any(s[0] in ("pending", "process") for s in job)

    elif row["id"] == "tabs_full":
        oracle = run_oracle(a, tmp_path, monkeypatch)
        job, result = run_job(b, tabs=[1, 9])
        assert full_oracle_run(oracle)
        assert [v["error"] for v in result.videos] == [None, "tabs_full"]
        assert [s for s in job if s[0] == "scan"] == [("scan", video(2))]
        assert not any(s[0] in ("pending", "process") for s in job)

    elif row["id"] == "legacy_guard":
        oracle = run_oracle(a, tmp_path, monkeypatch)
        assert full_oracle_run(oracle)
        job, result = run_job(b, legacy_running=True)
        assert (result.status, result.reason) == ("skipped", "legacy_cron_running") and job == []
        job, result = run_job(b, legacy_due=True)
        assert (result.status, result.reason) == ("skipped", "legacy_cron_due")
        assert not any(s[0] in ("scan", "process") for s in job)

    elif row["id"] == "no_session":
        assert row["side"] == "console"
        oracle = run_oracle(a, tmp_path, monkeypatch)
        assert full_oracle_run(oracle)  # the cron has no session guard at all
        # The Python job has none either: the guard is the console's, before any exec (C2).
        job, result = run_job(b)
        assert result.status == "ok"

    elif row["id"] == "budget":
        oracle = run_oracle(a, tmp_path, monkeypatch)
        assert full_oracle_run(oracle)
        job, result = run_job(b, fetch_seconds=ts.SCAN_BUDGET_S + 1)
        assert [s for s in job if s[0] == "scan"] == [("scan", video(2))]
        assert result.counters["videos_skipped"] == 2
        c = tmp_path / "c.sqlite3"
        shutil.copy2(tmp_path / "fixture.sqlite3", c)
        job, result = run_job(c, fetch_seconds=2000)  # 2 videos (2nd starts at 2000 s), then 500 s < 720 s left
        assert [s[0] for s in job].count("scan") == 2
        assert not any(s[0] == "process" for s in job) and result.counters["queue_skipped"] == 3

    else:  # pragma: no cover - a new row needs its own branch
        raise AssertionError(f"untested deviation {row['id']}")


def test_deviation_ids_are_the_six_of_adr_054():
    assert [row["id"] for row in DOCUMENTED_DEVIATIONS] == [
        "captcha_unsolved", "session_lost", "tabs_full", "legacy_guard", "no_session", "budget",
    ]


# --- AC 3 end to end: a new video is attempted within the next 2 runs, failures included -----------------------------


def test_new_video_is_attempted_within_two_runs_even_when_every_fetch_fails(tmp_path):
    db = tmp_path / "famine.sqlite3"
    store = TikTokBackofficeStore(db)
    store.upsert_campaign(Campaign(slug="proxy", name="Proxy", keywords=("proxy",), reply_template="R"))
    for i in range(12):
        store.add_video(TikTokVideo(video_url=video(i), caption="Commente proxy"))
        store.assign_video_campaign(video_url=video(i), campaign_slug="proxy")
    with sqlite3.connect(db) as connection:
        for i in range(12):
            connection.execute("UPDATE tiktok_videos SET discovered_at = ? WHERE video_url = ?",
                               (f"2026-09-{i + 1:02d} 00:00:00", video(i)))

    def failing(url):
        return {"ok": False, "error": "comments_expected_but_not_extracted", "comments": []}

    first, result = run_job(db, page=failing)
    assert len([s for s in first if s[0] == "scan"]) == 8 and result.exit_code == 17

    added = "https://www.tiktok.com/@dupflodev/video/9999"
    store.add_video(TikTokVideo(video_url=added, caption="Commente proxy"))
    store.assign_video_campaign(video_url=added, campaign_slug="proxy")

    second, _ = run_job(db, page=failing)
    assert ("scan", added) in second
    assert second[2] == ("scan", added)  # in practice, first of the very next run
    # failures are dated too: the 4 videos run 1 left out come before the 8 it attempted (no head hogging)
    assert {("scan", video(i)) for i in range(4)} <= set(second)
