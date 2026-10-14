"""s24 — page gestures of the job `tiktok-reply` (console ADR 055: D4, D9, P8, P9; AC 2, 8).

Pure functions on synthetic DOM snapshots (tests/fixtures/tiktok_reply/*.json, shapes of the read-only scripts of
app/tiktok_reply_selectors.py), and TikTokReplyPage on a fake Camofox client. No network, no Camofox.
"""

import json
import re
from pathlib import Path

import pytest

from app import tiktok_reply_page as trp
from app import tiktok_reply_selectors as sel
from app.camofox_reader import CamofoxHTTPError, CamofoxUnreachable
from app.captcha_selectors import PRESENT_JS

FIXTURES = Path(__file__).parent / "fixtures" / "tiktok_reply"
APP = Path(__file__).resolve().parents[1] / "app"
OWNER = "@SENTINEL_OWNER"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- pure functions -------------------------------------------------------------------------------------------------


def test_normalize():
    assert trp.normalize("  Salut !\n\nVoici le  lien\t") == "Salut ! Voici le lien"
    assert trp.normalize("é") == "é"  # NFC
    assert trp.normalize("Je viens de t’envoyer") == "Je viens de t’envoyer"  # typographic apostrophe kept
    assert trp.normalize("t’envoyer") != trp.normalize("t'envoyer")
    assert trp.normalize(None) == ""


COMMENT_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.json") if "snapshot" in json.loads(p.read_text("utf-8")))
THREAD_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.json") if "thread" in json.loads(p.read_text("utf-8")))


def test_fixture_sets_are_not_empty():
    assert "owner_already_replied.json" in COMMENT_FIXTURES
    assert "dm_already_sent.json" in THREAD_FIXTURES


@pytest.mark.parametrize("name", COMMENT_FIXTURES)
def test_comment_fixtures(name):
    f = fixture(name)
    target = trp.find_target(f["snapshot"], f["target"]["author"], f["target"]["text"])
    expected = f["expected"]
    assert (target is not None) is expected["target_found"]
    if target is None:
        return
    assert target["author"].lower() == f["target"]["author"].lower()
    assert trp.owner_reply_present(target, f["owner"]) is expected["owner_reply_present"]
    reply = f.get("reply_text", "Je viens de t’envoyer le lien en message privé")
    assert trp.reply_verified(target, f["owner"], reply) is expected["reply_verified"]


def test_owner_reply_present_whatever_its_text_and_ignores_handle_case():
    target = {"replies": [{"author": "@sentinel_OWNER", "text": "coucou"}]}
    assert trp.owner_reply_present(target, OWNER) is True
    assert trp.owner_reply_present({"replies": [{"author": "@other", "text": "x"}]}, OWNER) is False
    assert trp.owner_reply_present({"replies": []}, OWNER) is False


def test_find_target_by_contained_text_when_not_exact():
    snap = {"comments": [{"author": "@A", "text": "Graphify stp merci", "replies": []}]}
    assert trp.find_target(snap, "@a", "graphify stp") is not None
    assert trp.find_target(snap, "@b", "graphify stp") is None


@pytest.mark.parametrize("name", THREAD_FIXTURES)
def test_thread_fixtures(name):
    f = fixture(name)
    assert trp.dm_present(f["thread"], f["dm_text"]) is f["expected"]["dm_present"]
    assert trp.dm_verified(f["thread"], f["dm_text"]) is f["expected"]["dm_verified"]


def test_dm_verified_needs_an_editor():
    thread = {"editor": None, "outgoing": ["hello"], "error": False}
    assert trp.dm_verified(thread, "hello") is False


def test_dm_verified_refuses_a_send_error():
    thread = {"editor": {"text": ""}, "outgoing": ["hello"], "error": True}
    assert trp.dm_verified(thread, "hello") is False


# --- fake Camofox ---------------------------------------------------------------------------------------------------


class FakeClient:
    def __init__(self, *, tabs=0, answers=None, mouse=False, click_error=None):
        self.calls = []
        self.tabs = tabs
        self.answers = answers or {}
        self.mouse_ok = mouse
        self.click_error = click_error
        self.next_tab = 0
        self.expressions = []

    def list_tabs(self, *, timeout=None):
        self.calls.append(("list_tabs",))
        return [{"tabId": str(i), "url": ""} for i in range(self.tabs)]

    def create_blank_tab(self, session_key, *, timeout=None):
        self.next_tab += 1
        self.tabs += 1
        self.calls.append(("create_blank_tab", session_key))
        return f"t{self.next_tab}"

    def navigate(self, tab, url, *, timeout=35):
        self.calls.append(("navigate", tab, url))

    def delete_tab(self, tab, *, timeout=None):
        self.tabs -= 1
        self.calls.append(("delete_tab", tab))

    def evaluate_value(self, tab, expression, *, timeout=None):
        self.expressions.append(expression)
        for key, value in self.answers.items():
            if key(expression):
                result = value.pop(0) if isinstance(value, list) else value
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError("unexpected expression")

    def type_text(self, tab, selector, text, *, delay_ms=60, timeout=None):
        self.calls.append(("type_text", selector, text))

    def press(self, tab, key, *, timeout=None):
        self.calls.append(("press", key))

    def click_selector(self, tab, selector, *, timeout=None):
        self.calls.append(("click_selector", selector))
        if self.click_error is not None:
            raise self.click_error

    def mouse_available(self, *, timeout=5.0):
        return self.mouse_ok

    def mouse(self, tab, actions, *, hold=False, timeout=25.0):
        self.calls.append(("mouse", tuple((a["type"], a.get("x"), a.get("y")) for a in actions)))
        return len(actions)


def is_comments(expr):
    return "const OWNER=" in expr


def is_thread(expr):
    return expr == sel.THREAD_JS


def is_profile(expr):
    return expr == sel.PROFILE_JS


def is_reply_editor(expr):
    return expr == sel.REPLY_EDITOR_JS


def page(client):
    return trp.TikTokReplyPage(client, owner=OWNER, sleep=lambda s: None)


def handle(selector, x=10, y=20):
    return {"selector": selector, "x": x, "y": y}


# --- typing (P8) ----------------------------------------------------------------------------------------------------


def test_multiline_typing_never_presses_enter():
    thread = {"login_gate": False, "editor": dict(handle("ED"), text=""), "outgoing": [], "send_button": None, "error": False}
    client = FakeClient(answers={is_thread: thread})
    page(client).type_dm("t1", "L1\nL2\n\nL4")
    assert [c for c in client.calls if c[0] in ("type_text", "press")] == [
        ("type_text", "ED", "L1"),
        ("press", "Shift+Enter"),
        ("type_text", "ED", "L2"),
        ("press", "Shift+Enter"),
        ("press", "Shift+Enter"),
        ("type_text", "ED", "L4"),
    ]


def test_crlf_is_normalized_and_no_typed_text_holds_a_newline():
    thread = {"login_gate": False, "editor": dict(handle("ED"), text=""), "outgoing": [], "send_button": None, "error": False}
    client = FakeClient(answers={is_thread: thread})
    page(client).type_dm("t1", "A\r\nB\rC")
    typed = [c[2] for c in client.calls if c[0] == "type_text"]
    assert typed == ["A", "B", "C"]
    assert not any(c == ("press", "Enter") for c in client.calls)


def test_type_dm_without_editor_is_page_unusable():
    client = FakeClient(answers={is_thread: {"login_gate": False, "editor": None, "outgoing": [], "send_button": None}})
    with pytest.raises(trp.PageUnusable):
        page(client).type_dm("t1", "x")
    assert not any(c[0] == "type_text" for c in client.calls)


# --- tabs -----------------------------------------------------------------------------------------------------------


def test_max_tabs_reached_opens_nothing():
    client = FakeClient(tabs=trp.MAX_TABS)
    with pytest.raises(trp.TabsFull):
        page(client).open_video("https://www.tiktok.com/@x/video/1")
    assert not any(c[0] == "create_blank_tab" for c in client.calls)


def test_open_video_uses_the_session_key_and_closes_everything_even_on_error():
    client = FakeClient(answers={is_comments: RuntimeError("boom")})
    p = page(client)
    tab = p.open_video("https://www.tiktok.com/@x/video/1")
    assert ("create_blank_tab", "console-tiktok-reply") in client.calls
    assert ("navigate", tab, "https://www.tiktok.com/@x/video/1") in client.calls
    with pytest.raises(RuntimeError):
        try:
            p.comment_snapshot(tab, author="@a", text="t")
        finally:
            p.close_all()
    assert ("delete_tab", tab) in client.calls
    assert p.open_tabs == []


def test_close_all_closes_each_opened_tab_once_and_swallows_errors():
    client = FakeClient()
    p = page(client)
    a = p.open_video("https://www.tiktok.com/@x/video/1")
    b = p.open_tab("https://www.tiktok.com/@y")
    real = client.delete_tab

    def flaky(tab, *, timeout=None):
        real(tab, timeout=timeout)
        if tab == a:
            raise CamofoxUnreachable()

    client.delete_tab = flaky
    p.close_all()
    assert [c for c in client.calls if c[0] == "delete_tab"] == [("delete_tab", a), ("delete_tab", b)]
    p.close_all()
    assert len([c for c in client.calls if c[0] == "delete_tab"]) == 2


# --- comments snapshot ----------------------------------------------------------------------------------------------


def test_owner_comes_from_the_constructor():
    snap = {"login_gate": False, "panel_open": True, "comment_icon": None, "comments": []}
    client = FakeClient(answers={is_comments: snap})
    page(client).comment_snapshot("t1", author="@a", text="x")
    assert any(json.dumps(OWNER) in e for e in client.expressions)
    for module in ("tiktok_reply_page.py", "tiktok_reply_selectors.py"):
        assert "dupflodev" not in (APP / module).read_text(encoding="utf-8")


def test_closed_panel_is_opened_then_replies_are_expanded():
    closed = {"login_gate": False, "panel_open": False, "comment_icon": handle("ICON"), "comments": []}
    target = {"author": "@a", "text": "x", "reply_button": handle("RB"), "view_replies": [handle("V1"), handle("V2"), handle("V3")], "replies": []}
    opened = {"login_gate": False, "panel_open": True, "comment_icon": None, "comments": [target]}
    expanded = {"login_gate": False, "panel_open": True, "comment_icon": None,
                "comments": [dict(target, view_replies=[], replies=[{"author": OWNER, "text": "ok"}])]}
    client = FakeClient(answers={is_comments: [closed, opened, expanded]})
    snap = page(client).comment_snapshot("t1", author="@a", text="x")
    clicks = [c[1] for c in client.calls if c[0] == "click_selector"]
    assert clicks == ["ICON", "V1", "V2"]
    assert snap is expanded


def test_login_gate_raises():
    client = FakeClient(answers={is_comments: {"login_gate": True, "panel_open": False, "comment_icon": None, "comments": []}})
    with pytest.raises(trp.LoginGate):
        page(client).comment_snapshot("t1", author="@a", text="x")


def test_captcha_present_uses_the_s22_probe():
    client = FakeClient(answers={(lambda e: e == PRESENT_JS): True})
    assert page(client).captcha_present("t1") is True


# --- reply ----------------------------------------------------------------------------------------------------------


def test_post_reply_clicks_reply_types_and_posts():
    target = {"author": "@a", "text": "x", "reply_button": handle("RB"), "view_replies": [], "replies": []}
    editor = {"login_gate": False, "editor": dict(handle("RED"), text=""), "post_button": handle("POST")}
    client = FakeClient(answers={is_reply_editor: editor})
    page(client).post_reply("t1", target, "Merci !")
    acts = [c for c in client.calls if c[0] in ("click_selector", "type_text", "press")]
    assert acts == [("click_selector", "RB"), ("type_text", "RED", "Merci !"), ("click_selector", "POST")]


def test_post_reply_without_reply_button_or_editor_is_page_unusable_before_any_typing():
    p = page(FakeClient())
    with pytest.raises(trp.PageUnusable):
        p.post_reply("t1", {"reply_button": None}, "x")
    client = FakeClient(answers={is_reply_editor: {"login_gate": False, "editor": None, "post_button": None}})
    with pytest.raises(trp.PageUnusable):
        page(client).post_reply("t1", {"reply_button": handle("RB")}, "x")
    assert not any(c[0] == "type_text" for c in client.calls)


def test_post_click_error_is_send_uncertain():
    editor = {"login_gate": False, "editor": dict(handle("RED"), text=""), "post_button": handle("POST")}

    class Client(FakeClient):
        def click_selector(self, tab, selector, *, timeout=None):
            self.calls.append(("click_selector", selector))
            if selector == "POST":
                raise CamofoxUnreachable()

    client = Client(answers={is_reply_editor: editor})
    with pytest.raises(trp.SendUncertain):
        page(client).post_reply("t1", {"reply_button": handle("RB")}, "x")


def test_click_falls_back_to_the_mouse_plugin_for_non_send_clicks():
    client = FakeClient(mouse=True, click_error=CamofoxHTTPError(500))
    target = {"author": "@a", "text": "x", "reply_button": handle("RB", 5, 6), "view_replies": [], "replies": []}
    client.answers = {is_reply_editor: {"login_gate": False, "editor": None, "post_button": None}}
    with pytest.raises(trp.PageUnusable):
        page(client).post_reply("t1", target, "x")
    assert ("mouse", (("move", 5, 6), ("down", None, None), ("up", None, None))) in client.calls


def test_send_click_is_never_retried():
    thread = {"login_gate": False, "editor": dict(handle("ED"), text="x"), "outgoing": [], "send_button": handle("SEND"), "error": False}
    client = FakeClient(answers={is_thread: thread}, mouse=True, click_error=CamofoxHTTPError(500))
    with pytest.raises(trp.SendUncertain):
        page(client).send_dm("t1")
    assert [c for c in client.calls if c[0] in ("click_selector", "mouse")] == [("click_selector", "SEND")]


def test_send_dm_without_button_is_page_unusable():
    thread = {"login_gate": False, "editor": dict(handle("ED"), text="x"), "outgoing": [], "send_button": None, "error": False}
    with pytest.raises(trp.PageUnusable):
        page(FakeClient(answers={is_thread: thread})).send_dm("t1")


# --- DM -------------------------------------------------------------------------------------------------------------


def test_open_dm_clicks_the_message_button():
    client = FakeClient(answers={is_profile: {"login_gate": False, "message_button": handle("MSG")}})
    tab = page(client).open_dm("@fan")
    assert ("navigate", tab, "https://www.tiktok.com/@fan") in client.calls
    assert ("click_selector", "MSG") in client.calls


def test_open_dm_without_message_button_is_dm_unavailable():
    client = FakeClient(answers={is_profile: {"login_gate": False, "message_button": None}})
    with pytest.raises(trp.DmUnavailable):
        page(client).open_dm("@fan")


def test_open_dm_login_gate():
    client = FakeClient(answers={is_profile: {"login_gate": True, "message_button": handle("MSG")}})
    with pytest.raises(trp.LoginGate):
        page(client).open_dm("@fan")


# --- source-level interdicts (D4, D9, P8) ---------------------------------------------------------------------------


def test_no_like_no_clipboard_no_enter_in_reply_modules():
    for module in ("tiktok_reply_page.py", "tiktok_reply_selectors.py"):
        text = (APP / module).read_text(encoding="utf-8")
        for needle in ("subprocess", "docker", "xclip", "xdotool", "execCommand", "digg", "dispatchEvent"):
            assert needle not in text, (module, needle)
        assert re.search(r"\blike", text, re.I) is None, module
        assert re.search(r"press\([^)]*[\"']Enter[\"']", text) is None, module
