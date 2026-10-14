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
    if "dm_already_there" in f["expected"]:  # s30 (R1): the guard before typing
        assert trp.dm_already_there(f["thread"], f["dm_text"]) is f["expected"]["dm_already_there"]


def test_s30_thread_fixtures_are_present():
    for name in ("dm_sent_conversation_only.json", "dm_send_not_confirmed.json", "dm_conversation_already_holds_it.json",
                 "dm_text_left_in_editor.json"):
        assert name in THREAD_FIXTURES
        assert "dm_already_there" in fixture(name)["expected"]


# --- s30 (console ADR 064, A2, R1): the conversation column as a second signal ---


def test_conv_count_counts_the_normalized_text():
    thread = {"conversation": "a Salut ! Voici le lien b  Salut !\nVoici   le lien"}
    assert trp.conv_count(thread, "Salut !\nVoici le lien") == 2
    assert trp.conv_count(thread, "absent") == 0
    assert trp.conv_count({}, "Salut") == 0
    assert trp.conv_count({"conversation": None}, "Salut") == 0
    assert trp.conv_count({"conversation": "x"}, "") == 0


def test_dm_already_there_is_a_bubble_or_the_conversation():
    assert trp.dm_already_there({"outgoing": ["hello"], "conversation": ""}, "hello") is True
    assert trp.dm_already_there({"outgoing": [], "conversation": "x hello y"}, "hello") is True
    assert trp.dm_already_there({"outgoing": [], "conversation": "x"}, "hello") is False


def test_dm_verified_by_the_conversation_needs_an_empty_editor_and_no_error():
    base = {"editor": {"text": ""}, "outgoing": [], "error": False, "conversation": "x hello"}
    assert trp.dm_verified(base, "hello") is True
    assert trp.dm_verified(dict(base, editor={"text": "hello"}), "hello") is False
    assert trp.dm_verified(dict(base, error=True), "hello") is False
    assert trp.dm_verified(dict(base, editor=None), "hello") is False
    assert trp.dm_verified(dict(base, conversation="x"), "hello") is False


# --- s30 (console ADR 064, D1): the `dm_probe` diagnostic lines ---

PROBE_LINE = re.compile(r"^dm_probe item=\d+ step=(profile|thread|verify)(?: [a-z0-9_]+=[A-Za-z0-9_-]{1,40})+$")


def test_probe_keys_are_the_closed_lists():
    assert tuple(trp.DM_PROBE_KEYS) == ("profile", "thread", "verify")
    assert trp.DM_PROBE_KEYS["profile"] == tuple(
        "captcha login_gate path_is_profile candidates chosen excluded_btns tag e2e role cls in_view x y w h"
        " nav_msg_links dm_block disabled ms".split())
    assert trp.DM_PROBE_KEYS["thread"] == tuple(
        "attempt renavigated click tabs_before tabs_after new_tab editor waited_ms captcha_ms polls editors ed_tag"
        " ed_e2e ed_role ed_cls ed1_tag ed1_e2e ed1_cls ed2_tag ed2_e2e ed2_cls path_messages chat_items send_btn"
        " send_tag send_e2e send_cls dm_block settled settle_reads".split())
    assert trp.DM_PROBE_KEYS["verify"] == tuple(
        "editor editor_empty error outgoing bubble_match conv_after verified polls ms".split())


@pytest.mark.parametrize("value, printed", [
    (True, "1"), (False, "0"), (0, "0"), (42, "42"), (12.6, "13"), (12.4, "12"), (None, "-"),
    ("DivChatBox", "DivChatBox"), ("message-button", "message-button"), ("BUTTON", "BUTTON"),
    ("@SENTINEL_H", "other"), ("/@x/video/7212345678", "other"), ("Salut SENTINEL", "other"),
    ("chat-1234567", "other"), ("", "other"), ("x" * 41, "other"), ({"a": 1}, "other"), ([1], "other"),
    (float("nan"), "other"),
])
def test_probe_value(value, printed):
    assert trp.probe_value(value) == printed


def test_probe_line_has_the_pinned_format_and_never_prints_a_foreign_key():
    line = trp.probe_line(191, "profile", {"captcha": False, "candidates": 1, "chosen": "text", "tag": "BUTTON",
                                           "e2e": "@SENTINEL_H", "x": 812.7, "handle": "SENTINEL_KEY"})
    assert PROBE_LINE.match(line), line
    assert line.startswith("dm_probe item=191 step=profile captcha=0 login_gate=- ")
    assert " candidates=1 chosen=text " in line and " tag=BUTTON " in line and " e2e=other " in line and " x=813 " in line
    assert "SENTINEL" not in line and "handle" not in line
    keys = [part.split("=")[0] for part in line.split()[3:]]
    assert keys == list(trp.DM_PROBE_KEYS["profile"])
    for step in ("thread", "verify"):
        assert PROBE_LINE.match(trp.probe_line(7, step, {})), step
        assert PROBE_LINE.match(trp.probe_line(7, step, {"ed_e2e": "Salut SENTINEL", "polls": 3})), step
    assert trp.probe_line(7, "unknown", {"x": 1}) is None


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


def is_panel_state(expr):
    return expr == sel.PANEL_STATE_JS


TAB_H = {"selector": "TAB", "x": 1575, "y": 76}
ICON_H = {"selector": "ICON", "x": 1114, "y": 684}
P_CLOSED = {"state": "closed", "comments_tab": None, "comment_icon": ICON_H, "comment_count": 1}
P_RELATED = {"state": "related", "comments_tab": TAB_H, "comment_icon": ICON_H, "comment_count": 1}
P_COMMENTS = {"state": "comments", "comments_tab": TAB_H, "comment_icon": ICON_H, "comment_count": 1}


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


def test_m5_type_dm_refuses_an_editor_that_is_not_empty():
    # s25 (s24 m5, console ADR 059, P7): a draft left in the editor is never completed nor sent
    thread = fixture("dm_editor_not_empty.json")["thread"]
    client = FakeClient(answers={is_thread: thread})
    with pytest.raises(trp.PageUnusable) as info:
        page(client).type_dm("t1", "Salut !")
    assert str(info.value) == "dm_editor_not_empty"
    assert not any(c[0] in ("type_text", "press") for c in client.calls)


@pytest.mark.parametrize("text", ["", "   ", None])
def test_m5_type_dm_types_into_an_empty_editor(text):
    thread = {"login_gate": False, "editor": dict(handle("ED"), text=text), "outgoing": [], "send_button": None, "error": False}
    client = FakeClient(answers={is_thread: thread})
    page(client).type_dm("t1", "Salut")
    assert ("type_text", "ED", "Salut") in client.calls


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


# s28 : épingle amendée (D7, R8, console ADR 062) — panneau fermé : l'icône, puis l'onglet « Comments » si le panneau
# s'ouvre sur « You may like ».
def test_closed_panel_is_opened_then_replies_are_expanded():
    closed = {"login_gate": False, "panel_open": False, "comment_icon": handle("ICON"), "comments": []}
    target = {"author": "@a", "text": "x", "reply_button": handle("RB"), "view_replies": [handle("V1"), handle("V2"), handle("V3")], "replies": []}
    opened = {"login_gate": False, "panel_open": True, "comment_icon": None, "comments": [target]}
    expanded = {"login_gate": False, "panel_open": True, "comment_icon": None,
                "comments": [dict(target, view_replies=[], replies=[{"author": OWNER, "text": "ok"}])]}
    client = FakeClient(answers={is_comments: [closed, opened, expanded], is_panel_state: [P_CLOSED, P_RELATED]})
    snap = page(client).comment_snapshot("t1", author="@a", text="x")
    clicks = [c[1] for c in client.calls if c[0] == "click_selector"]
    assert clicks == ["ICON", "TAB", "V1", "V2"]
    assert snap is expanded


def test_you_may_like_clicks_the_comments_tab_never_the_icon():
    related = {"login_gate": False, "panel_open": False, "comment_icon": handle("ICON"), "comments": []}
    opened = {"login_gate": False, "panel_open": True, "comment_icon": None, "comments": []}
    client = FakeClient(answers={is_comments: [related, opened], is_panel_state: [P_RELATED]})
    sleeps = []
    snap = trp.TikTokReplyPage(client, owner=OWNER, sleep=sleeps.append).comment_snapshot("t1", author="@a", text="x")
    assert [c[1] for c in client.calls if c[0] == "click_selector"] == ["TAB"]
    assert sleeps == [sel.PANEL_SETTLE_S]
    assert snap is opened


def test_closed_panel_click_falls_back_to_the_mouse():
    closed = {"login_gate": False, "panel_open": False, "comment_icon": handle("ICON"), "comments": []}
    opened = {"login_gate": False, "panel_open": True, "comment_icon": None, "comments": []}
    client = FakeClient(answers={is_comments: [closed, opened], is_panel_state: [P_CLOSED, P_COMMENTS]},
                        mouse=True, click_error=CamofoxHTTPError(422))
    page(client).comment_snapshot("t1", author="@a", text="x")
    assert [c[0] for c in client.calls if c[0] in ("click_selector", "mouse")] == ["click_selector", "mouse"]


def test_open_panel_reads_no_panel_state_and_clicks_nothing():
    opened = {"login_gate": False, "panel_open": True, "comment_icon": handle("ICON"), "comments": []}
    client = FakeClient(answers={is_comments: opened})
    page(client).comment_snapshot("t1", author="@a", text="x")
    assert not any(c[0] in ("click_selector", "mouse") for c in client.calls)
    assert sel.PANEL_STATE_JS not in client.expressions


def test_unknown_panel_without_tab_clicks_nothing():
    closed = {"login_gate": False, "panel_open": False, "comment_icon": handle("ICON"), "comments": []}
    client = FakeClient(answers={is_comments: closed, is_panel_state: {"state": "unknown", "comments_tab": None,
                                                                       "comment_icon": handle("ICON")}})
    page(client).comment_snapshot("t1", author="@a", text="x")
    assert not any(c[0] in ("click_selector", "mouse") for c in client.calls)


def test_login_gate_is_checked_before_the_panel_state():
    client = FakeClient(answers={is_comments: {"login_gate": True, "panel_open": False, "comment_icon": handle("ICON"),
                                               "comments": []}, is_panel_state: P_RELATED})
    with pytest.raises(trp.LoginGate):
        page(client).comment_snapshot("t1", author="@a", text="x")
    assert sel.PANEL_STATE_JS not in client.expressions


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


# --- DM (s30, console ADR 064: D2, D3, D4, A3-A6) -------------------------------------------------------------------
# s30 : les anciens tests open_dm (clic du bouton, absence de bouton, login gate) sont réécrits à la nouvelle signature
# open_dm(handle, *, captcha_gate), même sens : le bouton « Message » est cliqué, une absence de bouton n'est
# `dm_unavailable` que sur un signal structurel de refus (sinon `no_dm_button`), le login gate lève LoginGate.


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class DmClient(FakeClient):
    """FakeClient with a real tab list (list_tabs scriptable), callable answers and a hook after each click."""

    def __init__(self, *, profiles, threads, on_click=None, **kwargs):
        super().__init__(answers={}, **kwargs)
        self.profiles = list(profiles)
        self.threads = threads
        self.on_click = on_click
        self.tab_list = [{"tabId": "other", "url": "https://www.tiktok.com/@florian", "listItemId": "browser"}]
        self.clicks = 0

    def list_tabs(self, *, timeout=None):
        self.calls.append(("list_tabs",))
        return [dict(t) for t in self.tab_list]

    def create_blank_tab(self, session_key, *, timeout=None):
        tab = super().create_blank_tab(session_key, timeout=timeout)
        self.tab_list.append({"tabId": tab, "url": "about:blank", "listItemId": session_key})
        return tab

    def delete_tab(self, tab, *, timeout=None):
        super().delete_tab(tab, timeout=timeout)
        self.tab_list = [t for t in self.tab_list if t["tabId"] != tab]

    def evaluate_value(self, tab, expression, *, timeout=None):
        self.expressions.append(expression)
        if expression == sel.PROFILE_JS:
            self.calls.append(("PROFILE_JS",))
            answer = self.profiles.pop(0) if len(self.profiles) > 1 else self.profiles[0]
        elif expression == sel.THREAD_JS:
            self.calls.append(("THREAD_JS",))
            answer = self.threads(self) if callable(self.threads) else self.threads
        else:
            raise AssertionError("unexpected expression")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def click_selector(self, tab, selector, *, timeout=None):
        super().click_selector(tab, selector, timeout=timeout)
        self.clicks += 1
        if self.on_click is not None:
            self.on_click(self, selector)


def profile_snap(button="MSG", *, path=True, disabled=False, dm_block=False, chosen="text", in_view=True,
                 login_gate=False):
    shape = {"tag": "BUTTON", "e2e": None, "role": None, "cls": "ButtonMessage", "x": 900, "y": 300, "w": 120,
             "h": 36, "in_view": in_view}
    return {"login_gate": login_gate, "path_is_profile": path, "message_button": handle(button) if button else None,
            "disabled": disabled, "dm_block": dm_block,
            "probe": {"candidates": 1 if button else (2 if chosen == "ambiguous" else 0),
                      "chosen": chosen if button or chosen == "ambiguous" else "none",
                      "btn": shape if button else None, "nav_msg_links": 1, "excluded_btns": 0, "path_is_profile": path}}


def thread_snap(editor=True, *, login_gate=False, text="", outgoing=(), conversation="", error=False):
    ed_shape = {"tag": "DIV", "e2e": None, "role": "textbox", "cls": "DivEditor", "x": 700, "y": 900, "w": 700,
                "h": 40, "in_view": True}
    return {"login_gate": login_gate, "editor": dict(handle("ED"), text=text) if editor else None,
            "outgoing": list(outgoing), "send_button": handle("SEND") if editor else None, "error": error,
            "conversation": conversation,
            "probe": {"editors": 1 if editor else 0, "ed": ed_shape if editor else None, "ed1": None, "ed2": None,
                      "path_messages": True, "chat_items": 0, "send": None, "dm_block": False}}


class Gate:
    def __init__(self, client, clock, *, cost=None, error=None):
        self.client, self.clock, self.cost, self.error = client, clock, cost or {}, error
        self.n = 0

    def __call__(self, tab):
        self.n += 1
        self.client.calls.append(("gate", tab))
        if self.error is not None:
            raise self.error
        spent = self.cost.get(self.n, 0.0)
        self.clock.t += spent
        return bool(spent)


def dm_page(client, clock):
    return trp.TikTokReplyPage(client, owner=OWNER, sleep=clock.sleep, clock=clock)


def clicks(client):
    return [c for c in client.calls if c[0] in ("click_selector", "mouse")]


def editor_after(n_clicks):
    return lambda client: thread_snap(editor=client.clicks >= n_clicks)


def probes(p, step):
    return [f for s, f in p.take_dm_probe() if s == step]


def test_open_dm_clicks_the_message_button_and_finds_the_editor_at_the_first_read():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap()], threads=lambda c: thread_snap())
    p = dm_page(client, clock)
    tab = p.open_dm("@fan", captcha_gate=Gate(client, clock))
    assert ("navigate", tab, "https://www.tiktok.com/@fan") in client.calls
    assert clicks(client) == [("click_selector", "MSG")]
    probe = p.take_dm_probe()
    assert [s for s, _ in probe] == ["profile", "thread"]
    thread = probe[1][1]
    assert (thread["attempt"], thread["editor"], thread["polls"], thread["click"]) == (1, True, 1, "selector")
    assert p.take_dm_probe() == []


def test_open_dm_second_click_on_a_recomputed_selector_when_the_editor_comes_late():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap("MSG1"), profile_snap("MSG2")], threads=editor_after(2))
    p = dm_page(client, clock)
    p.open_dm("@fan", captcha_gate=Gate(client, clock))
    assert clicks(client) == [("click_selector", "MSG1"), ("click_selector", "MSG2")]
    assert [c for c in client.calls if c[0] == "PROFILE_JS"] == [("PROFILE_JS",), ("PROFILE_JS",)]
    threads = probes(p, "thread")
    assert [(t["attempt"], t["editor"], t["renavigated"]) for t in threads] == [(1, False, False), (2, True, False)]
    assert threads[0]["waited_ms"] >= 15000 and threads[0]["polls"] == 15


def test_open_dm_without_editor_after_two_attempts_is_no_dm_editor_and_types_nothing():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap()], threads=lambda c: thread_snap(editor=False))
    p = dm_page(client, clock)
    with pytest.raises(trp.PageUnusable) as info:
        p.open_dm("@fan", captcha_gate=Gate(client, clock))
    assert str(info.value) == "no_dm_editor"
    assert len(clicks(client)) == 2
    assert sum(s for s in clock.sleeps if s == sel.DM_EDITOR_POLL_S) <= 2 * sel.DM_EDITOR_WAIT_S
    assert not any(c[0] in ("type_text", "press") for c in client.calls)


def test_open_dm_navigation_link_only_is_no_dm_button_without_any_click():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap(None)], threads=lambda c: thread_snap())
    p = dm_page(client, clock)
    with pytest.raises(trp.PageUnusable) as info:
        p.open_dm("@fan", captcha_gate=Gate(client, clock))
    assert str(info.value) == "no_dm_button"
    assert clicks(client) == [] and "THREAD_JS" not in [c[0] for c in client.calls]
    assert "no_dm_button" in trp.PAGE_UNUSABLE_CODES and "no_dm_editor" in trp.PAGE_UNUSABLE_CODES
    profile = probes(p, "profile")[0]
    assert (profile["candidates"], profile["chosen"]) == (0, "none")


def test_open_dm_ambiguous_button_is_no_dm_button_even_with_a_refusal_wording():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap(None, chosen="ambiguous", dm_block=True)], threads=lambda c: thread_snap())
    with pytest.raises(trp.PageUnusable) as info:
        dm_page(client, clock).open_dm("@fan", captcha_gate=Gate(client, clock))
    assert str(info.value) == "no_dm_button" and clicks(client) == []


@pytest.mark.parametrize("snap", [profile_snap(None, dm_block=True), profile_snap("MSG", disabled=True)])
def test_open_dm_refusal_or_disabled_button_is_dm_unavailable_without_any_click(snap):
    clock = FakeClock()
    client = DmClient(profiles=[snap], threads=lambda c: thread_snap())
    with pytest.raises(trp.DmUnavailable):
        dm_page(client, clock).open_dm("@fan", captcha_gate=Gate(client, clock))
    assert clicks(client) == []


def test_open_dm_captcha_gate_comes_before_the_profile_is_read():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap()], threads=lambda c: thread_snap())
    dm_page(client, clock).open_dm("@fan", captcha_gate=Gate(client, clock))
    names = [c[0] for c in client.calls]
    assert names.index("gate") < names.index("PROFILE_JS")

    class Unsolved(Exception):
        pass

    client = DmClient(profiles=[profile_snap()], threads=lambda c: thread_snap())
    with pytest.raises(Unsolved):
        dm_page(client, clock).open_dm("@fan", captcha_gate=Gate(client, clock, error=Unsolved()))
    assert "PROFILE_JS" not in [c[0] for c in client.calls] and clicks(client) == []


def test_open_dm_captcha_gate_at_every_read_of_the_wait():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap()], threads=editor_after(2))
    gate = Gate(client, clock)
    p = dm_page(client, clock)
    p.open_dm("@fan", captcha_gate=gate)
    polls = sum(t["polls"] + t["settle_reads"] for t in probes(p, "thread"))
    # one before each PROFILE_JS (2) and one before each read of the wait and of the settle (s30 review fix MA1)
    assert gate.n == 2 + polls
    names = [c[0] for c in client.calls]
    for i, name in enumerate(names):
        if name == "THREAD_JS":
            assert "gate" in names[max(0, i - 3):i]


@pytest.mark.parametrize("url, closed", [("https://www.tiktok.com/messages?lang=fr&u=1", True),
                                         ("https://example.org/page", False)])
def test_open_dm_new_tab_after_the_click_stops_at_once(url, closed):
    clock = FakeClock()

    def opens_tab(client, selector):
        client.tab_list.append({"tabId": "NEW", "url": url, "listItemId": ""})

    client = DmClient(profiles=[profile_snap()], threads=lambda c: thread_snap(editor=False), on_click=opens_tab)
    p = dm_page(client, clock)
    with pytest.raises(trp.PageUnusable) as info:
        p.open_dm("@fan", captcha_gate=Gate(client, clock))
    assert str(info.value) == "no_dm_editor"
    assert len(clicks(client)) == 1
    assert (("delete_tab", "NEW") in client.calls) is closed
    assert [c for c in client.calls if c[0] == "delete_tab" and c[1] != "NEW"] == []
    thread = probes(p, "thread")[0]
    assert thread["new_tab"] is True and thread["tabs_after"] == thread["tabs_before"] + 1


def test_open_dm_second_attempt_reuses_the_profile_page_only_when_it_is_still_the_profile():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap("MSG1"), profile_snap("MSG2", path=True)], threads=editor_after(2))
    dm_page(client, clock).open_dm("@fan", captcha_gate=Gate(client, clock))
    assert len([c for c in client.calls if c[0] == "navigate"]) == 1

    for second in (profile_snap("MSG2", path=False), profile_snap(None, path=True)):
        clock = FakeClock()
        client = DmClient(profiles=[profile_snap("MSG1"), second, profile_snap("MSG3")], threads=editor_after(2))
        gate = Gate(client, clock)
        p = dm_page(client, clock)
        tab = p.open_dm("@fan", captcha_gate=gate)
        navigations = [c for c in client.calls if c[0] == "navigate"]
        assert navigations == [("navigate", tab, "https://www.tiktok.com/@fan")] * 2
        names = [c[0] for c in client.calls]
        after = names[names.index("navigate", names.index("navigate") + 1):]
        assert after.index("gate") < after.index("PROFILE_JS")
        assert clicks(client) == [("click_selector", "MSG1"), ("click_selector", "MSG3")]
        assert probes(p, "thread")[1]["renavigated"] is True


def test_open_dm_second_attempt_without_a_button_after_renavigation_is_no_dm_editor():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap("MSG1"), profile_snap(None, path=False), profile_snap(None)],
                      threads=lambda c: thread_snap(editor=False))
    with pytest.raises(trp.PageUnusable) as info:
        dm_page(client, clock).open_dm("@fan", captcha_gate=Gate(client, clock))
    assert str(info.value) == "no_dm_editor" and len(clicks(client)) == 1


def test_open_dm_mouse_fallback_only_when_the_button_is_in_view():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap(in_view=True)], threads=lambda c: thread_snap(), mouse=True,
                      click_error=CamofoxHTTPError(422))
    p = dm_page(client, clock)
    p.open_dm("@fan", captcha_gate=Gate(client, clock))
    assert [c[0] for c in clicks(client)] == ["click_selector", "mouse"]
    assert probes(p, "thread")[0]["click"] == "mouse"

    client = DmClient(profiles=[profile_snap(in_view=False)], threads=lambda c: thread_snap(), mouse=True,
                      click_error=CamofoxHTTPError(422))
    with pytest.raises(CamofoxHTTPError):
        dm_page(client, clock).open_dm("@fan", captcha_gate=Gate(client, clock))
    assert [c[0] for c in clicks(client)] == ["click_selector"]


def test_verify_dm_reads_again_without_any_gesture_and_stops_at_the_first_settled_confirmation():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap()], threads=lambda c: thread_snap(outgoing=["autre"]))
    p = dm_page(client, clock)
    assert p.verify_dm("t1", "hello") is False
    assert sum(clock.sleeps) <= sel.DM_VERIFY_WAIT_S
    assert clicks(client) == [] and not any(c[0] in ("type_text", "press") for c in client.calls)
    verify = probes(p, "verify")[0]
    assert verify["verified"] is False and verify["polls"] == 11

    seen = {"n": 0}

    def third(c):
        seen["n"] += 1
        return thread_snap(outgoing=["hello"] if seen["n"] >= 3 else [])

    client = DmClient(profiles=[profile_snap()], threads=third)
    p = dm_page(client, FakeClock())
    assert p.verify_dm("t1", "hello") is True
    # s30 review fix MA1: confirmed at the 3rd read, settled (identical) at the 4th, then no more reads
    assert seen["n"] == 4
    verify = probes(p, "verify")[0]
    assert (verify["verified"], verify["polls"], verify["bubble_match"], verify["editor_empty"]) == (True, 4, True, True)


def test_verify_dm_by_the_conversation_column():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap()], threads=lambda c: thread_snap(conversation="Fan hello"))
    p = dm_page(client, clock)
    assert p.verify_dm("t1", "hello") is True
    verify = probes(p, "verify")[0]
    assert (verify["bubble_match"], verify["conv_after"]) == (False, 1)


def test_open_dm_captcha_time_is_not_part_of_the_wait():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap()], threads=lambda c: thread_snap(editor=False))
    gate = Gate(client, clock, cost={3: 40.0})  # 1 = profile, 2 = first read, 3 = second read
    p = dm_page(client, clock)
    with pytest.raises(trp.PageUnusable):
        p.open_dm("@fan", captcha_gate=gate)
    first = probes(p, "thread")[0]
    assert first["polls"] >= 13
    assert first["captcha_ms"] >= 40000


def test_open_dm_login_gate_during_the_wait():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap()], threads=lambda c: thread_snap(editor=False, login_gate=True))
    with pytest.raises(trp.LoginGate):
        dm_page(client, clock).open_dm("@fan", captcha_gate=Gate(client, clock))


def test_open_dm_login_gate_on_the_profile():
    clock = FakeClock()
    client = DmClient(profiles=[profile_snap(login_gate=True)], threads=lambda c: thread_snap())
    with pytest.raises(trp.LoginGate):
        dm_page(client, clock).open_dm("@fan", captcha_gate=Gate(client, clock))
    assert clicks(client) == []


# --- s30 review fix MA1: the guard and the verification read a settled conversation only ---------------------------


def history_after(clock, clicked, steps):
    """THREAD_JS with an editor at once; the conversation column is the last `value` whose `at` <= t - click."""
    def read(client):
        if clicked.get("t") is None:
            return thread_snap(editor=False)
        elapsed = clock.t - clicked["t"]
        text = ""
        for at, value in steps:
            if elapsed >= at:
                text = value
        return thread_snap(conversation=text)
    return read


def mark_click(clock, clicked):
    def hook(client, selector):
        clicked["t"] = clock.t
    return hook


def test_ma1_open_dm_waits_for_a_history_loaded_after_the_editor_before_the_guard():
    clock, clicked = FakeClock(), {}
    client = DmClient(profiles=[profile_snap()], threads=history_after(clock, clicked, [(3, "Fan old hello")]),
                      on_click=mark_click(clock, clicked))
    p = dm_page(client, clock)
    tab = p.open_dm("@fan", captcha_gate=Gate(client, clock))
    assert clock.t - clicked["t"] >= sel.THREAD_SETTLE_S
    # the replayed item's DM already went: the guard read right after open_dm sees it => nothing typed
    assert trp.dm_already_there(p.thread_snapshot(tab), "hello") is True
    thread = probes(p, "thread")[0]
    assert (thread["editor"], thread["polls"], thread["settled"]) == (True, 1, True)
    assert thread["settle_reads"] >= 3 and len(clicks(client)) == 1


def test_ma1_open_dm_needs_two_identical_reads_of_a_loading_history():
    clock, clicked = FakeClock(), {}
    steps = [(1, "a"), (2, "a b"), (3, "a b c"), (4, "a b c d"), (5, "a b c d e"), (6, "a b c d e hello")]
    client = DmClient(profiles=[profile_snap()], threads=history_after(clock, clicked, steps),
                      on_click=mark_click(clock, clicked))
    p = dm_page(client, clock)
    tab = p.open_dm("@fan", captcha_gate=Gate(client, clock))
    assert clock.t - clicked["t"] >= 7
    assert trp.dm_already_there(p.thread_snapshot(tab), "hello") is True


def test_ma1_open_dm_never_settled_is_no_dm_editor_types_nothing_and_clicks_once():
    clock, seen = FakeClock(), {"n": 0}

    def moving(client):
        seen["n"] += 1
        return thread_snap(conversation="x" * seen["n"])

    client = DmClient(profiles=[profile_snap()], threads=moving)
    p = dm_page(client, clock)
    with pytest.raises(trp.PageUnusable) as info:
        p.open_dm("@fan", captcha_gate=Gate(client, clock))
    assert str(info.value) == "no_dm_editor"
    assert len(clicks(client)) == 1
    assert not any(c[0] in ("type_text", "press") for c in client.calls)
    thread = probes(p, "thread")[0]
    assert (thread["editor"], thread["settled"]) == (True, False)
    assert thread["waited_ms"] <= (sel.DM_EDITOR_POLL_S * 2 + sel.DM_SETTLE_WAIT_S) * 1000


def test_ma1_verify_dm_never_confirms_on_a_conversation_still_moving():
    clock, seen = FakeClock(), {"n": 0}

    def moving(client):
        seen["n"] += 1
        return thread_snap(conversation="hello " + "x" * seen["n"])

    client = DmClient(profiles=[profile_snap()], threads=moving)
    p = dm_page(client, clock)
    assert p.verify_dm("t1", "hello") is False
    assert clicks(client) == [] and not any(c[0] in ("type_text", "press") for c in client.calls)
    assert probes(p, "verify")[0]["verified"] is False


def test_ma1_the_send_settle_is_at_least_the_thread_settle():
    # verify_dm's first read comes AFTER_SEND_SETTLE_S after the send click: never right after the send
    assert sel.AFTER_SEND_SETTLE_S >= sel.THREAD_SETTLE_S == 4.0


# s30 review m3: the budget counts the Camofox calls, not only the fixed waits. NOMINAL_CALL_S = one call as seen in
# prod (run 1182: 46 s for ~25 s of fixed waits and ~20 calls). The worst DM path is replayed on a clock where every
# call costs NOMINAL_CALL_S. A slower Camofox (up to STEP_TIMEOUT_S per call) can overrun ITEM_DEADLINE_S: the
# check_deadline() right after open_dm then releases the item before anything is typed (_ItemDeadline, nothing sent).
NOMINAL_CALL_S = 1.0


class CostClock(FakeClock):
    pass


class CostClient(DmClient):
    def __init__(self, clock, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock

    def _cost(self):
        self.clock.t += NOMINAL_CALL_S

    def list_tabs(self, *, timeout=None):
        self._cost()
        return super().list_tabs(timeout=timeout)

    def create_blank_tab(self, session_key, *, timeout=None):
        self._cost()
        return super().create_blank_tab(session_key, timeout=timeout)

    def navigate(self, tab, url, *, timeout=35):
        self._cost()
        return super().navigate(tab, url, timeout=timeout)

    def evaluate_value(self, tab, expression, *, timeout=None):
        self._cost()
        return super().evaluate_value(tab, expression, timeout=timeout)

    def click_selector(self, tab, selector, *, timeout=None):
        self._cost()
        return super().click_selector(tab, selector, timeout=timeout)


class CostGate(Gate):
    def __call__(self, tab):  # the captcha probe is one Camofox call (excluded from the wait window, not from time)
        self.clock.t += NOMINAL_CALL_S
        return super().__call__(tab)


def test_dm_wait_budget_invariant():
    from app import tiktok_reply as tr

    assert sel.DM_OPEN_ATTEMPTS == 2 and sel.DM_EDITOR_POLL_S == 1.0 and sel.DM_EDITOR_WAIT_S == 15.0
    assert sel.DM_VERIFY_WAIT_S == 10.0 and sel.THREAD_SETTLE_S == 4.0 and sel.DM_SETTLE_WAIT_S == 8.0
    fixed = (sel.PROFILE_SETTLE_S * 2 + sel.DM_EDITOR_WAIT_S * sel.DM_OPEN_ATTEMPTS + sel.DM_SETTLE_WAIT_S
             + sel.AFTER_SEND_SETTLE_S + sel.DM_VERIFY_WAIT_S + sel.VIDEO_SETTLE_S + sel.PANEL_SETTLE_S * 2
             + sel.REPLIES_SETTLE_S * sel.MAX_VIEW_REPLIES_CLICKS)
    assert fixed <= tr.ITEM_DEADLINE_S / 2

    profiles = [profile_snap("MSG1"), profile_snap("MSG2", path=False), profile_snap("MSG3")]

    # calibration: no editor at all => the number of reads of the second attempt's wait
    clock = CostClock()
    client = CostClient(clock, profiles=list(profiles), threads=lambda c: thread_snap(editor=False))
    p = dm_page(client, clock)
    with pytest.raises(trp.PageUnusable):
        p.open_dm("@fan", captcha_gate=CostGate(client, clock))
    last_read = probes(p, "thread")[1]["polls"]

    # worst open_dm: renavigation, the editor at the last read of attempt 2, a conversation that never settles
    clock, seen = CostClock(), {"n": 0}

    def worst(c):
        if c.clicks < 2:
            return thread_snap(editor=False)
        seen["n"] += 1
        return thread_snap(editor=seen["n"] >= last_read, conversation="x" * seen["n"])

    client = CostClient(clock, profiles=list(profiles), threads=worst)
    began = clock.t
    with pytest.raises(trp.PageUnusable):
        dm_page(client, clock).open_dm("@fan", captcha_gate=CostGate(client, clock))
    open_dm_s = clock.t - began

    # worst verify_dm: never confirmed
    clock = CostClock()
    client = CostClient(clock, profiles=[profile_snap()], threads=lambda c: thread_snap(outgoing=["autre"]))
    began = clock.t
    assert dm_page(client, clock).verify_dm("t1", "hello") is False
    verify_s = clock.t - began

    calls = NOMINAL_CALL_S
    guard_type_send = 2 * calls + 2 * calls + 2 * calls + sel.AFTER_SEND_SETTLE_S  # gate+THREAD_JS, type, send
    video = (3 * calls + sel.VIDEO_SETTLE_S + calls + 9 * calls + sel.PANEL_SETTLE_S * 2
             + sel.REPLIES_SETTLE_S * sel.MAX_VIEW_REPLIES_CLICKS)
    assert open_dm_s + guard_type_send + verify_s + video <= tr.ITEM_DEADLINE_S


# --- source-level interdicts (D4, D9, P8) ---------------------------------------------------------------------------


def test_no_like_no_clipboard_no_enter_in_reply_modules():
    for module in ("tiktok_reply_page.py", "tiktok_reply_selectors.py"):
        text = (APP / module).read_text(encoding="utf-8")
        for needle in ("subprocess", "docker", "xclip", "xdotool", "execCommand", "digg", "dispatchEvent"):
            assert needle not in text, (module, needle)
        # s28 : épingle amendée (console ADR 062, D2) — seul le libellé de l'onglet de lecture « You may like »
        # (panelState()) est exempté ; aucun autre « like » (aucun J'aime).
        assert re.search(r"\blike", text.replace("You may like", ""), re.I) is None, module
        assert re.search(r"press\([^)]*[\"']Enter[\"']", text) is None, module
