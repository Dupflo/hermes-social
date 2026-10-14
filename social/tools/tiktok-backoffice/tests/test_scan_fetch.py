"""s23 — fetch_comments_from_camofox, scan options (console ADR 054, D5/P2/P3).

A captcha met during the scan goes to `on_captcha` in the current tab, never after a reload; the tab is closed
when asked; `expired()` bounds the video. With the defaults, the CLI / legacy cron path is unchanged.
Scripted fake client only: no network.
"""

import pytest

from app import camofox_reader
from app.camofox_reader import (
    _ACTIVATE_COMMENTS_TAB_JS,
    _COMMENT_EXTRACTION_JS,
    CamofoxUnreachable,
    fetch_comments_from_camofox,
)
from app.captcha_selectors import PRESENT_JS

VIDEO = "https://www.tiktok.com/@dupflodev/video/7657941429938949399"


def dom(*, comments=(("@alice", "proxy stp", "c1"),), captcha=False, url=VIDEO):
    return {
        "url": url,
        "title": "Proxy | TikTok",
        "logged_in": True,
        "captcha": captcha,
        "body_preview": "dupflodev · 7-2 Commente PROXY",
        "active_right_panel_preview": "",
        "comment_nodes": [{"author": a, "text": t, "comment_id": c} for a, t, c in comments],
    }


INCOHERENT = dom(url="https://www.tiktok.com/@someone/video/1", comments=())


class FakeClient:
    def __init__(self, *, present=(), doms=(dom(),), create_error=None, delete_error=None):
        self.log = []
        self.present = list(present)
        self.doms = list(doms)
        self.create_error = create_error
        self.delete_error = delete_error

    def create_tab(self, *, session_key, url):
        self.log.append(("create_tab", session_key))
        if self.create_error is not None:
            raise self.create_error
        return "tab-1"

    def evaluate(self, *, tab_id, expression, timeout=None):
        assert tab_id == "tab-1"
        if expression == PRESENT_JS:
            self.log.append(("present",))
            return self.present.pop(0) if self.present else False
        if expression == _ACTIVATE_COMMENTS_TAB_JS:
            self.log.append(("activate",))
            return {"comment_icon_candidates": []}
        if expression == _COMMENT_EXTRACTION_JS:
            self.log.append(("extract",))
            return self.doms.pop(0) if len(self.doms) > 1 else self.doms[0]
        if expression.startswith("location.replace("):
            self.log.append(("reload",))
            return True
        raise AssertionError("unexpected expression")

    def delete_tab(self, tab_id, *, timeout=None):
        self.log.append(("delete_tab", tab_id, timeout))
        if self.delete_error is not None:
            raise self.delete_error

    def names(self):
        return [entry[0] for entry in self.log]


def no_sleep(_seconds):
    return None


class Solver:
    def __init__(self, client, answers):
        self.client = client
        self.answers = list(answers)

    def __call__(self, tab_id):
        self.client.log.append(("on_captcha", tab_id))
        return self.answers.pop(0)


def scan(client, **kwargs):
    kwargs.setdefault("sleep", no_sleep)
    kwargs.setdefault("close_tab", True)
    return fetch_comments_from_camofox(video_url=VIDEO, client=client, session_key="console-tiktok-scan", **kwargs)


def test_defaults_unchanged_no_probe_no_close_and_regex_captcha(monkeypatch, capsys):
    fake = FakeClient(doms=(dom(captcha=True),))
    monkeypatch.setattr(camofox_reader.CamofoxClient, "from_env", staticmethod(lambda environ=None: fake))
    monkeypatch.setattr(camofox_reader.time, "sleep", no_sleep)

    result = fetch_comments_from_camofox(video_url=VIDEO)

    assert result["ok"] is False and result["error"] == "captcha_required"
    assert "present" not in fake.names()
    assert "delete_tab" not in fake.names()
    assert fake.log[0] == ("create_tab", "tiktok-comments-fetch")
    assert capsys.readouterr() == ("", "")


def test_captcha_after_navigation_is_solved_in_the_same_tab(capsys):
    fake = FakeClient(present=[True, False])
    solver = Solver(fake, [True])

    result = scan(fake, on_captcha=solver)

    assert result["ok"] is True
    assert result["captcha"] is False
    assert result["comments"] == [{"id": "c1", "author": "@alice", "text": "proxy stp"}]
    names = fake.names()
    assert names[:4] == ["create_tab", "present", "on_captcha", "activate"]
    assert "reload" not in names
    assert names[-1] == "delete_tab"
    assert capsys.readouterr() == ("", "")


def test_captcha_before_a_reload_is_handed_over_first_and_skips_that_reload():
    fake = FakeClient(present=[False, True, False], doms=(INCOHERENT, dom()))
    solver = Solver(fake, [True])

    result = scan(fake, on_captcha=solver)

    assert result["ok"] is True
    names = fake.names()
    assert "reload" not in names  # the solver ran where the reload would have been
    assert names == [
        "create_tab", "present", "activate", "extract", "present", "on_captcha",
        "present", "activate", "extract", "delete_tab",
    ]


def test_unsolved_captcha_stops_without_reload_and_closes_the_tab():
    fake = FakeClient(present=[True])
    solver = Solver(fake, [False])

    result = scan(fake, on_captcha=solver)

    assert result == {"ok": False, "error": "captcha_unsolved", "comments": []}
    assert "reload" not in fake.names()
    assert "activate" not in fake.names()
    assert fake.log[-1] == ("delete_tab", "tab-1", 10)


def test_verify_in_a_comment_is_not_a_captcha_when_the_selectors_say_none():
    fake = FakeClient(present=[False], doms=(dom(comments=(("@bob", "Please verify this", "c9"),), captcha=True),))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert result["captcha"] is False
    assert result["comments"] == [{"id": "c9", "author": "@bob", "text": "Please verify this"}]


def test_expired_after_the_first_attempt_is_a_video_timeout():
    fake = FakeClient(doms=(INCOHERENT, dom()))
    calls = []

    def expired():
        calls.append(1)
        return len(calls) > 1

    result = scan(fake, on_captcha=Solver(fake, []), expired=expired)

    assert result["ok"] is False and result["error"] == "video_timeout"
    assert "reload" not in fake.names()
    assert fake.names().count("extract") == 1
    assert fake.log[-1] == ("delete_tab", "tab-1", 10)


def test_delete_tab_failure_is_swallowed():
    fake = FakeClient(delete_error=CamofoxUnreachable())

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert fake.names()[-1] == "delete_tab"


def test_create_tab_failure_never_deletes_a_tab():
    fake = FakeClient(create_error=CamofoxUnreachable())

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is False and result["error"] == "camofox_unreachable"
    assert "delete_tab" not in fake.names()


@pytest.mark.parametrize("close_tab", [False, True])
def test_close_tab_flag_alone_decides_the_delete(close_tab):
    fake = FakeClient()

    scan(fake, close_tab=close_tab)

    assert ("delete_tab" in fake.names()) is close_tab
    assert "present" not in fake.names()
