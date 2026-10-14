"""s28 — opening the comments panel by trusted clicks (console ADR 062, D3/D4/D6).

`panel_targets` is the pure decision on a panelState() snapshot; `open_comments_panel` executes it on a Camofox
client: a real click by unique CSS path (`click_selector`), the `console-mouse` plugin at the centre as fallback.
The comment icon is a toggle: it is never clicked while a « Comments » tab is visible. Fake client only: no network.
"""

import pytest

from app import comments_panel as cp
from app.camofox_reader import CamofoxHTTPError, CamofoxTimeout, CamofoxUnreachable, GUARD_EVAL_TIMEOUT_S
from app.tiktok_reply_selectors import PANEL_STATE_JS


def h(selector, x=10, y=20):
    return {"selector": selector, "x": x, "y": y}


TAB = h("TAB", 1575, 76)
ICON = h("ICON", 1114, 684)
COMMENTS = {"state": "comments", "comments_tab": TAB, "comment_icon": ICON, "comment_count": 3}
RELATED = {"state": "related", "comments_tab": TAB, "comment_icon": ICON, "comment_count": 3}
CLOSED = {"state": "closed", "comments_tab": None, "comment_icon": ICON, "comment_count": 3}
UNKNOWN_TAB = {"state": "unknown", "comments_tab": TAB, "comment_icon": ICON, "comment_count": None}
UNKNOWN_BARE = {"state": "unknown", "comments_tab": None, "comment_icon": ICON, "comment_count": None}


class FakeClient:
    def __init__(self, snapshots, *, click_errors=None, mouse_ok=False, mouse_error=None, response_url=None):
        self.snapshots = list(snapshots)
        self.click_errors = list(click_errors or [])
        self.mouse_ok = mouse_ok
        self.mouse_error = mouse_error
        self.response_url = response_url
        self.calls = []

    def evaluate(self, *, tab_id, expression, timeout=None):
        assert tab_id == "tab-1"
        assert expression == PANEL_STATE_JS
        assert timeout == GUARD_EVAL_TIMEOUT_S
        self.calls.append(("panel_state",))
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def click_selector(self, tab_id, selector, *, timeout=None):
        assert timeout == cp.CLICK_TIMEOUT_S
        self.calls.append(("click_selector", selector))
        if self.click_errors:
            error = self.click_errors.pop(0)
            if error is not None:
                raise error

    def mouse_available(self, *, timeout=5.0):
        assert timeout == 5
        self.calls.append(("mouse_available",))
        return self.mouse_ok

    def mouse(self, tab_id, actions, *, hold=False, timeout=25.0):
        assert timeout == cp.CLICK_TIMEOUT_S
        self.calls.append(("mouse", tuple((a["type"], a.get("x"), a.get("y")) for a in actions)))
        if self.mouse_error is not None:
            raise self.mouse_error
        return len(actions)


def run(client, *, expired=None, memo=None):
    pauses = []
    result = cp.open_comments_panel(client, "tab-1", pause=pauses.append, expired=expired, memo=memo)
    return result, pauses


def clicks(client):
    return [c for c in client.calls if c[0] in ("click_selector", "mouse")]


# --- pure decision -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snapshot, expected",
    [
        (COMMENTS, []),
        (RELATED, ["comments_tab"]),
        (CLOSED, ["comment_icon"]),
        (UNKNOWN_TAB, ["comments_tab"]),
        (UNKNOWN_BARE, []),
        (dict(RELATED, comments_tab=None), []),
        (None, []),
        ("comments", []),
        ({"state": "weird", "comments_tab": TAB, "comment_icon": ICON}, ["comments_tab"]),
    ],
)
def test_panel_targets(snapshot, expected):
    assert cp.panel_targets(snapshot) == expected


def test_the_icon_is_never_a_target_while_a_comments_tab_exists():
    for state in ("comments", "related", "closed", "unknown", "weird"):
        snap = {"state": state, "comments_tab": TAB, "comment_icon": ICON}
        assert "comment_icon" not in cp.panel_targets(snap)
        assert "comment_icon" not in cp.panel_targets(snap, after_icon=True)


def test_after_the_icon_only_a_related_panel_gets_the_tab():
    assert cp.panel_targets(RELATED, after_icon=True) == ["comments_tab"]
    assert cp.panel_targets(UNKNOWN_TAB, after_icon=True) == []
    assert cp.panel_targets(CLOSED, after_icon=True) == []
    assert cp.panel_targets(COMMENTS, after_icon=True) == []


def test_panel_snapshot_normalises_odd_results():
    assert cp.panel_snapshot(None) == {"state": "unknown", "comments_tab": None, "comment_icon": None,
                                       "comment_count": None}
    odd = cp.panel_snapshot({"state": "related", "comments_tab": {"selector": 3}, "comment_icon": "x",
                             "comment_count": "12"})
    assert odd == {"state": "related", "comments_tab": None, "comment_icon": None, "comment_count": None}
    assert cp.panel_snapshot({"state": "closed", "comment_count": -1})["comment_count"] is None


# --- executor ------------------------------------------------------------------------------------------------------


def test_open_panel_is_not_clicked():
    client = FakeClient([COMMENTS])
    result, pauses = run(client)
    assert clicks(client) == []
    assert result == {"attempt": {"state": "comments", "clicked": None, "via": None, "error": None},
                      "comment_count": 3}
    assert pauses == []


def test_you_may_like_gets_one_click_on_the_comments_tab():
    client = FakeClient([RELATED])
    result, _ = run(client)
    assert clicks(client) == [("click_selector", "TAB")]
    assert result["attempt"] == {"state": "related", "clicked": "comments_tab", "via": "selector", "error": None}


def test_a_non_dict_snapshot_clicks_nothing():
    client = FakeClient(["oops"])
    result, _ = run(client)
    assert clicks(client) == []
    assert result["attempt"]["state"] == "unknown"


def test_closed_clicks_the_icon_pauses_snapshots_again_then_the_tab():
    client = FakeClient([CLOSED, RELATED])
    result, pauses = run(client)
    assert client.calls == [("panel_state",), ("click_selector", "ICON"), ("panel_state",), ("click_selector", "TAB")]
    assert pauses == [cp.PANEL_OPEN_SETTLE_S]
    assert cp.PANEL_OPEN_SETTLE_S == 1.5
    assert result["attempt"] == {"state": "closed", "clicked": "comment_icon+comments_tab", "via": "selector",
                                 "error": None}


def test_closed_then_comments_after_the_icon_stops_there():
    client = FakeClient([CLOSED, COMMENTS])
    result, _ = run(client)
    assert clicks(client) == [("click_selector", "ICON")]
    assert result["attempt"]["clicked"] == "comment_icon"


def test_closed_still_closed_after_the_icon_never_clicks_the_icon_again():
    client = FakeClient([CLOSED, CLOSED])
    run(client)
    assert clicks(client) == [("click_selector", "ICON")]


def test_unknown_without_tab_clicks_nothing_and_with_tab_clicks_the_tab():
    client = FakeClient([UNKNOWN_BARE])
    run(client)
    assert clicks(client) == []
    client = FakeClient([UNKNOWN_TAB])
    run(client)
    assert clicks(client) == [("click_selector", "TAB")]


def test_expired_before_anything_evaluates_nothing():
    client = FakeClient([RELATED])
    result, _ = run(client, expired=lambda: True)
    assert client.calls == []
    assert result["attempt"]["clicked"] is None


def test_expired_after_the_snapshot_clicks_nothing_more():
    client = FakeClient([CLOSED, RELATED])
    result, pauses = run(client, expired=lambda: len(client.calls) >= 1)
    assert client.calls == [("panel_state",)]
    assert pauses == []


def test_expired_after_the_icon_neither_pauses_nor_snapshots_again():
    client = FakeClient([CLOSED, RELATED])
    result, pauses = run(client, expired=lambda: len(client.calls) >= 2)
    assert client.calls == [("panel_state",), ("click_selector", "ICON")]
    assert pauses == []
    assert result["attempt"]["clicked"] == "comment_icon"


def test_ambiguous_selector_falls_back_to_the_mouse_at_the_centre():
    client = FakeClient([RELATED], click_errors=[CamofoxHTTPError(422)], mouse_ok=True)
    result, _ = run(client)
    assert clicks(client) == [
        ("click_selector", "TAB"),
        ("mouse", (("move", 1575.0, 76.0), ("down", None, None), ("up", None, None))),
    ]
    assert result["attempt"] == {"state": "related", "clicked": "comments_tab", "via": "mouse", "error": None}


def test_a_value_error_falls_back_to_the_mouse_too():
    client = FakeClient([RELATED], click_errors=[ValueError("unexpected click payload")], mouse_ok=True)
    result, _ = run(client)
    assert result["attempt"]["via"] == "mouse"


def test_without_the_plugin_the_error_is_recorded_without_raising():
    client = FakeClient([RELATED], click_errors=[CamofoxHTTPError(422)], mouse_ok=False)
    result, _ = run(client)
    assert result["attempt"] == {"state": "related", "clicked": None, "via": None, "error": "http_422"}
    assert ("mouse_available",) in client.calls


def test_a_mouse_error_is_recorded_without_raising():
    client = FakeClient([RELATED], click_errors=[CamofoxHTTPError(422)], mouse_ok=True, mouse_error=CamofoxHTTPError(403))
    result, _ = run(client)
    assert result["attempt"] == {"state": "related", "clicked": None, "via": None, "error": "http_403"}
    client = FakeClient([RELATED], click_errors=[ValueError("x")], mouse_ok=True, mouse_error=ValueError("y"))
    result, _ = run(client)
    assert result["attempt"]["error"] == "ValueError"


def test_the_plugin_probe_is_memorised_across_calls():
    memo = {}
    client = FakeClient([RELATED], click_errors=[CamofoxHTTPError(422), CamofoxHTTPError(422)], mouse_ok=True)
    run(client, memo=memo)
    run(client, memo=memo)
    assert client.calls.count(("mouse_available",)) == 1


@pytest.mark.parametrize("error", [CamofoxTimeout(), CamofoxUnreachable()])
def test_a_timeout_or_unreachable_click_goes_up_without_fallback(error):
    client = FakeClient([RELATED], click_errors=[error], mouse_ok=True)
    with pytest.raises(type(error)):
        run(client)
    assert ("mouse_available",) not in client.calls


def test_a_failed_icon_click_does_not_pause_nor_click_the_tab():
    client = FakeClient([CLOSED, RELATED], click_errors=[CamofoxHTTPError(500)], mouse_ok=False)
    result, pauses = run(client)
    assert clicks(client) == [("click_selector", "ICON")]
    assert pauses == []
    assert result["attempt"] == {"state": "closed", "clicked": None, "via": None, "error": "http_500"}


def test_the_click_response_is_never_read_and_the_result_has_no_url():
    client = FakeClient([RELATED])
    client.click_selector = lambda tab_id, selector, *, timeout=None: {"ok": True, "url": "https://x.invalid/@h"}
    result, _ = run(client)
    assert "invalid" not in repr(result) and "@" not in repr(result)
