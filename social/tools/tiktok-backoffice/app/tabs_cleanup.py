"""Close the orphan tabs of the legacy CLI comment reader (s26, console ADR 060, D7/P6).

`python -m app.tabs_cleanup` lists the Camofox tabs of `CAMOFOX_USER_ID` and counts those whose `listItemId` (the
`sessionKey` of `POST /tabs`) is the legacy CLI session key; `--apply` closes them (`DELETE /tabs/:id`). Never
another key: not `console-*`, not the noVNC tab, not a tab without key. `--apply` while the legacy cron runs closes
nothing (`mode=skipped reason=legacy_running`). One line of counts and codes only: never an URL, a title or a tab id.
Exit 0 (skipped included), 1 when Camofox is unreachable or its answer unreadable.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, TextIO

from app import tiktok_scan
from app.camofox_reader import CamofoxClient, CamofoxError

SESSION_KEY = "tiktok-comments-fetch"
DELETE_TIMEOUT_S = 10
LIST_TIMEOUT_S = 10


@dataclass(frozen=True)
class CleanupResult:
    mode: str  # dry_run | apply | skipped
    total: int = 0
    matched: int = 0
    closed: int = 0
    failed: int = 0
    reason: str | None = None  # None | legacy_running | camofox_unreachable

    def line(self) -> str:
        return (
            f"tabs-cleanup: mode={self.mode} total={self.total} matched={self.matched} closed={self.closed}"
            f" failed={self.failed} reason={self.reason or '-'}"
        )


def run_cleanup(client: Any, *, apply: bool, legacy_running: Callable[[], bool]) -> CleanupResult:
    mode = "apply" if apply else "dry_run"
    try:
        tabs = client.list_tabs(timeout=LIST_TIMEOUT_S)
    except (CamofoxError, ValueError, OSError):
        return CleanupResult(mode, reason="camofox_unreachable")
    matched = [tab["tabId"] for tab in tabs if tab.get("listItemId") == SESSION_KEY]
    if not apply:
        return CleanupResult(mode, total=len(tabs), matched=len(matched))
    if legacy_running():
        return CleanupResult("skipped", total=len(tabs), matched=len(matched), reason="legacy_running")
    closed = failed = 0
    for tab_id in matched:
        try:
            client.delete_tab(tab_id, timeout=DELETE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - counted, never printed
            failed += 1
        else:
            closed += 1
    return CleanupResult(mode, total=len(tabs), matched=len(matched), closed=closed, failed=failed)


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tabs_cleanup")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    client = CamofoxClient.from_env(environ)
    # Resolved at call time (tests monkeypatch tiktok_scan.legacy_cron_running).
    result = run_cleanup(client, apply=args.apply, legacy_running=lambda: tiktok_scan.legacy_cron_running())
    out.write(result.line() + "\n")
    out.flush()
    return 1 if result.reason == "camofox_unreachable" else 0


if __name__ == "__main__":
    sys.exit(main())
