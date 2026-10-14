"""TikTok replies and DMs, run by the hermes-console job `tiktok-reply` (s24, console ADR 055).

It replaces the versioned legacy processor (scripts/process_tiktok_review_one.py) and the detection of the Hermes
cron « TikTok fallback surveillance »: the review queue (`pending_review`, slug != guide, author `@…`, oldest first)
is worked here only, one item at a time:

0. guard: the legacy cron `bf85146a1ae8` is running (`/proc`) or enabled and not paused in jobs.json (unreadable
   file = enabled, fail closed) => `skipped`, nothing written; asked again before every item (P3);
1. queue items ingested more than 7 days ago are closed `expired`/`older_than_7d`, and items not ingested by a
   console read after their video's baseline are closed `expired`/`legacy_backlog` (D3, B3): no page, no budget;
2. per item: conditional claim `pending_review` -> `sending` (D8); campaign unknown / inactive / without DM text =>
   `failed` without any browser (D2); base guard (our own `sent` rows); video page: captcha => s22 solver, owner
   reply already under the comment and no own row => `posted`/`already_present`, nothing sent (AC 8); DM by real
   keystrokes in the profile's thread, then the public reply, each send VERIFIED in the page before it counts (D9);
3. every clicked send writes its `tiktok_outbound_messages` row at once (`sent` | `failed`), the status after (S3).

No « J'aime » (console ADR 057). Hourly limit (default 10 actions, `TIKTOK_REPLY_MAX_PER_HOUR`) counted in
`tiktok_outbound_messages` over a sliding hour (D7).

Outcomes (ADR 011 contract; the exit code wins over the CONSOLE_RESULT line, which is always printed last):

- exit 0: `ok` (reason none | no_pending | rate_limited) | `skipped` + legacy_cron_running | legacy_cron_enabled |
  rate_limited (+ check_only for `--check-only`);
- exit 21 browser unavailable / page unusable, 22 captcha unsolved, 23 internal error (class name only) or SIGTERM,
  24 TikTok session lost, 25 invalid configuration (owner handle, hourly limit, --max-items).

Hygiene: the output holds numeric ids, counters and codes from closed lists only (OUTCOMES, ACTION_STATES). Never
an author, a handle, a comment, DM or reply text, an URL, nor an exception message. The SadCaptcha key is read from
the environment and never printed.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, TextIO

from app.camofox_reader import CamofoxClient, CamofoxError
from app.captcha_solver import CaptchaReport, diagnostic_lines, solve_captcha
from app.config import Settings
from app.store import TikTokBackofficeStore, sqlite_utc
from app.tiktok_reply_page import (
    PAGE_UNUSABLE_CODES,
    DmUnavailable,
    LoginGate,
    PageError,
    PageUnusable,
    SendUncertain,
    TikTokReplyPage,
    dm_present,
    dm_verified,
    find_target,
    owner_reply_present,
    reply_verified,
)
from app.tiktok_reply_page import SESSION_KEY  # noqa: F401 - part of the job contract
from app.tiktok_scan import LEGACY_CRON_ID, LEGACY_JOBS_JSON, legacy_cron_running

ITEM_LIMIT = 5  # 10 actions / 2 per item
ITEM_DEADLINE_S = 240.0
SOLVER_BUDGET_S = 240.0
MAX_CAPTCHAS_PER_RUN = 3
RUN_BUDGET_S = 1200.0  # = ITEM_LIMIT x ITEM_DEADLINE_S; registry timeoutSec 1500 = 1200 + 300
QUEUE_MARGIN = 45  # queue read = max_items + 45: room for items that fail without a browser (N4)
DEFAULT_MAX_PER_HOUR = 10
WINDOW_7D_S = 7 * 86400
RATE_WINDOW_S = 3600
OWNER_PATTERN = re.compile(r"^@[A-Za-z0-9._]{2,24}$")

EXIT_BROWSER_UNAVAILABLE = 21
EXIT_CAPTCHA_UNSOLVED = 22
EXIT_INTERNAL = 23
EXIT_SESSION_LOST = 24
EXIT_CONFIG_INVALID = 25

OUTCOMES = (
    "posted",
    "already_present",
    "legacy_backlog",
    "campaign_unknown",
    "campaign_inactive",
    "no_dm_text",
    "target_not_found",
    "dm_unavailable",
    "not_verified",
    "public_reply_not_verified",
    "released",
    "claim_lost",
)
ACTION_STATES = ("sent", "unverified", "present", "skipped", "-")
ITEM_KEYS = ("id", "outcome", "dm", "reply")  # keys of CONSOLE_RESULT `items` (the `page=` code stays on stdout)
FAILED_OUTCOMES = frozenset(
    {"campaign_unknown", "campaign_inactive", "no_dm_text", "target_not_found", "dm_unavailable", "not_verified",
     "public_reply_not_verified"}
)

COUNTERS = (
    "queue_pending",
    "items_expired",
    "items_legacy_backlog",
    "items_claimed",
    "items_posted",
    "items_already_present",
    "items_failed",
    "items_released",
    "items_claim_lost",
    "deferred",
    "actions_sent",
    "actions_unverified",
    "dm_sent",
    "replies_sent",
    "budget_limit",
    "budget_left",
    "pending_after",
    "captchas",
    "captchas_solved",
    "captcha_tries",
)

_STOP_CODES = {
    "browser_unavailable": EXIT_BROWSER_UNAVAILABLE,
    "captcha_unsolved": EXIT_CAPTCHA_UNSOLVED,
    "session_lost": EXIT_SESSION_LOST,
}
_BROWSER_ERRORS = (CamofoxError, ValueError, OSError)

_clock: Callable[[], float] = time.time
_sleep: Callable[[float], None] = time.sleep


class ReplyTerminated(BaseException):
    """SIGTERM of the runner's `timeout` (S7): unwinds the `finally` blocks, then exit 23."""


class _Stop(Exception):
    """End of the run after this item: closed code of _STOP_CODES."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ItemDeadline(Exception):
    pass


@dataclass
class ReplyResult:
    status: str
    reason: str | None
    exit_code: int
    counters: dict[str, int]
    items: list[dict[str, Any]] = field(default_factory=list)
    error_class: str | None = None
    released_code: str | None = None
    started: bool = False  # the queue was read (the `start` line is printed)
    budget_at_start: int = 0


# --- legacy cron guard (P3) -----------------------------------------------------------------------------------------


def legacy_cron_active(
    *, jobs_json: str = LEGACY_JOBS_JSON, running: Callable[[], bool] = legacy_cron_running
) -> str | None:
    """`legacy_cron_running` | `legacy_cron_enabled` | None. Reads only `enabled` and `state` of the legacy job."""
    if running():
        return "legacy_cron_running"
    try:
        with open(jobs_json, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return "legacy_cron_enabled"
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return "legacy_cron_enabled"
    job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == LEGACY_CRON_ID), None)
    if job is None:
        return None
    if job.get("enabled") and job.get("state") != "paused":
        return "legacy_cron_enabled"
    return None


# --- one item (P2) --------------------------------------------------------------------------------------------------


def planned_actions(item: Mapping[str, Any]) -> int:
    """0 for an item that fails without a browser (N4), else the DM + the public reply when there is one."""
    if not item.get("campaign_found") or not item.get("campaign_active") or not str(item.get("dm_text") or "").strip():
        return 0
    return 1 + (1 if str(item.get("reply_text") or "").strip() else 0)


class _Run:
    def __init__(self, *, store: Any, page: Any, solve: Callable[..., str], clock: Callable[[], float], owner: str,
                 report: CaptchaReport, counters: dict[str, int]) -> None:
        self.store, self.page, self.solve, self.clock, self.owner = store, page, solve, clock, owner
        self.report, self.counters = report, counters
        self.solver_used = 0.0

    # captcha (s22 solver, ADR 052/053)

    def captcha_cleared(self, tab: str, item_clock: dict[str, float]) -> bool:
        if not self.page.captcha_present(tab):
            return True
        if len(self.report.captchas) >= MAX_CAPTCHAS_PER_RUN:
            self.report.captchas.append(
                {"seq": len(self.report.captchas) + 1, "outcome": "unavailable", "at": int(self.clock() * 1000), "attempts": []}
            )
            return False
        began = self.clock()
        try:
            outcome = self.solve(tab, deadline=began + max(0.0, SOLVER_BUDGET_S - self.solver_used))
        finally:
            spent = self.clock() - began
            self.solver_used += spent
            item_clock["paused"] += spent
        return outcome == "solved"

    def process(self, item: Mapping[str, Any]) -> dict[str, Any]:
        rid = int(item["id"])
        entry = {"id": rid, "outcome": "", "dm": "-", "reply": "-"}
        # `in_flight`: a send gesture was started; a SIGTERM inside it may follow the click, so the item is never
        # released then (it stays `sending`, replayable after 30 min, the base and page guards preventing a repeat).
        state: dict[str, Any] = {"acted": False, "in_flight": None, "flight_counted": False}
        item_clock = {"started": self.clock(), "paused": 0.0}

        def finish(status: str, outcome: str, reason: str | None) -> dict[str, Any]:
            # s25 (s24 m4, console ADR 059, P8): a refused finish = the item was requeued meanwhile; the actions
            # (and their rows) stay, the item is reported `claim_lost`.
            if not self.store.finish_review_item(rid, status, reason=reason):
                outcome = "claim_lost"
            entry["outcome"] = outcome
            return entry

        def check_deadline() -> None:
            if not state["acted"] and self.clock() - item_clock["started"] - item_clock["paused"] > ITEM_DEADLINE_S:
                raise _ItemDeadline()

        if not item.get("campaign_found"):
            return finish("failed", "campaign_unknown", "campaign_unknown")
        if not item.get("campaign_active"):
            return finish("failed", "campaign_inactive", "campaign_inactive")
        dm_text = str(item.get("dm_text") or "")
        if not dm_text.strip():
            return finish("failed", "no_dm_text", "no_dm_text")
        reply_text = str(item.get("reply_text") or "").strip()
        author, comment = str(item["author"]), str(item["comment_text"])

        sent = self.store.sent_outbound_types(rid)
        dm_done, reply_done, own_sent = "private_reply" in sent, "public_reply" in sent, bool(sent)
        if dm_done:
            entry["dm"] = "skipped"

        def record(message_type: str, text: str, ok: bool) -> None:
            self.store.record_outbound(review_id=rid, recipient=author, message_type=message_type, text=text,
                                       status="sent" if ok else "failed")
            landed()
            if not ok:
                self.counters["actions_unverified"] += 1

        def take_off(message_type: str, text: str) -> None:
            state["in_flight"], state["flight_counted"] = (message_type, text), False

        def landed() -> None:
            state["in_flight"] = None

        def acted() -> None:
            state["acted"] = True
            state["flight_counted"] = True
            self.counters["actions_sent"] += 1
            self.counters["budget_left"] = max(0, self.counters["budget_left"] - 1)

        try:
            try:
                video = self.page.open_video(item["video_url"])
                check_deadline()
                if not self.captcha_cleared(video, item_clock):
                    raise _Stop("captcha_unsolved")
                check_deadline()
                snap = self.page.comment_snapshot(video, author=author, text=comment)
                check_deadline()
                target = find_target(snap, author, comment)
                if target is None:
                    return finish("failed", "target_not_found", "target_not_found")
                owner_present = owner_reply_present(target, self.owner)
                if owner_present and not own_sent:
                    entry["dm"], entry["reply"] = "skipped", "present"
                    return finish("posted", "already_present", "already_present")
                if owner_present:
                    reply_done = True
                    entry["reply"] = "present"
                elif reply_done:
                    entry["reply"] = "skipped"

                if not dm_done:
                    try:
                        dm_tab = self.page.open_dm(author)
                    except DmUnavailable:
                        return finish("failed", "dm_unavailable", "dm_unavailable")
                    check_deadline()
                    if not self.captcha_cleared(dm_tab, item_clock):
                        raise _Stop("captcha_unsolved")
                    check_deadline()
                    if dm_present(self.page.thread_snapshot(dm_tab), dm_text):
                        entry["dm"] = "present"
                    else:
                        self.page.type_dm(dm_tab, dm_text)
                        check_deadline()
                        take_off("private_reply", dm_text)
                        try:
                            self.page.send_dm(dm_tab)
                        except SendUncertain:
                            acted()
                            record("private_reply", dm_text, False)
                            entry["dm"] = "unverified"
                            finish("failed", "not_verified", "not_verified")
                            raise _Stop("browser_unavailable") from None
                        acted()
                        error: str | None = None
                        try:
                            verified = dm_verified(self.page.thread_snapshot(dm_tab), dm_text)
                        except (PageError, *_BROWSER_ERRORS):
                            verified, error = False, "browser_unavailable"
                        record("private_reply", dm_text, verified)
                        if not verified:
                            entry["dm"] = "unverified"
                            finish("failed", "not_verified", "not_verified")
                            if error:
                                raise _Stop(error)
                            return entry
                        entry["dm"] = "sent"
                        self.counters["dm_sent"] += 1
                    self.page.close(dm_tab)

                if reply_text and not reply_done:
                    take_off("public_reply", reply_text)
                    try:
                        self.page.post_reply(video, target, reply_text)
                    except SendUncertain:
                        acted()
                        record("public_reply", reply_text, False)
                        entry["reply"] = "unverified"
                        finish("failed", "public_reply_not_verified", "public_reply_not_verified")
                        raise _Stop("browser_unavailable") from None
                    acted()
                    error = None
                    try:
                        after = self.page.comment_snapshot(video, author=author, text=comment)
                        verified = reply_verified(find_target(after, author, comment) or {}, self.owner, reply_text)
                    except (PageError, *_BROWSER_ERRORS):
                        verified, error = False, "browser_unavailable"
                    record("public_reply", reply_text, verified)
                    if not verified:
                        entry["reply"] = "unverified"
                        finish("failed", "public_reply_not_verified", "public_reply_not_verified")
                        if error:
                            raise _Stop(error)
                        return entry
                    entry["reply"] = "sent"
                    self.counters["replies_sent"] += 1

                if state["acted"]:
                    return finish("posted", "posted", None)
                return finish("posted", "already_present", "already_present")
            except _ItemDeadline:
                self.store.release_review_item(rid)
                entry["outcome"] = "released"
                return entry
            except (_Stop, ReplyTerminated):
                raise
            except LoginGate:
                if state["acted"]:
                    # s25 (s24 m1, console ADR 059): session lost after the verified DM, the item never stays `sending`.
                    finish("failed", "public_reply_not_verified", "public_reply_not_verified")
                raise _Stop("session_lost") from None
            except (PageError, *_BROWSER_ERRORS) as exc:
                if state["acted"]:
                    # A page failure between the DM and the public reply: the DM row stays, the item is replayable.
                    finish("failed", "public_reply_not_verified", "public_reply_not_verified")
                elif isinstance(exc, PageUnusable) and exc.args and exc.args[0] in PAGE_UNUSABLE_CODES:
                    entry["page"] = exc.args[0]  # s25 (P7): printed on the `item` line only
                raise _Stop("browser_unavailable") from None
        except _Stop as stop:
            if not state["acted"] and not entry["outcome"]:
                self.store.release_review_item(rid)
                entry["outcome"] = "released"
            stop.entry = entry  # type: ignore[attr-defined]
            raise
        except ReplyTerminated:
            flight = state["in_flight"]
            if flight is not None:
                # SIGTERM during or right after a send gesture: the action may have happened (S7).
                if not state["flight_counted"]:
                    acted()
                record(flight[0], flight[1], False)
            elif not state["acted"]:
                self.store.release_review_item(rid)
            raise
        finally:
            self.page.close_all()


# --- run ------------------------------------------------------------------------------------------------------------


def run_reply(
    *,
    store: Any,
    page: Any,
    legacy_active: Callable[[], str | None],
    solve: Callable[..., str],
    clock: Callable[[], float],
    limit: int,
    owner: str,
    max_items: int = ITEM_LIMIT,
    report: CaptchaReport | None = None,
) -> ReplyResult:
    report = report if report is not None else CaptchaReport()
    counters = {name: 0 for name in COUNTERS}
    counters["budget_limit"] = int(limit)

    def captcha_counters() -> None:
        counters["captchas"] = len(report.captchas)
        counters["captchas_solved"] = sum(1 for c in report.captchas if c.get("outcome") == "solved")
        counters["captcha_tries"] = sum(len(c.get("attempts") or []) for c in report.captchas)

    legacy = legacy_active()
    if legacy is not None:
        return ReplyResult("skipped", legacy, 0, counters)

    t0 = clock()
    counters["items_expired"] = int(store.expire_stale_pending(sqlite_utc(t0 - WINDOW_7D_S)))
    counters["items_legacy_backlog"] = int(store.expire_legacy_backlog())
    queue = store.reply_queue(max_items + QUEUE_MARGIN)
    counters["queue_pending"] = len(queue)
    counters["budget_left"] = max(0, int(limit) - int(store.outbound_count_since(sqlite_utc(t0 - RATE_WINDOW_S))))
    budget_at_start = counters["budget_left"]

    items: list[dict[str, Any]] = []
    status, reason, code = "ok", None, 0
    error_class: str | None = None
    released_code: str | None = None

    if not queue:
        reason = "no_pending"
    run = _Run(store=store, page=page, solve=solve, clock=clock, owner=owner, report=report, counters=counters)
    browser_items = 0
    for index, item in enumerate(queue):
        remaining = len(queue) - index
        planned = planned_actions(item)
        if planned and RUN_BUDGET_S - (clock() - t0) < ITEM_DEADLINE_S:
            counters["deferred"] += remaining
            break
        guard = legacy_active()
        if guard is not None:
            counters["deferred"] += remaining
            status, reason = "skipped", guard
            break
        if planned > counters["budget_left"]:
            counters["deferred"] += remaining
            status, reason = ("skipped" if counters["items_claimed"] == 0 else "ok"), "rate_limited"
            break
        if planned and browser_items >= max_items:
            counters["deferred"] += remaining
            break
        if not store.claim_review_item(int(item["id"]), sqlite_utc(clock() - WINDOW_7D_S)):
            counters["items_claim_lost"] += 1
            items.append({"id": int(item["id"]), "outcome": "claim_lost", "dm": "-", "reply": "-"})
            continue
        counters["items_claimed"] += 1
        if planned:
            browser_items += 1
        try:
            entry = run.process(item)
        except _Stop as stop:
            entry = getattr(stop, "entry", None)
            released_code = stop.code
            status, reason, code = "failed", stop.code, _STOP_CODES[stop.code]
            counters["deferred"] += remaining - 1
        except ReplyTerminated:
            status, reason, code, error_class = "failed", "internal", EXIT_INTERNAL, "ReplyTerminated"
            entry = None
        except Exception as exc:  # noqa: BLE001 - class name only; the item stays `sending` (manual replay)
            status, reason, code, error_class = "failed", "internal", EXIT_INTERNAL, type(exc).__name__
            entry = None
        if entry is not None:
            items.append(entry)
            outcome = entry["outcome"]
            if outcome == "posted":
                counters["items_posted"] += 1
            elif outcome == "already_present":
                counters["items_already_present"] += 1
            elif outcome == "released":
                counters["items_released"] += 1
            elif outcome == "claim_lost":
                counters["items_claim_lost"] += 1
            elif outcome in FAILED_OUTCOMES:
                counters["items_failed"] += 1
        if code:
            break

    counters["pending_after"] = int(store.review_status_counts().get("pending_review", 0))
    captcha_counters()
    return ReplyResult(status, reason, code, counters, items, error_class, released_code if code else None, True,
                       budget_at_start)


# --- output ---------------------------------------------------------------------------------------------------------


def emit_lines(result: ReplyResult) -> list[str]:
    c = result.counters
    budget = f"{c['budget_left']}/{c['budget_limit']}"
    lines = []
    if result.started:
        lines.append(
            f"tiktok-reply: start pending={c['queue_pending']} expirable={c['items_expired']}"
            f" legacy_backlog={c['items_legacy_backlog']} budget={result.budget_at_start}/{c['budget_limit']} owner=ok"
        )
    for i in result.items:
        page = f" page={i['page']}" if i.get("page") in PAGE_UNUSABLE_CODES else ""
        lines.append(f"item {i['id']}: outcome={i['outcome']} dm={i['dm']} reply={i['reply']}{page}")
    if result.reason == "rate_limited" and c["deferred"]:
        lines.append(f"queue: deferred rate_limited n={c['deferred']}")
    if result.released_code is not None and c["items_released"]:
        lines.append(f"queue: released {result.released_code} n={c['items_released']}")
    if result.error_class is not None:
        lines.append(f"tiktok-reply: error=internal class={result.error_class}")
    lines.append(
        f"tiktok-reply: status={result.status} reason={result.reason or '-'} sent={c['actions_sent']}"
        f" expired={c['items_expired']} legacy={c['items_legacy_backlog']} failed={c['items_failed']}"
        f" deferred={c['deferred']} budget={budget}"
    )
    return lines


def console_result_line(result: ReplyResult, report: CaptchaReport) -> str:
    # On exit 21-25 the status of this line carries no meaning (the exit code wins, ADR 011); it still carries the
    # captchas met (ADR 052 §2). parseConsoleResult only accepts ok | empty | skipped.
    payload: dict[str, Any] = {
        "status": "skipped" if result.status == "skipped" else "ok",
        "reason": result.reason,
        "counters": dict(result.counters),
        "items": [{key: i[key] for key in ITEM_KEYS} for i in result.items[:ITEM_LIMIT]],
    }
    if report.captchas:
        payload["captchas"] = report.to_json()
    return "CONSOLE_RESULT " + json.dumps(payload, separators=(",", ":"))


# --- entry point (H7) -----------------------------------------------------------------------------------------------


class _ConfigInvalid(Exception):
    pass


def read_limit(environ: Mapping[str, str]) -> int:
    raw = (environ.get("TIKTOK_REPLY_MAX_PER_HOUR") or "").strip()
    if not raw:
        return DEFAULT_MAX_PER_HOUR
    if not re.fullmatch(r"\d{1,2}", raw) or not 1 <= int(raw) <= 60:
        raise _ConfigInvalid()
    return int(raw)


def read_owner(environ: Mapping[str, str]) -> str | None:
    raw = (environ.get("TIKTOK_OWNER_HANDLE") or "").strip()
    return raw if OWNER_PATTERN.fullmatch(raw) else None


def read_max_items(value: str | None) -> int:
    if value is None:
        return ITEM_LIMIT
    if not re.fullmatch(r"\d", value.strip()) or not 1 <= int(value) <= ITEM_LIMIT:
        raise _ConfigInvalid()
    return int(value)


def _zero_payload(report: CaptchaReport, reason: str) -> str:
    counters = {name: 0 for name in COUNTERS}
    counters["captchas"] = len(report.captchas)
    counters["captchas_solved"] = sum(1 for c in report.captchas if c.get("outcome") == "solved")
    counters["captcha_tries"] = sum(len(c.get("attempts") or []) for c in report.captchas)
    payload: dict[str, Any] = {"status": "ok", "reason": reason, "counters": counters, "items": []}
    if report.captchas:
        payload["captchas"] = report.to_json()
    return "CONSOLE_RESULT " + json.dumps(payload, separators=(",", ":"))


def _on_sigterm(signum: int, frame: Any) -> None:
    raise ReplyTerminated()


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ, out: TextIO = sys.stdout) -> int:
    def emit(line: str) -> None:
        out.write(line + "\n")
        out.flush()

    report = CaptchaReport()
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        try:
            parser = argparse.ArgumentParser(prog="python -m app.tiktok_reply", add_help=False)
            parser.add_argument("--check-only", action="store_true")
            parser.add_argument("--max-items", default=None)
            args, unknown = parser.parse_known_args(argv)
            if unknown:
                raise _ConfigInvalid()
            limit = read_limit(environ)
            max_items = read_max_items(args.max_items)
            owner = read_owner(environ)
            if owner is None and not args.check_only:
                raise _ConfigInvalid()
        except _ConfigInvalid:
            emit("tiktok-reply: error=config_invalid")
            emit(_zero_payload(report, "config_invalid"))
            return EXIT_CONFIG_INVALID

        store = TikTokBackofficeStore(Settings().tiktok_backoffice_db)
        api_key = environ.get("SADCAPTCHA_API_KEY") or ""

        if args.check_only:
            now = _clock()
            counts = store.reply_check_counts(sqlite_utc(now - WINDOW_7D_S))
            legacy = legacy_cron_active(jobs_json=LEGACY_JOBS_JSON, running=legacy_cron_running)
            left = max(0, limit - int(store.outbound_count_since(sqlite_utc(now - RATE_WINDOW_S))))
            legacy_code = {None: "none", "legacy_cron_enabled": "enabled", "legacy_cron_running": "running"}[legacy]
            emit(
                f"tiktok-reply: check pending={counts['pending']} expirable={counts['expirable']}"
                f" legacy_backlog={counts['legacy_backlog']} legacy={legacy_code} budget={left}/{limit}"
                f" owner={'ok' if owner else 'missing'} captcha_key={'ok' if api_key.strip() else 'missing'}"
                f" dm_text_campaigns={counts['dm_text_campaigns']}/{counts['active_campaigns']}"
            )
            emit("CONSOLE_RESULT " + json.dumps({"status": "skipped", "reason": "check_only", "counters": {}},
                                                separators=(",", ":")))
            return 0

        client = CamofoxClient.from_env(environ)
        page = TikTokReplyPage(client, owner=owner, sleep=_sleep)
        rng = random.Random()

        def solve(tab_id: str, *, deadline: float) -> str:
            return solve_captcha(
                client, tab_id, api_key=api_key, report=report, clock=_clock, sleep=_sleep, rng=rng, deadline=deadline
            )

        result = run_reply(
            store=store,
            page=page,
            legacy_active=functools.partial(legacy_cron_active, jobs_json=LEGACY_JOBS_JSON, running=legacy_cron_running),
            solve=solve,
            clock=_clock,
            limit=limit,
            owner=owner,
            max_items=max_items,
            report=report,
        )
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:  # noqa: BLE001 - class name only, never the message
        emit(f"tiktok-reply: error=internal class={type(exc).__name__}")
        for line in diagnostic_lines(report):
            emit(line)
        emit(_zero_payload(report, "internal"))
        return EXIT_INTERNAL
    finally:
        signal.signal(signal.SIGTERM, previous)

    for line in emit_lines(result):
        emit(line)
    for line in diagnostic_lines(report):  # s25 (console ADR 059): right before CONSOLE_RESULT, still the last line
        emit(line)
    emit(console_result_line(result, report))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
