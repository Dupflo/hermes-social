"""s23 — app.tiktok_scan: the console job `tiktok-scan` (console ADR 054).

Dependencies injected: real store on a temporary base, fake fetch, fake solver, simulated clock.
No network, no Camofox. s24 : épingle amendée (processeur retiré du scan, console ADR 055) — the scan no longer
touches the review queue; its processor tests are removed and listed in the PR description.
"""

import inspect

import json
import os
import sqlite3
from pathlib import Path

import pytest

from app import tiktok_scan as ts
from app.camofox_reader import CamofoxUnreachable
from app.captcha_solver import CaptchaReport
from app.models import Campaign, TikTokVideo
from app.store import TikTokBackofficeStore

SENTINELS = ("@SENTINEL_AUTHOR", "SENTINEL_TEXT", "SENTINEL_DETAIL", "SENTINEL_DM", "SENTINEL_TRACE", "SENTINEL_PROMPT")


# --- fixtures -------------------------------------------------------------------------------------------------------


class Clock:
    def __init__(self, start=1_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def video_url(i):
    return f"https://www.tiktok.com/@dupflodev/video/{7000 + i}"


def make_store(tmp_path, *, videos=2, items=3):
    store = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")
    store.upsert_campaign(Campaign(slug="proxy", name="Proxy", keywords=("proxy",), reply_template="R"))
    with sqlite3.connect(store.database) as connection:
        for i in range(videos):
            connection.execute(
                "INSERT INTO tiktok_videos (video_url, video_id, active, discovered_at) VALUES (?, ?, 1, ?)",
                (video_url(i), str(7000 + i), f"2026-09-01 00:{59 - i:02d}:00"),
            )
            connection.execute(
                "INSERT INTO tiktok_video_campaigns (video_url, campaign_slug, approved) VALUES (?, 'proxy', 1)",
                (video_url(i),),
            )
        connection.execute(
            "INSERT OR IGNORE INTO tiktok_videos (video_url, video_id, active) VALUES ('https://x/old', '1', 1)"
        )
        for n in range(items):
            connection.execute(
                "INSERT INTO tiktok_comments (comment_id, video_url, author, text) VALUES (?, 'https://x/old', ?, 'proxy')",
                (f"old{n}", f"@user{n}"),
            )
            connection.execute(
                """
                INSERT INTO tiktok_review_items (comment_id, campaign_slug, matched_keyword, reply_text, status, created_at)
                VALUES (?, 'proxy', 'proxy', 'R', 'pending_review', ?)
                """,
                (f"old{n}", f"2026-08-0{n + 1} 00:00:00"),
            )
    return store


def pending_ids(store):
    return [row["id"] for row in store.pending_review_items(10)]


def last_scanned(store, url):
    with sqlite3.connect(store.database) as connection:
        return connection.execute("SELECT last_scanned_at FROM tiktok_videos WHERE video_url = ?", (url,)).fetchone()[0]


def ok_fetch(comments=(("@alice", "proxy stp"),)):
    return {"ok": True, "comments": [{"author": a, "text": t} for a, t in comments]}


class Fetch:
    """Fake fetch: `script(url, on_captcha, expired, clock)` or a fixed result per call index."""

    def __init__(self, clock, results=None, script=None, advance=0.0):
        self.clock = clock
        self.results = list(results or [])
        self.script = script
        self.advance = advance
        self.urls = []

    def __call__(self, *, video_url, on_captcha, expired, sleep=None):
        url = video_url
        self.urls.append(url)
        if self.script is not None:
            return self.script(url, on_captcha, expired, self.clock)
        self.clock.advance(self.advance)
        if self.results:
            result = self.results.pop(0)
            return result(url) if callable(result) else result
        return ok_fetch((("@alice", f"proxy {url[-4:]}"),))


class Solver:
    def __init__(self, report, clock, outcomes, spend=0.0):
        self.report = report
        self.clock = clock
        self.outcomes = list(outcomes)
        self.spend = spend
        self.calls = []

    def __call__(self, tab_id, *, deadline):
        self.calls.append((tab_id, deadline))
        outcome = self.outcomes.pop(0)
        self.report.captchas.append({"seq": len(self.report.captchas) + 1, "outcome": outcome, "at": 0, "attempts": [
            {"try": 1, "type": "puzzle", "variant": "v1", "result": "solved" if outcome == "solved" else "failed", "ms": 10}
        ]})
        self.clock.advance(self.spend)
        return outcome


def run(store, *, clock=None, fetch=None, tabs=lambda: 1, legacy_running=lambda: False,
        legacy_imminent=lambda bound: False, solve=None, report=None):
    clock = clock or Clock()
    report = report if report is not None else CaptchaReport()
    return ts.run_scan(
        store=store,
        fetch=fetch or Fetch(clock),
        tabs_count=tabs,
        legacy_running=legacy_running,
        legacy_imminent=legacy_imminent,
        solve=solve or Solver(report, clock, []),
        clock=clock,
        sleep=lambda s: None,
        report=report,
    )


def output_of(result, report=None):
    lines = ts.emit_lines(result)
    lines.append(ts.console_result_line(result, report or CaptchaReport()))
    return "\n".join(lines)


# --- P1 table -------------------------------------------------------------------------------------------------------


def statuses(store):
    with sqlite3.connect(store.database) as connection:
        return connection.execute("SELECT id, status, failure_reason FROM tiktok_review_items ORDER BY id").fetchall()


def test_nominal_run_scans_and_leaves_the_queue_alone(tmp_path):
    store = make_store(tmp_path, videos=2, items=3)
    before = statuses(store)

    result = run(store)

    assert (result.status, result.reason, result.exit_code) == ("ok", None, 0)
    c = result.counters
    assert c["videos_targeted"] == 2 and c["videos_ok"] == 2 and c["videos_failed"] == 0
    assert c["comments_extracted"] == 2 and c["comments_new"] == 2 and c["reviews_created"] == 2
    assert c["reviews_baseline"] == 2  # both videos are read for the first time (P7)
    assert c["videos_skipped"] == 0
    assert c["pending_after"] == 3
    assert set(c) == set(ts.COUNTERS)
    assert [v["id"] for v in result.videos] == ["7000", "7001"]
    assert result.videos[0] == {"id": "7000", "extracted": 1, "new": 1, "reviews": 1, "error": None, "baseline": 1}
    assert all(last_scanned(store, video_url(i)) for i in range(2))
    assert statuses(store)[: len(before)] == before  # existing items untouched


def test_run_scan_has_no_processor_parameter():
    assert "process_one" not in inspect.signature(ts.run_scan).parameters


def test_scan_source_never_names_the_processor_nor_the_queue():
    source = Path(ts.__file__).read_text(encoding="utf-8")
    for needle in ("process_tiktok_review_one", "subprocess", "pending_review_items"):
        assert needle not in source


def test_scan_leaves_every_pending_item_status_unchanged(tmp_path):
    store = make_store(tmp_path, videos=2, items=3)
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_review_items SET status = 'sending' WHERE id = 2")
    before = statuses(store)
    run(store)
    run(store)
    assert statuses(store)[: len(before)] == before


def baseline_at(store, url):
    with sqlite3.connect(store.database) as connection:
        return connection.execute("SELECT baseline_at FROM tiktok_videos WHERE video_url = ?", (url,)).fetchone()[0]


def new_items(store, url):
    with sqlite3.connect(store.database) as connection:
        return connection.execute(
            """SELECT ri.status, ri.failure_reason FROM tiktok_review_items ri
               JOIN tiktok_comments c ON c.comment_id = ri.comment_id WHERE c.video_url = ? ORDER BY ri.id""",
            (url,),
        ).fetchall()


def test_first_read_is_a_baseline_then_the_second_read_creates_pending_items(tmp_path):
    store = make_store(tmp_path, videos=1, items=0)
    clock = Clock()
    fetch = Fetch(clock, results=[ok_fetch((("@a", "proxy un"),)), ok_fetch((("@a", "proxy un"), ("@b", "proxy deux")))])

    first = run(store, clock=clock, fetch=fetch)
    assert first.videos[0]["baseline"] == 1 and first.counters["reviews_baseline"] == 1
    assert baseline_at(store, video_url(0)) is not None
    assert new_items(store, video_url(0)) == [("expired", "first_scan_baseline")]
    assert "video 7000: extracted=1 new=1 reviews=1 error=- baseline=1" in ts.emit_lines(first)

    second = run(store, clock=clock, fetch=fetch)
    assert second.videos[0]["baseline"] == 0 and second.counters["reviews_baseline"] == 0
    assert new_items(store, video_url(0)) == [("expired", "first_scan_baseline"), ("pending_review", None)]
    assert [r["video_url"] for r in store.reply_queue(10)] == [video_url(0)]


def test_video_with_legacy_comments_is_not_a_baseline_and_its_new_items_are_eligible(tmp_path):
    store = make_store(tmp_path, videos=1, items=0)
    store.ingest_comments(video_url=video_url(0), comments=[{"author": "@old", "text": "bonjour"}])  # legacy path
    assert baseline_at(store, video_url(0)) is None

    result = run(store)

    assert result.videos[0]["baseline"] == 0
    assert baseline_at(store, video_url(0)) is not None
    assert new_items(store, video_url(0)) == [("pending_review", None)]
    assert len(store.reply_queue(10)) == 1


def test_failed_fetch_sets_no_baseline(tmp_path):
    store = make_store(tmp_path, videos=1, items=0)
    clock = Clock()
    run(store, clock=clock, fetch=Fetch(clock, results=[{"ok": False, "error": "camofox_fetch_failed", "comments": []}]))
    assert baseline_at(store, video_url(0)) is None
    assert last_scanned(store, video_url(0)) is not None


def test_failed_ingestion_sets_no_baseline(tmp_path, monkeypatch):
    store = make_store(tmp_path, videos=1, items=0)
    clock = Clock()
    result = run(store, clock=clock, fetch=Fetch(clock, results=[{"ok": True, "comments": [{"author": "@a", "text": "proxy"}, None]}]))
    assert result.videos[0]["error"] == "ingest_failed"
    assert baseline_at(store, video_url(0)) is None
    assert new_items(store, video_url(0)) == []


def test_read_with_zero_comments_sets_the_baseline(tmp_path):
    store = make_store(tmp_path, videos=1, items=0)
    clock = Clock()
    result = run(store, clock=clock, fetch=Fetch(clock, results=[{"ok": True, "comments": []}]))
    assert result.videos[0]["error"] is None
    assert baseline_at(store, video_url(0)) is not None


def test_legacy_cron_running_at_start_touches_nothing(tmp_path):
    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"store.{name} called")

    fetch = Fetch(Clock())

    result = run(Untouchable(), fetch=fetch, legacy_running=lambda: True)

    assert (result.status, result.reason, result.exit_code) == ("skipped", "legacy_cron_running", 0)
    assert fetch.urls == []
    assert result.videos == []


def test_legacy_cron_due_before_the_second_video(tmp_path):
    store = make_store(tmp_path, videos=2, items=3)
    calls = []

    def imminent(bound):
        calls.append(bound)
        return len(calls) >= 2

    result = run(store, legacy_imminent=imminent)

    assert (result.status, result.reason, result.exit_code) == ("skipped", "legacy_cron_due", 0)
    assert calls == [ts.LEGACY_VIDEO_BOUND_S, ts.LEGACY_VIDEO_BOUND_S]
    assert len(result.videos) == 1
    assert result.counters["videos_skipped"] == 1
    assert last_scanned(store, video_url(1)) is None


def test_legacy_guard_is_never_asked_after_the_last_video(tmp_path):
    store = make_store(tmp_path, videos=2, items=3)
    calls = []

    def imminent(bound):
        calls.append(bound)
        return len(calls) > 2  # 2 videos: a 3rd question would be after the run

    result = run(store, legacy_imminent=imminent)

    assert (result.status, result.exit_code) == ("ok", 0)
    assert calls == [ts.LEGACY_VIDEO_BOUND_S] * 2


def test_no_targets_is_skipped(tmp_path):
    store = make_store(tmp_path, videos=0, items=3)
    before = statuses(store)

    result = run(store)

    assert (result.status, result.reason, result.exit_code) == ("skipped", "no_targets", 0)
    assert statuses(store) == before


def test_all_attempted_videos_failed_is_exit_17(tmp_path):
    store = make_store(tmp_path, videos=2, items=3)
    clock = Clock()
    fetch = Fetch(clock, results=[{"ok": False, "error": "camofox_unreachable", "detail": "SENTINEL_DETAIL", "comments": []}] * 2)

    result = run(store, clock=clock, fetch=fetch)

    assert (result.status, result.exit_code) == ("failed", 17)
    assert [v["error"] for v in result.videos] == ["camofox_unreachable"] * 2
    assert all(last_scanned(store, video_url(i)) for i in range(2))  # failures are dated too (D9)


def test_session_lost_stops_the_scan(tmp_path):
    store = make_store(tmp_path, videos=3, items=3)
    clock = Clock()
    fetch = Fetch(clock, results=[ok_fetch(), {"ok": False, "error": "tiktok_page_not_actionable", "comments": []}])

    result = run(store, clock=clock, fetch=fetch)

    assert (result.status, result.exit_code) == ("failed", 20)
    assert len(fetch.urls) == 2
    assert result.counters["videos_skipped"] == 1


def test_tabs_full_on_the_second_video_stops(tmp_path):
    store = make_store(tmp_path, videos=3, items=3)
    counts = iter([1, 9])

    result = run(store, tabs=lambda: next(counts))

    assert (result.status, result.exit_code) == ("ok", 0)
    assert [v["error"] for v in result.videos] == [None, "tabs_full"]
    assert result.counters["videos_ok"] == 1 and result.counters["videos_failed"] == 1


def test_tabs_full_on_the_first_video_is_exit_17(tmp_path):
    store = make_store(tmp_path, videos=2, items=3)

    result = run(store, tabs=lambda: 9)

    assert (result.status, result.exit_code) == ("failed", 17)


def test_tabs_listing_error_is_a_camofox_unreachable_video(tmp_path):
    store = make_store(tmp_path, videos=2, items=0)
    answers = iter([CamofoxUnreachable(), 1])

    def tabs():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    result = run(store, tabs=tabs)

    assert [v["error"] for v in result.videos] == ["camofox_unreachable", None]
    assert result.status == "ok"


def test_unexpected_fetch_error_codes_are_closed(tmp_path):
    store = make_store(tmp_path, videos=2, items=0)
    clock = Clock()
    fetch = Fetch(clock, results=[{"ok": False, "error": "SENTINEL_DETAIL", "comments": []}, ok_fetch()])

    result = run(store, clock=clock, fetch=fetch)

    assert result.videos[0]["error"] == "unknown"


def test_ingest_failure_is_a_video_error(tmp_path, monkeypatch):
    store = make_store(tmp_path, videos=2, items=0)
    real = store.ingest_comments
    calls = []

    def flaky(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(**kwargs)

    monkeypatch.setattr(store, "ingest_comments", flaky)
    result = run(store)

    assert [v["error"] for v in result.videos] == ["ingest_failed", None]
    assert result.counters["videos_ok"] == 1 and result.status == "ok"
    assert last_scanned(store, video_url(0)) is not None


# --- deadlines and budgets ------------------------------------------------------------------------------------------


def test_slow_video_times_out_and_the_next_one_is_read(tmp_path):
    store = make_store(tmp_path, videos=2, items=3)
    clock = Clock()

    def script(url, on_captcha, expired, clk):
        if url == video_url(0):
            clk.advance(300)
        if expired():
            return {"ok": False, "error": "video_timeout", "comments": []}
        return ok_fetch()

    result = run(store, clock=clock, fetch=Fetch(clock, script=script))

    assert [v["error"] for v in result.videos] == ["video_timeout", None]
    assert result.status == "ok"


def test_scan_budget_no_video_starts_after_2160_s(tmp_path):
    store = make_store(tmp_path, videos=8, items=0)
    clock = Clock()
    t0 = clock()
    fetch = Fetch(clock, advance=342)
    starts = []
    real_call = fetch.__call__

    def spy(**kwargs):
        starts.append(clock() - t0)
        return real_call(**kwargs)

    result = run(store, clock=clock, fetch=spy)

    assert all(start <= ts.SCAN_BUDGET_S for start in starts)
    assert len(starts) == 7  # 6 x 342 = 2052 s: the 7th starts, the 8th (2394 s) does not
    assert result.counters["videos_skipped"] == 1
    assert last_scanned(store, video_url(7)) is None  # not attempted, not dated: first in line next run


# --- captchas -------------------------------------------------------------------------------------------------------


def captcha_script(url, on_captcha, expired, clk):
    clk.advance(100)
    if not on_captcha("tab-1"):
        return {"ok": False, "error": "captcha_unsolved", "comments": []}
    clk.advance(100)
    if expired():
        return {"ok": False, "error": "video_timeout", "comments": []}
    return ok_fetch()


def test_solved_captcha_lets_the_video_be_read_and_its_time_is_suspended(tmp_path):
    store = make_store(tmp_path, videos=1, items=3)
    clock = Clock()
    report = CaptchaReport()
    solver = Solver(report, clock, ["solved"], spend=200)  # 100 + 200 + 100 > 240, but the solver is suspended

    result = run(store, clock=clock, fetch=Fetch(clock, script=captcha_script), solve=solver, report=report)

    assert result.videos[0]["error"] is None
    assert result.status == "ok"
    assert result.counters["captchas"] == 1 and result.counters["captchas_solved"] == 1
    assert result.counters["captcha_tries"] == 1
    tab, deadline = solver.calls[0]
    assert tab == "tab-1" and deadline == pytest.approx(clock.t - 300 + ts.SOLVER_BUDGET_S)


def test_unsolved_captcha_is_exit_18(tmp_path):
    store = make_store(tmp_path, videos=2, items=3)
    clock = Clock()
    report = CaptchaReport()

    result = run(store, clock=clock, fetch=Fetch(clock, script=captcha_script),
                 solve=Solver(report, clock, ["unsolved"]), report=report)

    assert (result.status, result.exit_code) == ("failed", 18)
    assert len(result.videos) == 1
    assert report.captchas[0]["outcome"] == "unsolved"
    assert json.loads(ts.console_result_line(result, report)[len("CONSOLE_RESULT "):])["captchas"][0]["outcome"] == "unsolved"


def test_fourth_captcha_of_a_run_is_refused_without_calling_the_solver(tmp_path):
    store = make_store(tmp_path, videos=4, items=0)
    clock = Clock()
    report = CaptchaReport()
    solver = Solver(report, clock, ["solved", "solved", "solved"])

    result = run(store, clock=clock, fetch=Fetch(clock, script=captcha_script), solve=solver, report=report)

    assert len(solver.calls) == 3
    assert [v["error"] for v in result.videos] == [None, None, None, "captcha_unsolved"]
    assert result.exit_code == 18
    # review n°1 m2: the refused captcha is still logged (ADR 052), with the solver's own "no try made" outcome
    assert [c["outcome"] for c in report.captchas] == ["solved", "solved", "solved", "unavailable"]
    assert report.captchas[3]["seq"] == 4 and report.captchas[3]["attempts"] == []
    assert result.counters["captchas"] == 4 and result.counters["captchas_solved"] == 3
    payload = json.loads(ts.console_result_line(result, report)[len("CONSOLE_RESULT "):])
    assert payload["captchas"][3] == {"seq": 4, "outcome": "unavailable", "at": report.captchas[3]["at"], "attempts": []}


def test_solver_budget_is_per_run(tmp_path):
    store = make_store(tmp_path, videos=2, items=0)
    clock = Clock()
    report = CaptchaReport()
    solver = Solver(report, clock, ["solved", "solved"], spend=200)

    run(store, clock=clock, fetch=Fetch(clock, script=captcha_script), solve=solver, report=report)

    (_, first), (_, second) = solver.calls
    assert first == pytest.approx(clock.t - 700 + ts.SOLVER_BUDGET_S)
    # second call: only 240 - 200 = 40 s of solver budget left for the run
    assert second == pytest.approx(clock.t - 300 + 40)


def test_video_with_the_solver_spending_its_budget_stays_under_the_legacy_bound(tmp_path):
    # s25 (console ADR 059, P4, s23 n1): the solver hands back by its deadline, so a video's worst case is its
    # 240 s deadline + one try in flight + list_tabs + PRESENT_JS probe + delete_tab + the run's 240 s of solver,
    # without any solver overrun. Real solver, every Camofox / SadCaptcha call consuming its maximal delay.
    import random

    from app.captcha_solver import solve_captcha
    from test_captcha_solver import KEY as SOLVER_KEY
    from test_captcha_solver import Clock as SolverClock
    from test_captcha_solver import SlowCamofox, SlowSad

    store = make_store(tmp_path, videos=1, items=0)
    clock = SolverClock()
    report = CaptchaReport()
    client = SlowCamofox("rotate_v2", clock, verdicts=[[True]] * 3)
    sad = SlowSad(clock)
    spent = []

    def solve(tab_id, *, deadline):
        client.deadline = sad.deadline = deadline
        began = clock()
        outcome = solve_captcha(client, tab_id, api_key=SOLVER_KEY, report=report, clock=clock, sleep=clock.sleep,
                                rng=random.Random(1), deadline=deadline, sadcaptcha=sad)
        spent.append(clock() - began)
        assert clock() <= deadline
        return outcome

    in_flight = 92 + 10 + 30 + 10  # one fetch try, list_tabs, PRESENT_JS probe, delete_tab

    def script(url, on_captcha, expired, clk):
        clock.t += ts.VIDEO_DEADLINE_S - 1
        solved = on_captcha("tab-1")
        clock.t += in_flight
        return ok_fetch() if solved else {"ok": False, "error": "captcha_unsolved", "comments": []}

    start = clock()
    run(store, clock=clock, fetch=Fetch(clock, script=script), solve=solve, report=report)
    assert spent and spent[0] <= ts.SOLVER_BUDGET_S
    assert clock() - start <= ts.LEGACY_VIDEO_BOUND_S


# --- legacy cron guards ---------------------------------------------------------------------------------------------


def write_proc(root, pid, cmdline):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(cmdline)


def test_legacy_cron_running_reads_proc_cmdlines(tmp_path):
    proc = tmp_path / "proc"
    write_proc(proc, 10, b"python3\0-m\0app.tiktok_scan\0")
    (proc / "self").mkdir()
    assert ts.legacy_cron_running(str(proc), self_pid=1) is False

    write_proc(proc, 11, b"python3\0/opt/data/scripts/tiktok_cron_auto_dm.py\0--apply\0")
    assert ts.legacy_cron_running(str(proc), self_pid=1) is True
    assert ts.legacy_cron_running(str(proc), self_pid=11) is False  # its own pid is ignored


def test_legacy_cron_running_sees_the_wrapper_and_ignores_unreadable(tmp_path):
    proc = tmp_path / "proc"
    write_proc(proc, 20, b"bash\0/opt/data/scripts/tiktok_cron_auto_dm_apply.sh\0")
    assert ts.legacy_cron_running(str(proc), self_pid=1) is True

    proc2 = tmp_path / "proc2"
    (proc2 / "30").mkdir(parents=True)  # no cmdline file: process gone
    (proc2 / "31").mkdir()
    (proc2 / "31" / "cmdline").mkdir()  # unreadable as a file
    assert ts.legacy_cron_running(str(proc2), self_pid=1) is False


NOW = 1_800_000_000.0


def iso(ts_):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts_, tz=timezone.utc).isoformat()


def write_jobs(path, **job):
    entry = {"id": ts.LEGACY_CRON_ID, "name": "TikTok cron auto-DM verified worker", "enabled": True,
             "state": "scheduled", "prompt": "SENTINEL_PROMPT"}
    entry.update(job)
    other = {"id": "42cade327881", "enabled": True, "state": "scheduled", "next_run_at": iso(NOW + 10)}
    path.write_text(json.dumps({"jobs": [other, entry]}), encoding="utf-8")
    return path


def imminent(path, bound, running=lambda: False):
    return ts.legacy_cron_imminent(bound, now=lambda: NOW, jobs_json=str(path), running=running)


def test_legacy_cron_imminent_bounds(tmp_path):
    # review n°1 m1: a video's worst case is ~382 s (240 s deadline + one try in flight + list_tabs 10 s + PRESENT_JS
    # probe 30 s + delete_tab 10 s) + the run's 240 s of solver (+ one try's overrun): bound = 240 + 240 + 220 = 700 s.
    # s24 : épingle amendée (processeur retiré du scan, console ADR 055) — LEGACY_ITEM_BOUND_S is gone.
    assert ts.LEGACY_VIDEO_BOUND_S == ts.VIDEO_DEADLINE_S + ts.SOLVER_BUDGET_S + 220 == 700
    assert not hasattr(ts, "LEGACY_ITEM_BOUND_S")
    jobs = write_jobs(tmp_path / "jobs.json", next_run_at=iso(NOW + 730))
    assert imminent(jobs, ts.LEGACY_VIDEO_BOUND_S) is False
    write_jobs(jobs, next_run_at=iso(NOW + 680))
    assert imminent(jobs, ts.LEGACY_VIDEO_BOUND_S) is True
    write_jobs(jobs, next_run_at=iso(NOW + 630))
    assert imminent(jobs, ts.LEGACY_VIDEO_BOUND_S) is True
    write_jobs(jobs, next_run_at=iso(NOW + 100))
    assert imminent(jobs, ts.LEGACY_VIDEO_BOUND_S) is True
    write_jobs(jobs, next_run_at=iso(NOW - 30))
    assert imminent(jobs, ts.LEGACY_VIDEO_BOUND_S) is True


def test_legacy_cron_imminent_naive_dates_are_utc(tmp_path):
    from datetime import datetime, timezone

    naive = datetime.fromtimestamp(NOW + 630, tz=timezone.utc).replace(tzinfo=None).isoformat()
    jobs = write_jobs(tmp_path / "jobs.json", next_run_at=naive)
    assert imminent(jobs, 600) is False and imminent(jobs, 660) is True


@pytest.mark.parametrize("job", [{"enabled": False}, {"state": "paused"}])
def test_legacy_cron_paused_or_disabled_is_not_imminent(tmp_path, job):
    jobs = write_jobs(tmp_path / "jobs.json", next_run_at=iso(NOW + 10), **job)
    assert imminent(jobs, 600) is False


def test_legacy_cron_unreadable_file_is_imminent(tmp_path):
    assert imminent(tmp_path / "absent.json", 600) is True
    truncated = tmp_path / "t.json"
    truncated.write_text('{"jobs": [', encoding="utf-8")
    assert imminent(truncated, 600) is True
    bad_date = write_jobs(tmp_path / "b.json", next_run_at="tomorrow")
    assert imminent(bad_date, 600) is True
    no_date = write_jobs(tmp_path / "n.json")
    assert imminent(no_date, 600) is True


def test_legacy_cron_absent_job_and_other_jobs_are_ignored(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"jobs": [{"id": "42cade327881", "enabled": True, "next_run_at": iso(NOW + 1)}]}))
    assert imminent(path, 600) is False
    assert imminent(path, 600, running=lambda: True) is True


def test_legacy_cron_file_content_is_never_printed(tmp_path, capsys):
    jobs = write_jobs(tmp_path / "jobs.json", next_run_at=iso(NOW + 100))
    imminent(jobs, 600)
    imminent(tmp_path / "absent.json", 600)
    out, err = capsys.readouterr()
    assert "SENTINEL_PROMPT" not in out + err


# --- hygiene --------------------------------------------------------------------------------------------------------


def test_output_never_carries_private_data(tmp_path):
    store = make_store(tmp_path, videos=2, items=1)
    clock = Clock()
    fetch = Fetch(clock, results=[
        {"ok": True, "comments": [{"author": "@SENTINEL_AUTHOR", "text": "proxy SENTINEL_TEXT"}], "detail": "SENTINEL_DETAIL",
         "diagnostics": {"title": "SENTINEL_TEXT"}},
        {"ok": False, "error": "camofox_fetch_failed", "detail": "SENTINEL_DETAIL", "comments": []},
    ])
    result = run(store, clock=clock, fetch=fetch)
    text = output_of(result)

    for sentinel in SENTINELS:
        assert sentinel not in text
    assert "video 7000: extracted=1 new=1 reviews=1 error=- baseline=1" in text
    assert "video 7001: extracted=0 new=0 reviews=0 error=camofox_fetch_failed baseline=0" in text
    assert text.splitlines()[-1].startswith("CONSOLE_RESULT ")


# --- main (H5) ------------------------------------------------------------------------------------------------------

import io

KEY = "SADKEY-SENTINEL-123"


class MainClient:
    def __init__(self, tabs=1, tabs_error=None):
        self.tabs = tabs
        self.tabs_error = tabs_error
        self.mouse_probes = 0

    def list_tabs(self, *, timeout=None):
        if self.tabs_error is not None:
            raise self.tabs_error
        return [{"tabId": str(i), "url": ""} for i in range(self.tabs)]

    def mouse_available(self, *, timeout=5.0):
        self.mouse_probes += 1
        return False  # the real solver ends `unavailable` before any paid call


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    store = make_store(tmp_path, videos=2, items=2)
    monkeypatch.setenv("TIKTOK_BACKOFFICE_DB", str(store.database))
    client = MainClient()
    monkeypatch.setattr(ts.CamofoxClient, "from_env", classmethod(lambda cls, environ=None: client))
    monkeypatch.setattr(ts, "legacy_cron_running", lambda: False)
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": []}), encoding="utf-8")
    monkeypatch.setattr(ts, "LEGACY_JOBS_JSON", str(jobs))
    monkeypatch.setattr(ts, "_sleep", lambda s: None)
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return ok_fetch()

    monkeypatch.setattr(ts, "fetch_comments_from_camofox", fetch)
    return {"store": store, "client": client, "fetch_calls": calls, "monkeypatch": monkeypatch}


def run_main(argv=(), environ=None, capsys=None):
    out = io.StringIO()
    code = ts.main(list(argv), environ if environ is not None else {"SADCAPTCHA_API_KEY": KEY}, out)
    return code, out.getvalue()


def last_result(text):
    last = text.strip().splitlines()[-1]
    assert last.startswith("CONSOLE_RESULT ")
    return json.loads(last[len("CONSOLE_RESULT "):])


def test_main_nominal_wiring_and_result(main_env, capsys):
    code, text = run_main()

    assert code == 0
    payload = last_result(text)
    assert payload["status"] == "ok" and payload["reason"] is None
    assert set(payload["counters"]) == set(ts.COUNTERS)
    assert len(payload["videos"]) == 2 and "items" not in payload
    call = main_env["fetch_calls"][0]
    assert call["client"] is main_env["client"]
    assert call["session_key"] == ts.SESSION_KEY and call["close_tab"] is True
    assert KEY not in text + "".join(capsys.readouterr())


def test_main_list_tabs_error_is_a_camofox_unreachable_video(main_env):
    main_env["client"].tabs_error = CamofoxUnreachable()
    code, text = run_main()
    assert code == 17
    assert {v["error"] for v in last_result(text)["videos"]} == {"camofox_unreachable"}


@pytest.mark.parametrize(
    ("fetch_result", "code"),
    [
        ({"ok": False, "error": "camofox_fetch_failed", "detail": "SENTINEL_DETAIL", "comments": []}, 17),
        ({"ok": False, "error": "tiktok_page_not_actionable", "comments": []}, 20),
    ],
)
def test_main_failing_exits_still_end_with_console_result(main_env, fetch_result, code, capsys):
    main_env["monkeypatch"].setattr(ts, "fetch_comments_from_camofox", lambda **kw: dict(fetch_result))
    got, text = run_main()
    assert got == code
    payload = last_result(text)
    assert set(payload["counters"]) == set(ts.COUNTERS)
    assert "SENTINEL_DETAIL" not in text
    assert KEY not in text + "".join(capsys.readouterr())


def test_main_unsolved_captcha_is_exit_18_with_the_report(main_env, capsys):
    def fetch(**kwargs):
        if not kwargs["on_captcha"]("tab-1"):
            return {"ok": False, "error": "captcha_unsolved", "comments": []}
        return ok_fetch()

    main_env["monkeypatch"].setattr(ts, "fetch_comments_from_camofox", fetch)
    code, text = run_main()
    assert code == 18
    payload = last_result(text)
    assert payload["captchas"][0]["outcome"] == "unavailable"  # real solver, plugin probe refused
    assert payload["counters"]["captchas"] == 1
    assert KEY not in text + "".join(capsys.readouterr())


def test_main_internal_error_is_exit_19_class_only(main_env, capsys):
    def fetch(**kwargs):
        raise RuntimeError("SENTINEL_TRACE " + KEY)

    main_env["monkeypatch"].setattr(ts, "fetch_comments_from_camofox", fetch)
    code, text = run_main()
    assert code == 19
    assert "tiktok-scan: error=internal class=RuntimeError" in text
    payload = last_result(text)
    assert payload["reason"] == "internal" and set(payload["counters"]) == set(ts.COUNTERS)
    assert "SENTINEL_TRACE" not in text
    assert KEY not in text + "".join(capsys.readouterr())



DIAGNOSTICS = [
    {"seq": 1, "try": 1, "variant": "rotate_v2", "step": "not_gone", "angle": 137, "distance": 105, "requests": 2,
     "moves": 10, "gesture_ms": 15000, "http": 200, "captcha_button": "clicked", "ms": 30000},
    {"seq": 1, "try": 2, "variant": "rotate_v2", "step": "solved", "angle": 12, "distance": 9, "requests": 2,
     "moves": 10, "gesture_ms": 14000, "http": 200, "captcha_button": None, "ms": 25000},
]


def fake_solve_captcha(client, tab_id, *, report, **kwargs):
    report.captchas.append({"seq": 1, "outcome": "solved", "at": 0, "attempts": [
        {"try": 1, "type": "rotate", "variant": "v2", "result": "failed", "ms": 30000},
        {"try": 2, "type": "rotate", "variant": "v2", "result": "solved", "ms": 25000},
    ]})
    report.diagnostics.extend(dict(d) for d in DIAGNOSTICS)
    return "solved"


def assert_captcha_lines_before_the_result(text):
    lines = text.strip().splitlines()
    assert lines[-1].startswith("CONSOLE_RESULT ")
    captcha = [i for i, line in enumerate(lines) if line.startswith("captcha ")]
    assert [lines[i].split(": ")[0] for i in captcha] == ["captcha 1.1", "captcha 1.2"]
    assert captcha == [len(lines) - 3, len(lines) - 2]


def test_main_prints_the_captcha_diagnostic_right_before_the_result(main_env):
    main_env["monkeypatch"].setattr(ts, "solve_captcha", fake_solve_captcha)

    seen = []

    def fetch(**kwargs):
        if not seen:  # one captcha, on the first video only
            seen.append(1)
            assert kwargs["on_captcha"]("tab-1")
        return ok_fetch()

    main_env["monkeypatch"].setattr(ts, "fetch_comments_from_camofox", fetch)
    code, text = run_main()
    assert code == 0
    assert_captcha_lines_before_the_result(text)


def test_main_internal_error_still_prints_the_captcha_diagnostic(main_env):
    main_env["monkeypatch"].setattr(ts, "solve_captcha", fake_solve_captcha)

    def failing(**kw):
        kw["solve"]("tab-1", deadline=0.0)
        raise RuntimeError("SENTINEL_TRACE")

    main_env["monkeypatch"].setattr(ts, "run_scan", failing)
    code, text = run_main()
    assert code == 19
    assert_captcha_lines_before_the_result(text)
    assert "SENTINEL_TRACE" not in text


def test_main_without_captcha_prints_no_captcha_line(main_env):
    code, text = run_main()
    assert code == 0
    assert not any(line.startswith("captcha ") for line in text.splitlines())


def test_main_check_only_reads_without_fetch(main_env, capsys):
    code, text = run_main(["--check-only"], environ={"SADCAPTCHA_API_KEY": KEY})
    assert code == 0
    lines = text.strip().splitlines()
    assert lines[-2] == "tiktok-scan: check targets=2 pending=2 legacy_running=0 legacy_due=0"
    assert last_result(text) == {"status": "skipped", "reason": "check_only", "counters": {}}
    assert main_env["fetch_calls"] == []
    assert KEY not in text + "".join(capsys.readouterr())


def test_main_check_only_reports_a_due_cron(main_env, tmp_path):
    main_env["monkeypatch"].setattr(ts, "LEGACY_JOBS_JSON", str(tmp_path / "absent.json"))
    code, text = run_main(["--check-only"], environ={})
    assert code == 0
    assert text.strip().splitlines()[-2] == "tiktok-scan: check targets=2 pending=2 legacy_running=0 legacy_due=1"


def test_main_check_only_legacy_due_uses_the_video_bound(main_env, tmp_path):
    # review n°1 m1: 650 s before the cron is inside the 700 s video bound
    main_env["monkeypatch"].setattr(ts, "_clock", lambda: NOW)
    write_jobs(tmp_path / "jobs.json", next_run_at=iso(NOW + 650))
    code, text = run_main(["--check-only"], environ={})
    assert code == 0
    assert text.strip().splitlines()[-2].endswith(" legacy_due=1")
    write_jobs(tmp_path / "jobs.json", next_run_at=iso(NOW + 730))
    code, text = run_main(["--check-only"], environ={})
    assert text.strip().splitlines()[-2].endswith(" legacy_due=0")
