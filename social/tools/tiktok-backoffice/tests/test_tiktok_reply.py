"""s24 — app.tiktok_reply: the console job `tiktok-reply` (console ADR 055; AC 2, 5, 6, 7, 8).

Real store on a temporary base, scripted fake page (the interface of app.tiktok_reply_page.TikTokReplyPage), fake
solver, simulated clock. No network, no Camofox, no TikTok.
"""

import hashlib
import io
import json
import os
import re
import signal
import sqlite3
import time
from pathlib import Path

import pytest

from app import tiktok_reply as tr
from app import tiktok_reply_page as trp
from app.camofox_reader import CamofoxUnreachable
from app.captcha_solver import CaptchaReport
from app.models import Campaign
from app.store import TikTokBackofficeStore, sqlite_utc

OWNER = "@dupflodev_test"
DM = "Salut !\nVoici le lien : http://ex.am/ple"
REPLY = "Je viens de t’envoyer le lien en message privé"
VIDEO = "https://www.tiktok.com/@dupflodev/video/7000"
DAY = 86400


class Clock:
    def __init__(self, start=None):
        self.t = time.time() if start is None else start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


# --- base -----------------------------------------------------------------------------------------------------------


def make_store(tmp_path, *, dm_text=DM, active=True):
    store = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")
    store.upsert_campaign(Campaign(slug="proxy", name="Proxy", keywords=("proxy",), reply_template=REPLY, active=active))
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_campaigns SET dm_text = ? WHERE slug = 'proxy'", (dm_text,))
        connection.execute(
            "INSERT INTO tiktok_videos (video_url, video_id, baseline_at) VALUES (?, '7000', '2000-01-01 00:00:00')", (VIDEO,)
        )
    return store


_seq = [0]


def add_item(store, *, author=None, text="proxy stp", created=None, slug="proxy", reply_text=REPLY, status="pending_review",
             video=VIDEO, clock=None):
    _seq[0] += 1
    n = _seq[0]
    author = author or f"@fan{n}"
    created = created if created is not None else sqlite_utc((clock() if clock else time.time()) - 3600 + n)
    with sqlite3.connect(store.database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO tiktok_comments (comment_id, video_url, author, text) VALUES (?, ?, ?, ?)",
            (f"c{n}", video, author, text),
        )
        cursor = connection.execute(
            """INSERT INTO tiktok_review_items (comment_id, campaign_slug, matched_keyword, reply_text, status, created_at, updated_at)
               VALUES (?, ?, 'proxy', ?, ?, ?, ?)""",
            (f"c{n}", slug, reply_text, status, created, created),
        )
        return cursor.lastrowid, author


def item_row(store, rid):
    with sqlite3.connect(store.database) as connection:
        return connection.execute("SELECT status, failure_reason FROM tiktok_review_items WHERE id = ?", (rid,)).fetchone()


def outbound(store):
    with sqlite3.connect(store.database) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(r) for r in connection.execute("SELECT * FROM tiktok_outbound_messages ORDER BY id")]


def add_outbound(store, n, *, at, status="sent", rid=999999, mtype="private_reply"):
    for _ in range(n):
        store.record_outbound(review_id=rid, recipient="@x", message_type=mtype, text="t", status=status)
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE tiktok_outbound_messages SET created_at = ? WHERE source_id = ?", (sqlite_utc(at), str(rid)))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- fake page ------------------------------------------------------------------------------------------------------


class FakePage:
    """The TikTok side: comments of one video, DM threads per handle, and scripted failures."""

    def __init__(self, owner=OWNER):
        self.owner = owner
        self.calls = []
        self.comments = {}  # author -> {"text", "replies"}
        self.threads = {}  # author -> outgoing bubbles
        self.dm_lands = True
        self.reply_lands = True
        self.captcha = []  # successive answers of captcha_present
        self.login_gate = False
        self.no_message = set()
        self.fail = {}  # step -> exception (raised once)
        self.hooks = {}  # step -> callable run at the step
        self.typed = {}
        # s30 (console ADR 064): conversation column text per handle, scripted `dm_probe` records
        self.conversations = {}
        self.probe_profile = {"captcha": False, "candidates": 1, "chosen": "text", "tag": "BUTTON"}
        self.probe_thread = {"attempt": 1, "editor": True, "polls": 2}
        self.probe = []

    def seed(self, author, text="proxy stp", replies=()):
        self.comments[author] = {"text": text, "replies": [{"author": a, "text": t} for a, t in replies]}

    def _step(self, name, *args):
        self.calls.append((name,) + args)
        hook = self.hooks.get(name)
        if hook is not None:
            hook()
        exc = self.fail.pop(name, None)
        if exc is not None:
            raise exc

    def open_video(self, url):
        self._step("open_video")
        return "video-tab"

    def captcha_present(self, tab):
        self._step("captcha_present", tab)
        return self.captcha.pop(0) if self.captcha else False

    def comment_snapshot(self, tab, *, author, text):
        self._step("comment_snapshot")
        if self.login_gate:
            raise trp.LoginGate("login_gate")
        return {"comments": [
            {"author": a, "text": c["text"], "reply_button": {"selector": "RB", "x": 1, "y": 1}, "view_replies": [],
             "replies": list(c["replies"])}
            for a, c in self.comments.items()
        ]}

    def open_dm(self, handle, *, captcha_gate):
        self._step("open_dm", handle)
        self.probe = [("profile", dict(self.probe_profile))]
        captcha_gate("dm-tab")  # s30: the captcha gate comes before the profile is read
        if self.login_gate:
            raise trp.LoginGate("login_gate")
        if handle in self.no_message:
            raise trp.DmUnavailable("dm_unavailable")
        self.current = handle
        self.threads.setdefault(handle, [])
        self.probe.append(("thread", dict(self.probe_thread)))
        return "dm-tab"

    def thread_snapshot(self, tab):
        self._step("thread_snapshot")
        return {"editor": {"selector": "ED", "text": self.typed.get(self.current, "")}, "outgoing": list(self.threads[self.current]),
                "send_button": {"selector": "S"}, "error": False, "conversation": self.conversations.get(self.current, "")}

    def verify_dm(self, tab, text):
        self._step("verify_dm")
        ok = trp.dm_verified(self.thread_snapshot(tab), text)  # through thread_snapshot: the hooks stay
        self.probe.append(("verify", {"verified": ok, "polls": 1}))
        return ok

    def take_dm_probe(self):
        probe, self.probe = self.probe, []
        return probe

    def type_dm(self, tab, text):
        self._step("type_dm", text)
        self.typed[self.current] = text

    def send_dm(self, tab):
        self._step("send_dm", self.current)
        text = self.typed.pop(self.current, "")
        if self.dm_lands:
            self.threads[self.current].append(text)

    def post_reply(self, tab, target, text):
        self._step("post_reply", target["author"], text)
        if self.reply_lands:
            self.comments[target["author"]]["replies"].append({"author": self.owner, "text": text})

    def close(self, tab):
        self.calls.append(("close", tab))

    def close_all(self):
        self.calls.append(("close_all",))

    def names(self):
        return [c[0] for c in self.calls]

    def sends(self):
        return [c for c in self.calls if c[0] in ("send_dm", "post_reply")]


class Solver:
    def __init__(self, report, outcomes, clock=None, spend=0.0):
        self.report = report
        self.outcomes = list(outcomes)
        self.calls = []
        self.clock = clock
        self.spend = spend

    def __call__(self, tab, *, deadline):
        self.calls.append((tab, deadline))
        outcome = self.outcomes.pop(0)
        self.report.captchas.append({"seq": len(self.report.captchas) + 1, "outcome": outcome, "at": 0,
                                     "attempts": [{"try": 1, "type": "puzzle", "variant": "v2", "result": "solved" if outcome == "solved" else "failed", "ms": 5}]})
        if self.clock is not None:
            self.clock.advance(self.spend)
        return outcome


def run(store, page, *, clock=None, legacy=None, solve=None, limit=10, max_items=tr.ITEM_LIMIT, report=None):
    clock = clock or Clock()
    report = report if report is not None else CaptchaReport()
    return tr.run_reply(
        store=store,
        page=page,
        legacy_active=legacy or (lambda: None),
        solve=solve or Solver(report, []),
        clock=clock,
        limit=limit,
        owner=OWNER,
        max_items=max_items,
        report=report,
    )


def output_of(result, report=None):
    return "\n".join(tr.emit_lines(result) + [tr.console_result_line(result, report or CaptchaReport())])


# --- invariants -----------------------------------------------------------------------------------------------------


def test_budget_invariants_b1():
    assert tr.RUN_BUDGET_S + 300 <= 1500
    assert tr.ITEM_LIMIT * tr.ITEM_DEADLINE_S <= tr.RUN_BUDGET_S
    assert (tr.EXIT_BROWSER_UNAVAILABLE, tr.EXIT_CAPTCHA_UNSOLVED, tr.EXIT_INTERNAL, tr.EXIT_SESSION_LOST,
            tr.EXIT_CONFIG_INVALID) == (21, 22, 23, 24, 25)
    assert tr.DEFAULT_MAX_PER_HOUR == 10 and tr.WINDOW_7D_S == 7 * DAY and tr.RATE_WINDOW_S == 3600


# --- legacy guard (P3) ----------------------------------------------------------------------------------------------


def write_jobs(path, **job):
    entry = {"id": tr.LEGACY_CRON_ID, "enabled": True, "state": "scheduled", "prompt": "SENTINEL_PROMPT"}
    entry.update(job)
    path.write_text(json.dumps({"jobs": [{"id": "42cade327881", "enabled": True}, entry]}), encoding="utf-8")
    return path


def test_legacy_cron_active_reads_enabled_and_state(tmp_path):
    jobs = write_jobs(tmp_path / "jobs.json")
    assert tr.legacy_cron_active(jobs_json=str(jobs), running=lambda: False) == "legacy_cron_enabled"
    write_jobs(jobs, state="paused")
    assert tr.legacy_cron_active(jobs_json=str(jobs), running=lambda: False) is None
    write_jobs(jobs, enabled=False)
    assert tr.legacy_cron_active(jobs_json=str(jobs), running=lambda: False) is None
    assert tr.legacy_cron_active(jobs_json=str(jobs), running=lambda: True) == "legacy_cron_running"


def test_legacy_cron_active_fails_closed(tmp_path, capsys):
    assert tr.legacy_cron_active(jobs_json=str(tmp_path / "absent.json"), running=lambda: False) == "legacy_cron_enabled"
    bad = tmp_path / "bad.json"
    bad.write_text('{"jobs": [', encoding="utf-8")
    assert tr.legacy_cron_active(jobs_json=str(bad), running=lambda: False) == "legacy_cron_enabled"
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"jobs": [{"id": "42cade327881", "enabled": True}]}), encoding="utf-8")
    assert tr.legacy_cron_active(jobs_json=str(other), running=lambda: False) is None
    out, err = capsys.readouterr()
    assert "SENTINEL_PROMPT" not in out + err


def test_legacy_cron_enabled_skips_without_any_write(tmp_path):
    store = make_store(tmp_path)
    add_item(store, created=sqlite_utc(time.time() - 30 * DAY))
    add_item(store)
    before = digest(store.database)
    page = FakePage()

    result = run(store, page, legacy=lambda: "legacy_cron_enabled")

    assert (result.status, result.reason, result.exit_code) == ("skipped", "legacy_cron_enabled", 0)
    assert page.calls == []
    assert digest(store.database) == before


def test_legacy_cron_back_before_the_second_item(tmp_path):
    store = make_store(tmp_path)
    page = FakePage()
    r1, a1 = add_item(store)
    r2, a2 = add_item(store)
    page.seed(a1)
    page.seed(a2)
    answers = iter([None, None, "legacy_cron_enabled"])

    result = run(store, page, legacy=lambda: next(answers))

    assert (result.status, result.reason, result.exit_code) == ("skipped", "legacy_cron_enabled", 0)
    assert item_row(store, r1) == ("posted", None)
    assert item_row(store, r2) == ("pending_review", None)
    assert result.counters["deferred"] == 1


def test_paused_legacy_job_runs_normally(tmp_path):
    jobs = write_jobs(tmp_path / "jobs.json", state="paused")
    store = make_store(tmp_path)
    page = FakePage()
    rid, author = add_item(store)
    page.seed(author)
    result = run(store, page, legacy=lambda: tr.legacy_cron_active(jobs_json=str(jobs), running=lambda: False))
    assert result.status == "ok" and item_row(store, rid) == ("posted", None)


# --- queue, expiration (D3, B3) -------------------------------------------------------------------------------------


def test_empty_queue_is_ok_no_pending_without_page(tmp_path):
    store = make_store(tmp_path)
    page = FakePage()
    result = run(store, page)
    assert (result.status, result.reason, result.exit_code) == ("ok", "no_pending", 0)
    assert page.calls == []
    assert set(result.counters) == set(tr.COUNTERS)


def test_v1_backlog_is_all_expired_without_any_page_call(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(time.mktime((2026, 10, 20, 12, 0, 0, 0, 0, 0)) - time.timezone)
    ids = []
    start = time.mktime((2026, 8, 2, 0, 0, 0, 0, 0, 0)) - time.timezone
    end = time.mktime((2026, 10, 12, 0, 0, 0, 0, 0, 0)) - time.timezone
    for i in range(39):
        ids.append(add_item(store, created=sqlite_utc(start + (end - start) * i / 38))[0])
    page = FakePage()

    result = run(store, page, clock=clock)

    assert (result.status, result.reason) == ("ok", "no_pending")
    assert result.counters["items_expired"] == 39
    assert all(item_row(store, rid) == ("expired", "older_than_7d") for rid in ids)
    assert page.calls == []


def test_six_days_old_item_after_baseline_is_processed(tmp_path):
    store = make_store(tmp_path)
    page = FakePage()
    rid, author = add_item(store, created=sqlite_utc(time.time() - 6 * DAY))
    page.seed(author)
    result = run(store, page)
    assert item_row(store, rid) == ("posted", None)
    assert result.counters["items_expired"] == 0


def test_b3_legacy_ingested_item_is_closed_legacy_backlog(tmp_path):
    store = make_store(tmp_path)
    other = "https://www.tiktok.com/@dupflodev/video/7001"
    with sqlite3.connect(store.database) as connection:
        connection.execute("INSERT INTO tiktok_videos (video_url, video_id) VALUES (?, '7001')", (other,))
    legacy, _ = add_item(store, created=sqlite_utc(time.time() - 2 * DAY), video=other)
    eligible, author = add_item(store)
    page = FakePage()
    page.seed(author)
    add_outbound(store, 8, at=time.time() - 60)

    result = run(store, page)

    assert item_row(store, legacy) == ("expired", "legacy_backlog")
    assert result.counters["items_legacy_backlog"] == 1
    assert item_row(store, eligible) == ("posted", None)
    assert [c for c in page.calls if c[0] == "open_dm"] == [("open_dm", author)]
    assert result.counters["budget_left"] == 0


# --- campaign data (D1, D2, N4) -------------------------------------------------------------------------------------


def test_n4_head_without_dm_text_consumes_no_budget(tmp_path):
    store = make_store(tmp_path)
    store.upsert_campaign(Campaign(slug="nodm", name="No DM", keywords=("x",), reply_template=REPLY))
    head, _ = add_item(store, slug="nodm")
    second, author = add_item(store)
    page = FakePage()
    page.seed(author)

    result = run(store, page, limit=2)

    assert item_row(store, head) == ("failed", "no_dm_text")
    assert item_row(store, second) == ("posted", None)
    assert result.counters["actions_sent"] == 2


@pytest.mark.parametrize(
    ("setup", "reason"),
    [("unknown", "campaign_unknown"), ("inactive", "campaign_inactive"), ("blank_dm", "no_dm_text")],
)
def test_d2_campaign_problems_fail_without_page_and_do_not_block_the_queue(tmp_path, setup, reason):
    store = make_store(tmp_path)
    if setup == "inactive":
        store.upsert_campaign(Campaign(slug="off", name="Off", keywords=("x",), reply_template=REPLY, active=False))
        with sqlite3.connect(store.database) as connection:
            connection.execute("UPDATE tiktok_campaigns SET dm_text = 'dm' WHERE slug = 'off'")
        head, _ = add_item(store, slug="off")
    elif setup == "blank_dm":
        store.upsert_campaign(Campaign(slug="blank", name="Blank", keywords=("x",), reply_template=REPLY))
        with sqlite3.connect(store.database) as connection:
            connection.execute("UPDATE tiktok_campaigns SET dm_text = '  \n ' WHERE slug = 'blank'")
        head, _ = add_item(store, slug="blank")
    else:
        head, _ = add_item(store, slug="systeme")
    second, author = add_item(store)
    page = FakePage()
    page.seed(author)

    result = run(store, page)

    assert item_row(store, head) == ("failed", reason)
    assert item_row(store, second) == ("posted", None)
    assert [c for c in page.calls if c[0] == "open_video"] == [("open_video",)]
    assert {"id": head, "outcome": reason, "dm": "-", "reply": "-"} in result.items


def test_d1_texts_come_from_the_base(tmp_path):
    store = make_store(tmp_path, dm_text="SENTINEL_DM_FROM_BASE")
    rid, author = add_item(store, reply_text="SENTINEL_REPLY_FROM_ITEM")
    page = FakePage()
    page.seed(author)
    run(store, page)
    assert ("type_dm", "SENTINEL_DM_FROM_BASE") in page.calls
    assert ("post_reply", author, "SENTINEL_REPLY_FROM_ITEM") in page.calls
    assert "CAMPAIGN_TITLES" not in Path(tr.__file__).read_text(encoding="utf-8")


# --- nominal and guards (D9, AC 8) ----------------------------------------------------------------------------------


def test_nominal_dm_then_reply_both_verified(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)

    result = run(store, page)

    assert (result.status, result.reason, result.exit_code) == ("ok", None, 0)
    assert item_row(store, rid) == ("posted", None)
    rows = outbound(store)
    assert [(r["message_type"], r["status"], r["recipient_id"], r["source_id"]) for r in rows] == [
        ("private_reply", "sent", author, str(rid)),
        ("public_reply", "sent", author, str(rid)),
    ]
    assert rows[0]["message_text"] == DM and rows[1]["message_text"] == REPLY
    assert [c[0] for c in page.sends()] == ["send_dm", "post_reply"]
    assert result.items == [{"id": rid, "outcome": "posted", "dm": "sent", "reply": "sent"}]
    c = result.counters
    assert (c["items_claimed"], c["items_posted"], c["actions_sent"], c["dm_sent"], c["replies_sent"]) == (1, 1, 2, 1, 1)
    assert c["budget_limit"] == 10 and c["budget_left"] == 8
    assert ("close_all",) in page.calls


def test_ac8_owner_reply_present_and_no_own_send_is_not_reprocessed(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author, replies=[(OWNER.upper(), "merci !")])

    result = run(store, page)

    assert item_row(store, rid) == ("posted", "already_present")
    assert page.sends() == []
    assert "open_dm" not in page.names()
    assert outbound(store) == []
    assert result.counters["budget_left"] == 10 and result.counters["items_already_present"] == 1
    assert result.items[0]["outcome"] == "already_present"


def test_dm_present_in_thread_without_own_row_is_not_resent_but_reply_is_made(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.threads[author] = [DM.replace("\n", " ")]

    result = run(store, page)

    assert "send_dm" not in page.names() and "type_dm" not in page.names()
    assert [c[0] for c in page.sends()] == ["post_reply"]
    assert item_row(store, rid) == ("posted", None)
    assert result.items[0] == {"id": rid, "outcome": "posted", "dm": "present", "reply": "sent"}
    assert [r["message_type"] for r in outbound(store)] == ["public_reply"]


def test_base_guard_existing_dm_row_means_no_dm(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    store.record_outbound(review_id=rid, recipient=author, message_type="private_reply", text=DM, status="sent")
    page = FakePage()
    page.seed(author)

    run(store, page)

    assert "open_dm" not in page.names()
    assert [c[0] for c in page.sends()] == ["post_reply"]
    assert item_row(store, rid) == ("posted", None)


def test_base_guard_existing_dm_row_and_owner_reply_present_sends_nothing(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    store.record_outbound(review_id=rid, recipient=author, message_type="private_reply", text=DM, status="sent")
    page = FakePage()
    page.seed(author, replies=[(OWNER, REPLY)])

    result = run(store, page)

    assert page.sends() == []
    assert item_row(store, rid) == ("posted", "already_present")
    assert result.items[0]["reply"] == "present"


def test_reply_text_empty_means_dm_only_and_one_action(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store, reply_text="")
    page = FakePage()
    page.seed(author)
    result = run(store, page, limit=1)
    assert [c[0] for c in page.sends()] == ["send_dm"]
    assert item_row(store, rid) == ("posted", None)
    assert result.items[0]["reply"] == "-"


def test_target_absent_fails_target_not_found(tmp_path):
    store = make_store(tmp_path)
    rid, _ = add_item(store)
    page = FakePage()
    result = run(store, page)
    assert item_row(store, rid) == ("failed", "target_not_found")
    assert page.sends() == []
    assert result.counters["items_failed"] == 1


def test_dm_unavailable_fails_without_action_nor_reply(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.no_message.add(author)
    run(store, page)
    assert item_row(store, rid) == ("failed", "dm_unavailable")
    assert page.sends() == []


# --- verification (D9, S3) ------------------------------------------------------------------------------------------


def test_unverified_dm_fails_with_a_failed_row_and_no_reply(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.dm_lands = False

    result = run(store, page)

    assert item_row(store, rid) == ("failed", "not_verified")
    assert [(r["message_type"], r["status"]) for r in outbound(store)] == [("private_reply", "failed")]
    assert "post_reply" not in page.names()
    assert result.items[0] == {"id": rid, "outcome": "not_verified", "dm": "unverified", "reply": "-"}
    assert result.counters["actions_unverified"] == 1 and result.counters["actions_sent"] == 1


def test_unverified_reply_keeps_the_dm_row(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.reply_lands = False

    run(store, page)

    assert item_row(store, rid) == ("failed", "public_reply_not_verified")
    assert [(r["message_type"], r["status"]) for r in outbound(store)] == [("private_reply", "sent"), ("public_reply", "failed")]


# --- hourly limit (D7, AC 7) ----------------------------------------------------------------------------------------


def test_limit_reached_at_start_is_skipped_rate_limited_without_page(tmp_path):
    store = make_store(tmp_path)
    clock = Clock()
    rid, author = add_item(store)
    add_outbound(store, 10, at=clock() - 600)
    page = FakePage()
    result = run(store, page, clock=clock)
    assert (result.status, result.reason, result.exit_code) == ("skipped", "rate_limited", 0)
    assert page.calls == []
    assert result.counters["deferred"] == 1
    assert item_row(store, rid) == ("pending_review", None)


def test_failed_rows_count_and_older_rows_do_not(tmp_path):
    store = make_store(tmp_path)
    clock = Clock()
    add_item(store)
    add_outbound(store, 9, at=clock() - 600, status="failed")
    add_outbound(store, 5, at=clock() - 3700, rid=888888)
    result = run(store, FakePage(), clock=clock)
    assert (result.status, result.reason) == ("skipped", "rate_limited")
    assert result.counters["budget_left"] == 1


def test_budget_spent_during_the_run_is_ok_rate_limited(tmp_path):
    store = make_store(tmp_path)
    clock = Clock()
    page = FakePage()
    ids = []
    for _ in range(3):
        rid, author = add_item(store)
        page.seed(author)
        ids.append(rid)
    add_outbound(store, 8, at=clock() - 600)

    result = run(store, page, clock=clock)

    assert (result.status, result.reason, result.exit_code) == ("ok", "rate_limited", 0)
    assert [item_row(store, r)[0] for r in ids] == ["posted", "pending_review", "pending_review"]
    assert result.counters["deferred"] == 2
    assert "queue: deferred rate_limited n=2" in tr.emit_lines(result)


def test_limit_one_and_head_needing_two_actions_is_skipped(tmp_path):
    store = make_store(tmp_path)
    add_item(store)
    result = run(store, FakePage(), limit=1)
    assert (result.status, result.reason) == ("skipped", "rate_limited")


# --- released to the queue (P1) -------------------------------------------------------------------------------------


def test_unsolved_captcha_before_action_releases_and_exits_22(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.captcha = [True]
    report = CaptchaReport()
    result = run(store, page, solve=Solver(report, ["unsolved"]), report=report)
    assert (result.status, result.exit_code) == ("failed", 22)
    assert item_row(store, rid) == ("pending_review", None)
    assert page.sends() == []
    assert result.counters["captchas"] == 1 and result.counters["items_released"] == 1
    assert "queue: released captcha_unsolved n=1" in tr.emit_lines(result)
    assert json.loads(tr.console_result_line(result, report)[len("CONSOLE_RESULT "):])["captchas"][0]["outcome"] == "unsolved"


def test_solved_captcha_goes_on(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.captcha = [True]
    report = CaptchaReport()
    result = run(store, page, solve=Solver(report, ["solved"]), report=report)
    assert item_row(store, rid) == ("posted", None)
    assert result.counters["captchas_solved"] == 1 and result.counters["captcha_tries"] == 1


def test_fourth_captcha_is_refused_without_solver(tmp_path):
    store = make_store(tmp_path)
    page = FakePage()
    for _ in range(4):
        _, author = add_item(store, reply_text="")
        page.seed(author)
    page.captcha = [True, False, True, False, True, False, True]
    report = CaptchaReport()
    solver = Solver(report, ["solved", "solved", "solved"])
    result = run(store, page, solve=solver, report=report)
    assert len(solver.calls) == 3
    assert result.exit_code == 22
    assert [c["outcome"] for c in report.captchas] == ["solved", "solved", "solved", "unavailable"]


def test_login_gate_releases_and_exits_24(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.login_gate = True
    result = run(store, page)
    assert (result.status, result.exit_code) == ("failed", 24)
    assert item_row(store, rid) == ("pending_review", None)


def test_m1_login_gate_after_a_verified_dm_fails_the_item_and_keeps_the_dm_row(tmp_path):
    # s25 (s24 m1, console ADR 059): the session is lost between the verified DM and the public reply
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.fail["post_reply"] = trp.LoginGate("login_gate")
    result = run(store, page)
    assert (result.status, result.exit_code) == ("failed", 24)
    assert item_row(store, rid) == ("failed", "public_reply_not_verified")
    assert [(r["message_type"], r["status"]) for r in outbound(store)] == [("private_reply", "sent")]
    assert f"item {rid}: outcome=public_reply_not_verified dm=sent reply=-" in tr.emit_lines(result)
    assert result.counters["dm_sent"] == 1 and result.counters["items_failed"] == 1


def test_m4_finish_refused_after_a_dm_is_claim_lost(tmp_path):
    # s25 (s24 m4, console ADR 059): the item was requeued between the click and the end; the actions happened
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    store.finish_review_item = lambda review_id, status, *, reason: False
    result = run(store, page)
    assert result.items == [{"id": rid, "outcome": "claim_lost", "dm": "sent", "reply": "sent"}]
    assert result.counters["items_claim_lost"] == 1
    assert result.counters["items_posted"] == 0 and result.counters["items_failed"] == 0
    assert result.counters["dm_sent"] == 1 and result.counters["actions_sent"] == 2
    assert [(r["message_type"], r["status"]) for r in outbound(store)] == [("private_reply", "sent"), ("public_reply", "sent")]
    assert (result.status, result.exit_code) == ("ok", 0)


def test_m5_dm_editor_not_empty_releases_the_item_with_its_page_code(tmp_path):
    # s25 (s24 m5, console ADR 059, P7): nothing typed, nothing sent, the item released, exit 21
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.fail["type_dm"] = trp.PageUnusable("dm_editor_not_empty")
    report = CaptchaReport()
    result = run(store, page, report=report)
    assert (result.status, result.exit_code) == ("failed", 21)
    assert item_row(store, rid) == ("pending_review", None)
    assert outbound(store) == [] and page.sends() == []
    assert f"item {rid}: outcome=released dm=- reply=- page=dm_editor_not_empty" in tr.emit_lines(result)
    payload = json.loads(tr.console_result_line(result, report)[len("CONSOLE_RESULT "):])
    assert payload["items"] == [{"id": rid, "outcome": "released", "dm": "-", "reply": "-"}]


def test_page_code_only_for_a_page_unusable_release(tmp_path):
    store = make_store(tmp_path)
    _, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.fail["open_dm"] = CamofoxUnreachable()
    result = run(store, page)
    assert result.exit_code == 21
    assert not any(" page=" in line for line in tr.emit_lines(result))


def test_camofox_unreachable_before_action_releases_and_exits_21(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    rid2, author2 = add_item(store)
    page = FakePage()
    page.seed(author)
    page.fail["open_video"] = CamofoxUnreachable()
    result = run(store, page)
    assert (result.status, result.exit_code) == ("failed", 21)
    assert item_row(store, rid) == ("pending_review", None)
    assert item_row(store, rid2) == ("pending_review", None)
    assert "queue: released browser_unavailable n=1" in tr.emit_lines(result)


def test_tabs_full_before_action_releases_and_exits_21(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.fail["open_video"] = trp.TabsFull("tabs_full")
    result = run(store, page)
    assert result.exit_code == 21 and item_row(store, rid) == ("pending_review", None)


def test_camofox_error_after_the_send_click_is_never_released(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.fail["send_dm"] = trp.SendUncertain("CamofoxUnreachable")
    result = run(store, page)
    assert result.exit_code == 21
    assert item_row(store, rid) == ("failed", "not_verified")
    assert [(r["message_type"], r["status"]) for r in outbound(store)] == [("private_reply", "failed")]


def test_verification_error_after_the_send_click_is_not_verified(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    calls = {"n": 0}

    def after_send():
        calls["n"] += 1

    real = page.thread_snapshot

    def snapshot(tab):
        if "send_dm" in page.names():
            raise CamofoxUnreachable()
        return real(tab)

    page.thread_snapshot = snapshot
    result = run(store, page)
    assert result.exit_code == 21
    assert item_row(store, rid) == ("failed", "not_verified")
    assert [(r["message_type"], r["status"]) for r in outbound(store)] == [("private_reply", "failed")]


def test_unexpected_exception_leaves_the_item_sending_exit_23(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.fail["comment_snapshot"] = KeyError("SENTINEL_TRACE")
    result = run(store, page)
    assert (result.status, result.exit_code) == ("failed", 23)
    assert result.error_class == "KeyError"
    assert item_row(store, rid) == ("sending", None)
    assert "SENTINEL_TRACE" not in output_of(result)


def test_claim_lost_opens_no_page(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    rid2, author2 = add_item(store)
    page = FakePage()
    page.seed(author2)
    real = store.claim_review_item

    def racing(review_id, cutoff):
        if review_id == rid:
            real(review_id, cutoff)  # another run takes it first
        return real(review_id, cutoff)

    store.claim_review_item = racing
    result = run(store, page)
    assert result.counters["items_claim_lost"] == 1
    assert {"id": rid, "outcome": "claim_lost", "dm": "-", "reply": "-"} in result.items
    assert [c for c in page.calls if c[0] == "open_dm"] == [("open_dm", author2)]


def test_max_items_one(tmp_path):
    store = make_store(tmp_path)
    page = FakePage()
    for _ in range(3):
        _, author = add_item(store)
        page.seed(author)
    result = run(store, page, max_items=1)
    assert result.counters["items_claimed"] == 1 and len(result.items) == 1
    assert result.counters["deferred"] == 2 and result.status == "ok" and result.reason is None


def test_run_time_budget_defers_the_rest(tmp_path):
    store = make_store(tmp_path)
    clock = Clock()
    page = FakePage()
    ids = []
    for _ in range(2):
        rid, author = add_item(store, clock=clock)
        page.seed(author)
        ids.append(rid)
    page.hooks["post_reply"] = lambda: clock.advance(tr.RUN_BUDGET_S - tr.ITEM_DEADLINE_S + 1)
    result = run(store, page, clock=clock)
    assert [item_row(store, r)[0] for r in ids] == ["posted", "pending_review"]
    assert (result.status, result.reason) == ("ok", None) and result.counters["deferred"] == 1


def test_item_deadline_before_any_action_releases_the_item_and_goes_on(tmp_path):
    store = make_store(tmp_path)
    clock = Clock()
    page = FakePage()
    r1, a1 = add_item(store, clock=clock)
    r2, a2 = add_item(store, clock=clock)
    page.seed(a1)
    page.seed(a2)
    seen = {"n": 0}

    def slow():
        seen["n"] += 1
        if seen["n"] == 1:
            clock.advance(tr.ITEM_DEADLINE_S + 1)

    page.hooks["comment_snapshot"] = slow
    result = run(store, page, clock=clock)
    assert item_row(store, r1) == ("pending_review", None)
    assert item_row(store, r2) == ("posted", None)
    assert {"id": r1, "outcome": "released", "dm": "-", "reply": "-"} in result.items
    assert result.status == "ok" and result.counters["items_released"] == 1


# --- s30 (console ADR 064): DM path at run level -----------------------------------------------------------------

PROBE_LINE = re.compile(r"^dm_probe item=\d+ step=(profile|thread|verify)(?: [a-z0-9_]+=[A-Za-z0-9_-]{1,40})+$")


@pytest.mark.parametrize("code", ["no_dm_editor", "no_dm_button"])
def test_s30_no_dm_editor_or_button_releases_the_item_exit_21_with_its_page_code(tmp_path, code):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.fail["open_dm"] = trp.PageUnusable(code)
    result = run(store, page)
    assert (result.status, result.exit_code) == ("failed", 21)
    assert item_row(store, rid) == ("pending_review", None)
    assert outbound(store) == [] and page.sends() == []
    assert f"item {rid}: outcome=released dm=- reply=- page={code}" in tr.emit_lines(result)


def test_s30_r1_conversation_already_holding_the_dm_types_nothing(tmp_path):
    # manual replay of a not_verified item whose DM went: no bubble recognised, no `sent` row, the conversation holds it
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.conversations[author] = "Fan " + DM.replace("\n", " ") + " Seen"
    result = run(store, page)
    assert "type_dm" not in page.names() and "send_dm" not in page.names()
    assert result.items[0] == {"id": rid, "outcome": "posted", "dm": "present", "reply": "sent"}
    assert [r["message_type"] for r in outbound(store)] == ["public_reply"]


def test_s30_captcha_on_the_profile_unsolved_releases_exit_22(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.captcha = [False, True]  # the video, then the profile (captcha gate of open_dm)
    report = CaptchaReport()
    solver = Solver(report, ["unsolved"])
    result = run(store, page, solve=solver, report=report)
    assert (result.status, result.exit_code) == ("failed", 22)
    assert item_row(store, rid) == ("pending_review", None)
    assert page.sends() == [] and "type_dm" not in page.names()
    assert [c[0] for c in solver.calls] == ["dm-tab"]
    lines = tr.emit_lines(result)
    assert any(line.startswith(f"dm_probe item={rid} step=profile ") for line in lines)


def test_s30_captcha_on_the_profile_solved_goes_on(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.captcha = [False, True]
    report = CaptchaReport()
    result = run(store, page, solve=Solver(report, ["solved"]), report=report)
    assert item_row(store, rid) == ("posted", None)
    assert result.items[0]["dm"] == "sent"


def test_s30_unconfirmed_dm_is_sent_once_and_never_replayed(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    _, author2 = add_item(store)
    page = FakePage()
    page.seed(author)
    page.seed(author2)
    page.dm_lands = False
    result = run(store, page)
    assert [c for c in page.calls if c[0] == "send_dm"] == [("send_dm", author), ("send_dm", author2)]
    assert page.names().count("verify_dm") == 2
    assert item_row(store, rid) == ("failed", "not_verified")
    assert [(r["source_id"], r["status"]) for r in outbound(store)][0] == (str(rid), "failed")
    assert result.exit_code == 0 and result.counters["items_failed"] == 2


def test_s30_dm_probe_lines_follow_their_item_line_in_order_and_stay_out_of_console_result(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    report = CaptchaReport()
    result = run(store, page, report=report)
    lines = tr.emit_lines(result)
    at = lines.index(f"item {rid}: outcome=posted dm=sent reply=sent")
    probe = lines[at + 1:at + 4]
    assert [line.split()[2] for line in probe] == ["step=profile", "step=thread", "step=verify"]
    assert all(PROBE_LINE.match(line) for line in probe), probe
    assert all(line.startswith(f"dm_probe item={rid} ") for line in probe)
    assert len([line for line in lines if line.startswith("dm_probe ")]) == 3
    payload = tr.console_result_line(result, report)
    assert "dm_probe" not in payload
    assert json.loads(payload[len("CONSOLE_RESULT "):])["items"] == [
        {"id": rid, "outcome": "posted", "dm": "sent", "reply": "sent"}]
    assert tr.ITEM_KEYS == ("id", "outcome", "dm", "reply")


def test_s30_dm_probe_lines_kept_for_an_item_released_by_a_stop(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    page = FakePage()
    page.seed(author)
    page.fail["type_dm"] = trp.PageUnusable("dm_editor_not_empty")
    result = run(store, page)
    lines = tr.emit_lines(result)
    at = lines.index(f"item {rid}: outcome=released dm=- reply=- page=dm_editor_not_empty")
    assert [line.split()[2] for line in lines[at + 1:at + 3]] == ["step=profile", "step=thread"]


def test_s30_no_dm_probe_line_without_a_dm_path(tmp_path):
    store = make_store(tmp_path)
    rid, author = add_item(store)
    store.record_outbound(review_id=rid, recipient=author, message_type="private_reply", text=DM, status="sent")
    page = FakePage()
    page.seed(author)
    result = run(store, page)
    assert not any(line.startswith("dm_probe ") for line in tr.emit_lines(result))


# --- D4 and hygiene -------------------------------------------------------------------------------------------------


def test_d4_no_like_anywhere_in_the_job():
    source = Path(tr.__file__).read_text(encoding="utf-8")
    assert "digg" not in source and "xclip" not in source and "subprocess" not in source


def test_output_never_carries_private_data(tmp_path):
    store = make_store(tmp_path, dm_text="SENTINEL_DM")
    url = "https://www.tiktok.com/@SENTINEL_URL/video/1"
    with sqlite3.connect(store.database) as connection:
        connection.execute("INSERT INTO tiktok_videos (video_url, video_id, baseline_at) VALUES (?, '1', '2000-01-01 00:00:00')", (url,))
    rid, author = add_item(store, author="@SENTINEL_AUTHOR", text="SENTINEL_COMMENT", reply_text="SENTINEL_REPLY", video=url)
    rid2, author2 = add_item(store, author="@SENTINEL_AUTHOR2", text="SENTINEL_COMMENT", reply_text="SENTINEL_REPLY", video=url)
    page = FakePage()
    page.seed("@SENTINEL_AUTHOR", "SENTINEL_COMMENT")
    page.seed("@SENTINEL_AUTHOR2", "SENTINEL_COMMENT")
    page.hooks["open_dm"] = lambda: None
    page.fail["post_reply"] = None
    # s30 (console ADR 064, D1): a probe whose values would carry private data prints `other`, a foreign key nothing
    page.probe_profile = {"e2e": "@SENTINEL_PROBE_HANDLE", "cls": "Salut SENTINEL_PROBE_TEXT",
                          "tag": "/@SENTINEL_PROBE_URL/video/7212345678", "role": "chat-7212345",
                          "handle": "SENTINEL_PROBE_KEY"}
    seq = {"n": 0}

    def fail_second():
        seq["n"] += 1
        if seq["n"] == 2:
            raise ValueError("SENTINEL_TRACE")

    page.hooks["comment_snapshot"] = fail_second
    result = run(store, page)
    text = output_of(result)
    for sentinel in ("SENTINEL_AUTHOR", "SENTINEL_COMMENT", "SENTINEL_DM", "SENTINEL_REPLY", "SENTINEL_URL", "SENTINEL_TRACE",
                     "SENTINEL_PROBE", "7212345"):
        assert sentinel not in text
    assert any(line.startswith(f"dm_probe item={rid} step=profile ") for line in text.splitlines())
    assert text.splitlines()[-1].startswith("CONSOLE_RESULT ")


# --- main (H7) ------------------------------------------------------------------------------------------------------

KEY = "SADKEY-SENTINEL-123"


class MainClient:
    def mouse_available(self, *, timeout=5.0):
        return False


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    monkeypatch.setenv("TIKTOK_BACKOFFICE_DB", str(store.database))
    monkeypatch.setattr(tr.CamofoxClient, "from_env", classmethod(lambda cls, environ=None: MainClient()))
    monkeypatch.setattr(tr, "legacy_cron_running", lambda: False)
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": []}), encoding="utf-8")
    monkeypatch.setattr(tr, "LEGACY_JOBS_JSON", str(jobs))
    monkeypatch.setattr(tr, "_sleep", lambda s: None)
    page = FakePage(owner="@dupflodev")
    monkeypatch.setattr(tr, "TikTokReplyPage", lambda client, *, owner, sleep: page)
    return {"store": store, "page": page, "monkeypatch": monkeypatch, "jobs": jobs, "tmp": tmp_path}


ENV = {"TIKTOK_OWNER_HANDLE": "@dupflodev", "TIKTOK_REPLY_MAX_PER_HOUR": "10", "SADCAPTCHA_API_KEY": KEY}


def run_main(argv=(), environ=None):
    out = io.StringIO()
    code = tr.main(list(argv), dict(ENV) if environ is None else environ, out)
    return code, out.getvalue()


def last_result(text):
    last = text.strip().splitlines()[-1]
    assert last.startswith("CONSOLE_RESULT ")
    return json.loads(last[len("CONSOLE_RESULT "):])


def test_main_nominal(main_env, capsys):
    rid, author = add_item(main_env["store"])
    main_env["page"].owner = "@dupflodev"
    main_env["page"].seed(author)
    code, text = run_main()
    assert code == 0
    payload = last_result(text)
    assert payload["status"] == "ok" and set(payload["counters"]) == set(tr.COUNTERS)
    assert payload["items"] == [{"id": rid, "outcome": "posted", "dm": "sent", "reply": "sent"}]
    lines = text.strip().splitlines()
    assert lines[0] == "tiktok-reply: start pending=1 expirable=0 legacy_backlog=0 budget=10/10 owner=ok"
    assert f"item {rid}: outcome=posted dm=sent reply=sent" in lines
    assert "tiktok-reply: status=ok reason=- sent=2 expired=0 legacy=0 failed=0 deferred=0 budget=8/10" in lines
    assert KEY not in text + "".join(capsys.readouterr())


def test_main_limit_absent_defaults_to_10(main_env):
    env = dict(ENV)
    del env["TIKTOK_REPLY_MAX_PER_HOUR"]
    code, text = run_main(environ=env)
    assert code == 0 and last_result(text)["counters"]["budget_limit"] == 10


@pytest.mark.parametrize("value", ["0", "61", "abc", "-1", "1.5"])
def test_main_invalid_limit_is_exit_25_without_store(tmp_path, monkeypatch, value):
    db = tmp_path / "absent" / "never.sqlite3"
    monkeypatch.setenv("TIKTOK_BACKOFFICE_DB", str(db))
    code, text = run_main(environ=dict(ENV, TIKTOK_REPLY_MAX_PER_HOUR=value))
    assert code == 25
    assert set(last_result(text)["counters"]) == set(tr.COUNTERS)
    assert not db.exists() and not db.parent.exists()


@pytest.mark.parametrize("owner", [None, "dupflodev", "@a", "@has space", "@" + "x" * 25])
def test_main_invalid_owner_is_exit_25_without_store(tmp_path, monkeypatch, owner):
    db = tmp_path / "absent" / "never.sqlite3"
    monkeypatch.setenv("TIKTOK_BACKOFFICE_DB", str(db))
    env = dict(ENV)
    if owner is None:
        del env["TIKTOK_OWNER_HANDLE"]
    else:
        env["TIKTOK_OWNER_HANDLE"] = owner
    code, text = run_main(environ=env)
    assert code == 25 and "tiktok-reply: error=config_invalid" in text
    assert not db.parent.exists()


@pytest.mark.parametrize("value", ["0", "6", "x"])
def test_main_invalid_max_items_is_exit_25(main_env, value):
    code, text = run_main(["--max-items", value])
    assert code == 25
    last_result(text)


def test_main_max_items_bounds_the_items(main_env):
    for _ in range(3):
        _, author = add_item(main_env["store"])
        main_env["page"].seed(author)
    code, text = run_main(["--max-items", "1"])
    assert code == 0
    payload = last_result(text)
    assert len(payload["items"]) == 1 and payload["counters"]["items_claimed"] == 1


@pytest.mark.parametrize(
    ("setup", "code"),
    [("captcha", 22), ("login", 24), ("camofox", 21), ("internal", 23)],
)
def test_main_failing_exits_end_with_console_result(main_env, setup, code, capsys):
    _, author = add_item(main_env["store"])
    page = main_env["page"]
    page.seed(author)
    if setup == "captcha":
        page.captcha = [True]
        main_env["monkeypatch"].setattr(tr, "solve_captcha", lambda *a, **k: k["report"].captchas.append(
            {"seq": 1, "outcome": "unsolved", "at": 0, "attempts": []}) or "unsolved")
    elif setup == "login":
        page.login_gate = True
    elif setup == "camofox":
        page.fail["open_video"] = CamofoxUnreachable()
    else:
        page.fail["comment_snapshot"] = RuntimeError("SENTINEL_TRACE " + KEY)
    got, text = run_main()
    assert got == code
    payload = last_result(text)
    assert set(payload["counters"]) == set(tr.COUNTERS)
    if setup == "captcha":
        assert payload["captchas"][0]["outcome"] == "unsolved"
    if setup == "internal":
        assert "tiktok-reply: error=internal class=RuntimeError" in text
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
    _, author = add_item(main_env["store"])
    main_env["page"].seed(author)
    main_env["page"].captcha = [True]
    main_env["monkeypatch"].setattr(tr, "solve_captcha", fake_solve_captcha)
    code, text = run_main()
    assert code == 0
    assert_captcha_lines_before_the_result(text)


def test_main_internal_error_still_prints_the_captcha_diagnostic(main_env):
    def failing(**kw):
        fake_solve_captcha(None, "tab-1", report=kw["report"])
        raise LookupError("SENTINEL_TRACE")

    main_env["monkeypatch"].setattr(tr, "run_reply", failing)
    code, text = run_main()
    assert code == 23
    assert_captcha_lines_before_the_result(text)
    assert "SENTINEL_TRACE" not in text


def test_main_without_captcha_prints_no_captcha_line(main_env):
    _, author = add_item(main_env["store"])
    main_env["page"].seed(author)
    code, text = run_main()
    assert code == 0
    assert not any(line.startswith("captcha ") for line in text.splitlines())


def test_main_exception_outside_the_run_is_exit_23_class_only(main_env, capsys):
    main_env["monkeypatch"].setattr(tr, "run_reply", lambda **kw: (_ for _ in ()).throw(LookupError("SENTINEL_TRACE")))
    code, text = run_main()
    assert code == 23
    assert "tiktok-reply: error=internal class=LookupError" in text
    assert set(last_result(text)["counters"]) == set(tr.COUNTERS)
    assert "SENTINEL_TRACE" not in text


def test_main_check_only(main_env, capsys):
    store = main_env["store"]
    add_item(store)
    add_item(store, created=sqlite_utc(time.time() - 8 * DAY))
    other = "https://www.tiktok.com/@dupflodev/video/7001"
    with sqlite3.connect(store.database) as connection:
        connection.execute("INSERT INTO tiktok_videos (video_url, video_id) VALUES (?, '7001')", (other,))
    add_item(store, video=other)
    store.upsert_campaign(Campaign(slug="nodm", name="No DM", keywords=("x",), reply_template=REPLY))
    add_outbound(store, 3, at=time.time() - 60)
    before = digest(store.database)

    code, text = run_main(["--check-only"])

    assert code == 0
    assert text.strip().splitlines()[-2] == (
        "tiktok-reply: check pending=1 expirable=1 legacy_backlog=1 legacy=none budget=7/10 owner=ok"
        " captcha_key=ok dm_text_campaigns=1/2"
    )
    assert last_result(text) == {"status": "skipped", "reason": "check_only", "counters": {}}
    assert main_env["page"].calls == []
    assert digest(store.database) == before
    assert KEY not in text + "".join(capsys.readouterr())


def test_main_check_only_reports_missing_key_owner_and_enabled_cron(main_env):
    write_jobs(main_env["jobs"])
    env = {"TIKTOK_REPLY_MAX_PER_HOUR": "10"}
    code, text = run_main(["--check-only"], environ=env)
    assert code == 0
    line = text.strip().splitlines()[-2]
    assert " legacy=enabled " in line and " owner=missing " in line and " captcha_key=missing " in line


def test_main_legacy_enabled_is_skipped(main_env):
    write_jobs(main_env["jobs"])
    rid, author = add_item(main_env["store"])
    code, text = run_main()
    assert code == 0
    assert last_result(text)["status"] == "skipped" and last_result(text)["reason"] == "legacy_cron_enabled"
    assert item_row(main_env["store"], rid) == ("pending_review", None)


def test_main_sigterm_before_the_click_releases_the_item(main_env):
    rid, author = add_item(main_env["store"])
    page = main_env["page"]
    page.seed(author)
    page.hooks["type_dm"] = lambda: os.kill(os.getpid(), signal.SIGTERM)
    code, text = run_main()
    assert code == 23
    assert ("close_all",) in page.calls
    assert item_row(main_env["store"], rid) == ("pending_review", None)
    assert "send_dm" not in page.names()
    assert text.strip().splitlines()[-1].startswith("CONSOLE_RESULT ")
    assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.default_int_handler) or callable(signal.getsignal(signal.SIGTERM))


def test_main_sigterm_after_the_click_leaves_the_item_sending(main_env):
    rid, author = add_item(main_env["store"])
    page = main_env["page"]
    page.seed(author)
    real = page.send_dm

    def send(tab):
        real(tab)
        os.kill(os.getpid(), signal.SIGTERM)

    page.send_dm = send
    code, text = run_main()
    assert code == 23
    assert item_row(main_env["store"], rid) == ("sending", None)
    assert ("close_all",) in page.calls
    assert last_result(text)["counters"]["actions_sent"] == 1
    assert [(r["message_type"], r["status"]) for r in outbound(main_env["store"])] == [("private_reply", "failed")]


def test_main_restores_the_previous_sigterm_handler(main_env):
    before = signal.getsignal(signal.SIGTERM)
    run_main()
    assert signal.getsignal(signal.SIGTERM) is before
