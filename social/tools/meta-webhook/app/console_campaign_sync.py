"""Entry point of the hermes-console job `campaign-sync` (console ADR 040).

Run by scripts/console-campaign-sync.sh, inside hermes-social-hermes-1, under the runner's
`timeout -k 10 120` (console ADR 011/022). It runs the two kanban -> campaigns syncs protected by s13:

- Meta in process: `sync_kanban_campaigns_report(kanban_db, meta_db)` (no logic copied here);
- TikTok in a subprocess: `uv run --frozen --no-dev tiktok-backoffice --db <abs> sync-kanban-campaigns
  --kanban-db <abs>` in the tiktok-backoffice project, with its own console venv.

Each side is isolated: one failing never prevents the other. Exit codes (reserved for this job in the
console's global `jobs.reasons.exit_<n>`): 0 both ok (CONSOLE_RESULT last), 6 Meta failed, 7 TikTok
failed, 8 both failed, 9 kanban missing, 10 invalid configuration. This module writes nothing itself
and never prints card content, exception messages or the TikTok stderr: only paths, exception type
names and counters.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.kanban_sync import sync_kanban_campaigns_report

UV = "uv"
OUTER_BUDGET_SECONDS = 100
MIN_TIKTOK_TIMEOUT_SECONDS = 10

EXIT_OK = 0
EXIT_META_FAILED = 6
EXIT_TIKTOK_FAILED = 7
EXIT_BOTH_FAILED = 8
EXIT_KANBAN_MISSING = 9
EXIT_INVALID_CONFIG = 10

META_KEYS = ("meta_created", "meta_updated", "meta_skipped_console", "meta_disabled")
TIKTOK_KEYS = ("tiktok_created", "tiktok_updated", "tiktok_skipped_console")
TIKTOK_FIELDS = ("created", "updated", "skipped_console")
EXCEPTION_TYPE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception))\b")


@dataclass(frozen=True)
class SyncConfig:
    kanban_db: Path
    meta_db: Path
    tiktok_db: Path
    tiktok_project: Path
    tiktok_venv: Path


@dataclass
class SyncOutcome:
    lines: list[str] = field(default_factory=list)
    exit_code: int = EXIT_OK
    counters: dict[str, int] = field(default_factory=dict)


def _default_tiktok_timeout(started: float) -> float:
    """Leave room under the runner's outer `timeout 120` so the `tiktok: failed timeout` line survives."""
    elapsed = time.monotonic() - started
    return max(MIN_TIKTOK_TIMEOUT_SECONDS, OUTER_BUDGET_SECONDS - elapsed)


def _invalid_param(config: SyncConfig) -> str | None:
    for name in ("kanban_db", "meta_db", "tiktok_db", "tiktok_project", "tiktok_venv"):
        if not Path(getattr(config, name)).is_absolute():
            return name
    if not Path(config.meta_db).is_file():
        return "meta_db"
    if not Path(config.tiktok_db).is_file():
        return "tiktok_db"
    if not Path(config.tiktok_project).is_dir():
        return "tiktok_project"
    return None


def _run_meta(config: SyncConfig) -> tuple[str, dict[str, int] | None]:
    try:
        report = sync_kanban_campaigns_report(config.kanban_db, config.meta_db)
    except Exception as exc:  # noqa: BLE001 - isolated side: type name only, never the message
        return f"meta: failed {type(exc).__name__}", None
    counters = dict(zip(META_KEYS, (report.created, report.updated, report.skipped_console, report.disabled)))
    line = (
        f"meta: ok created={report.created} updated={report.updated} "
        f"skipped_console={report.skipped_console} disabled={report.disabled}"
    )
    return line, counters


def _parse_tiktok_stdout(stdout: str) -> dict[str, int] | None:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    values: dict[str, int] = {}
    for key in TIKTOK_FIELDS:
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        values[key] = value
    return values


def _exception_type(stderr: str) -> str | None:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return None
    match = EXCEPTION_TYPE.match(lines[-1])
    return match.group(1) if match else None


def _run_tiktok(config: SyncConfig, uv: str, timeout: float) -> tuple[str, dict[str, int] | None]:
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(config.tiktok_venv)
    proc = subprocess.Popen(
        [
            uv, "run", "--frozen", "--no-dev", "tiktok-backoffice",
            "--db", str(config.tiktok_db),
            "sync-kanban-campaigns",
            "--kanban-db", str(config.kanban_db),
        ],
        cwd=config.tiktok_project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        process_group=0,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return "tiktok: failed timeout", None

    if proc.returncode != 0:
        cause = _exception_type(stderr)
        return (f"tiktok: failed exit={proc.returncode} {cause}" if cause else f"tiktok: failed exit={proc.returncode}"), None
    values = _parse_tiktok_stdout(stdout)
    if values is None:
        return f"tiktok: failed exit={proc.returncode} invalid_output", None
    line = f"tiktok: ok created={values['created']} updated={values['updated']} skipped_console={values['skipped_console']}"
    return line, dict(zip(TIKTOK_KEYS, (values[key] for key in TIKTOK_FIELDS)))


def run_campaign_sync(config: SyncConfig, *, uv: str = UV, tiktok_timeout: float | None = None) -> SyncOutcome:
    started = time.monotonic()
    outcome = SyncOutcome()

    invalid = _invalid_param(config)
    if invalid is not None:
        outcome.lines.append(f"console: invalid_config {invalid}")
        outcome.exit_code = EXIT_INVALID_CONFIG
        return outcome

    outcome.lines.append(f"console: kanban_db={config.kanban_db} meta_db={config.meta_db} tiktok_db={config.tiktok_db}")
    if not Path(config.kanban_db).is_file():
        outcome.lines.append("console: kanban_missing")
        outcome.exit_code = EXIT_KANBAN_MISSING
        return outcome

    meta_line, meta_counters = _run_meta(config)
    outcome.lines.append(meta_line)
    timeout = tiktok_timeout if tiktok_timeout is not None else _default_tiktok_timeout(started)
    tiktok_line, tiktok_counters = _run_tiktok(config, uv, timeout)
    outcome.lines.append(tiktok_line)

    for side in (meta_counters, tiktok_counters):
        if side is not None:
            outcome.counters.update(side)
    if outcome.counters:
        outcome.lines.append("counters " + " ".join(f"{key}={value}" for key, value in outcome.counters.items()))

    if meta_counters is not None and tiktok_counters is not None:
        result = {"status": "ok", "counters": outcome.counters}
        outcome.lines.append("CONSOLE_RESULT " + json.dumps(result, separators=(",", ":")))
        outcome.exit_code = EXIT_OK
    elif meta_counters is None and tiktok_counters is None:
        outcome.exit_code = EXIT_BOTH_FAILED
    elif meta_counters is None:
        outcome.exit_code = EXIT_META_FAILED
    else:
        outcome.exit_code = EXIT_TIKTOK_FAILED
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hermes-console job campaign-sync: kanban -> Meta and TikTok campaigns.")
    parser.add_argument("--kanban-db", type=Path, required=True)
    parser.add_argument("--meta-db", type=Path, required=True)
    parser.add_argument("--tiktok-db", type=Path, required=True)
    parser.add_argument("--tiktok-project", type=Path, required=True)
    parser.add_argument("--tiktok-venv", type=Path, required=True)
    args = parser.parse_args(argv)

    config = SyncConfig(
        kanban_db=args.kanban_db,
        meta_db=args.meta_db,
        tiktok_db=args.tiktok_db,
        tiktok_project=args.tiktok_project,
        tiktok_venv=args.tiktok_venv,
    )
    outcome = run_campaign_sync(config, uv=UV)
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
