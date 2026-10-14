"""TikTok comment scan, run by the hermes-console job `tiktok-scan` (s23, console ADR 054).

It replaces the loop of the Hermes cron `bf85146a1ae8` (scripts/tiktok_cron_auto_dm.py --apply), not its bricks:

1. guard: the legacy cron is running (`/proc`) => `skipped`/`legacy_cron_running`, nothing else is done;
2. `review_status_counts()`, then `poll_targets(8)` (least recently attempted first);
3. per video: stop if the scan budget is spent, or if the legacy cron is due (`/opt/data/cron/jobs.json`, read only);
   refuse to open a tab when Camofox already holds MAX_TABS; fetch the comments in process (a captcha goes to the
   s22 solver in the current tab, the page is never loaded again), ingest them, and date the attempt whatever its result;
4. unless the scan stopped (captcha unsolved, session lost, tabs saturated, legacy cron due): the 3 oldest pending
   review items go, one at a time, to the versioned processor scripts/process_tiktok_review_one.py, called like the
   cron calls it (same interpreter `python3` of the hermes container, same argument, 600 s). An item only starts
   with a full processor window before the run budget, and never when the legacy cron is due;
5. `review_status_counts()` again.

Outcomes (ADR 011 contract; the exit code wins over the CONSOLE_RESULT line, which is always printed last):

- exit 0: `ok` | `skipped` + legacy_cron_running | legacy_cron_due | no_targets (+ check_only for `--check-only`);
  the console turns `ok` into `empty`/no_comments_extracted after 4 decisive runs at comments_extracted = 0;
- exit 17 no attempted video was read, 18 captcha unsolved during the scan, 19 internal error (class name only),
  20 TikTok session lost during the scan.

Hygiene: the output holds numeric ids, counters and codes from closed lists only. Never a comment author or text,
an URL, a fetch `detail`/`diagnostics`, the processor stdout/stderr, nor any field of jobs.json. The SadCaptcha key
is read from the environment and never printed.

s24 removes the `process_queue` step (the job `tiktok-reply` takes over the queue).
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from app.camofox_reader import CamofoxClient, CamofoxError, fetch_comments_from_camofox
from app.captcha_solver import CaptchaReport, solve_captcha
from app.config import Settings
from app.store import TikTokBackofficeStore

TARGET_LIMIT = 8
REVIEW_LIMIT = 3
VIDEO_DEADLINE_S = 240.0
PROCESSOR_TIMEOUT_S = 600
SOLVER_BUDGET_S = 240.0  # per run, suspended from the video deadline (s22 login pattern)
MAX_CAPTCHAS_PER_RUN = 3
MAX_TABS = 9
SESSION_KEY = "console-tiktok-scan"
LIST_TABS_TIMEOUT_S = 10

LEGACY_MARKER = "tiktok_cron_auto_dm"
LEGACY_CRON_ID = "bf85146a1ae8"
LEGACY_JOBS_JSON = "/opt/data/cron/jobs.json"
# A video may last its deadline + the run's solver budget + one step in flight: 600 s (orchestrator amendment, D1 b).
LEGACY_VIDEO_BOUND_S = VIDEO_DEADLINE_S + SOLVER_BUDGET_S + 120
LEGACY_ITEM_BOUND_S = PROCESSOR_TIMEOUT_S + 60

PROJECT_DIR = Path(__file__).resolve().parents[1]
PROCESSOR = PROJECT_DIR / "scripts" / "process_tiktok_review_one.py"
PROCESSOR_SHA256 = "08e5f29d0c87bd154588df5e603d0a5132814d3a420f03e742e39d9aa2892f93"

EXIT_SCAN_FAILED = 17
EXIT_CAPTCHA_UNSOLVED = 18
EXIT_INTERNAL = 19
EXIT_SESSION_LOST = 20

RUN_BUDGET_S = 4500.0  # registry timeoutSec 4800 - 300 s (uv, flock, counts, output)
SCAN_BUDGET_S = 2160.0  # no new video after this; scan ends by 2160 + 342 + 240 = 2742 s at worst
ITEM_START_MARGIN_S = 120

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
_STDOUT_STAGES = frozenset(
    {"guards_failed", "ignored_already_replied", "dm_not_verified", "ignored_already_dm_sent", "processed"}
)
_STDERR_STAGES = frozenset({"review_not_found", "campaign_title_unknown", "kanban_campaign_not_found", "dm_text_not_found"})
PROCESSOR_STAGES = (
    _STDOUT_STAGES
    | _STDERR_STAGES
    | {"processor_timeout", "processor_bad_json", "processor_missing", "processor_error"}
)

COUNTERS = (
    "videos_targeted",
    "videos_ok",
    "videos_failed",
    "comments_extracted",
    "comments_new",
    "reviews_created",
    "items_selected",
    "items_ok",
    "items_failed",
    "queue_skipped",
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
_runner: Callable[..., Any] = subprocess.run  # replaced by tests: the processor never runs there


@dataclass
class ScanResult:
    status: str
    reason: str | None
    exit_code: int
    counters: dict[str, int]
    videos: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    queue_skip_reason: str | None = None


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


# --- processor (P7) -------------------------------------------------------------------------------------------------


def _stage_from_stderr(stderr: str) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return "processor_error"
    match = re.match(r"^([a-z_]+):", lines[-1])
    if match and match.group(1) in _STDERR_STAGES:
        return match.group(1)  # the code only, never what follows (a slug, a title)
    return "processor_error"


def run_processor(review_id: int, *, python: str | None, runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Run the versioned processor on one review item, as the cron does. Keeps `stage` and `ok` only (P4)."""
    if not python or not PROCESSOR.exists():
        return {"id": review_id, "rc": None, "stage": "processor_missing", "ok": False}
    try:
        # No start_new_session: the processor stays in the exec's process group, so the runner's timeout kills it.
        p = runner(
            [python, str(PROCESSOR), str(review_id)],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=PROCESSOR_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"id": review_id, "rc": None, "stage": "processor_timeout", "ok": False}
    except OSError:
        return {"id": review_id, "rc": None, "stage": "processor_error", "ok": False}
    rc = int(p.returncode)
    try:
        parsed = json.loads(p.stdout or "")
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        stage = parsed.get("stage")
        stage = stage if isinstance(stage, str) and stage in _STDOUT_STAGES else "unknown"
        return {"id": review_id, "rc": rc, "stage": stage, "ok": rc == 0 and parsed.get("ok") is True}
    stage = _stage_from_stderr(p.stderr) if rc != 0 else "processor_bad_json"
    return {"id": review_id, "rc": rc, "stage": stage, "ok": False}


# --- scan (H4) ------------------------------------------------------------------------------------------------------


def _video_id(target: Mapping[str, Any]) -> str:
    raw = str(target.get("video_id") or "")
    if re.fullmatch(r"\d{1,32}", raw):
        return raw
    match = re.search(r"/video/(\d{1,32})", str(target.get("video_url") or ""))
    return match.group(1) if match else "?"


def _video_error(code: Any) -> str:
    return code if isinstance(code, str) and code in VIDEO_ERRORS else "unknown"


def run_scan(
    *,
    store: Any,
    fetch: Callable[..., dict[str, Any]],
    tabs_count: Callable[[], int],
    process_one: Callable[[int], dict[str, Any]],
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
    items: list[dict[str, Any]] = []

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
        entry: dict[str, Any] = {"id": _video_id(target), "extracted": 0, "new": 0, "reviews": 0, "error": None}
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
            if result.get("ok"):
                comments = result.get("comments") or []
                entry["extracted"] = len(comments)
                try:
                    ingest = store.ingest_comments(video_url=url, comments=comments)
                except Exception:  # noqa: BLE001 - the code only, never the message
                    entry["error"] = "ingest_failed"
                else:
                    entry["new"] = int(ingest.get("ingested") or 0)
                    entry["reviews"] = int(ingest.get("created_reviews") or 0)
            else:
                entry["error"] = _video_error(result.get("error"))
        store.mark_scan_attempt(url)
        if entry["error"] is None:
            counters["videos_ok"] += 1
            counters["comments_extracted"] += entry["extracted"]
            counters["comments_new"] += entry["new"]
            counters["reviews_created"] += entry["reviews"]
        else:
            counters["videos_failed"] += 1
        if entry["error"] == "captcha_unsolved":
            stop = "captcha_unsolved"
        elif entry["error"] == "tiktok_page_not_actionable":
            stop = "session_lost"
        if stop is not None:
            counters["videos_skipped"] += len(targets) - index - 1
            break

    queue_skip_reason: str | None = None
    if stop is None:
        pending = store.pending_review_items(REVIEW_LIMIT)
        counters["items_selected"] = len(pending)
        for index, item in enumerate(pending):
            if RUN_BUDGET_S - (clock() - t0) < PROCESSOR_TIMEOUT_S + ITEM_START_MARGIN_S:
                queue_skip_reason = "budget"
            elif legacy_imminent(LEGACY_ITEM_BOUND_S):
                queue_skip_reason = "legacy_cron_due"
            if queue_skip_reason is not None:
                counters["queue_skipped"] = len(pending) - index
                break
            outcome = process_one(int(item["id"]))
            items.append(outcome)
            counters["items_ok" if outcome.get("ok") else "items_failed"] += 1
    else:
        counters["queue_skipped"] = 1
        if stop == "legacy_cron_due":
            queue_skip_reason = "legacy_cron_due"

    counters["pending_after"] = int(store.review_status_counts().get("pending_review", 0))
    counters["captchas"] = len(report.captchas)
    counters["captchas_solved"] = sum(1 for c in report.captchas if c.get("outcome") == "solved")
    counters["captcha_tries"] = sum(len(c.get("attempts") or []) for c in report.captchas)

    incomplete = counters["videos_skipped"] > 0 or counters["queue_skipped"] > 0
    if stop == "captcha_unsolved":
        status, reason, code = "failed", _ERROR_REASONS[EXIT_CAPTCHA_UNSOLVED], EXIT_CAPTCHA_UNSOLVED
    elif stop == "session_lost":
        status, reason, code = "failed", _ERROR_REASONS[EXIT_SESSION_LOST], EXIT_SESSION_LOST
    elif queue_skip_reason == "legacy_cron_due" and incomplete:
        status, reason, code = "skipped", "legacy_cron_due", 0
    elif not targets:
        status, reason, code = "skipped", "no_targets", 0
    elif counters["videos_ok"] == 0:
        status, reason, code = "failed", _ERROR_REASONS[EXIT_SCAN_FAILED], EXIT_SCAN_FAILED
    else:
        status, reason, code = "ok", None, 0
    return ScanResult(status, reason, code, counters, videos, items, queue_skip_reason)


def emit_lines(result: ScanResult) -> list[str]:
    lines = []
    for v in result.videos:
        lines.append(
            f"video {v['id']}: extracted={v['extracted']} new={v['new']} reviews={v['reviews']} error={v['error'] or '-'}"
        )
    for i in result.items:
        rc = "-" if i.get("rc") is None else i["rc"]
        lines.append(f"item {i['id']}: rc={rc} stage={i['stage']} ok={1 if i.get('ok') else 0}")
    if result.queue_skip_reason in ("legacy_cron_due", "budget") and result.counters.get("queue_skipped"):
        lines.append(f"queue: skipped {result.queue_skip_reason} n={result.counters['queue_skipped']}")
    c = result.counters
    lines.append(
        f"tiktok-scan: status={result.status} reason={result.reason or '-'}"
        f" videos={c['videos_ok']}/{c['videos_targeted']}"
        f" comments={c['comments_extracted']} new={c['comments_new']}"
        f" items={c['items_ok']}/{c['items_selected']}"
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
        "items": [dict(i) for i in result.items[:REVIEW_LIMIT]],
    }
    if report.captchas:
        payload["captchas"] = report.to_json()
    return "CONSOLE_RESULT " + json.dumps(payload, separators=(",", ":"))


# --- entry point (H5) -----------------------------------------------------------------------------------------------


def _processor_python(environ: Mapping[str, str]) -> str | None:
    # The wrapper resolves `python3` of the hermes container before `uv run` (D3). Only an absolute path is trusted:
    # no fallback, otherwise the console venv's Python would run the processor.
    value = (environ.get("SCAN_PROCESSOR_PYTHON") or "").strip()
    return value if os.path.isabs(value) else None


def _processor_digest() -> str:
    try:
        data = PROCESSOR.read_bytes()
    except OSError:
        return "missing"
    return "ok" if hashlib.sha256(data).hexdigest() == PROCESSOR_SHA256 else "mismatch"


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
        python = _processor_python(environ)
        imminent = functools.partial(legacy_cron_imminent, now=_clock, jobs_json=LEGACY_JOBS_JSON)

        if args.check_only:
            targets = len([t for t in store.poll_targets(limit=TARGET_LIMIT) if t.get("video_url")])
            pending = int(store.review_status_counts().get("pending_review", 0))
            running = 1 if legacy_cron_running() else 0
            due = 1 if imminent(LEGACY_VIDEO_BOUND_S, running=legacy_cron_running) else 0
            emit(
                f"tiktok-scan: check targets={targets} pending={pending} legacy_running={running} legacy_due={due}"
                f" processor_sha256={_processor_digest()} processor_python={python or 'missing'}"
            )
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
            process_one=functools.partial(run_processor, python=python, runner=_runner),
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
        counters = {name: 0 for name in COUNTERS}
        counters.update(_captcha_counters(report))
        payload: dict[str, Any] = {"status": "ok", "reason": "internal", "counters": counters, "videos": [], "items": []}
        if report.captchas:
            payload["captchas"] = report.to_json()
        emit("CONSOLE_RESULT " + json.dumps(payload, separators=(",", ":")))
        return EXIT_INTERNAL

    for line in emit_lines(result):
        emit(line)
    emit(console_result_line(result, report))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
