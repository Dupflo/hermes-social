"""TikTok comment scan, run by the hermes-console job `tiktok-scan` (s23, console ADR 054; s24, console ADR 055).

It replaces the scan loop of the Hermes cron `bf85146a1ae8` (scripts/tiktok_cron_auto_dm.py --apply), not its bricks:

1. guard: the legacy cron is running (`/proc`) => `skipped`/`legacy_cron_running`, nothing else is done;
2. `review_status_counts()`, then `poll_targets(8)` (least recently attempted first);
3. per video: stop if the scan budget is spent, or if the legacy cron is due (`/opt/data/cron/jobs.json`, read only);
   refuse to open a tab when Camofox already holds MAX_TABS; fetch the comments in process (a captcha goes to the
   s22 solver in the current tab, the page is never loaded again), ingest them as a console read, and date the
   attempt whatever its result. The first successful console read of a never-read video is a baseline: its review
   items are created closed (`expired` / `first_scan_baseline`), since TikTok dates are lost at extraction;
4. `review_status_counts()` again.

s24 a retiré l'étape `process_queue` (console ADR 055): the job `tiktok-reply` alone works the review queue.

Outcomes (ADR 011 contract; the exit code wins over the CONSOLE_RESULT line, which is always printed last):

- exit 0: `ok` | `skipped` + legacy_cron_running | legacy_cron_due | no_targets (+ check_only for `--check-only`);
  the console turns `ok` into `empty`/no_comments_extracted after 4 decisive runs at comments_extracted = 0;
- exit 17 no attempted video was read, 18 captcha unsolved during the scan, 19 internal error (class name only),
  20 TikTok session lost during the scan.

Hygiene: the output holds numeric ids, counters and codes from closed lists only. Never a comment author or text,
an URL, a fetch `detail`/`diagnostics`, nor any field of jobs.json. The SadCaptcha key is read from the environment
and never printed.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, TextIO

from app.camofox_reader import CamofoxClient, CamofoxError, fetch_comments_from_camofox
from app.captcha_solver import CaptchaReport, diagnostic_lines, solve_captcha
from app.config import Settings
from app.store import TikTokBackofficeStore

TARGET_LIMIT = 8
VIDEO_DEADLINE_S = 240.0
SOLVER_BUDGET_S = 240.0  # per run, suspended from the video deadline (s22 login pattern)
MAX_CAPTCHAS_PER_RUN = 3
MAX_TABS = 9
SESSION_KEY = "console-tiktok-scan"
LIST_TABS_TIMEOUT_S = 10

LEGACY_MARKER = "tiktok_cron_auto_dm"
LEGACY_CRON_ID = "bf85146a1ae8"
LEGACY_JOBS_JSON = "/opt/data/cron/jobs.json"
# Worst case of one video (review n°1 m1): its 240 s deadline + one try in flight (~92 s) + list_tabs (10 s) +
# PRESENT_JS probe (30 s) + delete_tab (10 s) = ~382 s, plus the run's 240 s of solver = ~622 s, bounded by
# 240 + 240 + 220 = 700 s. s25 (console ADR 059, s23 n1): the solver hands back by its deadline (every step only
# starts with its own bound left), so there is no solver overrun term any more; 700 s is kept as the margin.
# s26 (console ADR 060, P5): the reader's fixed pauses became guard waits bounded by a monotonic budget (page 20 s,
# panel 8 s), each poll checking `expired()` and its budget first and lasting at most 10 s + a 1 s pause. Segments
# without an `expired()` check stay under the try in flight: page wait end + activation 41 s, mid-budget
# re-activation 41 s, panel wait end + extraction + final PRESENT_JS probe 71 s, re-navigation 38 s, so <= 92 s
# (+ the head PRESENT_JS probe, 30 s, counted apart) — measured by tests/test_scan_fetch.py; 700 s is kept.
# s28 (console ADR 062, T4): the activation became a passive panelState() (10 s) and trusted clicks (click_selector
# 10 s, else mouse_available 5 s + mouse 10 s), `expired()` checked before every evaluation, click and pause (1.5 s);
# the panel wait is 12 s with an unconditional second activation at mid-budget. Longest segment of the activation
# without a check: 15 s (plugin probe + mouse), under the former 30 s activate. Measured worst gap after expiry:
# 39 s (connected), 59 s (logged out), 67 s (drift), 30 s (closed panel, every click in 422 + mouse fallback), so a
# try in flight stays <= 92 s (+ 30 s head probe) <= 170 s = 220 - (list_tabs 10 + probe 30 + delete_tab 10).
LEGACY_VIDEO_BOUND_S = VIDEO_DEADLINE_S + SOLVER_BUDGET_S + 220

EXIT_SCAN_FAILED = 17
EXIT_CAPTCHA_UNSOLVED = 18
EXIT_INTERNAL = 19
EXIT_SESSION_LOST = 20

SCAN_BUDGET_S = 2160.0  # no new video after this; scan ends by 2160 + 342 + 240 = 2742 s at worst

VIDEO_ERRORS = frozenset(
    {
        "camofox_unreachable",
        "camofox_fetch_failed",
        "unexpected_camofox_result",
        "target_video_incoherent",
        "captcha_required",
        "tiktok_page_not_actionable",
        "comments_tab_not_active",
        "right_panel_recommendations",
        "comments_expected_but_not_extracted",
        "captcha_unsolved",
        "video_timeout",
        "tabs_full",
        "ingest_failed",
    }
)
# s26 (console ADR 060, D4/P4): sub-code of the reader's structural guard, closed list (hygiene ADR 054).
GUARD_STATES = ("ok", "ok_retry", "captcha_late", "login_gate", "no_signal")
# s29 (console ADR 063, D2): reason of a `target_video_incoherent` video and author check of an ok one, closed lists.
INCOHERENT_REASONS = ("video_id_missing", "loaded_different_video", "author_link_mismatch")
AUTHOR_CHECKS = ("match", "id_only")
COUNTERS = (
    "videos_targeted",
    "videos_ok",
    "videos_failed",
    "comments_extracted",
    "comments_new",
    "reviews_created",
    "reviews_baseline",
    "videos_skipped",
    "pending_after",
    "captchas",
    "captchas_solved",
    "captcha_tries",
)

_ERROR_REASONS = {
    EXIT_SCAN_FAILED: "scan_failed",
    EXIT_CAPTCHA_UNSOLVED: "captcha_unsolved",
    EXIT_INTERNAL: "internal",
    EXIT_SESSION_LOST: "session_lost",
}

_clock: Callable[[], float] = time.time
_sleep: Callable[[float], None] = time.sleep


@dataclass
class ScanResult:
    status: str
    reason: str | None
    exit_code: int
    counters: dict[str, int]
    videos: list[dict[str, Any]] = field(default_factory=list)


# --- legacy cron guards (D1 b) --------------------------------------------------------------------------------------


def legacy_cron_running(proc_root: str = "/proc", self_pid: int | None = None) -> bool:
    """True when a process whose command line names the legacy cron (script or wrapper) is alive."""
    own = os.getpid() if self_pid is None else self_pid
    marker = LEGACY_MARKER.encode()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit() or int(entry) == own:
            continue
        try:
            with open(os.path.join(proc_root, entry, "cmdline"), "rb") as fh:
                cmdline = fh.read()
        except OSError:
            continue
        if marker in cmdline:
            return True
    return False


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def legacy_cron_imminent(
    bound_s: float,
    *,
    now: Callable[[], float],
    jobs_json: str = LEGACY_JOBS_JSON,
    running: Callable[[], bool] = legacy_cron_running,
) -> bool:
    """True when the legacy cron runs, or may start within `bound_s` (read-only look at its own jobs.json entry).

    Only `enabled`, `state` and `next_run_at` of the job LEGACY_CRON_ID are read; nothing of the file is printed.
    Unreadable or unparsable file, or an enabled job without a readable `next_run_at` => imminent (fail closed).
    """
    if running():
        return True
    try:
        with open(jobs_json, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return True
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return True
    job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == LEGACY_CRON_ID), None)
    if job is None:
        return False
    if not job.get("enabled") or job.get("state") == "paused":
        return False
    next_run = _epoch(job.get("next_run_at"))
    if next_run is None:
        return True
    return next_run - now() < bound_s


# --- scan (H4) ------------------------------------------------------------------------------------------------------


def _video_id(target: Mapping[str, Any]) -> str:
    raw = str(target.get("video_id") or "")
    if re.fullmatch(r"\d{1,32}", raw):
        return raw
    match = re.search(r"/video/(\d{1,32})", str(target.get("video_url") or ""))
    return match.group(1) if match else "?"


def _video_error(code: Any) -> str:
    return code if isinstance(code, str) and code in VIDEO_ERRORS else "unknown"


def _guard(code: Any) -> str:
    if code is None:
        return "-"
    return code if isinstance(code, str) and code in GUARD_STATES else "unknown"


def _closed(code: Any, allowed: tuple[str, ...]) -> str | None:
    """None => no field; outside the closed list => `unknown`, never the value received."""
    if code is None:
        return None
    return code if isinstance(code, str) and code in allowed else "unknown"


def run_scan(
    *,
    store: Any,
    fetch: Callable[..., dict[str, Any]],
    tabs_count: Callable[[], int],
    legacy_running: Callable[[], bool],
    legacy_imminent: Callable[[float], bool],
    solve: Callable[..., str],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    report: CaptchaReport | None = None,
) -> ScanResult:
    report = report if report is not None else CaptchaReport()
    t0 = clock()
    counters = {name: 0 for name in COUNTERS}
    videos: list[dict[str, Any]] = []

    if legacy_running():
        return ScanResult("skipped", "legacy_cron_running", 0, counters)

    store.review_status_counts()
    targets = [t for t in store.poll_targets(limit=TARGET_LIMIT) if t.get("video_url")]
    counters["videos_targeted"] = len(targets)

    stop: str | None = None  # captcha_unsolved | session_lost | tabs_full | legacy_cron_due
    solver_used = 0.0

    for index, target in enumerate(targets):
        if clock() - t0 > SCAN_BUDGET_S:
            counters["videos_skipped"] += len(targets) - index
            break
        if legacy_imminent(LEGACY_VIDEO_BOUND_S):
            counters["videos_skipped"] += len(targets) - index
            stop = "legacy_cron_due"
            break
        url = target["video_url"]
        entry: dict[str, Any] = {
            "id": _video_id(target), "extracted": 0, "new": 0, "reviews": 0, "error": None, "baseline": 0,
            "guard": "-",
        }
        videos.append(entry)
        try:
            open_tabs = tabs_count()
        except (CamofoxError, ValueError, OSError):
            open_tabs = None
            entry["error"] = "camofox_unreachable"
        if open_tabs is not None and open_tabs >= MAX_TABS:
            entry["error"] = "tabs_full"
            counters["videos_failed"] += 1
            counters["videos_skipped"] += len(targets) - index - 1
            stop = "tabs_full"
            break
        if entry["error"] is None:
            started = clock()
            paused = 0.0

            def expired() -> bool:
                return clock() - started - paused > VIDEO_DEADLINE_S

            def on_captcha(tab_id: str) -> bool:
                nonlocal solver_used, paused
                if len(report.captchas) >= MAX_CAPTCHAS_PER_RUN:
                    # Refused without a solver call, still logged (ADR 052): no try made => `unavailable`, like the
                    # solver itself when its budget is spent before a first try.
                    report.captchas.append(
                        {"seq": len(report.captchas) + 1, "outcome": "unavailable", "at": int(clock() * 1000),
                         "attempts": []}
                    )
                    return False
                began = clock()
                try:
                    outcome = solve(tab_id, deadline=began + max(0.0, SOLVER_BUDGET_S - solver_used))
                finally:
                    spent = clock() - began
                    solver_used += spent
                    paused += spent
                return outcome == "solved"

            result = fetch(video_url=url, on_captcha=on_captcha, expired=expired, sleep=sleep)
            entry["guard"] = _guard(result.get("guard"))
            if result.get("ok"):
                comments = result.get("comments") or []
                entry["extracted"] = len(comments)
                try:
                    # s24 (P7, S1): the baseline is decided before the read is ingested, and dated by the
                    # ingestion's own transaction (a failed ingestion dates nothing).
                    baseline = bool(store.needs_baseline(url))
                    ingest = store.ingest_comments(video_url=url, comments=comments, console_read=True, baseline=baseline)
                except Exception:  # noqa: BLE001 - the code only, never the message
                    entry["error"] = "ingest_failed"
                else:
                    entry["new"] = int(ingest.get("ingested") or 0)
                    entry["reviews"] = int(ingest.get("created_reviews") or 0)
                    entry["baseline"] = 1 if baseline else 0
                    author_check = _closed(result.get("author_check"), AUTHOR_CHECKS)
                    if author_check is not None:
                        entry["author_check"] = author_check
            else:
                entry["error"] = _video_error(result.get("error"))
                if entry["error"] == "target_video_incoherent":
                    reason = _closed(result.get("incoherent_reason"), INCOHERENT_REASONS)
                    if reason is not None:
                        entry["incoherent_reason"] = reason
        store.mark_scan_attempt(url)
        if entry["error"] is None:
            counters["videos_ok"] += 1
            counters["comments_extracted"] += entry["extracted"]
            counters["comments_new"] += entry["new"]
            counters["reviews_created"] += entry["reviews"]
            if entry["baseline"]:
                counters["reviews_baseline"] += entry["reviews"]
        else:
            counters["videos_failed"] += 1
        if entry["error"] == "captcha_unsolved":
            stop = "captcha_unsolved"
        elif entry["error"] == "tiktok_page_not_actionable":
            stop = "session_lost"
        if stop is not None:
            counters["videos_skipped"] += len(targets) - index - 1
            break

    counters["pending_after"] = int(store.review_status_counts().get("pending_review", 0))
    counters["captchas"] = len(report.captchas)
    counters["captchas_solved"] = sum(1 for c in report.captchas if c.get("outcome") == "solved")
    counters["captcha_tries"] = sum(len(c.get("attempts") or []) for c in report.captchas)

    if stop == "captcha_unsolved":
        status, reason, code = "failed", _ERROR_REASONS[EXIT_CAPTCHA_UNSOLVED], EXIT_CAPTCHA_UNSOLVED
    elif stop == "session_lost":
        status, reason, code = "failed", _ERROR_REASONS[EXIT_SESSION_LOST], EXIT_SESSION_LOST
    elif stop == "legacy_cron_due":
        status, reason, code = "skipped", "legacy_cron_due", 0
    elif not targets:
        status, reason, code = "skipped", "no_targets", 0
    elif counters["videos_ok"] == 0:
        status, reason, code = "failed", _ERROR_REASONS[EXIT_SCAN_FAILED], EXIT_SCAN_FAILED
    else:
        status, reason, code = "ok", None, 0
    return ScanResult(status, reason, code, counters, videos)


def _coherence_field(v: dict[str, Any]) -> str:
    # s29 (D2): at most one optional last field; a line without it is unchanged.
    if v.get("error") == "target_video_incoherent" and v.get("incoherent_reason"):
        return f" incoherent_reason={v['incoherent_reason']}"
    if v.get("error") is None and v.get("author_check"):
        return f" author_check={v['author_check']}"
    return ""


def emit_lines(result: ScanResult) -> list[str]:
    lines = []
    for v in result.videos:
        lines.append(
            f"video {v['id']}: extracted={v['extracted']} new={v['new']} reviews={v['reviews']} error={v['error'] or '-'}"
            f" baseline={v.get('baseline', 0)} guard={v.get('guard', '-')}"
            + _coherence_field(v)
        )
    c = result.counters
    lines.append(
        f"tiktok-scan: status={result.status} reason={result.reason or '-'}"
        f" videos={c['videos_ok']}/{c['videos_targeted']}"
        f" comments={c['comments_extracted']} new={c['comments_new']}"
    )
    return lines


def console_result_line(result: ScanResult, report: CaptchaReport) -> str:
    # On exit 17-20 the status of this line carries no meaning (the exit code wins, ADR 011); it still carries the
    # captchas met (ADR 052 §2). parseConsoleResult only accepts ok | empty | skipped.
    payload: dict[str, Any] = {
        "status": "skipped" if result.status == "skipped" else "ok",
        "reason": result.reason,
        "counters": dict(result.counters),
        "videos": [dict(v) for v in result.videos[:TARGET_LIMIT]],
    }
    if report.captchas:
        payload["captchas"] = report.to_json()
    return "CONSOLE_RESULT " + json.dumps(payload, separators=(",", ":"))


# --- entry point (H5) -----------------------------------------------------------------------------------------------


def _captcha_counters(report: CaptchaReport) -> dict[str, int]:
    return {
        "captchas": len(report.captchas),
        "captchas_solved": sum(1 for c in report.captchas if c.get("outcome") == "solved"),
        "captcha_tries": sum(len(c.get("attempts") or []) for c in report.captchas),
    }


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ, out: TextIO = sys.stdout) -> int:
    def emit(line: str) -> None:
        out.write(line + "\n")
        out.flush()

    report = CaptchaReport()
    try:
        parser = argparse.ArgumentParser(prog="python -m app.tiktok_scan")
        parser.add_argument("--check-only", action="store_true")
        args = parser.parse_args(argv)
        store = TikTokBackofficeStore(Settings().tiktok_backoffice_db)
        imminent = functools.partial(legacy_cron_imminent, now=_clock, jobs_json=LEGACY_JOBS_JSON)

        if args.check_only:
            targets = len([t for t in store.poll_targets(limit=TARGET_LIMIT) if t.get("video_url")])
            pending = int(store.review_status_counts().get("pending_review", 0))
            running = 1 if legacy_cron_running() else 0
            due = 1 if imminent(LEGACY_VIDEO_BOUND_S, running=legacy_cron_running) else 0
            emit(f"tiktok-scan: check targets={targets} pending={pending} legacy_running={running} legacy_due={due}")
            emit("CONSOLE_RESULT " + json.dumps({"status": "skipped", "reason": "check_only", "counters": {}},
                                                separators=(",", ":")))
            return 0

        client = CamofoxClient.from_env(environ)
        api_key = environ.get("SADCAPTCHA_API_KEY") or ""
        rng = random.Random()

        def solve(tab_id: str, *, deadline: float) -> str:
            return solve_captcha(
                client, tab_id, api_key=api_key, report=report, clock=_clock, sleep=_sleep, rng=rng, deadline=deadline
            )

        result = run_scan(
            store=store,
            fetch=functools.partial(fetch_comments_from_camofox, client=client, session_key=SESSION_KEY, close_tab=True),
            tabs_count=lambda: len(client.list_tabs(timeout=LIST_TABS_TIMEOUT_S)),
            legacy_running=legacy_cron_running,
            legacy_imminent=functools.partial(imminent, running=legacy_cron_running),
            solve=solve,
            clock=_clock,
            sleep=_sleep,
            report=report,
        )
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:  # noqa: BLE001 - class name only, never the message
        emit(f"tiktok-scan: error=internal class={type(exc).__name__}")
        for line in diagnostic_lines(report):
            emit(line)
        counters = {name: 0 for name in COUNTERS}
        counters.update(_captcha_counters(report))
        payload: dict[str, Any] = {"status": "ok", "reason": "internal", "counters": counters, "videos": []}
        if report.captchas:
            payload["captchas"] = report.to_json()
        emit("CONSOLE_RESULT " + json.dumps(payload, separators=(",", ":")))
        return EXIT_INTERNAL

    for line in emit_lines(result):
        emit(line)
    for line in diagnostic_lines(report):  # s25 (console ADR 059): right before CONSOLE_RESULT, still the last line
        emit(line)
    emit(console_result_line(result, report))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
