"""s20: TikTok session check run by the hermes-console job `tiktok-session-check` (console ADR 049)."""

from __future__ import annotations

import io
import json
import re

import pytest

from app import session_check
from app.camofox_reader import CamofoxHTTPError, CamofoxUnreachable
from app.session_check import (
    CAPTCHA_JS,
    check,
    is_tiktok_url,
    main,
    session_cookies_present,
)

NOW = 1_800_000_000.0
FUTURE = NOW + 86_400
PAST = NOW - 1

GOOD_COOKIES = [
    {"name": "sessionid", "value": "SECRETCOOKIEVALUE", "domain": ".tiktok.com", "expires": FUTURE},
    {"name": "sid_tt", "value": "SECRETCOOKIEVALUE", "domain": ".tiktok.com", "expires": FUTURE},
]


class FakeClient:
    """Records every call as (method, detail). Behaviours are injected per method."""

    def __init__(
        self,
        *,
        api_key="SECRETAPIKEY",
        health=None,
        tabs=None,
        captcha=None,
        storage=None,
        create=None,
        delete=None,
        evaluate_error=None,
    ):
        self.api_key = api_key
        self.calls: list[tuple] = []
        self._health = health
        self._tabs = [] if tabs is None else tabs
        self._captcha = captcha or {}
        self._storage = list(storage) if storage is not None else [{"cookies": GOOD_COOKIES, "origins": []}]
        self._create = create
        self._delete = delete
        self._evaluate_error = evaluate_error or {}

    def health(self, *, timeout=None):
        self.calls.append(("health", "GET", "/health"))
        if isinstance(self._health, BaseException):
            raise self._health
        return {"ok": True}

    def list_tabs(self, *, timeout=None):
        self.calls.append(("list_tabs", "GET", "/tabs"))
        if isinstance(self._tabs, BaseException):
            raise self._tabs
        return self._tabs

    def evaluate(self, *, tab_id, expression, timeout=None):
        self.calls.append(("evaluate", "POST", f"/tabs/{tab_id}/evaluate", expression, timeout))
        if tab_id in self._evaluate_error:
            raise self._evaluate_error[tab_id]
        return self._captcha.get(tab_id, False)

    def create_blank_tab(self, session_key, *, timeout=None):
        self.calls.append(("create_blank_tab", "POST", "/tabs", session_key))
        if isinstance(self._create, BaseException):
            raise self._create
        return "blank-1"

    def delete_tab(self, tab_id, *, timeout=None):
        self.calls.append(("delete_tab", "DELETE", f"/tabs/{tab_id}"))
        if isinstance(self._delete, BaseException):
            raise self._delete

    def storage_state(self, timeout=60):
        self.calls.append(("storage_state", "GET", "/sessions/x/storage_state", timeout))
        item = self._storage.pop(0) if len(self._storage) > 1 else self._storage[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def names(self):
        return [c[0] for c in self.calls]


def _tab(tab_id, url):
    return {"tabId": tab_id, "url": url}


# --- pure helpers -------------------------------------------------------------------------------


@pytest.mark.parametrize("domain", ["tiktok.com", ".tiktok.com", "www.tiktok.com"])
def test_session_cookies_present_positive_domains(domain):
    cookies = [{**c, "domain": domain} for c in GOOD_COOKIES]
    assert session_cookies_present(cookies, NOW) is True


def test_session_cookies_present_session_cookie_without_expiry():
    cookies = [{**c, "expires": -1} for c in GOOD_COOKIES]
    assert session_cookies_present(cookies, NOW) is True


@pytest.mark.parametrize(
    "cookies",
    [
        [{**GOOD_COOKIES[0], "expires": PAST}, GOOD_COOKIES[1]],
        [{**c, "domain": "nottiktok.com"} for c in GOOD_COOKIES],
        [{**c, "domain": ".nottiktok.com"} for c in GOOD_COOKIES],
        [{**GOOD_COOKIES[0], "value": ""}, GOOD_COOKIES[1]],
        [GOOD_COOKIES[0]],
        [GOOD_COOKIES[1]],
        [],
    ],
    ids=["expired", "neighbour-domain", "dot-neighbour-domain", "empty-value", "sessionid-only", "sid_tt-only", "none"],
)
def test_session_cookies_present_negative(cookies):
    assert session_cookies_present(cookies, NOW) is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.tiktok.com/@a/video/1", True),
        ("https://tiktok.com/", True),
        ("https://m.tiktok.com/x", True),
        ("https://nottiktok.com/", False),
        ("https://evil.com/?u=tiktok.com", False),
        ("https://tiktok.com.evil.com/", False),
        ("about:blank", False),
        ("", False),
    ],
)
def test_is_tiktok_url(url, expected):
    assert is_tiktok_url(url) is expected


def test_captcha_js_is_the_passive_selector_expression():
    assert CAPTCHA_JS == (
        "!!(document.querySelector('.captcha-verify-container')"
        "||document.querySelector('#captcha-verify-image'))"
    )


# --- check(): verdicts --------------------------------------------------------------------------


def test_connected_with_existing_tabs_opens_nothing():
    client = FakeClient(tabs=[_tab("t1", "https://www.tiktok.com/foryou")])
    result = check(client, now_s=NOW)
    assert (result.status, result.reason, result.exit_code) == ("ok", "connected", 0)
    assert result.counters == {
        "session_cookies": 1,
        "tiktok_tabs": 1,
        "captcha_tabs": 0,
        "opened_tab": 0,
        "tabs_unreadable": 0,
        "tab_close_failed": 0,
    }
    assert "create_blank_tab" not in client.names()
    assert "delete_tab" not in client.names()


def test_logged_out_when_cookies_missing():
    client = FakeClient(tabs=[_tab("t1", "about:blank")], storage=[{"cookies": [], "origins": []}])
    result = check(client, now_s=NOW)
    assert (result.status, result.reason) == ("ok", "logged_out")
    assert result.counters["session_cookies"] == 0


def test_captcha_wins_over_cookies():
    client = FakeClient(
        tabs=[_tab("t1", "https://www.tiktok.com/@a/video/1"), _tab("t2", "https://www.tiktok.com/")],
        captcha={"t2": True},
    )
    result = check(client, now_s=NOW)
    assert (result.status, result.reason) == ("ok", "captcha")
    assert result.counters["captcha_tabs"] == 1
    assert result.counters["session_cookies"] == 1


@pytest.mark.parametrize("error", [CamofoxUnreachable(), CamofoxHTTPError(503)])
def test_camofox_down_when_health_fails(error):
    client = FakeClient(health=error)
    result = check(client, now_s=NOW)
    assert (result.status, result.reason, result.exit_code) == ("ok", "camofox_down", 0)
    assert "session_cookies" not in result.counters
    assert client.names() == ["health"]


def test_camofox_down_when_unreachable_after_health():
    client = FakeClient(tabs=CamofoxUnreachable())
    result = check(client, now_s=NOW)
    assert (result.status, result.reason) == ("ok", "camofox_down")
    assert "session_cookies" not in result.counters


def test_missing_key_is_skipped_without_any_tab_call():
    client = FakeClient(api_key="")
    result = check(client, now_s=NOW)
    assert (result.status, result.reason, result.exit_code) == ("skipped", "camofox_auth_missing", 0)
    assert client.names() == ["health"]
    assert "session_cookies" not in result.counters


def test_rejected_key_is_skipped_and_tab_closed():
    client = FakeClient(storage=[CamofoxHTTPError(403)])
    result = check(client, now_s=NOW)
    assert (result.status, result.reason, result.exit_code) == ("skipped", "camofox_auth_rejected", 0)
    assert "session_cookies" not in result.counters
    assert client.names()[-1] == "delete_tab"


def test_other_storage_error_is_exit_11():
    client = FakeClient(storage=[CamofoxHTTPError(500)])
    result = check(client, now_s=NOW)
    assert result.exit_code == 11


def test_tabs_full_is_exit_12():
    client = FakeClient(create=CamofoxHTTPError(429))
    result = check(client, now_s=NOW)
    assert result.exit_code == 12


@pytest.mark.parametrize("bad", [ValueError("no tabId"), CamofoxHTTPError(500)])
def test_create_tab_unexpected_is_exit_11(bad):
    client = FakeClient(create=bad)
    assert check(client, now_s=NOW).exit_code == 11


def test_invalid_storage_payload_is_exit_11():
    client = FakeClient(storage=[{"cookies": "nope"}])
    assert check(client, now_s=NOW).exit_code == 11


# --- check(): order and side effects ------------------------------------------------------------


def test_health_comes_first_and_blank_tab_opened_only_without_tabs():
    client = FakeClient(tabs=[])
    result = check(client, now_s=NOW)
    assert client.names() == ["health", "list_tabs", "create_blank_tab", "storage_state", "delete_tab"]
    assert client.calls[2][3] == "console-session-check"
    assert client.calls[-1][2] == "/tabs/blank-1"
    assert result.counters["opened_tab"] == 1


def test_opened_tab_closed_even_when_storage_raises():
    client = FakeClient(tabs=[], storage=[RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        check(client, now_s=NOW)
    assert client.names()[-1] == "delete_tab"


def test_close_failure_is_counted_and_keeps_verdict():
    client = FakeClient(tabs=[], delete=CamofoxHTTPError(500))
    result = check(client, now_s=NOW)
    assert (result.status, result.reason) == ("ok", "connected")
    assert result.counters["tab_close_failed"] == 1


def test_storage_404_retries_once_with_a_blank_tab():
    client = FakeClient(
        tabs=[_tab("t1", "https://www.tiktok.com/")],
        storage=[CamofoxHTTPError(404), {"cookies": GOOD_COOKIES, "origins": []}],
    )
    result = check(client, now_s=NOW)
    assert (result.status, result.reason) == ("ok", "connected")
    assert client.names().count("create_blank_tab") == 1
    assert client.names().count("storage_state") == 2
    assert client.names()[-1] == "delete_tab"
    assert result.counters["opened_tab"] == 1


def test_storage_404_twice_is_exit_11_and_tab_closed():
    client = FakeClient(tabs=[], storage=[CamofoxHTTPError(404), CamofoxHTTPError(404)])
    result = check(client, now_s=NOW)
    assert result.exit_code == 11
    assert client.names().count("create_blank_tab") == 1
    assert client.names().count("storage_state") == 2
    assert client.names()[-1] == "delete_tab"


def test_deadline_exceeded_is_exit_11():
    ticks = iter([0.0, 0.0, 0.0, 95.0, 95.0, 95.0, 95.0])

    def clock():
        return next(ticks, 95.0)

    client = FakeClient(tabs=[_tab("t1", "https://www.tiktok.com/")])
    result = check(client, now_s=NOW, clock=clock)
    assert result.exit_code == 11
    assert "storage_state" not in client.names()


def test_storage_timeout_bounded_by_remaining_time():
    ticks = iter([0.0, 50.0])

    def clock():
        return next(ticks, 50.0)

    client = FakeClient(tabs=[_tab("t1", "about:blank")])
    check(client, now_s=NOW, clock=clock)
    timeout = [c for c in client.calls if c[0] == "storage_state"][0][3]
    assert 0 < timeout <= 40


def test_evaluate_only_on_tiktok_tabs_with_exact_expression_and_10s():
    client = FakeClient(tabs=[
        _tab("t1", "https://www.tiktok.com/"),
        _tab("t2", "https://example.com/"),
        _tab("t3", "about:blank"),
        _tab("t4", "https://m.tiktok.com/x"),
    ])
    result = check(client, now_s=NOW)
    evaluated = [c for c in client.calls if c[0] == "evaluate"]
    assert [c[2] for c in evaluated] == ["/tabs/t1/evaluate", "/tabs/t4/evaluate"]
    assert all(c[3] == CAPTCHA_JS and c[4] == 10 for c in evaluated)
    assert result.counters["tiktok_tabs"] == 2


def test_unreadable_tab_is_counted_not_fatal():
    client = FakeClient(
        tabs=[_tab("t1", "https://www.tiktok.com/"), _tab("t2", "https://www.tiktok.com/x")],
        evaluate_error={"t1": RuntimeError("slow")},
    )
    result = check(client, now_s=NOW)
    assert (result.status, result.reason) == ("ok", "connected")
    assert result.counters["tabs_unreadable"] == 1


def test_never_navigates_clicks_or_deletes_sessions():
    scenarios = [
        FakeClient(tabs=[]),
        FakeClient(tabs=[_tab("t1", "https://www.tiktok.com/")], captcha={"t1": True}),
        FakeClient(tabs=[], storage=[CamofoxHTTPError(404), CamofoxHTTPError(404)]),
        FakeClient(storage=[CamofoxHTTPError(403)]),
    ]
    for client in scenarios:
        check(client, now_s=NOW)
        for call in client.calls:
            assert call[0] not in {"click", "create_tab", "navigate"}
            assert not (call[1] == "DELETE" and call[2].startswith("/sessions/"))
            if call[0] == "evaluate":
                assert call[3] == CAPTCHA_JS


def test_source_has_no_forbidden_camofox_calls():
    source = open(session_check.__file__, encoding="utf-8").read()
    for forbidden in ("click", "location.", "create_tab(", "url=", "/sessions/", "DELETE"):
        assert forbidden not in source


# --- main(): output contract and hygiene --------------------------------------------------------

CONSOLE_RESULT_REASON = re.compile(r"^[a-z0-9_]{1,64}$")


def _run_main(monkeypatch, client):
    monkeypatch.setattr(session_check, "_build_client", lambda environ: client)
    out = io.StringIO()
    code = main([], environ={}, out=out)
    return code, out.getvalue()


def _console_result(text):
    last = [line for line in text.splitlines() if line.strip()][-1]
    assert last.startswith("CONSOLE_RESULT ")
    payload = json.loads(last[len("CONSOLE_RESULT "):])
    assert payload["status"] in {"ok", "empty", "skipped"}
    assert CONSOLE_RESULT_REASON.match(payload["reason"])
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in payload["counters"].values())
    return payload


def test_main_ok_prints_state_line_then_console_result(monkeypatch):
    code, text = _run_main(monkeypatch, FakeClient(tabs=[]))
    assert code == 0
    lines = text.splitlines()
    assert lines[-2] == "tiktok-session-check: state=connected session_cookies=1 tabs=0 opened_tab=1"
    payload = _console_result(text)
    assert payload["status"] == "ok" and payload["reason"] == "connected"
    assert payload["counters"]["session_cookies"] == 1


def test_main_skipped(monkeypatch):
    code, text = _run_main(monkeypatch, FakeClient(api_key=""))
    assert code == 0
    payload = _console_result(text)
    assert (payload["status"], payload["reason"]) == ("skipped", "camofox_auth_missing")


@pytest.mark.parametrize(
    ("client", "code"),
    [
        (FakeClient(storage=[CamofoxHTTPError(500)]), 11),
        (FakeClient(create=CamofoxHTTPError(429)), 12),
    ],
)
def test_main_error_codes_print_no_console_result(monkeypatch, client, code):
    exit_code, text = _run_main(monkeypatch, client)
    assert exit_code == code
    assert "CONSOLE_RESULT" not in text
    assert text.strip().startswith("tiktok-session-check: error=")


def test_main_internal_error_prints_class_only(monkeypatch):
    client = FakeClient(storage=[RuntimeError("SECRETCOOKIEVALUE boom")])
    exit_code, text = _run_main(monkeypatch, client)
    assert exit_code == 13
    assert text.strip() == "tiktok-session-check: error=internal class=RuntimeError"


def test_main_does_not_swallow_keyboard_interrupt(monkeypatch):
    client = FakeClient(storage=[KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        _run_main(monkeypatch, client)


@pytest.mark.parametrize(
    "client",
    [
        FakeClient(tabs=[]),
        FakeClient(api_key=""),
        FakeClient(storage=[CamofoxHTTPError(403)]),
        FakeClient(storage=[CamofoxHTTPError(500)]),
        FakeClient(create=CamofoxHTTPError(429)),
        FakeClient(storage=[RuntimeError("SECRETCOOKIEVALUE sessionid=SECRETCOOKIEVALUE")]),
        FakeClient(tabs=[_tab("t1", "https://www.tiktok.com/")], evaluate_error={"t1": RuntimeError("SECRETCOOKIEVALUE")}),
        FakeClient(tabs=[], delete=RuntimeError("SECRETCOOKIEVALUE SECRETAPIKEY")),
    ],
    ids=["ok", "skipped-missing", "skipped-rejected", "exit11", "exit12", "exit13", "unreadable-tab", "close-failed"],
)
def test_hygiene_no_cookie_value_nor_key_on_any_path(monkeypatch, capsys, client):
    _, text = _run_main(monkeypatch, client)
    captured = capsys.readouterr()
    for blob in (text, captured.out, captured.err):
        assert "SECRETCOOKIEVALUE" not in blob
        assert "SECRETAPIKEY" not in blob


def test_build_client_reads_environment():
    client = session_check._build_client({"CAMOFOX_API_KEY": "k", "CAMOFOX_USER_ID": "hermes_z"})
    assert client.api_key == "k" and client.user_id == "hermes_z"
