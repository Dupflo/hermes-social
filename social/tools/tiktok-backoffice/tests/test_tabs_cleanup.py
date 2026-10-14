"""s26 (console ADR 060, D7/P6): closing of the orphan `tiktok-comments-fetch` tabs, against a local fake server.

Only the tabs whose `listItemId` is the legacy CLI session key are closed, never `console-*` nor the noVNC tab;
dry run by default, `--apply` to close, skipped while the legacy cron runs. One line of counts, never an URL, a
title or a tab id.
"""

from __future__ import annotations

import io
import socket

from test_camofox_client import server  # noqa: F401 - pytest fixture

from app import tabs_cleanup, tiktok_scan

TABS = {
    "running": True,
    "tabs": [
        {"tabId": "SENTINEL_TAB_1", "url": "https://www.tiktok.com/@SENTINEL_AUTHOR/video/1",
         "title": "SENTINEL_TITLE", "listItemId": "tiktok-comments-fetch"},
        {"tabId": "SENTINEL_TAB_2", "url": "https://www.tiktok.com/", "title": "t", "listItemId": "console-tiktok-scan"},
        {"tabId": "SENTINEL_TAB_3", "url": "https://www.tiktok.com/", "title": "t",
         "listItemId": "console-session-check"},
        {"tabId": "SENTINEL_TAB_4", "url": "http://localhost/browser", "title": "t"},
    ],
}
SENTINELS = ("SENTINEL_TAB", "SENTINEL_AUTHOR", "SENTINEL_TITLE", "tiktok.com", "localhost")


def run_main(base, argv, monkeypatch, *, running=False):
    monkeypatch.setattr(tiktok_scan, "legacy_cron_running", lambda *a, **k: running)
    out = io.StringIO()
    code = tabs_cleanup.main(argv, {"CAMOFOX_URL": base, "CAMOFOX_USER_ID": "hermes_u"}, out)
    return code, out.getvalue()


def deletes(recorder):
    return [r for r in recorder.requests if r["method"] == "DELETE"]


def assert_clean(text):
    assert len(text.splitlines()) == 1
    for sentinel in SENTINELS:
        assert sentinel not in text


def test_dry_run_closes_nothing(server, monkeypatch):  # noqa: F811
    recorder, base = server
    recorder.responses[("GET", "/tabs")] = (200, TABS)

    code, text = run_main(base, [], monkeypatch)

    assert code == 0
    assert deletes(recorder) == []
    assert text == "tabs-cleanup: mode=dry_run total=4 matched=1 closed=0 failed=0 reason=-\n"
    assert recorder.requests[0]["query"] == {"userId": ["hermes_u"]}
    assert_clean(text)


def test_apply_closes_only_the_legacy_cli_tabs(server, monkeypatch):  # noqa: F811
    recorder, base = server
    recorder.responses[("GET", "/tabs")] = (200, TABS)

    code, text = run_main(base, ["--apply"], monkeypatch)

    assert code == 0
    assert [(d["path"], d["query"]) for d in deletes(recorder)] == [("/tabs/SENTINEL_TAB_1", {"userId": ["hermes_u"]})]
    assert text == "tabs-cleanup: mode=apply total=4 matched=1 closed=1 failed=0 reason=-\n"
    assert_clean(text)


def test_apply_while_the_legacy_cron_runs_is_skipped(server, monkeypatch):  # noqa: F811
    recorder, base = server
    recorder.responses[("GET", "/tabs")] = (200, TABS)

    code, text = run_main(base, ["--apply"], monkeypatch, running=True)

    assert code == 0
    assert deletes(recorder) == []
    assert text.startswith("tabs-cleanup: mode=skipped ")
    assert text.rstrip().endswith(" closed=0 failed=0 reason=legacy_running")
    assert_clean(text)


def test_a_failed_delete_is_counted_and_the_others_are_closed(server, monkeypatch):  # noqa: F811
    recorder, base = server
    tabs = {"tabs": TABS["tabs"] + [{"tabId": "SENTINEL_TAB_5", "url": "u", "listItemId": "tiktok-comments-fetch"}]}
    recorder.responses[("GET", "/tabs")] = (200, tabs)
    recorder.responses[("DELETE", "/tabs/SENTINEL_TAB_1")] = (500, {"error": "boom"})

    code, text = run_main(base, ["--apply"], monkeypatch)

    assert code == 0
    assert [d["path"] for d in deletes(recorder)] == ["/tabs/SENTINEL_TAB_1", "/tabs/SENTINEL_TAB_5"]
    assert text == "tabs-cleanup: mode=apply total=5 matched=2 closed=1 failed=1 reason=-\n"
    assert_clean(text)


def test_camofox_down_is_exit_1(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    code, text = run_main(f"http://127.0.0.1:{port}", ["--apply"], monkeypatch)

    assert code == 1
    assert text == "tabs-cleanup: mode=apply total=0 matched=0 closed=0 failed=0 reason=camofox_unreachable\n"


def test_unreadable_tabs_payload_is_exit_1(server, monkeypatch):  # noqa: F811
    recorder, base = server
    recorder.responses[("GET", "/tabs")] = (200, {"nope": True})

    code, text = run_main(base, [], monkeypatch)

    assert code == 1
    assert text.rstrip().endswith("reason=camofox_unreachable")
    assert deletes(recorder) == []


def test_session_key_is_a_constant():
    assert tabs_cleanup.SESSION_KEY == "tiktok-comments-fetch"
