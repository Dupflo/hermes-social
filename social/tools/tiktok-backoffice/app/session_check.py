"""TikTok session check, run every 10 min by the hermes-console job `tiktok-session-check` (s20).

Console ADR 049: the Camofox key is read from this process environment (CAMOFOX_API_KEY, loaded by the
hermes container from deploy/.env); the console injects nothing. The state is reported through the
ADR 011 contract:

- a determined state ends `ok` with reason connected | logged_out | captcha | camofox_down;
- a missing or refused key ends `skipped` with reason camofox_auth_missing | camofox_auth_rejected;
- an unexpected failure is an exit code, without CONSOLE_RESULT: 11 unexpected Camofox response
  (or deadline exceeded), 12 tabs saturated (429), 13 internal error.

Side effects are deliberately minimal: the check never navigates nor reloads a tab, only evaluates a
passive selector expression on the TikTok tabs already open, and opens one about:blank tab only when
the session has none (Camofox recycles the oldest tab at its limit), always closed in a finally.

Secret hygiene: the output holds state names and numbers only. Never a storage_state body, a cookie
value, the key, a response repr or an exception message (only the exception class name).

This module opens no database (no Settings, no store): the CLI entry point is `python -m app.session_check`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, TextIO
from urllib.parse import urlsplit

from app.camofox_reader import CamofoxClient, CamofoxHTTPError, CamofoxUnreachable

CAPTCHA_JS = (
    "!!(document.querySelector('.captcha-verify-container')"
    "||document.querySelector('#captcha-verify-image'))"
)
SESSION_KEY = "console-session-check"
SESSION_COOKIES = ("sessionid", "sid_tt")

DEADLINE_S = 90.0
STEP_TIMEOUT_S = 10.0
STORAGE_TIMEOUT_MAX_S = 60.0

EXIT_UNEXPECTED = 11
EXIT_TABS_FULL = 12
EXIT_INTERNAL = 13

_ERROR_NAMES = {EXIT_UNEXPECTED: "unexpected_response", EXIT_TABS_FULL: "tabs_full"}


@dataclass
class Result:
    status: str = "ok"
    reason: str = ""
    counters: dict[str, int] = field(default_factory=dict)
    exit_code: int = 0


class _Abort(Exception):
    """Internal control flow: stop the check with a given exit code."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(exit_code)
        self.exit_code = exit_code


class _Down(Exception):
    """Internal control flow: Camofox stopped answering after /health."""


def _is_tiktok_host(host: str | None) -> bool:
    host = (host or "").strip().lstrip(".").lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def is_tiktok_url(url: str) -> bool:
    try:
        host = urlsplit(str(url or "")).hostname
    except ValueError:
        return False
    return _is_tiktok_host(host)


def session_cookies_present(cookies: Iterable[Any], now_s: float) -> bool:
    """True when both sessionid and sid_tt are present on a tiktok.com domain, non-empty, not expired.

    The value is only tested for emptiness; it is never returned nor printed.
    """
    found: set[str] = set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        if name not in SESSION_COOKIES:
            continue
        if not _is_tiktok_host(cookie.get("domain")):
            continue
        value = cookie.get("value")
        if not isinstance(value, str) or value == "":
            continue
        expires = cookie.get("expires", -1)
        if not isinstance(expires, (int, float)) or isinstance(expires, bool):
            continue
        if expires != -1 and expires <= now_s:
            continue
        found.add(name)
    return found == set(SESSION_COOKIES)


def check(client: Any, *, now_s: float, clock: Callable[[], float] = time.monotonic) -> Result:
    started = clock()
    counters = {"tiktok_tabs": 0, "captcha_tabs": 0, "opened_tab": 0, "tabs_unreadable": 0, "tab_close_failed": 0}

    def remaining() -> float:
        return DEADLINE_S - (clock() - started)

    def step_timeout() -> float:
        left = remaining()
        if left <= 0:
            raise _Abort(EXIT_UNEXPECTED)
        return min(STEP_TIMEOUT_S, left)

    # 1. Camofox alive? Any failure here is camofox_down (503 while recovering included).
    try:
        client.health(timeout=step_timeout())
    except (CamofoxUnreachable, CamofoxHTTPError):
        return Result("ok", "camofox_down", dict(counters))
    except _Abort as abort:
        return Result(counters=dict(counters), exit_code=abort.exit_code)

    # 2. No key: degraded, explicit, without opening anything.
    if not getattr(client, "api_key", ""):
        return Result("skipped", "camofox_auth_missing", dict(counters))

    opened: str | None = None
    try:
        try:
            tabs = client.list_tabs(timeout=step_timeout())
        except CamofoxUnreachable:
            raise _Down() from None
        except (CamofoxHTTPError, ValueError):
            raise _Abort(EXIT_UNEXPECTED) from None

        # 3. Passive captcha detection, on the TikTok tabs already open only.
        for tab in tabs:
            if not is_tiktok_url(tab.get("url", "")):
                continue
            counters["tiktok_tabs"] += 1
            try:
                shown = client.evaluate(tab_id=tab["tabId"], expression=CAPTCHA_JS, timeout=step_timeout())
            except _Abort:
                raise
            except Exception:
                counters["tabs_unreadable"] += 1
                continue
            if shown is True:
                counters["captcha_tabs"] += 1

        def open_blank() -> str:
            try:
                tab_id = client.create_blank_tab(SESSION_KEY, timeout=step_timeout())
            except CamofoxUnreachable:
                raise _Down() from None
            except CamofoxHTTPError as exc:
                raise _Abort(EXIT_TABS_FULL if exc.status == 429 else EXIT_UNEXPECTED) from None
            except ValueError:
                raise _Abort(EXIT_UNEXPECTED) from None
            counters["opened_tab"] = 1
            return tab_id

        # 4. storage_state needs a live session: open about:blank only when the session has no tab.
        if not tabs:
            opened = open_blank()

        def read_state() -> dict[str, Any]:
            left = remaining()
            if left <= 0:
                raise _Abort(EXIT_UNEXPECTED)
            return client.storage_state(timeout=min(STORAGE_TIMEOUT_MAX_S, left))

        state: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                state = read_state()
                break
            except CamofoxUnreachable:
                raise _Down() from None
            except ValueError:
                raise _Abort(EXIT_UNEXPECTED) from None
            except CamofoxHTTPError as exc:
                if exc.status == 403:
                    return Result("skipped", "camofox_auth_rejected", counters)
                if exc.status == 404 and attempt == 0:
                    # The session expired between list_tabs and the read: it has no tab any more.
                    if opened is None:
                        opened = open_blank()
                    continue
                raise _Abort(EXIT_UNEXPECTED) from None
        if state is None:
            raise _Abort(EXIT_UNEXPECTED)
        cookies = state.get("cookies")
        if not isinstance(cookies, list):
            raise _Abort(EXIT_UNEXPECTED)
        present = session_cookies_present(cookies, now_s)
    except _Down:
        return Result("ok", "camofox_down", counters)
    except _Abort as abort:
        return Result(counters=counters, exit_code=abort.exit_code)
    finally:
        if opened is not None:
            try:
                client.delete_tab(opened, timeout=STEP_TIMEOUT_S)
            except Exception:
                counters["tab_close_failed"] = 1

    if counters["captcha_tabs"] > 0:
        reason = "captcha"
    elif present:
        reason = "connected"
    else:
        reason = "logged_out"
    return Result("ok", reason, {"session_cookies": 1 if present else 0, **counters})


def _build_client(environ: Mapping[str, str]) -> CamofoxClient:
    return CamofoxClient.from_env(environ)


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ, out: TextIO = sys.stdout) -> int:
    def emit(line: str) -> None:
        out.write(line + "\n")
        out.flush()

    try:
        argparse.ArgumentParser(prog="python -m app.session_check").parse_args(argv)
        result = check(_build_client(environ), now_s=time.time())
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:  # noqa: BLE001 - class name only, never the message
        emit(f"tiktok-session-check: error=internal class={type(exc).__name__}")
        return EXIT_INTERNAL

    if result.exit_code:
        emit(f"tiktok-session-check: error={_ERROR_NAMES.get(result.exit_code, 'unknown')}")
        return result.exit_code

    cookies = result.counters.get("session_cookies")
    emit(
        f"tiktok-session-check: state={result.reason}"
        f" session_cookies={'na' if cookies is None else cookies}"
        f" tabs={result.counters.get('tiktok_tabs', 0)}"
        f" opened_tab={result.counters.get('opened_tab', 0)}"
    )
    payload = {"status": result.status, "reason": result.reason, "counters": result.counters}
    emit("CONSOLE_RESULT " + json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
