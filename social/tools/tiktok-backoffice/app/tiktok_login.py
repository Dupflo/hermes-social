"""TikTok login, run by the hermes-console job `tiktok-login` (s21, console ADR 050/051).

The console triggers it only after a `logged_out` verdict of `tiktok-session-check` (or a manual run), at most
TIKTOK_LOGIN_MAX_ATTEMPTS_24H times per 24 h (console side). This module:

1. reads the Camofox profile cookies first: session cookies present => `skipped`/`already_connected`, nothing
   is typed (protects a connected production session);
2. otherwise opens its own tab, navigates ONCE to the e-mail login page, types TIKTOK_USERNAME and
   TIKTOK_PASSWORD with real keystrokes (`mode: keyboard`) and submits;
3. polls a passive probe (the page is never loaded again): captcha, refused credentials, verification code screen, or the
   session cookies (=> `connected`, and the storage_state read that proves it also saves the profile).

Credentials come from this process environment: the console injects the vault values into the exec `Env`
(console ADR 051), else the container environment (deploy/.env) applies.

Outcomes (ADR 011 contract, console ADR 050 §6):

- attempt concluded => `ok` + connected | captcha_required | verification_code_required | code_timeout |
  code_rejected | bad_credentials | login_unknown; `attempted = 1` as soon as the login page is navigated to;
- nothing attempted => `skipped` + already_connected | credentials_missing | camofox_down |
  camofox_auth_missing | camofox_auth_rejected (+ would_attempt, with the diagnostic `--check-only` only);
- unexpected => exit 14 (unexpected Camofox response or deadline), 15 (tabs saturated), 16 (internal error),
  without CONSOLE_RESULT.

Before every screenshot, the identifier, password and code fields are masked (MASK_SECRETS_JS); a failed mask
means no screenshot. Secret hygiene: the output holds names and numbers only, never the identifier, the
password, the code, a response repr or an exception message. This module opens no database.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, TextIO

from app.camofox_reader import CamofoxClient, CamofoxError, CamofoxHTTPError, CamofoxUnreachable
from app.session_check import session_cookies_present
from app.tiktok_login_selectors import (
    CODE_INPUT_SELECTOR,
    CODE_SUBMIT_SELECTOR,
    LOGIN_PROBE_JS,
    LOGIN_URL,
    MASK_SECRETS_JS,
    PASSWORD_SELECTOR,
    SUBMIT_SELECTOR,
    USERNAME_SELECTOR,
)

# Runs directory shared with the console worker (hermes: deploy/data -> /opt/data; worker: /hermes-runs).
RUNS_DIR = "/opt/data/console-runs"
SCREENSHOTS_DIR = "/opt/data/tiktok-backoffice/screenshots"
SESSION_KEY = "console-tiktok-login"

DEADLINE_S = 180.0  # outside the code wait: 35 + 3 x 10 + 30 + 60 + margin
STEP_TIMEOUT_S = 10.0
NAVIGATE_TIMEOUT_S = 35.0
STORAGE_TIMEOUT_MAX_S = 60.0
POLL_S = 2.0
POLL_WINDOW_S = 30.0
MAX_TABS = 9
SCREENSHOTS_KEPT = 20
CODE_WAIT_DEFAULT_S = 600
CODE_WAIT_MIN_S = 120
CODE_WAIT_MAX_S = 600
HEARTBEAT_S = 30.0  # Camofox closes a tab after 5 min without toolCalls; evaluate counts, screenshot does not
MAX_CODE_ROUNDS = 3

EXIT_UNEXPECTED = 14
EXIT_TABS_FULL = 15
EXIT_INTERNAL = 16

_ERROR_NAMES = {EXIT_UNEXPECTED: "unexpected_response", EXIT_TABS_FULL: "tabs_full"}
_SCREENSHOT_RE = re.compile(r"^login-\d{13}\.png$")
_CODE_RE = re.compile(r"^\d{4,8}$")

# Injected by tests; main() uses them.
_clock: Callable[[], float] = time.time
_sleep: Callable[[float], None] = time.sleep


@dataclass
class Result:
    status: str = "ok"
    reason: str = ""
    counters: dict[str, int] = field(default_factory=dict)
    exit_code: int = 0


class _Abort(Exception):
    """Internal control flow: stop with a given exit code."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(exit_code)
        self.exit_code = exit_code


class _Down(Exception):
    """Internal control flow: Camofox stopped answering before anything was attempted."""


class _Outcome(Exception):
    """Internal control flow: an attempt concluded with a reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def awaiting_name(run_id: int) -> str:
    """Marker written by this script when the code screen is reached; read by the console worker."""
    return f"tiktok-login-{run_id}.awaiting.json"


def code_name(run_id: int) -> str:
    """Code file written by the console worker; read then deleted by this script before typing."""
    return f"tiktok-login-{run_id}.code"


def code_wait_seconds(env: Mapping[str, str]) -> int:
    raw = (env.get("TIKTOK_LOGIN_CODE_WAIT_SEC") or "").strip()
    if not re.fullmatch(r"\d{1,6}", raw):
        return CODE_WAIT_DEFAULT_S
    return max(CODE_WAIT_MIN_S, min(CODE_WAIT_MAX_S, int(raw)))


def purge_screenshots(directory: str, keep: int = SCREENSHOTS_KEPT) -> None:
    """Keep the `keep` newest login-<epoch_ms>.png captures; never touch any other file."""
    try:
        names = sorted(n for n in os.listdir(directory) if _SCREENSHOT_RE.match(n))
    except OSError:
        return
    for name in names[: max(0, len(names) - keep)]:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass


def _write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


class _Login:
    def __init__(
        self,
        client: Any,
        env: Mapping[str, str],
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        runs_dir: str,
        screenshots_dir: str,
        run_id: int | None,
        check_only: bool,
    ) -> None:
        self.client = client
        self.env = env
        self.clock = clock
        self.sleep = sleep
        self.runs_dir = runs_dir
        self.screenshots_dir = screenshots_dir
        self.run_id = run_id
        self.check_only = check_only
        self.started = clock()
        self.paused_s = 0.0  # time spent waiting for the code, outside the deadline
        self.wait_started: float | None = None
        self.tab: str | None = None
        self.form_seen = False
        # A code whose file could not be deleted: never re-read and re-typed (review m4). Never logged.
        self.undeleted_code: str | None = None
        self.counters = {
            "attempted": 0,
            "screenshot_at": 0,
            "session_cookies": 0,
            "code_rounds": 0,
            "storage_saved": 0,
            "tab_close_failed": 0,
            "codes_ignored": 0,
        }

    # --- time -------------------------------------------------------------------------------------------------

    def remaining(self) -> float:
        now = self.clock()
        paused = self.paused_s + (now - self.wait_started if self.wait_started is not None else 0.0)
        return DEADLINE_S - (now - self.started - paused)

    def step(self, cap: float = STEP_TIMEOUT_S) -> float:
        left = self.remaining()
        if left <= 0:
            raise _Abort(EXIT_UNEXPECTED)
        return min(cap, left)

    # --- Camofox helpers ---------------------------------------------------------------------------------------

    def probe(self) -> dict[str, bool] | None:
        try:
            value = self.client.evaluate_value(self.tab, LOGIN_PROBE_JS, timeout=self.step())
        except (CamofoxError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        probe = {k: value.get(k) is True for k in ("form", "captcha", "code_screen", "bad_credentials", "code_error")}
        if probe["form"]:
            self.form_seen = True
        return probe

    def cookies_present(self) -> bool:
        try:
            state = self.client.storage_state(timeout=self.step(STORAGE_TIMEOUT_MAX_S))
        except (CamofoxError, ValueError):
            return False
        cookies = state.get("cookies") if isinstance(state, dict) else None
        return isinstance(cookies, list) and session_cookies_present(cookies, self.clock())

    def capture(self) -> None:
        """Mask the identifier/password/code fields, then screenshot. Any doubt => no screenshot."""
        if self.tab is None:
            return
        try:
            masked = self.client.evaluate_value(self.tab, MASK_SECRETS_JS, timeout=STEP_TIMEOUT_S)
        except (CamofoxError, ValueError):
            return
        if not isinstance(masked, int) or isinstance(masked, bool) or masked < 0:
            return
        if masked == 0 and self.form_seen:
            return
        try:
            png = self.client.screenshot(self.tab, timeout=STEP_TIMEOUT_S)
        except (CamofoxError, ValueError):
            return
        try:
            os.makedirs(self.screenshots_dir, mode=0o700, exist_ok=True)
            at = int(self.clock() * 1000)
            for _ in range(5):
                path = os.path.join(self.screenshots_dir, f"login-{at:013d}.png")
                try:
                    _write_private(path, png)
                    break
                except FileExistsError:
                    at += 1
            else:
                return
        except OSError:
            return
        self.counters["screenshot_at"] = at
        purge_screenshots(self.screenshots_dir)

    def conclude(self, reason: str) -> Result:
        if reason != "connected":
            self.capture()
        return Result("ok", reason, dict(self.counters))

    # --- the attempt -------------------------------------------------------------------------------------------

    def run(self) -> Result:
        client = self.client
        try:
            client.health(timeout=self.step())
        except (CamofoxUnreachable, CamofoxHTTPError):
            return self.skipped("camofox_down")

        if not getattr(client, "api_key", ""):
            return self.skipped("camofox_auth_missing")
        username = (self.env.get("TIKTOK_USERNAME") or "").strip()
        password = self.env.get("TIKTOK_PASSWORD") or ""
        if not username or not password.strip():
            return self.skipped("credentials_missing")

        opened: str | None = None
        try:
            try:
                tabs = client.list_tabs(timeout=self.step())
            except CamofoxUnreachable:
                raise _Down() from None
            except (CamofoxHTTPError, ValueError):
                raise _Abort(EXIT_UNEXPECTED) from None

            def open_blank() -> str:
                try:
                    tab_id = client.create_blank_tab(SESSION_KEY, timeout=self.step())
                except CamofoxUnreachable:
                    raise _Down() from None
                except CamofoxHTTPError as exc:
                    raise _Abort(EXIT_TABS_FULL if exc.status == 429 else EXIT_UNEXPECTED) from None
                except ValueError:
                    raise _Abort(EXIT_UNEXPECTED) from None
                return tab_id

            # storage_state needs a live session: open about:blank only when the session has no tab.
            if not tabs:
                opened = open_blank()
                self.tab = opened

            state: dict[str, Any] | None = None
            for attempt in range(2):
                try:
                    state = client.storage_state(timeout=self.step(STORAGE_TIMEOUT_MAX_S))
                    break
                except CamofoxUnreachable:
                    raise _Down() from None
                except ValueError:
                    raise _Abort(EXIT_UNEXPECTED) from None
                except CamofoxHTTPError as exc:
                    if exc.status == 403:
                        return self.skipped("camofox_auth_rejected")
                    if exc.status == 404 and attempt == 0 and opened is None:
                        opened = open_blank()
                        self.tab = opened
                        continue
                    raise _Abort(EXIT_UNEXPECTED) from None
            cookies = state.get("cookies") if isinstance(state, dict) else None
            if not isinstance(cookies, list):
                raise _Abort(EXIT_UNEXPECTED)
            # D1: a connected profile is never touched.
            if session_cookies_present(cookies, self.clock()):
                return self.skipped("already_connected")
            if self.check_only:
                return self.skipped("would_attempt")

            if len(tabs) >= MAX_TABS:
                raise _Abort(EXIT_TABS_FULL)
            if opened is None:
                opened = open_blank()
                self.tab = opened

            try:
                return self.attempt(username, password)
            except _Outcome as outcome:
                return self.conclude(outcome.reason)
        except _Down:
            return self.skipped("camofox_down")
        except _Abort as abort:
            return Result(counters=dict(self.counters), exit_code=abort.exit_code)
        finally:
            self.cleanup()
            if opened is not None:
                try:
                    client.delete_tab(opened, timeout=STEP_TIMEOUT_S)
                except Exception:
                    self.counters["tab_close_failed"] = 1

    def skipped(self, reason: str) -> Result:
        return Result("skipped", reason, dict(self.counters))

    def attempt(self, username: str, password: str) -> Result:
        client, tab = self.client, self.tab
        # Correction 1: every navigation to the login page counts in the ceiling.
        self.counters["attempted"] = 1
        try:
            client.navigate(tab, LOGIN_URL, timeout=self.step(NAVIGATE_TIMEOUT_S))
        except (CamofoxError, ValueError):
            raise _Outcome("login_unknown") from None

        probe = self.probe()
        if probe is None:
            raise _Outcome("login_unknown")
        if probe["captcha"]:
            raise _Outcome("captcha_required")
        if not probe["form"]:
            raise _Outcome("login_unknown")
        try:
            client.type_text(tab, USERNAME_SELECTOR, username, timeout=self.step())
            client.type_text(tab, PASSWORD_SELECTOR, password, timeout=self.step())
            client.click_selector(tab, SUBMIT_SELECTOR, timeout=self.step())
        except (CamofoxError, ValueError):
            raise _Outcome("login_unknown") from None

        return self.poll_after_submit()

    def poll_after_submit(self) -> Result:
        waited = 0.0
        while waited < POLL_WINDOW_S:
            self.sleep(POLL_S)
            waited += POLL_S
            probe = self.probe()
            if probe is None:
                continue
            if probe["captcha"]:
                raise _Outcome("captcha_required")
            if probe["code_screen"]:
                return self.code_screen()
            if probe["bad_credentials"]:
                raise _Outcome("bad_credentials")
            if not probe["form"] and self.cookies_present():
                return self.connected()
        raise _Outcome("login_unknown")

    def connected(self) -> Result:
        # D9: the storage_state read that saw the cookies also saved the profile (persistence plugin).
        self.counters["session_cookies"] = 1
        self.counters["storage_saved"] = 1
        return Result("ok", "connected", dict(self.counters))

    # --- verification code (console ADR 050 §2) -----------------------------------------------------------

    def _channel_path(self, name: str) -> str:
        return os.path.join(self.runs_dir, name)

    def channel_available(self) -> bool:
        return (
            self.run_id is not None
            and os.path.isdir(self.runs_dir)
            and os.access(self.runs_dir, os.W_OK)
        )

    def write_marker(self, round_no: int, expires_at_ms: int, rejected_before: int) -> None:
        assert self.run_id is not None
        payload = json.dumps(
            {
                "round": round_no,
                "expires_at_ms": expires_at_ms,
                "screenshot_at": self.counters["screenshot_at"],
                "rejected_before": rejected_before,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        final = self._channel_path(awaiting_name(self.run_id))
        tmp = final + ".tmp"
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        _write_private(tmp, payload)
        os.replace(tmp, final)

    def take_code(self) -> str | None:
        """Read the code file then delete it BEFORE anything is typed. Never logged.

        If the file cannot be deleted, its code is remembered and an identical read is skipped afterwards, so a
        stale file is never typed twice (a new code from the console replaces the file and differs).
        """
        assert self.run_id is not None
        path = self._channel_path(code_name(self.run_id))
        try:
            with open(path, "rb") as handle:
                raw = handle.read(64)
        except OSError:
            return None
        code = raw.decode("ascii", "replace").strip()
        try:
            os.unlink(path)
            deleted = True
        except OSError:
            deleted = False
        if self.undeleted_code is not None and code == self.undeleted_code:
            return None  # the same undeletable file, already consumed
        if not deleted:
            self.undeleted_code = code
        return code

    def heartbeat(self) -> None:
        try:
            self.client.evaluate_value(self.tab, "1", timeout=STEP_TIMEOUT_S)
        except (CamofoxError, ValueError):
            pass

    def submit_code(self, code: str) -> str:
        """Type the code, submit, poll. Returns connected | rejected | another final reason."""
        try:
            self.client.type_text(self.tab, CODE_INPUT_SELECTOR, code, timeout=self.step())
            self.client.click_selector(self.tab, CODE_SUBMIT_SELECTOR, timeout=self.step())
        except (CamofoxError, ValueError):
            return "login_unknown"
        waited = 0.0
        while waited < POLL_WINDOW_S:
            self.sleep(POLL_S)
            waited += POLL_S
            probe = self.probe()
            if probe is None:
                continue
            if probe["captcha"]:
                return "captcha_required"
            if probe["code_screen"]:
                if probe["code_error"]:
                    return "rejected"
                continue
            if not probe["form"] and self.cookies_present():
                return "connected"
        return "login_unknown"

    def code_screen(self) -> Result:
        if not self.channel_available():
            # No channel (no CONSOLE_RUN_ID, runs directory missing or read-only): final, Florian ends in noVNC.
            raise _Outcome("verification_code_required")
        wait_s = code_wait_seconds(self.env)
        self.capture()
        expires_at_ms = int(self.clock() * 1000) + wait_s * 1000
        round_no, rejected_before = 1, 0
        self.counters["code_rounds"] = round_no
        self.write_marker(round_no, expires_at_ms, rejected_before)
        self.wait_started = self.clock()
        last_call = self.clock()
        try:
            while True:
                if self.clock() * 1000 >= expires_at_ms:
                    reason = "code_timeout"
                    break
                code = self.take_code()
                if code is not None:
                    if not _CODE_RE.fullmatch(code):
                        self.counters["codes_ignored"] += 1
                    else:
                        verdict = self.submit_code(code)
                        last_call = self.clock()
                        if verdict == "connected":
                            return self.connected()
                        if verdict != "rejected":
                            reason = verdict
                            break
                        if round_no >= MAX_CODE_ROUNDS:
                            reason = "code_rejected"
                            break
                        round_no, rejected_before = round_no + 1, 1
                        self.counters["code_rounds"] = round_no
                        self.write_marker(round_no, expires_at_ms, rejected_before)
                        continue
                if self.clock() - last_call >= HEARTBEAT_S:
                    self.heartbeat()
                    last_call = self.clock()
                self.sleep(POLL_S)
        finally:
            self.paused_s += self.clock() - self.wait_started
            self.wait_started = None
        raise _Outcome(reason)

    def cleanup(self) -> None:
        if self.run_id is None:
            return
        for name in (awaiting_name(self.run_id), code_name(self.run_id), awaiting_name(self.run_id) + ".tmp"):
            try:
                os.unlink(self._channel_path(name))
            except OSError:
                pass


def login(
    client: Any,
    env: Mapping[str, str],
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    runs_dir: str = RUNS_DIR,
    screenshots_dir: str = SCREENSHOTS_DIR,
    run_id: int | None = None,
    check_only: bool = False,
) -> Result:
    return _Login(
        client,
        env,
        clock=clock,
        sleep=sleep,
        runs_dir=runs_dir,
        screenshots_dir=screenshots_dir,
        run_id=run_id,
        check_only=check_only,
    ).run()


def _build_client(environ: Mapping[str, str]) -> CamofoxClient:
    return CamofoxClient.from_env(environ)


def _run_id(environ: Mapping[str, str]) -> int | None:
    raw = (environ.get("CONSOLE_RUN_ID") or "").strip()
    if not re.fullmatch(r"[1-9]\d{0,15}", raw):
        return None
    return int(raw)


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ, out: TextIO = sys.stdout) -> int:
    def emit(line: str) -> None:
        out.write(line + "\n")
        out.flush()

    try:
        parser = argparse.ArgumentParser(prog="python -m app.tiktok_login")
        parser.add_argument("--check-only", action="store_true")
        args = parser.parse_args(argv)
        previous_umask = os.umask(0o077)
        try:
            result = login(
                _build_client(environ),
                environ,
                clock=_clock,
                sleep=_sleep,
                runs_dir=RUNS_DIR,
                screenshots_dir=SCREENSHOTS_DIR,
                run_id=_run_id(environ),
                check_only=args.check_only,
            )
        finally:
            os.umask(previous_umask)
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:  # noqa: BLE001 - class name only, never the message
        emit(f"tiktok-login: error=internal class={type(exc).__name__}")
        return EXIT_INTERNAL

    if result.exit_code:
        emit(f"tiktok-login: error={_ERROR_NAMES.get(result.exit_code, 'unknown')}")
        return result.exit_code

    c = result.counters
    emit(
        f"tiktok-login: result={result.reason}"
        f" attempted={c.get('attempted', 0)}"
        f" screenshot={1 if c.get('screenshot_at') else 0}"
        f" rounds={c.get('code_rounds', 0)}"
    )
    payload = {"status": result.status, "reason": result.reason, "counters": c}
    emit("CONSOLE_RESULT " + json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
