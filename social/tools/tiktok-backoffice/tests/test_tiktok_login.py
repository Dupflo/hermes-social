"""s21: TikTok login run by the hermes-console job `tiktok-login` (console ADR 050/051).

A fake Camofox client replays the scenario fixtures of tests/fixtures/tiktok_login and journals every call;
the clock and `sleep` are injected, so the polling and the code wait run on simulated time.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from app import tiktok_login
from app.camofox_reader import CamofoxHTTPError, CamofoxUnreachable
from app.tiktok_login import login, main
from app.tiktok_login_selectors import (
    CODE_INPUT_SELECTOR,
    LOGIN_PROBE_JS,
    LOGIN_URL,
    MASK_SECRETS_JS,
    PASSWORD_SELECTOR,
    SUBMIT_SELECTOR,
    USERNAME_SELECTOR,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tiktok_login"
START = 1_800_000_000.0
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
ENV = {"TIKTOK_USERNAME": "secretuser@example.com", "TIKTOK_PASSWORD": "SECRETPASSWORD"}
NO_CODE_FIXTURES = (
    "already_connected",
    "form_then_connected",
    "form_then_captcha",
    "form_then_bad_credentials",
    "form_missing",
    "captcha_before_form",
)
CODE_FIXTURES = (
    "form_then_code_then_connected",
    "code_rejected_then_accepted",
    "code_rejected_three_times",
    "code_timeout",
)
API_CALLS = {"navigate", "type_text", "click_selector", "press", "evaluate_value"}  # those count as Camofox toolCalls


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Clock:
    def __init__(self) -> None:
        self.t = START

    def __call__(self) -> float:
        return self.t


class FakeClient:
    """Replays a fixture. Every call is journaled as (name, detail, time)."""

    def __init__(self, fixture: dict, clock: Clock, *, tabs=None, api_key="SECRETAPIKEY", raise_on=None, on_type=None):
        self.api_key = api_key
        self.clock = clock
        self.calls: list[tuple[str, object, float]] = []
        self.tabs = [] if tabs is None else tabs
        self.storage_states = list(fixture["storage_states"])
        self.probes = list(fixture["probes"])
        self.mask_count = fixture.get("mask_count", 0)
        b64 = fixture.get("screenshot_b64")
        self.png = base64.b64decode(b64) if b64 else PNG
        self.raise_on = raise_on or {}
        self.on_type = on_type
        self.tab_counter = 0
        self.mask_results: list[object] = []

    def _log(self, name, detail=None):
        self.calls.append((name, detail, self.clock()))
        error = self.raise_on.get(name)
        if callable(error) and not isinstance(error, BaseException):
            error = error(detail)
        if error is not None:
            raise error

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def health(self, *, timeout=None):
        self._log("health")
        return {"ok": True}

    def list_tabs(self, *, timeout=None):
        self._log("list_tabs")
        return list(self.tabs)

    def create_blank_tab(self, session_key, *, timeout=None):
        self._log("create_blank_tab", session_key)
        self.tab_counter += 1
        return f"login-tab-{self.tab_counter}"

    def delete_tab(self, tab_id, *, timeout=None):
        self._log("delete_tab", tab_id)

    def storage_state(self, timeout=60):
        self._log("storage_state")
        state = self.storage_states.pop(0) if len(self.storage_states) > 1 else self.storage_states[0]
        return {"cookies": state, "origins": []}

    def navigate(self, tab_id, url, *, timeout=35):
        self._log("navigate", url)

    def type_text(self, tab_id, selector, text, *, delay_ms=60, timeout=None):
        if self.on_type:
            self.on_type(selector, text)
        self._log("type_text", selector)

    def press(self, tab_id, key, *, timeout=None):
        self._log("press", key)

    def click_selector(self, tab_id, selector, *, timeout=None):
        self._log("click_selector", selector)

    def evaluate_value(self, tab_id, expression, *, timeout=None):
        if expression == LOGIN_PROBE_JS:
            self._log("evaluate_value", "probe")
            return dict(self.probes.pop(0) if len(self.probes) > 1 else self.probes[0])
        if expression == MASK_SECRETS_JS:
            self._log("evaluate_value", "mask")
            self.mask_results.append(self.mask_count)
            return self.mask_count
        self._log("evaluate_value", "heartbeat")
        return 1

    def screenshot(self, tab_id, *, timeout=None):
        self._log("screenshot")
        return self.png

    # s22: probe of the console-mouse plugin, reached by the real solver only when a SadCaptcha key is set.
    def mouse_available(self, *, timeout=5.0):
        self._log("mouse_available")
        return False

    def mouse(self, *args, **kwargs):
        raise AssertionError("no gesture in the login tests")


def make_sleep(clock: Clock, runs_dir: Path | None = None, run_id: int | None = None, codes=None):
    """Advance simulated time; deliver one fixture code per new awaiting round, like the console worker would."""
    pending = list(codes or [])
    delivered: set[int] = set()

    def sleep(seconds: float) -> None:
        clock.t += seconds
        if runs_dir is None or run_id is None:
            return
        marker = runs_dir / f"tiktok-login-{run_id}.awaiting.json"
        if marker.exists() and pending:
            current = json.loads(marker.read_text(encoding="utf-8"))["round"]
            if current not in delivered:
                delivered.add(current)
                (runs_dir / f"tiktok-login-{run_id}.code").write_text(pending.pop(0), encoding="utf-8")

    return sleep


def run(name: str, tmp_path: Path, *, env=None, run_id=None, check_only=False, **client_kwargs):
    fixture = load(name)
    clock = Clock()
    client = FakeClient(fixture, clock, **client_kwargs)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(exist_ok=True)
    shots = tmp_path / "shots"
    result = login(
        client,
        ENV if env is None else env,
        clock=clock,
        sleep=make_sleep(clock, runs_dir, run_id, fixture.get("codes")),
        runs_dir=str(runs_dir),
        screenshots_dir=str(shots),
        run_id=run_id,
        check_only=check_only,
    )
    return result, client, shots, runs_dir


# --- outcomes per fixture -------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", NO_CODE_FIXTURES)
def test_fixture_outcome(name, tmp_path):
    expected = load(name)["expected"]
    result, client, _, _ = run(name, tmp_path)
    assert result.exit_code == 0
    assert (result.status, result.reason) == (expected["status"], expected["reason"])
    assert result.counters["attempted"] == expected["attempted"]
    assert result.counters["code_rounds"] == expected["code_rounds"]
    assert "delete_tab" in client.names()


@pytest.mark.parametrize("name", ["captcha_before_form", "form_missing"])
def test_ceiling_counts_navigation_without_typing(name, tmp_path):
    result, client, _, _ = run(name, tmp_path)
    assert result.status == "ok"
    assert result.counters["attempted"] == 1
    assert client.names().count("navigate") == 1
    assert "type_text" not in client.names()


def test_already_connected_never_navigates_nor_types(tmp_path):
    result, client, _, _ = run("already_connected", tmp_path)
    assert (result.status, result.reason) == ("skipped", "already_connected")
    assert result.counters["attempted"] == 0
    for forbidden in ("navigate", "type_text", "click_selector", "press", "screenshot"):
        assert forbidden not in client.names()


@pytest.mark.parametrize("name", NO_CODE_FIXTURES + CODE_FIXTURES)
def test_session_cookies_are_read_before_any_navigation(name, tmp_path):
    _, client, _, _ = run(name, tmp_path)
    names = client.names()
    if "navigate" in names:
        assert names.index("storage_state") < names.index("navigate")


def test_check_only_without_cookies_would_attempt(tmp_path):
    result, client, _, _ = run("form_then_connected", tmp_path, check_only=True)
    assert (result.status, result.reason) == ("skipped", "would_attempt")
    assert result.counters["attempted"] == 0
    assert "navigate" not in client.names()


def test_check_only_with_cookies_is_already_connected(tmp_path):
    result, _, _, _ = run("already_connected", tmp_path, check_only=True)
    assert (result.status, result.reason) == ("skipped", "already_connected")


def test_login_types_both_fields_with_keystrokes_then_submits(tmp_path):
    _, client, _, _ = run("form_then_connected", tmp_path)
    typed = [c[1] for c in client.calls if c[0] == "type_text"]
    assert typed == [USERNAME_SELECTOR, PASSWORD_SELECTOR]
    assert ("click_selector", SUBMIT_SELECTOR) in [(c[0], c[1]) for c in client.calls]
    assert [c[1] for c in client.calls if c[0] == "navigate"] == [LOGIN_URL]


def test_connected_saves_storage_state_after_success(tmp_path):
    result, client, shots, _ = run("form_then_connected", tmp_path)
    names = client.names()
    assert (result.status, result.reason) == ("ok", "connected")
    submit = names.index("click_selector")
    assert "storage_state" in names[submit:]
    assert result.counters["storage_saved"] == 1
    assert result.counters["session_cookies"] == 1
    assert result.counters["screenshot_at"] == 0
    assert "screenshot" not in names
    assert not shots.exists() or not list(shots.iterdir())


@pytest.mark.parametrize("name", ["form_then_captcha", "form_then_bad_credentials", "form_missing", "captcha_before_form"])
def test_mask_precedes_every_screenshot(name, tmp_path):
    result, client, shots, _ = run(name, tmp_path)
    details = [(c[0], c[1]) for c in client.calls]
    shots_idx = [i for i, d in enumerate(details) if d[0] == "screenshot"]
    assert shots_idx, "a failure outcome takes a screenshot"
    for i in shots_idx:
        assert details[i - 1] == ("evaluate_value", "mask")
    assert result.counters["screenshot_at"] > 0
    files = list(shots.iterdir())
    assert [f.name for f in files] == [f"login-{result.counters['screenshot_at']}.png"]
    assert re.fullmatch(r"login-\d{13}\.png", files[0].name)
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert files[0].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_mask_counts_username_and_password_on_bad_credentials(tmp_path):
    _, client, _, _ = run("form_then_bad_credentials", tmp_path)
    # The fixture form holds the username field and the password field: both are counted by the mask.
    assert client.mask_results == [2]


def test_mask_failure_means_no_screenshot(tmp_path):
    def mask_fails(detail):
        return ValueError("unexpected evaluate payload") if detail == "mask" else None

    result, client, _, _ = run("form_then_captcha", tmp_path, raise_on={"evaluate_value": mask_fails})
    assert result.reason == "captcha_required"
    assert "screenshot" not in client.names()
    assert result.counters["screenshot_at"] == 0


def test_mask_of_zero_fields_while_form_seen_means_no_screenshot(tmp_path):
    fixture = load("form_then_bad_credentials")
    clock = Clock()
    client = FakeClient(fixture, clock)
    client.mask_count = 0
    result = login(client, ENV, clock=clock, sleep=make_sleep(clock), runs_dir=str(tmp_path),
                   screenshots_dir=str(tmp_path / "s"), run_id=None, check_only=False)
    assert result.reason == "bad_credentials"
    assert "screenshot" not in client.names()
    assert result.counters["screenshot_at"] == 0


def test_slow_navigate_is_login_unknown_with_one_navigation(tmp_path):
    fixture = load("form_then_connected")
    clock = Clock()

    def slow(detail):
        clock.t += 35
        return CamofoxUnreachable()

    client = FakeClient(fixture, clock, raise_on={"navigate": slow})
    result = login(client, ENV, clock=clock, sleep=make_sleep(clock), runs_dir=str(tmp_path),
                   screenshots_dir=str(tmp_path / "s"), run_id=None, check_only=False)
    assert (result.status, result.reason) == ("ok", "login_unknown")
    assert result.counters["attempted"] == 1
    names = client.names()
    assert names.count("navigate") == 1
    assert "type_text" not in names
    assert names.index("screenshot") == names.index("evaluate_value") + 1
    assert result.counters["screenshot_at"] > 0
    assert "delete_tab" in names


@pytest.mark.parametrize("name", NO_CODE_FIXTURES + CODE_FIXTURES)
def test_single_navigation_and_tab_always_closed(name, tmp_path):
    _, client, _, _ = run(name, tmp_path, run_id=7)
    names = client.names()
    assert names.count("navigate") <= 1
    assert "delete_tab" == names[-1] or "delete_tab" in names


def test_tab_closed_when_typing_raises(tmp_path):
    with pytest.raises(RuntimeError):
        run("form_then_connected", tmp_path, raise_on={"type_text": RuntimeError("boom")})


def test_tab_closed_in_finally_on_exception(tmp_path):
    fixture = load("form_then_connected")
    clock = Clock()
    client = FakeClient(fixture, clock, raise_on={"type_text": RuntimeError("boom")})
    with pytest.raises(RuntimeError):
        login(client, ENV, clock=clock, sleep=make_sleep(clock), runs_dir=str(tmp_path),
              screenshots_dir=str(tmp_path / "s"), run_id=None, check_only=False)
    assert client.names()[-1] == "delete_tab"


def test_existing_tabs_open_a_new_login_tab(tmp_path):
    tabs = [{"tabId": "florian", "url": "https://www.tiktok.com/foryou"}]
    _, client, _, _ = run("form_then_connected", tmp_path, tabs=tabs)
    navigated = [c for c in client.calls if c[0] == "navigate"]
    assert navigated
    assert [c[1] for c in client.calls if c[0] == "delete_tab"] == ["login-tab-1"]


def test_nine_tabs_is_exit_15_without_navigation(tmp_path):
    tabs = [{"tabId": f"t{i}", "url": "about:blank"} for i in range(9)]
    result, client, _, _ = run("form_then_connected", tmp_path, tabs=tabs)
    assert result.exit_code == 15
    assert "navigate" not in client.names()


def test_list_tabs_http_error_is_exit_14(tmp_path):
    result, _, _, _ = run("form_then_connected", tmp_path, raise_on={"list_tabs": CamofoxHTTPError(500)})
    assert result.exit_code == 14


def test_deadline_outside_code_wait_is_exit_14(tmp_path):
    fixture = load("form_then_connected")
    clock = Clock()

    def slow(detail):
        clock.t += 200
        return None

    client = FakeClient(fixture, clock, raise_on={"navigate": slow})
    result = login(client, ENV, clock=clock, sleep=make_sleep(clock), runs_dir=str(tmp_path),
                   screenshots_dir=str(tmp_path / "s"), run_id=None, check_only=False)
    assert result.exit_code == 14
    assert client.names()[-1] == "delete_tab"


@pytest.mark.parametrize(
    "kwargs, env, reason",
    [
        ({"raise_on": {"health": CamofoxUnreachable()}}, ENV, "camofox_down"),
        ({"raise_on": {"health": CamofoxHTTPError(503)}}, ENV, "camofox_down"),
        ({"api_key": ""}, ENV, "camofox_auth_missing"),
        ({}, {"TIKTOK_USERNAME": "u", "TIKTOK_PASSWORD": ""}, "credentials_missing"),
        ({}, {"TIKTOK_USERNAME": " ", "TIKTOK_PASSWORD": "p"}, "credentials_missing"),
        ({"raise_on": {"storage_state": CamofoxHTTPError(403)}}, ENV, "camofox_auth_rejected"),
        ({"raise_on": {"list_tabs": CamofoxUnreachable()}}, ENV, "camofox_down"),
    ],
)
def test_nothing_attempted_is_skipped(kwargs, env, reason, tmp_path):
    result, client, _, _ = run("form_then_connected", tmp_path, env=env, **kwargs)
    assert (result.status, result.reason) == ("skipped", reason)
    assert result.counters["attempted"] == 0
    assert "navigate" not in client.names()


# --- screenshots purge ----------------------------------------------------------------------------------------


def test_purge_keeps_the_20_newest_login_captures(tmp_path):
    for i in range(22):
        (tmp_path / f"login-{1_700_000_000_000 + i}.png").write_bytes(PNG)
    (tmp_path / "other.png").write_bytes(PNG)
    (tmp_path / "login-notes.txt").write_text("x")
    tiktok_login.purge_screenshots(str(tmp_path), keep=20)
    remaining = sorted(p.name for p in tmp_path.glob("login-*.png"))
    assert len(remaining) == 20
    assert "login-1700000000000.png" not in remaining and "login-1700000000001.png" not in remaining
    assert (tmp_path / "other.png").exists() and (tmp_path / "login-notes.txt").exists()


# --- main(): output contract and secret hygiene ---------------------------------------------------------------

CONSOLE_RE = re.compile(r"^CONSOLE_RESULT (\{.*\})$")


def run_main(monkeypatch, tmp_path, client, *, argv=None, environ=None):
    clock = client.clock
    monkeypatch.setattr(tiktok_login, "_build_client", lambda environ: client)
    monkeypatch.setattr(tiktok_login, "_clock", clock)
    monkeypatch.setattr(tiktok_login, "_sleep", make_sleep(clock))
    monkeypatch.setattr(tiktok_login, "SCREENSHOTS_DIR", str(tmp_path / "shots"))
    monkeypatch.setattr(tiktok_login, "RUNS_DIR", str(tmp_path))
    out = io.StringIO()
    code = main(argv or [], dict(environ if environ is not None else ENV), out)
    return code, out.getvalue()


def test_main_prints_one_line_then_console_result(monkeypatch, tmp_path):
    client = FakeClient(load("form_then_captcha"), Clock())
    code, text = run_main(monkeypatch, tmp_path, client)
    lines = text.strip().splitlines()
    assert code == 0
    assert len(lines) == 2
    assert re.fullmatch(r"tiktok-login: result=captcha_required attempted=1 screenshot=1 rounds=0", lines[0])
    payload = json.loads(CONSOLE_RE.match(lines[-1]).group(1))
    assert payload["status"] == "ok" and payload["reason"] == "captcha_required"
    assert re.fullmatch(r"[a-z0-9_]{1,64}", payload["reason"])
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in payload["counters"].values())


def test_main_check_only_prints_console_result(monkeypatch, tmp_path):
    client = FakeClient(load("form_then_connected"), Clock())
    code, text = run_main(monkeypatch, tmp_path, client, argv=["--check-only"])
    lines = text.strip().splitlines()
    assert code == 0
    assert lines[0].startswith("tiktok-login: result=would_attempt attempted=0")
    payload = json.loads(CONSOLE_RE.match(lines[-1]).group(1))
    assert (payload["status"], payload["reason"]) == ("skipped", "would_attempt")
    assert payload["counters"]["attempted"] == 0
    assert "navigate" not in client.names()


def test_main_exit_codes(monkeypatch, tmp_path):
    client = FakeClient(load("form_then_connected"), Clock(), raise_on={"list_tabs": CamofoxHTTPError(500)})
    code, text = run_main(monkeypatch, tmp_path, client)
    assert code == 14 and text.strip() == "tiktok-login: error=unexpected_response"
    client = FakeClient(load("form_then_connected"), Clock(), tabs=[{"tabId": str(i), "url": ""} for i in range(9)])
    code, text = run_main(monkeypatch, tmp_path, client)
    assert code == 15 and text.strip() == "tiktok-login: error=tabs_full"
    client = FakeClient(load("form_then_connected"), Clock(), raise_on={"type_text": RuntimeError("x")})
    code, text = run_main(monkeypatch, tmp_path, client)
    assert code == 16 and text.strip() == "tiktok-login: error=internal class=RuntimeError"
    assert "CONSOLE_RESULT" not in text


def _secret_error():
    return RuntimeError("SECRETPASSWORD secretuser 482917")


@pytest.mark.parametrize(
    "fixture, kwargs, expected_code",
    [
        ("form_then_bad_credentials", {}, 0),
        ("form_then_connected", {}, 0),
        ("already_connected", {}, 0),
        ("form_then_connected", {"raise_on": {"list_tabs": CamofoxHTTPError(500)}}, 14),
        ("form_then_connected", {"tabs": [{"tabId": str(i), "url": ""} for i in range(9)]}, 15),
        ("form_then_connected", {"raise_on": {"type_text": _secret_error()}}, 16),
        ("form_then_connected", {"raise_on": {"health": _secret_error()}}, 16),
        ("form_then_captcha", {"raise_on": {"screenshot": _secret_error()}}, 16),
    ],
)
def test_secrets_never_printed(monkeypatch, tmp_path, capsys, fixture, kwargs, expected_code):
    client = FakeClient(load(fixture), Clock(), **kwargs)
    monkeypatch.setattr(tiktok_login, "_build_client", lambda environ: client)
    monkeypatch.setattr(tiktok_login, "_clock", client.clock)
    monkeypatch.setattr(tiktok_login, "_sleep", make_sleep(client.clock))
    monkeypatch.setattr(tiktok_login, "SCREENSHOTS_DIR", str(tmp_path / "shots"))
    code = main([], dict(ENV, CONSOLE_RUN_ID="5"), sys.stdout)
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert code == expected_code
    for secret in ("SECRETPASSWORD", "secretuser", "482917", "SECRETAPIKEY"):
        assert secret not in text


# --- H3b: verification code channel (console ADR 050 §2) ------------------------------------------------------


def test_channel_names_are_shared_with_the_console():
    # Same literals as worker/tiktok-login-code.test.ts and core/jobs/exec-cmd.ts (HERMES_RUNS_DIR) in the console.
    assert tiktok_login.awaiting_name(42) == "tiktok-login-42.awaiting.json"
    assert tiktok_login.code_name(42) == "tiktok-login-42.code"
    assert tiktok_login.RUNS_DIR == "/opt/data/console-runs"


@pytest.mark.parametrize("name", CODE_FIXTURES)
def test_code_fixture_outcome(name, tmp_path):
    expected = load(name)["expected"]
    result, client, _, runs = run(name, tmp_path, run_id=42)
    assert result.exit_code == 0
    assert (result.status, result.reason) == (expected["status"], expected["reason"])
    assert result.counters["code_rounds"] == expected["code_rounds"]
    assert result.counters["attempted"] == 1
    assert list(runs.iterdir()) == []  # marker and code removed
    assert client.names().count("navigate") == 1


def test_connected_after_code_saves_storage_and_takes_no_failure_capture(tmp_path):
    result, client, _, _ = run("form_then_code_then_connected", tmp_path, run_id=42)
    assert result.counters["storage_saved"] == 1
    typed = [c[1] for c in client.calls if c[0] == "type_text"]
    assert typed == [USERNAME_SELECTOR, PASSWORD_SELECTOR, CODE_INPUT_SELECTOR]
    # One capture only: the code screen at entry (masked first), none at connected.
    assert client.names().count("screenshot") == 1


def test_marker_content_and_mode(tmp_path):
    seen = {}

    def on_type(selector, text):
        if selector == CODE_INPUT_SELECTOR:
            marker = tmp_path / "runs" / "tiktok-login-42.awaiting.json"
            seen["marker"] = json.loads(marker.read_text(encoding="utf-8"))
            seen["mode"] = stat.S_IMODE(marker.stat().st_mode)

    result, _, _, _ = run("form_then_code_then_connected", tmp_path, run_id=42, on_type=on_type)
    marker = seen["marker"]
    assert set(marker) == {"round", "expires_at_ms", "screenshot_at", "rejected_before"}
    assert marker["round"] == 1 and marker["rejected_before"] == 0
    assert marker["screenshot_at"] == result.counters["screenshot_at"] > 0
    assert marker["expires_at_ms"] - marker["screenshot_at"] == 600_000  # default TIKTOK_LOGIN_CODE_WAIT_SEC
    assert seen["mode"] == 0o600


def test_rejected_code_opens_a_new_round_with_the_same_expiry(tmp_path):
    markers = []

    def on_type(selector, text):
        if selector == CODE_INPUT_SELECTOR:
            markers.append(json.loads((tmp_path / "runs" / "tiktok-login-42.awaiting.json").read_text(encoding="utf-8")))

    result, client, _, _ = run("code_rejected_then_accepted", tmp_path, run_id=42, on_type=on_type)
    assert result.reason == "connected"
    assert [m["round"] for m in markers] == [1, 2]
    assert [m["rejected_before"] for m in markers] == [0, 1]
    assert markers[0]["expires_at_ms"] == markers[1]["expires_at_ms"]


def test_code_file_deleted_before_typing(tmp_path):
    states = []

    def on_type(selector, text):
        if selector == CODE_INPUT_SELECTOR:
            states.append((tmp_path / "runs" / "tiktok-login-42.code").exists())

    run("code_rejected_then_accepted", tmp_path, run_id=42, on_type=on_type)
    assert states == [False, False]


def test_code_file_that_cannot_be_deleted_is_never_typed_twice(monkeypatch, tmp_path):
    # Review m4: if unlink of the code file fails, the same code must not be re-read and re-typed next round.
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if str(path).endswith(".code"):
            raise PermissionError("denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(tiktok_login.os, "unlink", unlink)
    typed = []

    def on_type(selector, text):
        if selector == CODE_INPUT_SELECTOR:
            typed.append(text)

    result, _, _, _ = run("code_rejected_then_accepted", tmp_path, run_id=42, on_type=on_type)
    assert typed == ["111111", "482917"]
    assert result.reason == "connected"


def test_heartbeat_keeps_the_tab_alive_during_the_whole_wait(tmp_path):
    result, client, _, _ = run("code_timeout", tmp_path, run_id=42)
    assert result.reason == "code_timeout"
    times = [c[2] for c in client.calls if c[0] in API_CALLS]
    waited = times[-1] - times[0]
    assert waited >= 590  # the whole 600 s window was simulated
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert max(gaps) <= 60
    assert "navigate" not in [c[0] for c in client.calls[client.names().index("navigate") + 1:]]


def test_code_wait_is_outside_the_deadline(tmp_path):
    # 600 s of wait > the 180 s deadline: the run still concludes (no exit 14).
    result, _, _, _ = run("code_timeout", tmp_path, run_id=42)
    assert result.exit_code == 0


def test_code_wait_seconds_are_bounded(tmp_path):
    for raw, expected in (("30", 120), ("9999", 600), ("abc", 600), ("", 600), ("300", 300)):
        assert tiktok_login.code_wait_seconds({"TIKTOK_LOGIN_CODE_WAIT_SEC": raw}) == expected
    assert tiktok_login.code_wait_seconds({}) == 600


def test_malformed_code_is_ignored(tmp_path):
    fixture = load("code_timeout")
    clock = Clock()
    client = FakeClient(fixture, clock)
    runs = tmp_path / "runs"
    runs.mkdir()
    sleep = make_sleep(clock, runs, 42, ["12ab"])
    result = login(client, ENV, clock=clock, sleep=sleep, runs_dir=str(runs), screenshots_dir=str(tmp_path / "s"),
                   run_id=42, check_only=False)
    assert result.reason == "code_timeout"
    assert result.counters["codes_ignored"] == 1
    assert CODE_INPUT_SELECTOR not in [c[1] for c in client.calls if c[0] == "type_text"]
    assert list(runs.iterdir()) == []


@pytest.mark.parametrize("run_id", [None])
def test_without_run_id_the_code_screen_is_final(run_id, tmp_path):
    result, client, shots, runs = run("form_then_code_then_connected", tmp_path, run_id=run_id)
    assert (result.status, result.reason) == ("ok", "verification_code_required")
    assert list(runs.iterdir()) == []
    assert result.counters["screenshot_at"] > 0
    assert client.names()[client.names().index("screenshot") - 1] == "evaluate_value"


def test_unwritable_runs_dir_makes_the_code_screen_final(tmp_path):
    fixture = load("form_then_code_then_connected")
    clock = Clock()
    client = FakeClient(fixture, clock)
    result = login(client, ENV, clock=clock, sleep=make_sleep(clock), runs_dir=str(tmp_path / "missing"),
                   screenshots_dir=str(tmp_path / "s"), run_id=42, check_only=False)
    assert result.reason == "verification_code_required"


def test_marker_and_code_removed_on_exception(tmp_path):
    def boom(detail):
        return RuntimeError("482917") if detail == CODE_INPUT_SELECTOR else None

    fixture = load("form_then_code_then_connected")
    clock = Clock()
    runs = tmp_path / "runs"
    runs.mkdir()
    client = FakeClient(fixture, clock, raise_on={"type_text": boom})
    with pytest.raises(RuntimeError):
        login(client, ENV, clock=clock, sleep=make_sleep(clock, runs, 42, ["482917"]), runs_dir=str(runs),
              screenshots_dir=str(tmp_path / "s"), run_id=42, check_only=False)
    assert list(runs.iterdir()) == []
    assert client.names()[-1] == "delete_tab"


@pytest.mark.parametrize("name", CODE_FIXTURES)
def test_code_never_printed(monkeypatch, tmp_path, capsys, name):
    fixture = load(name)
    client = FakeClient(fixture, Clock())
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(tiktok_login, "_build_client", lambda environ: client)
    monkeypatch.setattr(tiktok_login, "_clock", client.clock)
    monkeypatch.setattr(tiktok_login, "_sleep", make_sleep(client.clock, runs, 42, ["482917", "482917", "482917"]))
    monkeypatch.setattr(tiktok_login, "SCREENSHOTS_DIR", str(tmp_path / "shots"))
    monkeypatch.setattr(tiktok_login, "RUNS_DIR", str(runs))
    assert main([], dict(ENV, CONSOLE_RUN_ID="42"), sys.stdout) == 0
    captured = capsys.readouterr()
    assert "482917" not in captured.out + captured.err


def test_code_never_printed_when_an_exception_carries_it(monkeypatch, tmp_path, capsys):
    client = FakeClient(load("form_then_code_then_connected"), Clock(),
                        raise_on={"click_selector": lambda d: RuntimeError("482917") if d != SUBMIT_SELECTOR else None})
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(tiktok_login, "_build_client", lambda environ: client)
    monkeypatch.setattr(tiktok_login, "_clock", client.clock)
    monkeypatch.setattr(tiktok_login, "_sleep", make_sleep(client.clock, runs, 42, ["482917"]))
    monkeypatch.setattr(tiktok_login, "SCREENSHOTS_DIR", str(tmp_path / "shots"))
    monkeypatch.setattr(tiktok_login, "RUNS_DIR", str(runs))
    assert main([], dict(ENV, CONSOLE_RUN_ID="42"), sys.stdout) == 16
    captured = capsys.readouterr()
    assert "482917" not in captured.out + captured.err
    assert list(runs.iterdir()) == []


# --- s22: captcha solver integration (console ADR 052/053) -----------------------------------------------------

from app.captcha_solver import CaptchaReport  # noqa: E402

KEY = "SADKEY-SENTINEL-123"
CAPTCHA_FIXTURES = (
    "captcha_before_form_solved",
    "form_then_captcha_solved",
    "form_then_captcha_unsolved",
    "code_then_captcha_solved",
    "captcha_four_times",
)
_ATTEMPTS = {
    "solved": [{"try": 1, "type": "rotate", "variant": "v2", "result": "solved", "ms": 4200}],
    "unsolved": [{"try": n, "type": "rotate", "variant": "v2", "result": "failed", "ms": 3000} for n in (1, 2, 3)],
}


def fake_solver(outcomes, *, seconds=0.0, calls=None):
    """Stands for app.captcha_solver.solve_captcha: one captcha appended per call, `seconds` of simulated time."""
    pending = list(outcomes)

    def solver(client, tab_id, *, api_key, report, clock, sleep, rng, deadline, sadcaptcha=None):
        if calls is not None:
            calls.append({"tab": tab_id, "api_key": api_key, "deadline": deadline, "now": clock()})
        outcome = pending.pop(0) if pending else "unsolved"
        if seconds:
            sleep(seconds)
        report.captchas.append({
            "seq": len(report.captchas) + 1,
            "outcome": outcome,
            "at": int(clock() * 1000),
            "attempts": [dict(a) for a in _ATTEMPTS.get(outcome, [])],
        })
        return outcome

    return solver


def run_captcha(name, tmp_path, *, env=None, run_id=42, seconds=0.0, calls=None, solver=None, **client_kwargs):
    fixture = load(name)
    clock = Clock()
    client = FakeClient(fixture, clock, **client_kwargs)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(exist_ok=True)
    report = CaptchaReport()
    result = login(
        client,
        dict(ENV, SADCAPTCHA_API_KEY=KEY) if env is None else env,
        clock=clock,
        sleep=make_sleep(clock, runs_dir, run_id, fixture.get("codes")),
        runs_dir=str(runs_dir),
        screenshots_dir=str(tmp_path / "shots"),
        run_id=run_id,
        check_only=False,
        captcha_report=report,
        solver=solver or fake_solver(fixture.get("solver_outcomes", []), seconds=seconds, calls=calls),
    )
    return result, client, report


def test_solver_constants():
    assert tiktok_login.SOLVER_BUDGET_S == 240.0
    assert tiktok_login.MAX_CAPTCHAS_PER_RUN == 3
    assert tiktok_login.DEADLINE_S == 180.0


@pytest.mark.parametrize("name", CAPTCHA_FIXTURES)
def test_captcha_fixture_outcome(name, tmp_path):
    fixture = load(name)
    expected = fixture["expected"]
    result, client, report = run_captcha(name, tmp_path)
    assert result.exit_code == 0
    assert (result.status, result.reason) == (expected["status"], expected["reason"])
    assert result.counters["attempted"] == expected["attempted"]
    assert result.counters["code_rounds"] == expected["code_rounds"]
    outcomes = fixture["solver_outcomes"]
    assert [c["outcome"] for c in report.captchas] == outcomes
    assert result.counters["captchas"] == len(outcomes)
    assert result.counters["captchas_solved"] == outcomes.count("solved")
    assert result.counters["captcha_tries"] == sum(len(c["attempts"]) for c in report.captchas)
    names = client.names()
    assert names.count("navigate") == 1
    assert names[-1] == "delete_tab"


def test_captcha_before_form_solved_types_the_form_after_the_solver(tmp_path):
    calls = []
    result, client, _ = run_captcha("captcha_before_form_solved", tmp_path, calls=calls)
    assert result.reason == "connected"
    assert calls and calls[0]["tab"] == "login-tab-1" and calls[0]["api_key"] == KEY
    typed = [c[1] for c in client.calls if c[0] == "type_text"]
    assert typed == [USERNAME_SELECTOR, PASSWORD_SELECTOR]


def test_captcha_four_times_logs_three_then_concludes(tmp_path):
    # Amendment 2: the ceiling is reached after a 3rd solved captcha => captcha_required (console cause `unsolved`).
    calls = []
    result, _, report = run_captcha("captcha_four_times", tmp_path, calls=calls)
    assert result.reason == "captcha_required"
    assert len(calls) == 3
    assert [c["outcome"] for c in report.captchas] == ["solved", "solved", "solved"]
    assert [c["seq"] for c in report.captchas] == [1, 2, 3]
    assert result.counters["captchas_solved"] == 3


def test_unsolved_captcha_takes_the_masked_screenshot(tmp_path):
    result, client, _ = run_captcha("form_then_captcha_unsolved", tmp_path)
    assert result.reason == "captcha_required"
    names = client.names()
    i = names.index("screenshot")
    assert client.calls[i - 1][:2] == ("evaluate_value", "mask")
    assert result.counters["screenshot_at"] > 0


@pytest.mark.parametrize("name", ["captcha_before_form", "form_then_captcha"])
def test_without_key_s21_scenarios_still_end_captcha_required_with_a_disabled_captcha(name, tmp_path):
    from app.captcha_solver import solve_captcha

    result, client, report = run_captcha(name, tmp_path, env=dict(ENV), solver=solve_captcha)
    assert (result.status, result.reason) == ("ok", "captcha_required")
    assert [c["outcome"] for c in report.captchas] == ["disabled"]
    assert report.captchas[0]["attempts"] == []
    assert "mouse_available" not in client.names()
    assert result.counters["captchas"] == 1 and result.counters["captcha_tries"] == 0


def test_with_a_key_but_no_plugin_the_captcha_is_unavailable(tmp_path):
    from app.captcha_solver import solve_captcha

    result, client, report = run_captcha("form_then_captcha", tmp_path, solver=solve_captcha)
    assert result.reason == "captcha_required"
    assert [c["outcome"] for c in report.captchas] == ["unavailable"]
    assert "mouse_available" in client.names()


def test_default_login_without_report_still_works(tmp_path):
    # s21 callers (no report given): the login keeps its outcome.
    result, _, _, _ = run("form_then_captcha", tmp_path)
    assert result.reason == "captcha_required"
    assert result.counters["captchas"] == 1


def test_solver_time_is_outside_the_deadline(tmp_path):
    result, _, _ = run_captcha("form_then_captcha_solved", tmp_path, seconds=200)
    assert result.exit_code == 0
    assert result.reason == "connected"


def test_solver_budget_is_shared_by_the_run(tmp_path):
    calls = []
    run_captcha("captcha_four_times", tmp_path, seconds=50, calls=calls)
    budgets = [c["deadline"] - c["now"] for c in calls]
    assert budgets == [240.0, 190.0, 140.0]


def test_solver_during_the_code_wait_does_not_double_pause(tmp_path):
    calls = []
    result, _, _ = run_captcha("code_then_captcha_solved", tmp_path, seconds=100, calls=calls)
    assert result.reason == "connected"
    assert result.exit_code == 0


def test_two_hundred_seconds_outside_the_solver_still_exit_14(tmp_path):
    clock_holder = {}

    def slow(detail):
        clock_holder["client"].clock.t += 200
        return None

    fixture = load("captcha_before_form_solved")
    clock = Clock()
    client = FakeClient(fixture, clock, raise_on={"type_text": slow})
    clock_holder["client"] = client
    report = CaptchaReport()
    result = login(client, dict(ENV, SADCAPTCHA_API_KEY=KEY), clock=clock, sleep=make_sleep(clock),
                   runs_dir=str(tmp_path), screenshots_dir=str(tmp_path / "s"), run_id=None, check_only=False,
                   captcha_report=report, solver=fake_solver(["solved"]))
    assert result.exit_code == 14
    assert len(report.captchas) == 1


def _run_main_captcha(monkeypatch, tmp_path, name, *, solver_outcomes=None, environ=None, **client_kwargs):
    fixture = load(name)
    client = FakeClient(fixture, Clock(), **client_kwargs)
    monkeypatch.setattr(tiktok_login, "solve_captcha", fake_solver(
        fixture.get("solver_outcomes", []) if solver_outcomes is None else solver_outcomes))
    return run_main(monkeypatch, tmp_path, client,
                    environ=dict(ENV, SADCAPTCHA_API_KEY=KEY) if environ is None else environ)


def _last_payload(text):
    lines = text.strip().splitlines()
    match = CONSOLE_RE.match(lines[-1])
    assert match, lines[-1]
    return lines, json.loads(match.group(1))


def test_main_ok_with_captchas_adds_the_report(monkeypatch, tmp_path):
    code, text = _run_main_captcha(monkeypatch, tmp_path, "form_then_captcha_solved")
    assert code == 0
    lines, payload = _last_payload(text)
    assert len(lines) == 2
    assert lines[0].startswith("tiktok-login: result=connected")
    assert payload["status"] == "ok" and payload["reason"] == "connected"
    assert payload["captchas"] == [{"seq": 1, "outcome": "solved", "at": payload["captchas"][0]["at"],
                                    "attempts": _ATTEMPTS["solved"]}]
    assert payload["counters"]["captchas"] == 1 and payload["counters"]["captchas_solved"] == 1
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in payload["counters"].values())


def test_main_without_captcha_has_no_captchas_key(monkeypatch, tmp_path):
    code, text = _run_main_captcha(monkeypatch, tmp_path, "form_then_connected")
    assert code == 0
    _, payload = _last_payload(text)
    assert "captchas" not in payload


def test_main_exit_14_after_a_captcha_ends_with_the_report(monkeypatch, tmp_path):
    clock = Clock()

    def slow(detail):
        clock.t += 200
        return None

    fixture = load("captcha_before_form_solved")
    client = FakeClient(fixture, clock, raise_on={"type_text": slow})
    monkeypatch.setattr(tiktok_login, "solve_captcha", fake_solver(["solved"]))
    code, text = run_main(monkeypatch, tmp_path, client, environ=dict(ENV, SADCAPTCHA_API_KEY=KEY))
    assert code == 14
    lines, payload = _last_payload(text)
    assert lines[0] == "tiktok-login: error=unexpected_response"
    assert payload["status"] == "ok" and payload["reason"] == "unexpected_response"
    assert [c["outcome"] for c in payload["captchas"]] == ["solved"]
    assert payload["counters"]["captchas"] == 1


def test_main_exit_16_after_a_captcha_ends_with_the_report(monkeypatch, tmp_path):
    code, text = _run_main_captcha(monkeypatch, tmp_path, "captcha_before_form_solved",
                                   raise_on={"type_text": RuntimeError("SECRETPASSWORD")})
    assert code == 16
    lines, payload = _last_payload(text)
    assert lines[0] == "tiktok-login: error=internal class=RuntimeError"
    assert payload["reason"] == "internal"
    assert [c["outcome"] for c in payload["captchas"]] == ["solved"]
    assert payload["counters"] == {"captchas": 1, "captchas_solved": 1, "captcha_tries": 1}
    assert "SECRETPASSWORD" not in text


def test_main_exit_15_without_captcha_is_unchanged(monkeypatch, tmp_path):
    code, text = _run_main_captcha(monkeypatch, tmp_path, "form_then_connected",
                                   tabs=[{"tabId": str(i), "url": ""} for i in range(9)])
    assert code == 15 and text.strip() == "tiktok-login: error=tabs_full"


@pytest.mark.parametrize(
    "fixture, kwargs, expected_code",
    [
        ("form_then_captcha_solved", {}, 0),
        ("form_then_captcha_unsolved", {}, 0),
        ("already_connected", {}, 0),
        ("captcha_before_form_solved", {"raise_on": {"list_tabs": CamofoxHTTPError(500)}}, 14),
        ("form_then_connected", {"tabs": [{"tabId": str(i), "url": ""} for i in range(9)]}, 15),
        ("captcha_before_form_solved", {"raise_on": {"type_text": RuntimeError(KEY)}}, 16),
    ],
)
def test_sadcaptcha_key_never_printed(monkeypatch, tmp_path, capsys, fixture, kwargs, expected_code):
    data = load(fixture)
    client = FakeClient(data, Clock(), **kwargs)
    monkeypatch.setattr(tiktok_login, "_build_client", lambda environ: client)
    monkeypatch.setattr(tiktok_login, "_clock", client.clock)
    monkeypatch.setattr(tiktok_login, "_sleep", make_sleep(client.clock))
    monkeypatch.setattr(tiktok_login, "SCREENSHOTS_DIR", str(tmp_path / "shots"))
    monkeypatch.setattr(tiktok_login, "RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(tiktok_login, "solve_captcha", fake_solver(data.get("solver_outcomes", [])))
    code = main([], dict(ENV, SADCAPTCHA_API_KEY=KEY, CONSOLE_RUN_ID="5"), sys.stdout)
    captured = capsys.readouterr()
    assert code == expected_code
    for secret in (KEY, "SECRETPASSWORD", "secretuser"):
        assert secret not in captured.out + captured.err


def test_sadcaptcha_key_never_printed_with_the_real_solver(monkeypatch, tmp_path, capsys):
    client = FakeClient(load("form_then_captcha"), Clock())
    code, text = run_main(monkeypatch, tmp_path, client, environ=dict(ENV, SADCAPTCHA_API_KEY=KEY))
    captured = capsys.readouterr()
    assert code == 0
    _, payload = _last_payload(text)
    assert [c["outcome"] for c in payload["captchas"]] == ["unavailable"]
    assert KEY not in text + captured.out + captured.err


def test_docstring_names_the_new_output_contract():
    assert "CONSOLE_RESULT only if captchas were met" in tiktok_login.__doc__
