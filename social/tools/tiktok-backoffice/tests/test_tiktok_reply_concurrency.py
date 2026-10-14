"""s24 — exactly once, end to end (console ADR 055: D8, B1; AC 1, 6).

File base shared by distinct TikTokBackofficeStore instances; fake pages share one TikTok "world" (comments and
DM threads), so a duplicate send would be visible. No network, no Camofox.
"""

import sqlite3
import threading
import time

from app import tiktok_reply as tr
from app import tiktok_scan as ts
from app.captcha_solver import CaptchaReport
from app.store import TikTokBackofficeStore, sqlite_utc
from test_tiktok_reply import DM, OWNER, VIDEO, Clock, FakePage, add_item, item_row, make_store, outbound

# Console requeue SQL: copy of TIKTOK_REQUEUE_SQL (console core/replay/index.ts, C4, ADR 056), named parameters
# @id / @stale / @window as better-sqlite3 binds them.
CONSOLE_REQUEUE_SQL = """UPDATE tiktok_review_items
SET status = 'pending_review', failure_reason = NULL, updated_at = CURRENT_TIMESTAMP
WHERE id = @id
  AND created_at >= @window
  AND (status IN ('failed', 'needs_manual_public_fallback')
       OR (status = 'sending' AND updated_at < @stale))"""


def console_requeue(db, review_id, now=None):
    now = time.time() if now is None else now
    with sqlite3.connect(db) as connection:
        cursor = connection.execute(
            CONSOLE_REQUEUE_SQL.replace("@", ":"),
            {"id": review_id, "stale": sqlite_utc(now - 30 * 60), "window": sqlite_utc(now - 7 * 86400)},
        )
        return cursor.rowcount


def age_updated_at(db, review_id, seconds):
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE tiktok_review_items SET updated_at = ? WHERE id = ?", (sqlite_utc(time.time() - seconds), review_id)
        )


def shared_pages(n):
    first = FakePage()
    pages = [first]
    for _ in range(n - 1):
        page = FakePage()
        page.comments, page.threads = first.comments, first.threads
        pages.append(page)
    return pages


def run_once(store, page, **kwargs):
    report = CaptchaReport()
    return tr.run_reply(store=store, page=page, legacy_active=lambda: None, solve=lambda tab, *, deadline: "unsolved",
                        clock=Clock(), limit=kwargs.get("limit", 60), owner=OWNER, max_items=kwargs.get("max_items", 5),
                        report=report)


class BarrierStore:
    """A distinct store instance that waits for the other run right after reading the queue."""

    def __init__(self, db, barrier):
        self.store = TikTokBackofficeStore(db)
        self.barrier = barrier
        self.claims = []

    def reply_queue(self, limit):
        rows = self.store.reply_queue(limit)
        self.barrier.wait(timeout=10)
        return rows

    def claim_review_item(self, review_id, cutoff):
        ok = self.store.claim_review_item(review_id, cutoff)
        self.claims.append((review_id, ok))
        return ok

    def __getattr__(self, name):
        return getattr(self.store, name)


def test_two_concurrent_runs_never_process_the_same_item(tmp_path):
    base = make_store(tmp_path)
    pages = shared_pages(2)
    ids = []
    for _ in range(4):
        rid, author = add_item(base)
        pages[0].seed(author)
        ids.append(rid)
    barrier = threading.Barrier(2)
    stores = [BarrierStore(base.database, barrier) for _ in range(2)]
    results = [None, None]

    def worker(i):
        results[i] = run_once(stores[i], pages[i])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    successes = {}
    for s in stores:
        for rid, ok in s.claims:
            successes[rid] = successes.get(rid, 0) + (1 if ok else 0)
    assert all(successes.get(rid, 0) == 1 for rid in ids)
    rows = outbound(base)
    keys = [(r["source_id"], r["message_type"]) for r in rows]
    assert len(keys) == len(set(keys)) == len(ids) * 2
    clicks = sum(len(p.sends()) for p in pages)
    assert clicks == len(ids) * 2
    assert all(item_row(base, rid) == ("posted", None) for rid in ids)
    assert sum(r.counters["items_claimed"] for r in results) == len(ids)


def test_scan_during_a_reply_run_never_changes_an_existing_item(tmp_path):
    base = make_store(tmp_path)
    page = FakePage()
    rid, author = add_item(base)
    other, other_author = add_item(base)
    page.seed(author)
    page.seed(other_author)
    with sqlite3.connect(base.database) as connection:
        connection.execute("INSERT INTO tiktok_video_campaigns (video_url, campaign_slug, approved) VALUES (?, 'proxy', 1)", (VIDEO,))
    scan_store = TikTokBackofficeStore(base.database)
    seen = {}

    def scan():
        if seen:
            return
        before = {r: item_row(base, r) for r in (rid, other)}
        result = ts.run_scan(
            store=scan_store,
            fetch=lambda **kw: {"ok": True, "comments": [{"author": "@newfan", "text": "proxy aussi"}]},
            tabs_count=lambda: 1,
            legacy_running=lambda: False,
            legacy_imminent=lambda bound: False,
            solve=lambda tab, *, deadline: "unsolved",
            clock=lambda: 0.0,
            sleep=lambda s: None,
            report=CaptchaReport(),
        )
        seen["scan"] = result
        assert {r: item_row(base, r) for r in (rid, other)} == before

    page.hooks["send_dm"] = scan
    run_once(base, page, max_items=1)

    assert seen["scan"].counters["reviews_created"] == 1
    assert item_row(base, rid) == ("posted", None)
    assert item_row(base, other) == ("pending_review", None)


def test_old_sending_item_is_never_taken_and_a_requeued_one_gets_no_second_dm(tmp_path):
    base = make_store(tmp_path)
    page = FakePage()
    rid, author = add_item(base, status="sending")
    page.seed(author)
    age_updated_at(base.database, rid, 2 * 3600)

    run_once(base, page)
    assert page.calls == [] and item_row(base, rid) == ("sending", None)

    base.record_outbound(review_id=rid, recipient=author, message_type="private_reply", text=DM, status="sent")
    assert console_requeue(base.database, rid) == 1
    run_once(base, page)

    assert "send_dm" not in page.names() and "open_dm" not in page.names()
    assert [c[0] for c in page.sends()] == ["post_reply"]
    assert item_row(base, rid) == ("posted", None)


def test_b1_requeue_between_the_dm_click_and_finish_never_sends_a_second_dm(tmp_path):
    base = make_store(tmp_path)
    page = FakePage()
    rid, author = add_item(base)
    page.seed(author)
    fired = {}

    def requeue_after_click():
        if fired:
            return
        fired["yes"] = True
        age_updated_at(base.database, rid, 31 * 60)
        assert console_requeue(base.database, rid) == 1

    real_send = page.send_dm

    def send(tab):
        real_send(tab)
        requeue_after_click()  # right after the click, before verification, row and finish

    page.send_dm = send
    first = run_once(base, page)

    assert item_row(base, rid) == ("pending_review", None)  # finish refused: the console requeued it
    assert [r["message_type"] for r in outbound(base) if r["status"] == "sent"][:1] == ["private_reply"]
    assert first.items[0]["outcome"] == "posted"

    second = run_once(base, page)

    assert [c[0] for c in page.calls].count("send_dm") == 1
    assert item_row(base, rid) == ("posted", "already_present")
    assert second.counters["actions_sent"] == 0
